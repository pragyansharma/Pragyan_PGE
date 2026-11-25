from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from risk_platform.exercise5_model.methodology import score_frame
from risk_platform.exercise5_model.production_runner import assert_matches_golden, model_version, run

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ROOT / "data" / "samples" / "model" / "features.csv"
PARAMS = ROOT / "src" / "risk_platform" / "exercise5_model" / "params.yml"
SCHEDULE = yaml.safe_load((ROOT / "config" / "model_schedule.yml").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


def test_production_output_matches_the_notebook_function(tmp_path: Path):
    params = yaml.safe_load(PARAMS.read_text(encoding="utf-8"))
    golden_frame = score_frame(pd.read_csv(FEATURES), params)
    golden = tmp_path / "golden.csv"
    golden_frame.to_csv(golden, index=False)
    produced = tmp_path / "scores.csv"
    manifest = run(FEATURES, PARAMS, produced, SCHEDULE, now=NOW)
    assert manifest["status"] == "succeeded"
    assert manifest["model_version"].startswith("ds-notebook-unversioned+")
    assert_matches_golden(produced, golden)
    # A second run replaces the output file.
    run(FEATURES, PARAMS, produced, SCHEDULE, now=NOW)
    assert len(pd.read_csv(produced)) == 3


def test_a_failed_run_pages_engineering_and_not_the_feature_payload(tmp_path: Path):
    broken = tmp_path / "broken.csv"
    frame = pd.read_csv(FEATURES)
    frame.loc[0, "proximity_ft"] = None
    frame.to_csv(broken, index=False)
    alert = tmp_path / "alert.json"
    with pytest.raises(ValueError, match="does not impute"):
        run(broken, PARAMS, tmp_path / "scores.csv", SCHEDULE, now=NOW, alert_path=alert)
    payload = alert.read_text(encoding="utf-8")
    assert "data_engineering_oncall" in payload
    assert "data_science_methodology_owner" in payload
    assert "E-1" not in payload
    manifest = (tmp_path / "run_manifest.json").read_text(encoding="utf-8")
    assert '"status": "failed"' in manifest


def test_model_version_changes_when_the_parameter_file_changes(tmp_path: Path):
    original = model_version(PARAMS, SCHEDULE["model_version_prefix"])
    changed = tmp_path / "params.yml"
    text = PARAMS.read_text(encoding="utf-8").replace("0.7", "0.6")
    changed.write_text(text, encoding="utf-8")
    assert model_version(changed, SCHEDULE["model_version_prefix"]) != original


def test_the_notebook_entrypoint_still_has_the_hard_coded_paths():
    source = (ROOT / "src" / "risk_platform" / "exercise5_model" / "notebook_as_received.py").read_text(encoding="utf-8")
    runner = (ROOT / "src" / "risk_platform" / "exercise5_model" / "production_runner.py").read_text(encoding="utf-8")
    assert "C:\\Users\\analyst\\Desktop\\features.csv" in source
    assert "C:\\Users\\analyst\\Desktop" not in runner
