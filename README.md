<<<<<<< HEAD
# Enterprise risk data platform

Part A is the slide deck. Part B is this write-up and the code for the five exercises. The risk method stays with data science.

## What to open

| Asked for | Where it is |
| --- | --- |
| Part A, 5–8 slides, all five design areas | `slides/Enterprise_Risk_Data_Platform.pptx` |
| Assumptions | `docs/solution_design.md` |
| Exercise 1. Field-file rules, severities, feedback, supersession | `src/risk_platform/exercise1_field_ingest.py`, `config/field_measurement_rules.yml` |
| Exercise 2. Reconciliation and daily monitoring | `src/risk_platform/exercise2_reconciliation.py`, `config/reconciliation.yml` |
| Exercise 3. SQL clean layer and the assertion that fails the build | `src/risk_platform/sql/case_clean.sql`, `src/risk_platform/exercise3_case_clean.py` |
| Exercise 4. Spatial join, coordinates, and lineage | `src/risk_platform/exercise4_spatial.py`, `config/spatial.yml` |
| Exercise 5. Production run, parity, and who is notified | `src/risk_platform/exercise5_model/` |

Sample output from the last run is in `outputs/`. The sample inputs are in `data/samples/`.

```powershell
python -m pip install -r requirements.txt
python -m pytest
python run_assessment.py
```

Production target is Databricks (Delta, Unity Catalog, Workflows) and ArcGIS. The SQL runs here on DuckDB. The file, reconciliation, spatial, and model jobs run in Python. Same rules either place.

Exercise 3 fails the build when `assertion_case_clean_lossless` returns a row. A quality flag does not fail it. A missing row does. Structure numbers stay text, including `1018` and `010/072`.

Exercise 5 leaves the scoring function alone. The runner supplies the paths, checks feature freshness, stamps a version, replaces the output file on rerun, and sends an alert that contains the error only. The notebook was not in the packet, so `methodology.py` is the stand-in with that interface.

## Assumptions that change the design

- Case management is a daily snapshot until it can emit changes. Soft-delete is an attribute, not a population rule.
- A corrected field file replaces the earlier file for the same engineer, date, and zone only when the new file is accepted. A rejected re-export leaves the previous file in place. The same bytes are a no-op.
- The program spans five State Plane zones. The sample has Zone 2 and Zone 3. Distance is US survey feet in the structure's own CRS, and zones are never mixed. Cases without coordinates do not get an invented point.
- 1,373 versus 1,498 is not reconciled until the report's record ids and all five filters are in hand. The two filters whose fields are missing do not get to explain the gap.
- Clearance distances in this sample are provisional. County polygons and the GIS team's official datum transformation are still required.
=======
# Pragyan_PGE
Production-ready enterprise risk data platform integrating case, GIS, field, and model data with validation, reconciliation, spatial analysis, and scheduled risk scoring.
>>>>>>> 1f311c1d39efd4f4545fa7d8bde132170850368d
