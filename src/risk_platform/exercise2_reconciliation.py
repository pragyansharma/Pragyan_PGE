"""Reconcile the report population to the synced copy, one record id at a time.

1,373 against 1,498 is a count, not a classification. Until the report's
record ids are in hand, the status is awaiting_report_keys.

A predicate that cannot be run does not explain a row.
The published columns are the record id, the class, and the predicate id.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd
import yaml


@dataclass
class ReconciliationResult:
    status: str
    classifications: list[dict]
    summary: dict
    alerts: list[dict]


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def reconcile(
    sync: pd.DataFrame | None,
    report: pd.DataFrame | None,
    config: dict,
    prior_unexplained: int | None = None,
) -> ReconciliationResult:
    """Classify both sides. Missing report keys stop the reconciliation."""
    scenario = config["scenario"]
    if report is None or report.empty or "record_id" not in getattr(report, "columns", []):
        summary = {
            "status": "awaiting_report_keys",
            "report_count_claimed": scenario["report_count"],
            "sync_count_claimed": scenario["sync_count"],
            "gap_claimed": scenario["sync_count"] - scenario["report_count"],
            "unexplained_count": None,
            "message": (
                "Both counts are published. The gap is not classified until the "
                "report owner sends the record ids behind the report count."
            ),
        }
        return ReconciliationResult(
            status="awaiting_report_keys",
            classifications=[],
            summary=summary,
            alerts=_alerts("awaiting_report_keys", config, summary),
        )

    connection = duckdb.connect()
    connection.register("sync_in", sync.astype("string"))
    connection.register("report_in", report.astype("string"))
    _install_predicates(connection, config)
    connection.execute(_CLASSIFY_SQL)
    classifications = connection.execute(
        """
        SELECT record_id, classification, explained_by, in_sync, in_report
        FROM classification
        ORDER BY record_id
        """
    ).fetchdf().to_dict(orient="records")
    summary = connection.execute(
        """
        SELECT
            COUNT(*) AS keys_compared,
            SUM(CASE WHEN classification = 'matched' THEN 1 ELSE 0 END) AS matched,
            SUM(CASE WHEN classification = 'attribute_conflict' THEN 1 ELSE 0 END) AS attribute_conflict,
            SUM(CASE WHEN classification = 'sync_only_explained' THEN 1 ELSE 0 END) AS sync_only_explained,
            SUM(CASE WHEN classification = 'sync_only_unexplained' THEN 1 ELSE 0 END) AS sync_only_unexplained,
            SUM(CASE WHEN classification = 'report_only' THEN 1 ELSE 0 END) AS report_only
        FROM classification
        """
    ).fetchdf().to_dict(orient="records")[0]
    summary = {key: int(value) for key, value in summary.items()}
    summary["status"] = "classified"
    summary["report_count_claimed"] = scenario["report_count"]
    summary["sync_count_claimed"] = scenario["sync_count"]
    summary["prior_unexplained"] = prior_unexplained
    summary["unexplained_jump"] = (
        prior_unexplained is not None and summary["sync_only_unexplained"] > prior_unexplained
    )
    # report_only is the run status. An increase in unexplained rows is still paged.
    if summary["report_only"] > 0:
        status = "report_only_present"
    elif summary["unexplained_jump"]:
        status = "unexplained_jump"
    else:
        status = "classified"
    summary["status"] = status
    return ReconciliationResult(
        status=status,
        classifications=classifications,
        summary=summary,
        alerts=_alerts(status, config, summary),
    )


def _install_predicates(connection: duckdb.DuckDBPyConnection, config: dict) -> None:
    """Register the predicates that can be executed. Unresolved predicates are not applied."""
    rows = []
    for predicate in config["known_predicates"]:
        rows.append(
            {
                "predicate_id": predicate["id"],
                "column_name": predicate["column"],
                "equals_value": predicate.get("equals"),
                "on_or_after": predicate.get("on_or_after"),
                "expected": predicate["expected"],
            }
        )
    connection.register("known_predicate", pd.DataFrame(rows))


_CLASSIFY_SQL = """
CREATE TABLE classification AS
WITH keys AS (
    SELECT record_id FROM sync_in
    UNION
    SELECT record_id FROM report_in
),
-- A known predicate fails closed: a null attribute does not pass.
sync_checks AS (
    SELECT
        s.record_id,
        p.predicate_id,
        CASE p.column_name
            WHEN 'is_deleted' THEN s.is_deleted = p.equals_value
            WHEN 'record_type' THEN s.record_type = p.equals_value
            WHEN 'review_status' THEN s.review_status = p.equals_value
            WHEN 'created_civil_date' THEN s.created_civil_date >= p.on_or_after
            ELSE FALSE
        END AS passed
    FROM sync_in s
    CROSS JOIN known_predicate p
),
sync_result AS (
    SELECT
        record_id,
        BOOL_AND(passed) AS passes_known,
        string_agg(CASE WHEN NOT passed THEN predicate_id END, ',') AS failed_predicates
    FROM sync_checks
    GROUP BY record_id
),
attribute_compare AS (
    SELECT
        s.record_id,
        (
            s.is_deleted IS NOT DISTINCT FROM r.is_deleted
            AND s.created_civil_date IS NOT DISTINCT FROM r.created_civil_date
            AND s.record_type IS NOT DISTINCT FROM r.record_type
            AND s.review_status IS NOT DISTINCT FROM r.review_status
        ) AS attributes_agree
    FROM sync_in s
    JOIN report_in r USING (record_id)
)
SELECT
    k.record_id,
    s.record_id IS NOT NULL AS in_sync,
    r.record_id IS NOT NULL AS in_report,
    CASE
        WHEN s.record_id IS NULL THEN 'report_only'
        WHEN r.record_id IS NOT NULL AND NOT coalesce(a.attributes_agree, TRUE) THEN 'attribute_conflict'
        WHEN r.record_id IS NOT NULL THEN 'matched'
        WHEN NOT coalesce(c.passes_known, FALSE) THEN 'sync_only_explained'
        ELSE 'sync_only_unexplained'
    END AS classification,
    CASE
        WHEN s.record_id IS NULL THEN 'not_in_sync'
        WHEN r.record_id IS NOT NULL AND NOT coalesce(a.attributes_agree, TRUE) THEN 'attributes_differ'
        WHEN r.record_id IS NOT NULL THEN ''
        ELSE coalesce(c.failed_predicates, '')
    END AS explained_by
