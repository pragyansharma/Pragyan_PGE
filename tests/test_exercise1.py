from pathlib import Path

from risk_platform.exercise1_field_ingest import FieldIngestSession, load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "config" / "field_measurement_rules.yml")
FIELD = ROOT / "data" / "samples" / "field"


def _session() -> FieldIngestSession:
    session = FieldIngestSession(RULES)
    session.ingest_path(FIELD / "AB_20260301_zone2_meas.csv", arrived_at="2026-03-01T18:00:00+00:00")
    session.ingest_path(FIELD / "AB_20260302_zone2_meas.csv", arrived_at="2026-03-02T18:00:00+00:00")
    session.ingest_path(FIELD / "CD_zone2_export_final.csv", arrived_at="2026-03-02T19:00:00+00:00")
    return session


def test_bad_file_name_and_meters_are_rejected_and_visible():
    session = _session()
    rejected = [record for record in session.registry if record.file_name.startswith("CD_")]
    assert rejected[0].status == "rejected"
    rule_ids = {finding.rule_id for finding in rejected[0].findings}
    assert "file_name_contract" in rule_ids
    assert "distance_unit_ftus" in rule_ids
    quarantine = session.quarantine_rows()
    assert {row["label"] for row in quarantine} == {
        "CASE-000201_H",
        "CASE-000201_V",
        "CASE-000201_3D",
        "CASE-000201_S",
        "CASE-000201_HT",
    }
    loaded_labels = {row["label"] for row in session.loaded_rows()}
    assert "CASE-000201_H" not in loaded_labels


def test_feedback_goes_to_the_engineer_without_coordinates():
    session = _session()
    feedback = session.feedback_by_engineer()
    cd_text = " ".join(line["expected"] for line in feedback["CD"])
    assert "Distance (ftUS)" in cd_text
    assert "initials_yyyymmdd_zoneN_meas" in cd_text
    assert "1934287" not in cd_text
    ab_rules = {line["rule_id"] for line in feedback["AB"]}
    assert "label_separator" in ab_rules
    assert "label_suffix" in ab_rules
    assert "notes_shape_on_s" in ab_rules
    assert "tag_shape_on_ht" in ab_rules
    assert "measurement_set_incomplete" in ab_rules
    assert "distance_recomputes" in ab_rules
    for line in feedback["AB"] + feedback["CD"]:
        assert "Easting" not in line["expected"]


def test_horizontal_versus_straight_line_is_flagged_not_replaced():
    session = _session()
    rows = session.loaded_rows()
    suspect = next(row for row in rows if row["label"] == "CASE-000103_S")
    assert "distance_recomputes" in suspect["finding_ids"]
    assert suspect["reported_distance_ftus"] == 105.518
    assert abs(suspect["recomputed_distance_ftus"] - 104.138) < 0.01


def test_later_day_is_current_and_the_earlier_day_is_kept():
    session = _session()
    rows = session.loaded_rows()
    case_102 = [row for row in rows if row["case_id"] == "CASE-000102" and row["measure_code"] == "HT"]
    assert len(case_102) == 2
    current = [row for row in case_102 if row["is_current_for_case"]]
    assert len(current) == 1
    assert current[0]["measurement_date"] == "2026-03-02"
    earlier = [row for row in case_102 if not row["is_current_for_case"]]
    assert earlier[0]["current_reason"] == "later_measurement_exists"
    assert earlier[0]["file_is_active"] is True


def test_duplicate_measure_keeps_the_legal_label_current():
    session = _session()
    rows = [
        row
        for row in session.loaded_rows()
        if row["case_id"] == "CASE-000103" and row["measure_code"] == "H"
    ]
    assert len(rows) == 2
    current = [row for row in rows if row["is_current_for_case"]]
    assert len(current) == 1
    assert current[0]["label"] == "CASE-000103_H"
    loser = [row for row in rows if not row["is_current_for_case"]]
    assert loser[0]["label"] == "CASE-000103-H"
    assert loser[0]["current_reason"] == "duplicate_measure_not_selected"


def test_same_bytes_do_not_double_count():
    session = _session()
    before = len(session.loaded_rows())
    second = session.ingest_path(FIELD / "AB_20260301_zone2_meas.csv")
    assert second.status == "duplicate_noop"
    assert len(session.loaded_rows()) == before


def test_rejected_reexport_does_not_replace_a_good_file(tmp_path: Path):
    session = FieldIngestSession(RULES)
    session.ingest_path(FIELD / "AB_20260301_zone2_meas.csv")
    bad = tmp_path / "AB_20260301_zone2_meas.csv"
    text = (FIELD / "AB_20260301_zone2_meas.csv").read_text(encoding="utf-8")
    bad.write_text(text.replace("Distance (ftUS)", "Distance (m)"), encoding="utf-8")
    attempt = session.ingest_path(bad)
    assert attempt.status == "rejected_attempt"
    active = [record for record in session.registry if record.status == "active"]
    assert len(active) == 1
    assert active[0].file_name == "AB_20260301_zone2_meas.csv"
    assert "still the active file" in attempt.note


def test_corrected_reexport_supersedes_without_dropping_history(tmp_path: Path):
    session = FieldIngestSession(RULES)
    session.ingest_path(FIELD / "AB_20260301_zone2_meas.csv")
    corrected = tmp_path / "AB_20260301_zone2_meas.csv"
    text = (FIELD / "AB_20260301_zone2_meas.csv").read_text(encoding="utf-8")
    corrected.write_text(
        text.replace("Shed / stored materials / debris; present", "Shed; present"),
        encoding="utf-8",
    )
    replacement = session.ingest_path(corrected)
    assert replacement.status == "active"
    assert replacement.supersedes_ingest_id
    rows = session.loaded_rows()
    superseded = [row for row in rows if row["file_status"] == "superseded"]
    assert superseded
    assert all(not row["is_current_for_case"] for row in superseded)
    current_tags = [
        row for row in rows if row["is_current_for_case"] and row["case_id"] == "CASE-000102" and row["measure_code"] == "HT"
    ]
    assert len(current_tags) == 1
    assert current_tags[0]["file_status"] == "active"
