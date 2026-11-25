from pathlib import Path

import pandas as pd

from risk_platform.exercise4_spatial import join_cases_to_structures, load_config

ROOT = Path(__file__).resolve().parents[1]


def _result() -> pd.DataFrame:
    config = load_config(ROOT / "config" / "spatial.yml")
    cases = pd.read_csv(ROOT / "data" / "samples" / "case_locations.csv", dtype=str)
    structures = pd.read_csv(ROOT / "data" / "samples" / "structures.csv", dtype=str)
    return join_cases_to_structures(cases, structures, config, computed_at="2026-11-20T00:00:00+00:00")


def test_zones_are_not_mixed_and_unlocated_cases_are_kept():
    result = _result()
    located = result[result["location_method"] == "surveyed_wgs84_transformed_to_structure_crs"]
    # Six cases have coordinates. Each is measured in both State Plane zones.
    assert len(located) == 12
    assert set(located["distance_crs_epsg"]) == {6418, 6420}
    assert located["distance_ftus"].notna().all()
    assert (located["trusted_for_clearance"] == False).all()  # noqa: E712
    unresolved = result[result["location_method"] == "unresolved_no_coordinates"]
    assert set(unresolved["case_record_id"]) == {
        "CASE-200002",
        "CASE-200005",
        "CASE-200007",
        "CASE-200009",
    }
    assert unresolved["distance_ftus"].isna().all()
    address_only = unresolved[unresolved["case_record_id"] == "CASE-200002"].iloc[0]
    assert address_only["next_step"] == "geocode_address_and_confirm_against_parcel"
    parcel_only = unresolved[unresolved["case_record_id"] == "CASE-200005"].iloc[0]
    assert parcel_only["next_step"] == "parcel_centroid_from_authoritative_fabric"
    # The street address is not repeated on the spatial result.
    assert "Example Rd" not in result.astype(str).to_string()


def test_lineage_columns_are_present_and_structure_numbers_stay_text():
    result = _result()
    for column in (
        "case_snapshot_id",
        "structure_snapshot_id",
        "spatial_rule_version",
        "model_version",
        "proj_pipeline",
        "conductor_distance_reason",
    ):
        assert column in result.columns
    assert (result["model_version"].isna()).all()
    assert (result["conductor_distance_reason"] == "linework_not_in_sample").all()
    numbered = result[result["structure_number"].notna()]
    assert numbered["structure_number"].str.contains("/").all()
