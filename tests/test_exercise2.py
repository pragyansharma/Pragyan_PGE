from pathlib import Path

import pandas as pd

from risk_platform.exercise2_reconciliation import load_config, reconcile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config" / "reconciliation.yml")
FIXTURE = ROOT / "data" / "samples" / "reconciliation"


def _frames():
    sync = pd.read_csv(FIXTURE / "sync_illustrative.csv", dtype=str)
    report = pd.read_csv(FIXTURE / "report_illustrative.csv", dtype=str)
    return sync, report


def test_missing_report_keys_block_the_recon_and_keep_the_gap_visible():
    result = reconcile(None, None, CONFIG)
    assert result.status == "awaiting_report_keys"
    assert result.summary["gap_claimed"] == 125
    assert result.summary["report_count_claimed"] == 1373
    assert result.summary["sync_count_claimed"] == 1498
    assert result.classifications == []
    assert any(alert["code"] == "recon_blocked" and alert["channel"] == "page" for alert in result.alerts)
    assert "Land Rights" not in result.alerts[0]["detail"]


def test_every_illustrative_key_is_classified_and_hidden_filters_do_not_explain_rows():
    sync, report = _frames()
    result = reconcile(sync, report, CONFIG)
    by_id = {row["record_id"]: row["classification"] for row in result.classifications}
    assert by_id == {
        "S-MATCH": "matched",
        "S-DELETED": "sync_only_explained",
        "S-OLD": "sync_only_explained",
        "S-TYPE": "sync_only_explained",
        "S-UNEXPLAINED": "sync_only_unexplained",
        "S-CONFLICT": "attribute_conflict",
        "S-REPORT-ONLY": "report_only",
    }
    unexplained = next(row for row in result.classifications if row["record_id"] == "S-UNEXPLAINED")
    assert unexplained["explained_by"] == ""
    assert result.summary["report_only"] == 1
    assert any(alert["code"] == "report_only_count_gt_0" for alert in result.alerts)


def test_a_jump_in_the_unexplained_residual_is_paged():
    sync, report = _frames()
    result = reconcile(sync, report, CONFIG, prior_unexplained=0)
    # report_only is the run status. The unexplained increase is paged on its own.
    assert result.status == "report_only_present"
    assert any(alert["channel"] == "page" and alert["code"] == "unexplained_jump_vs_prior_day" for alert in result.alerts)
