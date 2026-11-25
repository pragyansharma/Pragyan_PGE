# Enterprise risk data platform

Part A is the design. Part B is the code, and it runs on the sample.

Data science owns the method, the features, and the category cut points. This submission is the integration, validation, operation, and delivery.

## Assumptions

- The program is an overhead-line clearance and encroachment workflow. The sample is invented and small: about 1,500 cases, about 100,000 structures, a handful of engineers dropping a CSV a day, a weekly score.
- The team that has to live with this is small. One senior data engineer, one analytics engineer, a shared platform group, a GIS team that owns Esri, a data scientist who owns the method, and the system owners for case management and ERP.
- Freshness that matters: operational dashboard inputs before 07:00 local. Field-file feedback within minutes of the drop, because the engineer is still at the machine. Scores published Monday morning before the operations meeting.
- Coordinates and clearance distances are critical-infrastructure data. They are not personal data, and they are still restricted. Quality outputs that leave the platform do not carry them.
- The case system can give a daily snapshot and a soft-delete flag. A change feed is not available. The snapshot stays until one is.
- There is no single legal definition of "the corridor distance" in the material I was given. I put 200 US survey feet in configuration as a placeholder for a hit, and I still store the nearest structure when it is farther than that. The number is not mine to invent for real; the standards group owns it.
- Production target: Azure Databricks, Delta Lake, Unity Catalog, ArcGIS Enterprise, Databricks Workflows. DuckDB runs the SQL in this repo. Python runs the file, spatial, reconciliation, and model jobs. Same contracts on the cluster.

## Part A — how the platform is shaped

### Architecture and integration

Five layers.

| Layer | What it is | What it refuses to do |
| --- | --- | --- |
| Landing | The original file or extract, bytes and checksum, arrival time | No parsing that can fail and hide the file |
| Bronze | Append-only, source column names, values stored as text, plus the load id | No casts, no dedup, no business rules |
| Silver | Typed, renamed where a glossary name helps, quality flags attached | No row loss. A filter here is a defect |
| Gold | One grain per consumer: current case, current measurement, case-to-structure, current score, daily reconciliation | Filters are allowed, and the count of rows removed by each filter is published |
| Consumption | Semantic model, feature service, case workflow | No second copy of the population rule |

Sources and how they move:

- Case management: daily snapshot. Compared to the previous snapshot on `record_id`. Insert, update, soft-delete, and "missing from today's file" are four different outcomes. Missing is not a delete until the source owner says the extract is complete.
- ERP: nightly snapshot. Assets change slowly. The join to a structure is a curated crosswalk on circuit plus structure number, both as text, exact match only. Unmatched assets are a queue, not a fuzzy match.
- GIS: on publish from the versioned geodatabase, not row by row. Each publish is a snapshot id. Geometries stay in ArcGIS. The warehouse stores keys, measures, and lineage, and a link back to the feature.
- Field files: on arrival. The contract is in Part B, Exercise 1.
- Remote sensing: incremental on observation id and timestamp. A late observation inside a 48-hour window reprocesses the cases it touches.
- Model output: a partition per score date and model version, replaced on rerun, never appended.

Dependencies run in one direction. Gold cannot read landing. The score job reads gold and refuses to run if the case or spatial snapshot is older than the configured age. That check is in the runner.

Trusted access is a grant, not a view copied into every report. Analysts get gold. GIS engineers get the spatial result. A field engineer gets their own feedback file. Bronze is for the people who debug pipelines.

### Pipeline design and data quality

The reference flow is case + field file + structure proximity.

Keys:

- Case business key: `record_id`. It is not unique in the sample. The clean-layer grain is `source_row_id`. A current-case view is a separate object with an explicit survivor rule.
- Field file key: engineer initials, measurement date, zone. A case measured on two days is two observations. A second file with the same key is either a no-op (same hash) or a supersession (new hash, file accepted).
- Structure key: `structure_id`. `structure_number` is not unique across circuits and is not a number, even when it looks like one (`1018`, `010/072`, `A002/056`).

Dedup, history, late data, schema change, backfill:

- History is the bronze append and, for files, the superseded rows kept with `file_is_active = false`. I do not update a row in place to "fix" it.
- Late field data reprocesses that file's cases. It does not reload the case history.
- A new column arrives in bronze untouched and is parked on the silver row as `unmapped_source_columns` until it is named in the contract. A missing contracted column stops the build. A renamed source column that shows up empty has to fail here, or it lands as a null and nobody notices.
- Backfill is a parameter: a snapshot id, a date range, or a file hash. The job is the same job. Nobody updates the gold table with a notebook.

What I accept, flag, quarantine, or stop is the table on slide 4. The principle under it: a rule's message states what was expected, the severity lives in config, and a shared output does not contain coordinates, notes, or the rest of the case payload.

