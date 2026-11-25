"""Nearest structure, in US survey feet, computed inside one State Plane zone.

The case point is EPSG:4326 and is transformed into the structure CRS before the distance is taken.
Zone 2 is EPSG:6418 and zone 3 is EPSG:6420. Eastings from the two zones are not subtracted.

A point inside both official bounding boxes is stored once per zone, with trusted_for_clearance false.
A case with no coordinates is returned with a null distance. This job does not geocode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyproj
import yaml

CASE_INPUT_CRS = "EPSG:4326"


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def join_cases_to_structures(
    cases: pd.DataFrame,
    structures: pd.DataFrame,
    config: dict,
    computed_at: str | None = None,
) -> pd.DataFrame:
    zones = config["zones"]
    radius = float(config["corridor_radius_ftus"])
    computed = computed_at or datetime.now(timezone.utc).isoformat()
    transformers = {
        zone["epsg"]: pyproj.Transformer.from_crs(CASE_INPUT_CRS, f"EPSG:{zone['epsg']}", always_xy=True)
        for zone in zones
    }
    pipelines = {epsg: _pipeline(transformer) for epsg, transformer in transformers.items()}
    structures = structures.copy()
    structures["epsg"] = structures["state_plane_zone"].map(_epsg_from_zone_name)
    results: list[dict] = []

    for case in cases.itertuples(index=False):
        lat = _float_or_none(case.latitude)
        lon = _float_or_none(case.longitude)
        address = _text(case.address)
        parcel = _text(case.parcel_number)
        base = {
            "case_record_id": case.record_id,
            "case_snapshot_id": config["case_snapshot_id"],
            "structure_snapshot_id": config["structure_snapshot_id"],
            "spatial_rule_version": config["spatial_rule_version"],
            # Set by the scoring job. Null on this output.
            "model_version": None,
            "computed_at_utc": computed,
            "input_crs": CASE_INPUT_CRS,
            "corridor_radius_ftus": radius,
            "address_present": bool(address),
            "parcel_present": bool(parcel),
            "conductor_distance_ftus": None,
            "conductor_distance_reason": "linework_not_in_sample",
            "datum_note": config["datum_note"],
            "proj_version": pyproj.proj_version_str,
        }
        if lat is None or lon is None:
            results.append(
                {
                    **base,
                    "location_method": "unresolved_no_coordinates",
                    "next_step": _next_step(address, parcel),
                    "structure_id": None,
                    "structure_number": None,
                    "circuit": None,
                    "structure_kv": None,
                    "structure_zone": None,
                    "distance_crs_epsg": None,
                    "distance_ftus": None,
                    "within_corridor": None,
                    "structures_within_corridor": None,
                    "zone_confidence": "not_applicable",
                    "trusted_for_clearance": False,
                    "trust_reason": "No surveyed coordinates. Distance was not imputed from the address or the parcel.",
                    "proj_pipeline": None,
                    "candidate_rank": None,
                }
            )
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            results.append(
                {
                    **base,
                    "location_method": "invalid_coordinates",
                    "next_step": "return_to_source",
                    "structure_id": None,
                    "structure_number": None,
                    "circuit": None,
                    "structure_kv": None,
                    "structure_zone": None,
                    "distance_crs_epsg": None,
                    "distance_ftus": None,
                    "within_corridor": None,
                    "structures_within_corridor": None,
                    "zone_confidence": "not_applicable",
                    "trusted_for_clearance": False,
                    "trust_reason": "Latitude or longitude is outside the legal range. The point was not repaired.",
                    "proj_pipeline": None,
                    "candidate_rank": None,
                }
            )
            continue

        containing = [zone for zone in zones if _in_bbox(lon, lat, zone)]
        ambiguous = len(containing) != 1
        for zone in zones:
            epsg = zone["epsg"]
            zone_structures = structures[structures["epsg"] == epsg]
            if zone_structures.empty:
                continue
            east, north = transformers[epsg].transform(lon, lat)
            distance = (
                (zone_structures["easting_ftus"].astype(float) - east) ** 2
                + (zone_structures["northing_ftus"].astype(float) - north) ** 2
            ) ** 0.5
            nearest_position = int(distance.idxmin())
            nearest = zone_structures.loc[nearest_position]
            nearest_distance = float(distance.loc[nearest_position])
            within = int((distance <= radius).sum())
            in_this_box = _in_bbox(lon, lat, zone)
            if ambiguous:
                confidence = "ambiguous_bbox_overlap"
                trusted = False
                reason = (
                    "The point falls in more than one State Plane bounding box, or the boxes "
                    "do not uniquely contain it. County polygons from the GIS team are required "
                    "before this distance is a clearance input. Both zones are reported."
                )
            elif in_this_box:
                confidence = "provisional_single_bbox"
                trusted = False
                reason = (
                    "The point falls in one bounding box. Bounding boxes are not the legal zone. "
                    "Trusted clearance waits for the county polygon and the GIS transformation."
                )
            else:
                confidence = "outside_zone_bbox"
                trusted = False
                reason = (
                    "This distance is in a zone whose bounding box does not contain the point. "
                    "It is a cross-zone candidate, not the clearance distance."
                )
            results.append(
                {
                    **base,
                    "location_method": "surveyed_wgs84_transformed_to_structure_crs",
                    "next_step": None,
                    "structure_id": nearest["structure_id"],
                    "structure_number": str(nearest["structure_number"]),
                    "circuit": nearest["circuit"],
                    "structure_kv": nearest["kv"],
                    "structure_zone": nearest["state_plane_zone"],
                    "distance_crs_epsg": epsg,
                    "distance_ftus": round(nearest_distance, 3),
                    "within_corridor": nearest_distance <= radius,
                    "structures_within_corridor": within,
                    "zone_confidence": confidence,
                    "trusted_for_clearance": trusted,
                    "trust_reason": reason,
                    "proj_pipeline": pipelines[epsg],
                    "candidate_rank": 1,
                }
            )
    return pd.DataFrame(results)


def _epsg_from_zone_name(name: str) -> int | None:
    if not isinstance(name, str) or "EPSG:" not in name:
        return None
    return int(name.split("EPSG:")[1].rstrip(")"))


def _pipeline(transformer: pyproj.Transformer) -> str:
    description = transformer.description or str(transformer)
    return description[:500]


def _in_bbox(lon: float, lat: float, zone: dict) -> bool:
    return zone["west"] <= lon <= zone["east"] and zone["south"] <= lat <= zone["north"]


def _next_step(address: str, parcel: str) -> str:
    if address and parcel:
        return "geocode_address_and_confirm_against_parcel"
    if address:
        return "geocode_address_for_human_acceptance"
    if parcel:
        return "parcel_centroid_from_authoritative_fabric"
    return "manual_locate"


def _float_or_none(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()
