from pathlib import Path

import pytest

from risk_platform.exercise3_case_clean import BuildFailed, build_case_clean, proof

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data" / "samples" / "cases_source_sample.csv"
SQL = (ROOT / "src" / "risk_platform" / "sql" / "case_clean.sql").read_text(encoding="utf-8")
RULES = (ROOT / "config" / "case_clean_rules.yml").read_text(encoding="utf-8")


def test_contract_values_in_config_are_the_values_in_the_sql():
    assert "Land Rights Support" in RULES and "Land Rights Support" in SQL
    assert "Encroachment Management" in RULES and "Encroachment Management" in SQL
    assert "2019-12-31" in RULES and "2019-12-31" in SQL
    assert "in_risk_population" in SQL
    assert "WHERE in_risk_population" not in SQL.split("CREATE OR REPLACE TABLE case_clean")[1].split("CREATE OR REPLACE VIEW")[0]


def test_every_input_row_survives_and_the_assertion_is_empty():
    connection = build_case_clean(CASES)
    counts = proof(connection)
    assert counts["input_rows"] == 31
    assert counts["clean_rows"] == 31
    assert counts["assertion_failures"] == 0
    assert counts["structure_number_type"] == "VARCHAR"
    assert counts["unresolved_conflicts"] == 2


def test_identifiers_that_look_numeric_stay_text():
    connection = build_case_clean(CASES)
    row = connection.execute(
        """
        SELECT structure_number
        FROM case_clean
        WHERE record_id = 'CASE-100001'
        """
    ).fetchone()
    assert row[0] == "1018"
    assert not isinstance(row[0], (int, float))
    preserved = connection.execute(
        "SELECT COUNT(*) FROM case_clean WHERE structure_number = '010/072'"
    ).fetchone()[0]
    assert preserved >= 1
    z_structure = connection.execute(
        "SELECT COUNT(*) FROM case_clean WHERE structure_number = 'Z008/001'"
    ).fetchone()[0]
    assert z_structure >= 1


def test_population_is_a_flag_and_deleted_rows_are_not_silently_removed():
    connection = build_case_clean(CASES)
    flags = connection.execute(
        """
        SELECT record_id, source_row_id, in_risk_population, is_deleted, status_bucket
        FROM case_clean
        ORDER BY source_row_id
        """
    ).fetchdf()
    by_row = flags.set_index("source_row_id")
    # Outside the population: Hydro Support, Project Support, a 2019 date, and a blank support type.
    out = set(flags.loc[~flags["in_risk_population"], "record_id"])
    assert out == {"CASE-100003", "CASE-100007", "CASE-100019", "CASE-100022"}
    deleted = flags.loc[flags["record_id"] == "CASE-100002"].iloc[0]
    assert bool(deleted["is_deleted"]) is True
    assert bool(deleted["in_risk_population"]) is True
    pending = flags.loc[flags["record_id"] == "CASE-100011"].iloc[0]
    assert pending["status_bucket"] == "other"
    assert bool(pending["in_risk_population"]) is True


def test_bad_coordinates_are_flagged_and_not_rewritten():
    connection = build_case_clean(CASES)
    swapped = connection.execute(
        """
        SELECT latitude, longitude, coordinates_usable, dq_rule_ids
        FROM case_clean WHERE record_id = 'CASE-100009'
        """
    ).fetchone()
    assert swapped[0] == -122.135589
    assert swapped[1] == 39.462319
    assert swapped[2] is False
    assert "coordinates_appear_swapped" in swapped[3]
    sign = connection.execute(
        """
        SELECT longitude, coordinates_usable, dq_rule_ids
        FROM case_clean WHERE record_id = 'CASE-100016'
        """
    ).fetchone()
    assert sign[0] == 121.999336
    assert sign[1] is False
    assert "longitude_missing_negative_sign" in sign[2]


def test_conflicting_duplicate_has_no_survivor():
    connection = build_case_clean(CASES)
    rows = connection.execute(
        """
        SELECT review_status, duplicate_resolution, duplicate_keeper
        FROM case_clean
        WHERE record_id = 'CASE-100005'
        ORDER BY source_row_id
        """
    ).fetchall()
    assert [row[0] for row in rows] == ["Not Needed", "Completed"]
    assert {row[1] for row in rows} == {"unresolved_conflict"}
    assert all(row[2] is False for row in rows)


def test_utc_conversion_uses_the_source_offset():
    connection = build_case_clean(CASES)
    created = connection.execute(
        """
        SELECT created_civil_date, created_at_utc
        FROM case_clean WHERE record_id = 'CASE-100000'
        """
    ).fetchone()
    assert str(created[0]) == "2024-03-13"
    assert str(created[1]).startswith("2024-03-13 17:51:00")


def test_dropping_a_row_fails_the_build():
    connection = build_case_clean(CASES)
    connection.execute("DELETE FROM case_clean WHERE source_row_id = 1")
    try:
        from risk_platform.exercise3_case_clean import assert_lossless

        assert_lossless(connection)
        raised = False
    except BuildFailed:
        raised = True
    assert raised


def test_missing_column_fails_before_a_silent_load(tmp_path: Path):
    import pandas as pd

    broken = tmp_path / "cases.csv"
    frame = pd.read_csv(CASES, dtype=str, keep_default_na=False).drop(columns=["parcel_number"])
    frame.to_csv(broken, index=False)
    with pytest.raises(BuildFailed, match="parcel_number"):
        build_case_clean(broken)