Visibility:

- The engineer who dropped the file gets a feedback file with the row number a spreadsheet uses, the label, the rule, and the expected sentence.
- The platform gets a count by rule id.
- The case build gets an assertion view. If it returns a row, the job fails. Dirty data does not fail that assertion. A lost row does.

### GIS and spatial data

Cases are WGS84. Structures are California State Plane, US survey feet. The program spans five State Plane zones. The sample only contains Zone 2 (EPSG:6418) and Zone 3 (EPSG:6420). The join is the same in each zone: transform the case point into that zone’s CRS and measure there. Zones are never mixed, so adding the other three zones is another EPSG on the structure row, not a new method.

A structure geometry that is empty, not a point, or outside its own zone is flagged and left out of the nearest-structure rank. The case still gets a row.

Zone membership is a county polygon the GIS team already maintains. The EPSG bounding boxes overlap, so a latitude test cannot assign a zone. Until that polygon is wired, a point in the overlap gets a nearest structure in each zone, both with `trusted_for_clearance = false`.

Records with no coordinates produce a row and a null distance. The next step is geocoding or a parcel-centroid join, accepted by a person, with its own lineage. This job does not call a geocoder.

The distance that matters for clearance is to the conductor. The sample has structure points only, so conductor distance is null and the reason is stored. Structure distance is still the right companion measure: the field file's S measurement is the horizontal distance to the nearest structure.

Scale: 1,500 by 100,000 distances in one zone is a direct calculation. A spatial index waits until conductor vertices arrive.

Every result stores the case snapshot, the structure snapshot, the EPSG, the PROJ pipeline and library version, the spatial rule version, whether the nearest structure is inside the configured corridor, and a model version column the score job fills later. PROJ's default NAD83(2011) to WGS84 step is a null shift, on the order of a meter. That is recorded, and it is why these distances are not clearance numbers.

Temporal alignment: when a case has a measurement date and the structure history exists, the join uses the structure snapshot current on that date. The sample has one snapshot, so the row says so.

### Productionization and reliability

- Testing: the lossless assertion, the field-file supersession cases, the reconciliation classes, the spatial "do not invent a point" cases, and exact score parity. They are in `tests/` and they run.
- Deployment: the scoring function is a module. The runner, the schedule, and the parameter path are outside it. A bad deploy is a job definition change, reviewed like any other change.
- Scheduling: field files on arrival; cases and ERP before the dashboard; GIS on publish; score Monday 05:00 America/Los_Angeles.
- Monitoring: row counts, assertion result, freshness, unexplained reconciliation residual, and score parity.
- Alerting: page the data-engineering on-call when a run fails, when recon cannot see the report's keys, or when report-only ids appear (those are rows we lost). Page the data scientist when their function throws. Email risk operations when the Monday SLA is missed or two runs fail in a row. A retry that succeeds does not email the business.
- Logging: load id, file hash, rule id, snapshot id. Not the note text and not the coordinate.
- Recovery: re-run the job for that snapshot or hash. Delta time travel if a gold publish has to be rolled back. No manual updates.
- Access: three grants, as above. Row-level security in the semantic model on circuit or region, once.
- Documentation and continuity: this repo is the runbook. The SQL file is the clean-layer contract. The YAML files are the severities. A second person can run `pytest`.

### Analytics and consumption

`fact_case_day` is one row per case per day, restricted to the published population flag, with status bucket, age in days, risk category, score, model version, distance, and the spatial rule version. Open issues, segmentation, aging, and remediation progress are filters and counts on that fact, plus a status-change fact when we need progress over time. Geographic distribution is county or zone from `dim_geography`, not a scatter of raw coordinates in the BI tool. The map is the feature service.

Calculated upstream, because two reports must not disagree: population membership, status bucket, age, distance, score, category. Left in the BI tool: counts, shares, sort, and the open/closed toggle. The toggle is a filter on a column, and the column's definition is upstream.

Refresh: case fact nightly, score weekly, both stamped with an as-of. The same page shows the reconciliation tile: synced count, report count, explained, unexplained, report-only.

At this size the daily fact is enough. The map stays a feature service, separate from the matrix visuals. Further aggregation waits until a report is actually slow.

## Part B — the five exercises

### Exercise 1 — files produced by people

Contract, in `config/field_measurement_rules.yml`. The code is `src/risk_platform/exercise1_field_ingest.py`.

File name must be `initials_yyyymmdd_zoneN_meas`, with `.csv` allowed as the extension and nothing else. The distance header must be `Distance (ftUS)`. Any other unit rejects the file. I do not convert. A file I cannot attribute cannot become current, and it cannot replace another file.

