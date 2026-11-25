"""Load the case extract as text, run sql/case_clean.sql, and apply the lossless assertion.

The build fails when assertion_case_clean_lossless returns a row.
A quality flag does not fail the build. A missing source row does.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

REQUIRED_COLUMNS = [
    "record_id",
    "record_type",
    "review_status",
    "created_date",
    "support_type",
    "encroachment_type",
    "structure_number",
    "circuit",
    "latitude",
    "longitude",
    "parcel_number",
    "is_deleted",
]

SQL_PATH = Path(__file__).resolve().parent / "sql" / "case_clean.sql"


class BuildFailed(RuntimeError):
    """Raised when the lossless assertion returns a row. The job exits non-zero."""


def load_source(csv_path: Path, load_id: str) -> pd.DataFrame:
    # Every column is read as text. 1018 and 010/072 are identifiers, and a numeric read would change them.
    frame = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise BuildFailed(
            "Schema check failed. Expected columns that are not in the file: "
            + ", ".join(missing)
            + ". The build stops because the clean layer would have to drop or invent fields."
        )
    extras = [column for column in frame.columns if column not in REQUIRED_COLUMNS]
    frame.insert(0, "source_row_id", range(1, len(frame) + 1))
    frame.insert(1, "load_id", load_id)
    frame.insert(2, "source_file", Path(csv_path).name)
    if extras:
        frame["unmapped_source_columns"] = frame[extras].apply(
            lambda row: json.dumps(row.to_dict(), ensure_ascii=True), axis=1
        )
        frame = frame.drop(columns=extras)
    else:
        frame["unmapped_source_columns"] = ""
    return frame


def build_case_clean(csv_path: Path, load_id: str = "cases-sample-appendix") -> duckdb.DuckDBPyConnection:
    frame = load_source(csv_path, load_id)
    connection = duckdb.connect()
    connection.register("case_source_frame", frame)
    connection.execute("CREATE TABLE case_source AS SELECT * FROM case_source_frame")
    connection.execute(SQL_PATH.read_text(encoding="utf-8"))
    assert_lossless(connection)
    return connection


def assert_lossless(connection: duckdb.DuckDBPyConnection) -> None:
    """Fail the build when a source row is missing from case_clean, added to it, or duplicated."""
    failures = connection.execute("SELECT * FROM assertion_case_clean_lossless").fetchall()
    if failures:
        preview = "; ".join(f"{kind} source_row_id={row_id}" for kind, row_id in failures[:10])
        raise BuildFailed(
            f"Lossless assertion failed with {len(failures)} row(s). "
            "Every input row must be present in case_clean. "
            f"First failures: {preview}"
        )


def proof(connection: duckdb.DuckDBPyConnection) -> dict:
    """Build counts: input, output, population, duplicates."""
    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM case_source) AS input_rows,
            (SELECT COUNT(*) FROM case_clean) AS clean_rows,
            (SELECT COUNT(*) FROM assertion_case_clean_lossless) AS assertion_failures,
            (SELECT COUNT(*) FROM case_clean WHERE in_risk_population) AS in_population,
            (SELECT COUNT(*) FROM case_clean WHERE NOT in_risk_population) AS out_of_population,
            (SELECT COUNT(*) FROM case_duplicate_review) AS duplicate_rows,
            (SELECT COUNT(*) FROM case_clean WHERE duplicate_resolution = 'unresolved_conflict') AS unresolved_conflicts,
            (SELECT typeof(structure_number) FROM case_clean LIMIT 1) AS structure_number_type
        """
    ).fetchone()
    keys = [
        "input_rows",
        "clean_rows",
        "assertion_failures",
        "in_population",
        "out_of_population",
        "duplicate_rows",
        "unresolved_conflicts",
        "structure_number_type",
    ]
    return dict(zip(keys, row))
