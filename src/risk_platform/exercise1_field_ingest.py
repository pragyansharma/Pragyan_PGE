"""Load field-measurement CSVs from the shared drop.

The file key is engineer, measurement date, and zone.
The same content hash is a no-op. A new hash replaces the active file only
when the file-level checks pass. A rejected re-export leaves the previous file active.

Flagged rows are loaded. The reported distance is the value kept.
Feedback carries the file, the spreadsheet row, the label, the rule, and what was expected.
Coordinates, notes, and tags are not on that file. The shared summary is a count by rule.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

LABEL_RE = re.compile(
    r"^(?P<case>CASE-\d{6})(?:_(?P<object>o\d+))?(?:_(?P<circuit>c\d+))?_(?P<measure>H|V|3D|S|HT)$"
)
CASE_AND_MEASURE = re.compile(r"^(?P<case>CASE-\d{6})(?P<sep>[-_])(?P<measure>H|V|3D|S|HT)$")
MEASURE_THEN_JUNK = re.compile(
    r"^(?P<case>CASE-\d{6})_(?P<measure>H|V|3D|S|HT)_(?P<junk>.+)$"
)
CASE_ONLY = re.compile(r"(CASE-\d{6})")


@dataclass
class Finding:
    rule_id: str
    severity: str
    expected: str
    spreadsheet_row: int | None = None
    label: str | None = None
    detail: str = ""

    def feedback_text(self) -> str:
        if self.detail:
            return f"{self.expected} {self.detail}"
        return self.expected


@dataclass
class FileRecord:
    ingest_id: str
    file_name: str
    file_hash: str
    arrived_at: str
    engineer: str | None
    measurement_date: str | None
    zone: str | None
    status: str
    findings: list[Finding] = field(default_factory=list)
    supersedes_ingest_id: str | None = None
    note: str = ""


def load_rules(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _rule(rules: dict, rule_id: str) -> Finding:
    spec = rules["rules"][rule_id]
    return Finding(rule_id=rule_id, severity=spec["severity"], expected=spec["expected"])


def _blank(value: str | None) -> bool:
    return value is None or str(value).strip() == ""


def _float_or_none(value: str) -> float | None:
    if _blank(value):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def file_stem(path: Path) -> str:
    name = path.name
    # Only a configured extension is removed. "export_final" belongs to the stem.
    if name.lower().endswith(".csv"):
        return name[: -len(".csv")]
    return name


def parse_file_name(stem: str, rules: dict) -> dict | None:
    if not re.match(rules["file_name"]["pattern"], stem):
        return None
    initials, ymd, zone_token, _meas = stem.split("_")
    return {
        "engineer": initials,
        "measurement_date": f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}",
        "zone": zone_token.replace("zone", ""),
    }


def _expected_distance(measure: str, e1: float, n1: float, h1: float, e2: float, n2: float, h2: float) -> float:
    de, dn, dh = e2 - e1, n2 - n1, h2 - h1
    if measure in ("H", "S"):
        return (de * de + dn * dn) ** 0.5
    if measure in ("V", "HT"):
        return abs(dh)
    if measure == "3D":
        return (de * de + dn * dn + dh * dh) ** 0.5
    raise ValueError(measure)


def parse_label(raw: str) -> dict:
    """Read a case id from a label that does not match the contract. The label is not rewritten."""
    text = (raw or "").strip()
    match = LABEL_RE.match(text)
    if match:
        return {
            "case_id": match.group("case"),
            "object_index": match.group("object") or "",
            "circuit_index": match.group("circuit") or "",
            "measure_code": match.group("measure"),
            "label_status": "valid",
            "problems": [],
        }
    problems = []
    sep = CASE_AND_MEASURE.match(text)
    if sep:
        if sep.group("sep") != "_":
            problems.append("label_separator")
        return {
            "case_id": sep.group("case"),
            "object_index": "",
            "circuit_index": "",
            "measure_code": sep.group("measure"),
            "label_status": "salvaged",
            "problems": problems,
        }
    junk = MEASURE_THEN_JUNK.match(text)
    if junk:
        problems.append("label_suffix")
        return {
            "case_id": junk.group("case"),
            "object_index": "",
            "circuit_index": "",
            "measure_code": junk.group("measure"),
            "label_status": "salvaged",
            "problems": problems,
        }
    case = CASE_ONLY.search(text)
    if case:
        problems.append("label_suffix")
        return {
            "case_id": case.group(1),
            "object_index": "",
            "circuit_index": "",
            "measure_code": "",
            "label_status": "salvaged",
            "problems": problems,
        }
    return {
        "case_id": "",
        "object_index": "",
        "circuit_index": "",
        "measure_code": "",
        "label_status": "unparseable",
        "problems": ["label_unparseable"],
    }


def _tag_finding(tag: str, rules: dict) -> Finding | None:
    expected_types = {item.lower() for item in rules["tags"]["object_types"]}
    expected_states = {item.lower() for item in rules["tags"]["states"]}
    parts = [part.strip() for part in tag.split(";")]
    finding = _rule(rules, "tag_shape_on_ht")
    if len(parts) != 2 or any(not part for part in parts):
        finding.detail = "The tag did not split into one object type and one state."
        return finding
    object_type, state = parts
    if "/" in object_type or "," in object_type:
        finding.detail = "Expected one object type. The tag lists more than one."
        return finding
    if object_type.lower() not in expected_types or state.lower() not in expected_states:
        finding.detail = "The object type or the state is not in the controlled list."
        return finding
    return None


def assess_row(record: dict, spreadsheet_row: int, rules: dict) -> tuple[dict, list[Finding]]:
    label = (record.get("Name") or "").strip()
    parsed = parse_label(label)
    findings: list[Finding] = []
    for problem in parsed["problems"]:
        finding = _rule(rules, problem)
        finding.spreadsheet_row = spreadsheet_row
        finding.label = label or None
        findings.append(finding)

    numbers = {
        key: _float_or_none(record.get(key, ""))
        for key in ("Easting1", "Northing1", "Height1", "Easting2", "Northing2", "Height2")
    }
    distance_header = rules["distance"]["required_header"]
    # A meters file has already failed the unit rule. A missing-distance finding would be a second, wrong message.
    has_distance_header = distance_header in record
    reported = _float_or_none(record.get(distance_header, "")) if has_distance_header else None
    endpoints_missing = any(value is None for value in numbers.values())
    if endpoints_missing or (has_distance_header and reported is None):
        finding = _rule(rules, "required_endpoint")
        finding.spreadsheet_row = spreadsheet_row
        finding.label = label or None
        findings.append(finding)

    measure = parsed["measure_code"]
    recomputed = None
    if (
        has_distance_header
        and measure in rules["measure_codes"]
        and all(value is not None for value in numbers.values())
    ):
        recomputed = _expected_distance(measure, *[numbers[k] for k in (
            "Easting1", "Northing1", "Height1", "Easting2", "Northing2", "Height2"
        )])
        if reported is not None and abs(reported - recomputed) > float(rules["distance"]["tolerance_ftus"]):
            finding = _rule(rules, "distance_recomputes")
            finding.spreadsheet_row = spreadsheet_row
            finding.label = label or None
            # The feedback line may show both distances. It does not show coordinates.
            straight = (
                (numbers["Easting2"] - numbers["Easting1"]) ** 2
                + (numbers["Northing2"] - numbers["Northing1"]) ** 2
                + (numbers["Height2"] - numbers["Height1"]) ** 2
            ) ** 0.5
            finding.detail = (
                f"Reported {reported:.3f} ftUS. "
                f"Recomputed for {measure} is {recomputed:.3f} ftUS. "
                f"Straight-line distance of the same endpoints is {straight:.3f} ftUS."
            )
            findings.append(finding)
        if measure == "HT":
            horizontal = (
                (numbers["Easting2"] - numbers["Easting1"]) ** 2
                + (numbers["Northing2"] - numbers["Northing1"]) ** 2
            ) ** 0.5
            if horizontal > float(rules["distance"]["max_horizontal_for_height_ftus"]):
                finding = _rule(rules, "height_axis_for_ht")
                finding.spreadsheet_row = spreadsheet_row
                finding.label = label or None
                findings.append(finding)

    notes = (record.get("Notes") or "").strip()
    tags = (record.get("Tags") or "").strip()
    if measure == "S":
        if not re.match(rules["notes"]["pattern"], notes):
            finding = _rule(rules, "notes_shape_on_s")
            finding.spreadsheet_row = spreadsheet_row
            finding.label = label or None
            findings.append(finding)
    elif notes:
        finding = _rule(rules, "notes_not_only_on_s")
        finding.spreadsheet_row = spreadsheet_row
        finding.label = label or None
        findings.append(finding)

    if measure == "HT":
        tag_finding = _tag_finding(tags, rules) if tags else _rule(rules, "tag_shape_on_ht")
        if tags == "":
            tag_finding.detail = "The HT tag is empty."
        if tag_finding is not None:
            tag_finding.spreadsheet_row = spreadsheet_row
            tag_finding.label = label or None
            findings.append(tag_finding)
    elif tags:
        finding = _rule(rules, "tag_not_only_on_ht")
        finding.spreadsheet_row = spreadsheet_row
        finding.label = label or None
        findings.append(finding)

    row_severity = _worst(findings)
    loaded = {
        "spreadsheet_row": spreadsheet_row,
        "label": label,
        "case_id": parsed["case_id"],
        "object_index": parsed["object_index"],
        "circuit_index": parsed["circuit_index"],
        "measure_code": measure,
        "label_status": parsed["label_status"],
        "reported_distance_ftus": reported,
        "recomputed_distance_ftus": None if recomputed is None else round(recomputed, 6),
        "row_severity": row_severity,
        "set_missing": "",
        "findings": findings,
    }
    return loaded, findings


def _worst(findings: list[Finding]) -> str:
    order = {"accept": 0, "flag": 1, "reject_row": 2, "reject_file": 3}
    if not findings:
        return "accept"
    return max((item.severity for item in findings), key=lambda severity: order[severity])


class FieldIngestSession:
    def __init__(self, rules: dict):
        self.rules = rules
        self.registry: list[FileRecord] = []
        self.payloads: dict[str, list[dict]] = {}
        self._sequence = 0

    def ingest_path(self, path: Path, arrived_at: str | None = None) -> FileRecord:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        arrived = arrived_at or datetime.now(timezone.utc).isoformat()
        stem = file_stem(path)
        identity = parse_file_name(stem, self.rules)
        self._sequence += 1
        ingest_id = f"ing-{self._sequence:04d}"
        findings: list[Finding] = []

        if identity is None:
            findings.append(_rule(self.rules, "file_name_contract"))
            # The leading token still attributes the feedback. The file name has already failed the contract.
            leading = stem.split("_")[0]
            engineer = leading if leading in self.rules["roster"]["initials"] else None
            identity_out = {"engineer": engineer, "measurement_date": None, "zone": None}
        else:
            identity_out = identity
            if identity["engineer"] not in self.rules["roster"]["initials"]:
                findings.append(_rule(self.rules, "engineer_on_roster"))

        business_key = None
        if identity is not None:
            business_key = (identity["engineer"], identity["measurement_date"], identity["zone"])
            prior = self._active_with_hash(business_key, digest)
            if prior is not None:
                record = FileRecord(
                    ingest_id=ingest_id,
                    file_name=path.name,
                    file_hash=digest,
                    arrived_at=arrived,
                    status="duplicate_noop",
                    note="Same engineer, date, zone, and file hash as the active file. No rows were added.",
                    **identity_out,
                )
                self.registry.append(record)
                return record

        header, rows = self._read(path, findings)
        if header is not None:
            self._check_shape(header, rows, findings)

        file_rejected = any(item.severity == "reject_file" for item in findings)
        record = FileRecord(
            ingest_id=ingest_id,
            file_name=path.name,
            file_hash=digest,
            arrived_at=arrived,
            status="rejected" if file_rejected else "active",
            findings=findings,
            **identity_out,
        )

        if business_key and not file_rejected:
            previous = self._active_key(business_key)
            if previous is not None:
                previous.status = "superseded"
                record.supersedes_ingest_id = previous.ingest_id
                record.note = (
                    f"Replaced {previous.file_name} ({previous.ingest_id}). "
                    "The earlier rows stay in history and are not current."
                )
        elif business_key and file_rejected:
            previous = self._active_key(business_key)
            record.status = "rejected_attempt"
            if previous is not None:
                record.note = (
                    f"Rejected re-export was not applied. {previous.file_name} "
                    f"({previous.ingest_id}) is still the active file."
                )
            else:
                record.note = "Rejected file was not loaded. There is no prior active file for this key."
        elif file_rejected:
            record.note = (
                "The file was not loaded. A name that does not match the contract "
                "cannot replace another file, and its rows cannot become current."
            )

        self.registry.append(record)
        if header is not None and rows is not None and record.status != "duplicate_noop":
            self.payloads[ingest_id] = self._build_rows(record, rows)
        return record

    def _read(self, path: Path, findings: list[Finding]) -> tuple[list[str] | None, list[dict] | None]:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            findings.append(_rule(self.rules, "file_readable"))
            return None, None
        if not text.strip():
            findings.append(_rule(self.rules, "file_not_empty"))
            return None, None
        try:
            reader = csv.DictReader(text.splitlines())
            if not reader.fieldnames:
                findings.append(_rule(self.rules, "file_readable"))
                return None, None
            return list(reader.fieldnames), list(reader)
        except csv.Error:
            findings.append(_rule(self.rules, "file_readable"))
            return None, None

    def _check_shape(self, header: list[str], rows: list[dict], findings: list[Finding]) -> None:
        if not rows:
            findings.append(_rule(self.rules, "file_not_empty"))
        missing = [column for column in self.rules["required_columns"] if column not in header]
        distance_header = self.rules["distance"]["required_header"]
        if distance_header not in header:
            # A distance column in the wrong unit is a unit failure. Absence of the column is a shape failure.
            distance_like = [column for column in header if column.lower().startswith("distance")]
            if distance_like:
                finding = _rule(self.rules, "distance_unit_ftus")
                finding.detail = f"The file declared '{distance_like[0]}'."
                findings.append(finding)
            else:
                missing.append(distance_header)
        if missing:
            finding = _rule(self.rules, "required_columns")
            finding.detail = "Missing: " + ", ".join(missing) + "."
            findings.append(finding)

    def _build_rows(self, record: FileRecord, rows: list[dict]) -> list[dict]:
        built = []
        labels: dict[str, int] = {}
        for index, source in enumerate(rows, start=1):
            spreadsheet_row = index + 1
            loaded, _findings = assess_row(source, spreadsheet_row, self.rules)
            label = loaded["label"]
            labels[label] = labels.get(label, 0) + 1
            loaded.update(
                {
                    "ingest_id": record.ingest_id,
                    "file_name": record.file_name,
                    "engineer": record.engineer,
                    "measurement_date": record.measurement_date,
                    "zone": record.zone,
                    "file_hash": record.file_hash,
                }
            )
            built.append(loaded)
        for loaded in built:
            if loaded["label"] and labels[loaded["label"]] > 1:
                finding = _rule(self.rules, "duplicate_label")
                finding.spreadsheet_row = loaded["spreadsheet_row"]
                finding.label = loaded["label"]
                loaded["findings"].append(finding)
                loaded["row_severity"] = _worst(loaded["findings"])
        self._flag_duplicate_measures(built)
        self._flag_incomplete_sets(built, record)
        return built

    def _flag_duplicate_measures(self, built: list[dict]) -> None:
        groups: dict[tuple, list[dict]] = {}
        for loaded in built:
            if not loaded["case_id"] or not loaded["measure_code"]:
                continue
            key = (loaded["case_id"], loaded["object_index"], loaded["circuit_index"], loaded["measure_code"])
            groups.setdefault(key, []).append(loaded)
        for group in groups.values():
            if len(group) < 2:
                continue
            for loaded in group:
                finding = _rule(self.rules, "duplicate_measure")
                finding.spreadsheet_row = loaded["spreadsheet_row"]
                finding.label = loaded["label"]
                loaded["findings"].append(finding)
                loaded["row_severity"] = _worst(loaded["findings"])

    def _flag_incomplete_sets(self, built: list[dict], record: FileRecord) -> None:
        present: dict[str, set[str]] = {}
        for loaded in built:
            if loaded["case_id"] and loaded["measure_code"]:
                present.setdefault(loaded["case_id"], set()).add(loaded["measure_code"])
        required = set(self.rules["required_set"])
        for case_id, codes in present.items():
            missing = sorted(required - codes)
            if not missing:
                continue
            finding = _rule(self.rules, "measurement_set_incomplete")
            finding.label = case_id
            finding.detail = "Missing: " + ", ".join(missing) + "."
            # The incomplete set is recorded once on the file. Each row of the case only lists the missing codes.
            record.findings.append(finding)
            for loaded in built:
                if loaded["case_id"] == case_id:
                    loaded["set_missing"] = ",".join(missing)

    def _active_key(self, business_key: tuple) -> FileRecord | None:
        for record in self.registry:
            if record.status == "active" and (record.engineer, record.measurement_date, record.zone) == business_key:
                return record
        return None

    def _active_with_hash(self, business_key: tuple, digest: str) -> FileRecord | None:
        current = self._active_key(business_key)
        if current is not None and current.file_hash == digest:
            return current
        return None

    def loaded_rows(self) -> list[dict]:
        """Rows loaded from active and superseded files. Quarantined rows are not included."""
        rows = []
        status_by_id = {record.ingest_id: record.status for record in self.registry}
        for ingest_id, payload in self.payloads.items():
            status = status_by_id[ingest_id]
            if status not in ("active", "superseded"):
                continue
            for source in payload:
                if source["row_severity"] == "reject_row":
                    continue
                row = {key: value for key, value in source.items() if key != "findings"}
                row["finding_ids"] = ",".join(item.rule_id for item in source["findings"])
                row["file_is_active"] = status == "active"
                row["file_status"] = status
                rows.append(row)
        self._mark_current(rows)
        return rows

    def quarantine_rows(self) -> list[dict]:
        rows = []
        status_by_id = {record.ingest_id: record for record in self.registry}
        for ingest_id, payload in self.payloads.items():
            record = status_by_id[ingest_id]
            file_rejected = record.status in ("rejected", "rejected_attempt")
            for source in payload:
                if not file_rejected and source["row_severity"] != "reject_row":
                    continue
                reasons = list(source["findings"])
                if file_rejected:
                    reasons = list(record.findings) + reasons
                rows.append(
                    {
                        "ingest_id": ingest_id,
                        "file_name": record.file_name,
                        "file_status": record.status,
                        "spreadsheet_row": source["spreadsheet_row"],
                        "label": source["label"],
                        "rule_ids": ",".join(item.rule_id for item in reasons),
                        "expected": " | ".join(item.feedback_text() for item in reasons),
                    }
                )
        return rows

    def _mark_current(self, rows: list[dict]) -> None:
        for row in rows:
            row["is_current_for_case"] = False
            row["current_reason"] = ""
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            if not row["file_is_active"] or not row["case_id"] or not row["measure_code"]:
                if not row["file_is_active"]:
                    row["current_reason"] = "file_superseded"
                continue
            key = (row["case_id"], row["object_index"], row["circuit_index"], row["measure_code"])
            groups.setdefault(key, []).append(row)
        for group in groups.values():
            latest = max(row["measurement_date"] or "" for row in group)
            contenders = [row for row in group if (row["measurement_date"] or "") == latest]
            earlier = [row for row in group if row not in contenders]
            for row in earlier:
                row["current_reason"] = "later_measurement_exists"
            engineers = {row["engineer"] for row in contenders}
            if len(engineers) > 1:
                for row in contenders:
                    row["current_reason"] = "multiple_observers_unresolved"
                continue
            winner = sorted(contenders, key=_current_sort_key)[0]
            winner["is_current_for_case"] = True
            winner["current_reason"] = "selected"
            for row in contenders:
                if row is not winner:
                    row["current_reason"] = "duplicate_measure_not_selected"

    def feedback_by_engineer(self) -> dict[str, list[dict]]:
        """Feedback for each engineer. The shared summary carries counts and no measured values."""
        buckets: dict[str, list[dict]] = {}
        for record in self.registry:
            if record.status == "duplicate_noop":
                continue
            engineer = record.engineer or "UNATTRIBUTED"
            if record.status in ("rejected", "rejected_attempt") or any(
                item.severity in ("flag", "reject_file", "reject_row") for item in record.findings
            ):
                for finding in record.findings:
                    if finding.spreadsheet_row is not None:
                        continue  # row copies are emitted from the payload
                    if finding.severity == "accept":
                        continue
                    buckets.setdefault(engineer, []).append(_feedback_line(record, finding))
            payload = self.payloads.get(record.ingest_id, [])
            for source in payload:
                for finding in source["findings"]:
                    if finding.severity == "accept":
                        continue
                    buckets.setdefault(engineer, []).append(_feedback_line(record, finding))
            if record.status == "active" and not _engineer_has_issues(record, payload):
                buckets.setdefault(engineer, []).append(
                    {
                        "file_name": record.file_name,
                        "file_status": record.status,
                        "spreadsheet_row": "",
                        "label": "",
                        "rule_id": "accepted",
                        "severity": "accept",
                        "expected": "The file met the contract and is the active file for this engineer, date, and zone.",
                        "note": record.note,
                    }
                )
        return buckets

    def quality_summary(self) -> list[dict]:
        counts: dict[tuple, int] = {}
        for record in self.registry:
            for finding in record.findings:
                if finding.spreadsheet_row is not None:
                    continue
                key = (finding.rule_id, finding.severity)
                counts[key] = counts.get(key, 0) + 1
            for source in self.payloads.get(record.ingest_id, []):
                for finding in source["findings"]:
                    key = (finding.rule_id, finding.severity)
                    counts[key] = counts.get(key, 0) + 1
        return [
            {"rule_id": rule_id, "severity": severity, "count": count}
            for (rule_id, severity), count in sorted(counts.items())
        ]


def _current_sort_key(row: dict) -> tuple:
    # Current row: a valid label first, then fewer flags, then the earlier spreadsheet row.
    severity_rank = {"accept": 0, "flag": 1, "reject_row": 2}.get(row["row_severity"], 9)
    label_rank = 0 if row["label_status"] == "valid" else 1
    flag_count = 0 if not row["finding_ids"] else len(row["finding_ids"].split(","))
    return (label_rank, severity_rank, flag_count, row["spreadsheet_row"])


def _engineer_has_issues(record: FileRecord, payload: list[dict]) -> bool:
    if any(item.severity != "accept" for item in record.findings):
        return True
    return any(source["findings"] for source in payload)


def _feedback_line(record: FileRecord, finding: Finding) -> dict:
    return {
        "file_name": record.file_name,
        "file_status": record.status,
        "spreadsheet_row": finding.spreadsheet_row or "",
        "label": finding.label or "",
        "rule_id": finding.rule_id,
        "severity": finding.severity,
        "expected": finding.feedback_text(),
        "note": record.note,
    }


def write_outputs(session: FieldIngestSession, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_dicts(directory / "file_registry.csv", [_registry_row(item) for item in session.registry])
    _write_dicts(directory / "field_measurement.csv", session.loaded_rows())
    _write_dicts(directory / "quarantine.csv", session.quarantine_rows())
    _write_dicts(directory / "quality_summary.csv", session.quality_summary())
    feedback_dir = directory / "feedback"
    feedback_dir.mkdir(exist_ok=True)
    for engineer, lines in session.feedback_by_engineer().items():
        _write_dicts(feedback_dir / f"{engineer}.csv", lines)


def _registry_row(record: FileRecord) -> dict:
    return {
        "ingest_id": record.ingest_id,
        "file_name": record.file_name,
        "file_hash": record.file_hash,
        "arrived_at": record.arrived_at,
        "engineer": record.engineer or "",
        "measurement_date": record.measurement_date or "",
        "zone": record.zone or "",
        "status": record.status,
        "supersedes_ingest_id": record.supersedes_ingest_id or "",
        "file_rules": ",".join(item.rule_id for item in record.findings if item.spreadsheet_row is None),
        "note": record.note,
    }


def _write_dicts(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