| Rule | Severity | What the engineer is told to expect |
| --- | --- | --- |
| File name | Reject the file | `initials_yyyymmdd_zoneN_meas` |
| Distance header | Reject the file | `Distance (ftUS)`. No conversion |
| Unknown engineer on an otherwise legal name | Flag, still load | Initials on the field roster |
| No case id in the label | Reject the row | `CASE-000000_H` and the other four codes |
| Hyphen instead of underscore, or `_01` after the code | Flag, still load | Underscore, and `_o1` / `_c1` before the measure code |
| Endpoint or distance not numeric | Reject the row | Both endpoints and a numeric distance |
| Distance does not recompute | Flag, still load | H and S horizontal, V and HT vertical, 3D straight-line, within 0.02 ft. Reported value kept |
| HT not vertical | Flag | Same easting and northing at both ends |
| S notes not `circuit \| structure \| kV \| line` | Flag | Vertical bars, as in the day-1 files |
| HT tag not one controlled type and `present` or `changed` | Flag | `Building; present` |
| Duplicate label or duplicate measure | Flag | Both rows kept. One is current, and the other says why it is not |
| Missing one of H, V, 3D, S, HT | Flag the case | The five codes |

On the sample, that is exactly what is wrong, and nothing else is "fixed":

- `AB_20260301` loads. The only flag is the HT tag on CASE-000102, which lists three object types.
- `AB_20260302` loads. CASE-000102 is a later observation, so it becomes current and the March 1 rows stay as history with `later_measurement_exists`. They are not superseded; supersession is for the same file key. CASE-000103 is missing 3D, uses a hyphen on one H, uses `_01` on V, puts commas in the S notes, and reports 105.518 ft where the horizontal distance is 104.139. 105.518 is the straight-line length. The reported value stays.
- `CD_zone2_export_final` is rejected for the name and for meters. The five rows are in quarantine. They are not current.

Feedback is per engineer (`outputs/exercise1/feedback`). It has the spreadsheet row, the label, the rule, and the expected sentence. It does not have coordinates, notes, or tags. The shared file is a count by rule.

Reruns: same hash for the same key is `duplicate_noop` and adds no rows. A new hash that passes validation supersedes the previous file; the old rows remain with `file_status = superseded` and are not current. A new hash that fails validation is `rejected_attempt`, and the previous active file stays active.

### Exercise 2 — two systems disagree on a count

1,373 in the report and 1,498 in the sync is a 125-row gap. Two of the five filters use fields the sync does not have. Those two stay unresolved.

What I ask for, and from whom:

- Report owner: the saved definition of all five filters, the as-of timestamp behind 1,373, an extract of the 1,373 record ids, whether the count is distinct ids, and whether the viewer's security role trimmed rows. A security trim is a likely explanation and it is still a hypothesis until they confirm it.
- Case-system owner: the two missing fields added to the sync, with definitions and a backfill, plus how soft-delete and merged cases behave.
- Until the id list arrives, the daily job publishes both counts with status `awaiting_report_keys` and pages. A gap nobody can list record by record is a blocked recon, not a green dashboard.

Once the ids exist, every id on either side gets one class:

| Class | Meaning |
| --- | --- |
| matched | In both, and the attributes we both have agree |
| attribute_conflict | In both, and an attribute we both have does not agree. Needs the report's attribute snapshot, not just its ids |
| sync_only_explained | In the sync only, and it fails a predicate we can actually run |
| sync_only_unexplained | In the sync only, and it passes every predicate we can run. This is the residual. The two missing filters do not get to claim it |
| report_only | In the report only. Treat as a sync miss until proven otherwise |

The illustrative fixture in `data/samples/reconciliation` is not the 1,498. It exists so the classes run. The production counts stay on the summary as claimed counts, separate from the classified counts, so a demo fixture cannot be mistaken for the business population.

Daily, the job writes `ops.recon_case_population` with the classification and the predicate id, and nothing else from the case. Page on report-only rows, on a blocked recon, and on a jump in the unexplained residual. Email the stable residual. The dashboard tile shows the breakdown every day, including the unexplained number.

### Exercise 3 — a clean layer that never drops a row

`src/risk_platform/sql/case_clean.sql`, executed by `exercise3_case_clean.py`.

The build reads the CSV as text. `1018`, `010/072`, `A002/056`, and `Z008/001` stay strings. A numeric cast drops the leading zero.

Typed columns sit beside the raw text. A value that parses is kept even when it is suspicious. CASE-100009 stays at latitude -122.135589 and longitude 39.462319, flagged `coordinates_appear_swapped`, with `coordinates_usable = false`. CASE-100016 keeps longitude 121.999336, flagged as a missing minus sign. Neither value is rewritten.