FROM keys k
LEFT JOIN sync_in s USING (record_id)
LEFT JOIN report_in r USING (record_id)
LEFT JOIN sync_result c USING (record_id)
LEFT JOIN attribute_compare a USING (record_id);
"""


def _alerts(status: str, config: dict, summary: dict) -> list[dict]:
    alerts = []
    page_when = set(config["alerts"]["page_when"])
    email_when = set(config["alerts"]["email_when"])
    fired = set()
    if status == "awaiting_report_keys":
        fired.add("awaiting_report_keys")
        fired.add("recon_blocked")
    if summary.get("report_only"):
        fired.add("report_only_count_gt_0")
    if summary.get("unexplained_jump"):
        fired.add("unexplained_jump_vs_prior_day")
    if summary.get("sync_only_unexplained"):
        fired.add("unexplained_count_gt_0")
    for code in sorted(fired & page_when):
        alerts.append(
            {
                "channel": "page",
                "code": code,
                "recipients": ",".join(config["alerts"]["recipients"]["page"]),
                "detail": _safe_detail(summary),
            }
        )
    for code in sorted(fired & email_when):
        alerts.append(
            {
                "channel": "email",
                "code": code,
                "recipients": ",".join(config["alerts"]["recipients"]["email"]),
                "detail": _safe_detail(summary),
            }
        )
    return alerts


def _safe_detail(summary: dict) -> str:
    """Alert detail is counts and status. Case attributes are not included."""
    kept = {
        key: summary.get(key)
        for key in (
            "status",
            "matched",
            "attribute_conflict",
            "sync_only_explained",
            "sync_only_unexplained",
            "report_only",
            "report_count_claimed",
            "sync_count_claimed",
            "gap_claimed",
        )
    }
    return str({key: value for key, value in kept.items() if value is not None})
