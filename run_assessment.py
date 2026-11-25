"""Run every exercise on the sample data and write outputs/."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import yaml

from risk_platform.exercise1_field_ingest import FieldIngestSession, load_rules, write_outputs
from risk_platform.exercise2_reconciliation import load_config as load_recon
from risk_platform.exercise2_reconciliation import reconcile
from risk_platform.exercise3_case_clean import build_case_clean, proof
from risk_platform.exercise4_spatial import join_cases_to_structures
from risk_platform.exercise4_spatial import load_config as load_spatial
from risk_platform.exercise5_model.production_runner import run as run_model

SAMPLES = ROOT / "data" / "samples"
OUT = ROOT / "outputs"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    _exercise1()
    _exercise2()
    _exercise3()
    _exercise4()
    _exercise5()
    print(f"Wrote {OUT}")


def _exercise1() -> None:
    rules = load_rules(ROOT / "config" / "field_measurement_rules.yml")
    session = FieldIngestSession(rules)
    field = SAMPLES / "field"
    session.ingest_path(field / "AB_20260301_zone2_meas.csv", arrived_at="2026-03-01T18:00:00+00:00")
    session.ingest_path(field / "AB_20260302_zone2_meas.csv", arrived_at="2026-03-02T18:00:00+00:00")
    session.ingest_path(field / "CD_zone2_export_final.csv", arrived_at="2026-03-02T19:00:00+00:00")
    # Same bytes delivered again. Expected result is a no-op, with no new rows.
    session.ingest_path(field / "AB_20260301_zone2_meas.csv", arrived_at="2026-03-01T20:00:00+00:00")
    write_outputs(session, OUT / "exercise1")


def _exercise2() -> None:
    config = load_recon(ROOT / "config" / "reconciliation.yml")
    blocked = reconcile(None, None, config)
    sync = pd.read_csv(SAMPLES / "reconciliation" / "sync_illustrative.csv", dtype=str)
    report = pd.read_csv(SAMPLES / "reconciliation" / "report_illustrative.csv", dtype=str)
    classified = reconcile(sync, report, config, prior_unexplained=0)
    directory = OUT / "exercise2"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "blocked.json").write_text(json.dumps(_pack(blocked), indent=2), encoding="utf-8")
    (directory / "classified.json").write_text(json.dumps(_pack(classified), indent=2), encoding="utf-8")


def _exercise3() -> None:
    connection = build_case_clean(SAMPLES / "cases_source_sample.csv")
    directory = OUT / "exercise3"
    directory.mkdir(parents=True, exist_ok=True)
    connection.execute(f"COPY case_clean TO '{_sql_path(directory / 'case_clean.csv')}' (HEADER, DELIMITER ',')")
    connection.execute(
        f"COPY (SELECT * FROM case_duplicate_review) TO '{_sql_path(directory / 'duplicate_review.csv')}' (HEADER, DELIMITER ',')"
    )
    (directory / "proof.json").write_text(json.dumps(proof(connection), indent=2), encoding="utf-8")


def _exercise4() -> None:
    config = load_spatial(ROOT / "config" / "spatial.yml")
    cases = pd.read_csv(SAMPLES / "case_locations.csv", dtype=str)
    structures = pd.read_csv(SAMPLES / "structures.csv", dtype=str)
    result = join_cases_to_structures(cases, structures, config, computed_at="2026-11-20T00:00:00+00:00")
    directory = OUT / "exercise4"
    directory.mkdir(parents=True, exist_ok=True)
    result.to_csv(directory / "case_structure_distance.csv", index=False)


def _exercise5() -> None:
    schedule = yaml.safe_load((ROOT / "config" / "model_schedule.yml").read_text(encoding="utf-8"))
    # Sample features are dated 2026-11-20. The demonstration uses that clock.
    # A scheduled run uses the real clock and fails when the extract is older than the limit.
    run_model(
        SAMPLES / "model" / "features.csv",
        ROOT / "src" / "risk_platform" / "exercise5_model" / "params.yml",
        OUT / "exercise5" / "risk_score.csv",
        schedule,
        now=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc),
    )


def _pack(result) -> dict:
    return {
        "status": result.status,
        "summary": result.summary,
        "alerts": result.alerts,
        "classifications": result.classifications,
    }


def _sql_path(path: Path) -> str:
    return str(path).replace("\\", "/")


if __name__ == "__main__":
    main()