The population rule is `in_risk_population`: record type Land Rights Support, support type Encroachment Management, civil date on the source timestamp after 2019-12-31. The civil date is the date token in the text, not the date after a UTC conversion, so an offset cannot move a record across New Year's by itself. Soft-delete is not part of the rule. CASE-100002 is deleted and still in the population; consumers can filter `is_deleted`, and that filter is then their filter. Out of population, and still present: CASE-100003 (Project Support), CASE-100007 (Hydro Support), CASE-100019 (2019-11-03), CASE-100022 (blank support type). Pending is the specified bucket `other`, not a defect.

CASE-100005 occurs twice, Not Needed and Completed, same created timestamp. Both rows stay. `duplicate_resolution` is `unresolved_conflict`. Neither is the keeper.

Quality findings are a list of sentences. Blocking defects (unusable id, unparseable date, unparseable delete flag) keep the row out of `case_clean_trusted` and do not fail the build.

The assertion is the view `assertion_case_clean_lossless`. It returns a row if an input row is missing, if the clean table invented a row, or if a source row id was duplicated. The Python job raises `BuildFailed` when that view is not empty, and the process exits non-zero. Deleting a row after a successful build makes the assertion fire. Filtering to the population inside `case_clean` does the same.

On this sample the proof is 31 in, 31 out, 0 assertion failures, structure number typed as VARCHAR, 2 unresolved conflict rows.

### Exercise 4 — a spatial join at this scale

`src/risk_platform/exercise4_spatial.py` and `config/spatial.yml`.

For each structure zone, transform the case from EPSG:4326 into that zone and take the Euclidean distance in US survey feet. Rank within the zone. The corridor radius (200 ft, placeholder) sets `within_corridor` and a count of structures inside it. The nearest structure is stored even when it is outside the radius, so "nothing nearby" is different from "we dropped the case."

Six sample cases have coordinates. Each produces two candidates, zone 2 and zone 3, because every one of those points sits in both official bounding boxes. `trusted_for_clearance` is false on all of them, with the reason written on the row. The nearest distances are tens of miles because the sample coordinates are not colocated with the structures. The nearest structure is still returned, and `within_corridor` stays false. Four cases have no coordinates. They produce one row each, null distance, and a next step: geocode plus parcel confirmation where there is an address, parcel centroid where there is only a parcel. The address string is not copied onto the result.

Stored with each hit: structure id, structure number, circuit, kV, zone, EPSG, distance, corridor flag, zone confidence, both snapshot ids, spatial rule version, PROJ version and pipeline, a null model version, and a null conductor distance with reason `linework_not_in_sample`.

At the real 1,500 by 100,000 the same method holds, with a bounding-box prefilter in the structure CRS at the corridor radius, still per zone. A distributed spatial index comes in with the conductor vertices, not before.

### Exercise 5 — the notebook, in production, unchanged

`notebook_as_received.py` is the hand-run shape: hard-coded paths, no freshness check, no alert. `methodology.py` is the function. The real notebook was not in the packet, so the function body is a placeholder with the same interface. Dropping their function in does not change the runner. The arithmetic stays in pandas. A Spark port is a separate parity exercise once they ask for one.

What changes around it:

- Paths are arguments. The YAML stays the YAML.
- `model_version` is `ds-notebook-unversioned` plus a hash of the function and the parameter file. The prefix stays until the data scientist assigns a real version. An edit to the function or the YAML changes the hash.
- The output partition for that version is replaced, not appended.
- The job checks `feature_as_of` against a max age and fails before scoring if the extract is stale.
- Null features raise inside the function. The runner does not catch that and impute.
- Failure writes a manifest and an alert. The page goes to the data-engineering on-call. A scoring exception also pages the data scientist. Risk operations is emailed when the SLA is missed, not on the first exception. The alert payload has the error, not the feature rows.

Parity is exact: same entity ids, same categories, same scores as calling `score_frame` directly. Both calls are the same function on the same machine, which is why exact equality is the bar. A port to another engine needs a tolerance the data scientist signs.

What I would raise with the data scientist, in this order:

1. The hard-coded paths and the weekly hand run. I have already moved the paths. I need them to confirm the YAML is the only parameter source.
2. There is no version on the method. I am hashing bytes until they give me one.
3. What is the as-of of a feature? I will not score a case against a measurement that did not exist at that as-of, and I need them to say which timestamp they mean.
4. Nulls currently throw. I will keep that unless imputation is part of the method, in which case it belongs in their function, not in my runner.
5. The function stays in pandas. If a Spark port is requested later, the numeric difference is theirs to sign off.

## Open points

If the case system can emit a modification timestamp and a change feed, the daily snapshot diff gets replaced with that feed. Until then the snapshot stays.

If one of the two hidden report filters turns out to be row-level security, the published count needs the audience named on it. The sync keeps the rows.

When the GIS team supplies county polygons and the official NAD83(2011) transformation, `trusted_for_clearance` can be set for the in-zone nearest structure. Until then it stays false.
