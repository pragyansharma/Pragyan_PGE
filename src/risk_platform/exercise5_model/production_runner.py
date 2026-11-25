"""Run the weekly score and compare published results with a reference CSV.

The scheduler supplies paths and serializes runs for each output directory.
A run publishes a validated CSV by atomic replacement. The manifest describes
the latest attempt. An optional alert file carries routing information.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import yaml

from risk_platform.exercise5_model.methodology import score_frame

METHODOLOGY_PATH = Path(__file__).with_name("methodology.py")
SCORE_COLUMNS = ["entity_id", "risk_score", "risk_category"]


def model_version(params_path: Path, prefix: str) -> str:
    """Identify the combination of scoring code and parameter bytes."""
    digest = hashlib.sha256(METHODOLOGY_PATH.read_bytes())
    digest.update(Path(params_path).read_bytes())
    return f"{prefix}+{digest.hexdigest()[:12]}"


@contextmanager
def _staged_path(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        staged = Path(handle.name)
    try:
        yield staged
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict) -> None:
    with _staged_path(path) as staged:
        staged.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _require_fresh(features: pd.DataFrame, max_age_hours: float, now: datetime) -> None:
    """Measure freshness by the newest timestamp in the extract."""
    if features.empty or "feature_as_of" not in features:
        raise ValueError("Expected a nonempty extract with feature_as_of timestamps.")
    timestamps = pd.to_datetime(features["feature_as_of"], utc=True, errors="coerce")
    if timestamps.isna().any():
        raise ValueError("Expected a valid feature_as_of timestamp on every row.")
    age_hours = (now - timestamps.max()).total_seconds() / 3600
    if age_hours < 0 or age_hours > max_age_hours:
        raise ValueError("Feature snapshot falls outside the configured freshness window.")


def _check_keys(frame: pd.DataFrame) -> None:
    ids = frame["entity_id"]
    if ids.isna().any() or ids.str.strip().eq("").any() or ids.duplicated().any():
        raise ValueError("Expected one unique, nonblank entity_id per row.")


def _read_scores(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path, usecols=SCORE_COLUMNS, keep_default_na=False,
        na_values={"risk_score": [""]}, float_precision="round_trip",
        dtype={"entity_id": "string", "risk_category": "string", "risk_score": "float64"},
    )
    _check_keys(frame)
    return frame[SCORE_COLUMNS].sort_values("entity_id").reset_index(drop=True)


def assert_matches_golden(produced: Path, golden: Path) -> None:
    """Require exact entity, category, score and missing-value agreement."""
    try:
        actual = _read_scores(produced)
        reference = _read_scores(golden)
    except (ValueError, KeyError) as exc:
        raise AssertionError("Parity check requires valid score columns and entity IDs.") from exc
    pd.testing.assert_frame_equal(actual, reference, check_exact=True)


def run(
    features_path: Path,
    params_path: Path,
    output_path: Path,
    schedule: dict,
    now: datetime | None = None,
    alert_path: Path | None = None,
) -> dict:
    """Score one extract. An injected clock sets the logical freshness cutoff."""
    started = datetime.now(timezone.utc)
    cutoff = now if now is not None else started
    manifest = {
        "status": "failed",
        "started_at_utc": started.isoformat(),
        "features_path": str(features_path),
        "params_path": str(params_path),
    }
    stage = "configuration"
    try:
        if cutoff.utcoffset() is None:
            raise ValueError("Expected a timezone-aware freshness cutoff.")
        cutoff = cutoff.astimezone(timezone.utc)
        manifest["freshness_as_of_utc"] = cutoff.isoformat()
        version = model_version(params_path, schedule["model_version_prefix"])
        manifest["model_version"] = version
        max_age = float(schedule.get("schedule", schedule)["upstream_max_age_hours"])
        if not isfinite(max_age) or max_age < 0:
            raise ValueError("Expected a finite, nonnegative freshness limit.")
        params = yaml.safe_load(params_path.read_text(encoding="utf-8"))

        stage = "features"
        features = pd.read_csv(features_path, dtype={"entity_id": "string"},
                               keep_default_na=False, na_values=[""])
        _check_keys(features)
        _require_fresh(features, max_age, cutoff)

        stage = "scoring"
        scored = score_frame(features, params)
        _check_keys(scored)
        if set(scored["entity_id"]) != set(features["entity_id"]):
            raise ValueError("Expected exactly one score for each input entity.")
        as_of = features.set_index("entity_id")["feature_as_of"]
        scored.insert(0, "model_version", version)
        scored.insert(1, "feature_as_of", scored["entity_id"].map(as_of))
        scored.insert(2, "scored_at_utc", started.isoformat())

        stage = "publication"
        with _staged_path(output_path) as staged:
            scored.to_csv(staged, index=False)
            expected = scored[SCORE_COLUMNS].sort_values("entity_id").reset_index(drop=True)
            pd.testing.assert_frame_equal(_read_scores(staged), expected, check_exact=True,
                                          check_dtype=False)
            checksum = hashlib.sha256(staged.read_bytes()).hexdigest()
        manifest.update(status="succeeded", row_count=len(scored),
                        output_path=str(output_path), output_sha256=checksum)
    except Exception as exc:
        manifest.update(error_type=type(exc).__name__, failed_stage=stage,
                        error_message=f"Weekly score failed during {stage}.")
        if alert_path is not None:
            _write_alert(alert_path, schedule, manifest)
        raise
    finally:
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(output_path.parent / "run_manifest.json", manifest)
    return manifest


def _write_alert(path: Path, schedule: dict, manifest: dict) -> None:
    """Write a local alert for an external notification service to deliver."""
    events = {"run_failed"}
    if manifest["failed_stage"] == "scoring":
        events.add("scoring_exception")
    routes = [
        {"channel": channel, "role": route["role"]}
        for channel, entries in schedule.get("alerts", {}).items()
        for route in entries
        if events.intersection(route["when"])
    ]
    _write_json(path, {
        "event": "risk_model_run_failed",
        "model_version": manifest.get("model_version"),
        "error_type": manifest["error_type"],
        "error_message": manifest["error_message"],
        "routes": routes,
    })
