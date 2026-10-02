# Study: an agent that builds the platform end to end and reports at every stage

**Status: proposal for review. Nothing in this document is built yet** except
where a row says "exists". It defines what the agent needs from us, what it
does at each stage, what each stage report contains, and who approves before
the next stage starts.

## 1. What we are adding

Today the orchestrator *operates* a platform that already exists:

- DE mode keeps the pipeline healthy.
- DA mode answers questions over Gold.

The new **builder mode** takes the platform from a raw source to a running
pipeline and model, one stage at a time:

```
0 Intake -> 1 Source profiling -> 2 Ingestion -> 3 Silver model -> 4 Gold model
  -> 5 Governance -> 6 ML model -> 7 Analyst readiness -> 8 Deploy -> 9 Operate
```

At every stage the agent does the work, produces a **stage report**, and stops
at a **gate** until the report is accepted. Stage 9 is the existing DE/DA
operation, with its healthcheck reports.

## 2. Design rules

These follow from how the rest of this repo works.

1. **Numbers in a report are computed by tools, never written by the model.**
   Row counts, null rates, pass/drop counts and metrics come from SQL or
   Python that a test can check. The model writes only the narrative around
   them, and must cite the computed figure it is talking about. This is the
   same rule as F2: the model never counts rows itself.
2. **The agent proposes; deploys go through review.**
   - Pipeline code, contracts, grants and bundle changes come out as a diff
     or pull request.
   - The agent may run its proposal on local or simulated data and on the
     **dev** target.
   - **Prod**, anything destructive (full refresh, VACUUM, dropping objects)
     and any grant or IAM change always need a named human approval. These
     are L1 in the problem catalog.
3. **Every gate is explicit.** A stage is `proposed` -> `report ready` ->
   `approved` or `changes requested`. The agent can't start stage N+1 until
   stage N is approved, and the approval and approver are recorded in the
   audit log.
4. **Same guardrails as today.** Masking before the model, the injection
   scan, size caps and the audit trail apply to every tool the builder uses.
   Data profiling reports never contain raw values from PHI columns, only
   counts and shapes.
5. **Each report has two forms.** A JSON file (machine-readable, diffable
   between runs, what the evals grade) and a rendered page for people. One
   template per stage, so reports are comparable run to run.

## 2a. How the stages connect

The stages are one chain, not ten separate jobs. Five mechanisms connect
them.

**1. One run, one ID.** A build run has a `run_id` that every stage, report,
artifact, audit-log line, MLflow run and deployed resource tag carries. Any
number in any report can be traced to the run and stage that produced it.

**2. Approved outputs are the next stage's only inputs.** Each stage publishes
**artifacts**, versioned files with a content hash. The next stage may only
read artifacts that are **approved**. It doesn't re-derive them or reach back
to raw inputs on its own.

```
I1-I13 inputs
   |
   v
[0 Intake] --inputs.validated--> [1 Profiling] --contract.v2, source_profile, phi_columns-->
[2 Ingestion] --bronze_tables, ingest_baseline--> [3 Silver] --silver_model, dq_thresholds,
silver_tables--> [4 Gold] --gold_model, metric_definitions--> [5 Governance] --governance_policy.applied-->
[6 ML] --model_version, feature_baseline--> [7 Readiness] --readiness_scorecard--> [8 Deploy]
--deployed_resources--> [9 Operate] --incidents, drift, SLA misses--> back into 1/3/4/6
```

**3. Each report links to what it was built on.** A stage report lists the
upstream artifacts (name, version, hash) and the upstream reports it relied
on. Example: the Gold report names the Silver model version and the metric
definitions it used. Together the reports form a chain you can walk from any
Gold number back to the source profile.

**4. A change upstream marks downstream stale.** If an approved artifact
changes - the contract gains a field, a Silver expectation threshold moves -
every downstream stage that consumed the old version is marked **stale** and
must be re-run and re-approved. The run manifest shows which.

**5. Operating feeds back.** Stage 9 doesn't just page people:
- a recurring incident (catalog ID), drift, or SLA miss opens a change
  request against the stage that owns it;
- schema drift goes to Stage 1, a data-quality spike to Stage 3, model drift
  to Stage 6;
- that change request re-enters the chain at that stage.

### The handoffs

| From -> to | Artifact handed over | What the receiving stage checks first |
|---|---|---|
| Inputs -> 0 | Inputs file (I1-I13) | Every required input present and consistent |
| 0 -> 1 | `inputs.validated` | Source locations reachable; sample available |
| 1 -> 2 | `contract.v<N>`, `source_profile`, `phi_columns` | Contract approved; PHI columns listed |
| 2 -> 3 | Bronze table names, `ingest_baseline` (rates, corrupt %) | Bronze row counts > 0; corrupt % under threshold |
| 3 -> 4 | `silver_model` (entities, keys, write patterns), `dq_thresholds`, Silver tables | Reconciliation Bronze = Silver + dropped + filtered |
| 4 -> 5 | `gold_model` (tables, grain, SCD), `metric_definitions` | Every Gold table has a declared grain and owner |
| 5 -> 6 | `governance_policy.applied` (coverage matrix) | Zero unmasked PHI columns |
| 4 + 5 -> 6 | Silver vitals + governed feature view | Features read through the governed path only |
| 6 -> 7 | `model_version` (MLflow), ONNX hash, `feature_baseline` | Registered version matches the ONNX file |
| 4 + 5 + 6 -> 7 | Gold tables, metric definitions, model | Golden questions map to defined metrics |
| 7 -> 8 | `readiness_scorecard` | Pass bar met (e.g. at least 95% accuracy, zero leaks) |
| 8 -> 9 | `deployed_resources` (pipeline/job IDs, target) | IDs resolve; first update succeeded |
| 9 -> 1/3/4/6 | Change request with catalog ID and evidence | Owning stage re-runs; downstream marked stale |

## 2b. What each stage connects to

Each stage works against the real system it builds. Where a local stand-in
exists, the same stage code runs on it first. Same code path, different
connection, the pattern `data_query.py` already uses (DuckDB locally, the
SQL warehouse when configured).

| Stage | Connects to (live) | Through | Local stand-in today |
|---|---|---|---|
| 1 Profiling | Kafka topic sample, GCS landing files | Kafka consumer (read-only), GCS read | Simulator output in DuckDB |
| 2 Ingestion | Kafka, GCS, DLT pipeline (Bronze) | Pipelines API, bundle deploy to dev | None (needs workspace) |
| 3 Silver | DLT pipeline (Silver), pipeline event log | Pipelines API, SQL warehouse | DuckDB over a simulator sample |
| 4 Gold | DLT (Gold), SQL warehouse | Statement Execution API | DuckDB Gold seed |
| 5 Governance | Unity Catalog (masks, filters, grants), system tables | SQL warehouse as each test group | Policy checks against `common/contracts.py` |
| 6 ML | MLflow registry, model artifact storage | MLflow client | Local file-based MLflow + ONNX |
| 7 Readiness | SQL warehouse (Gold), the agent itself | Statement Execution API, DA mode | DuckDB + eval harness |
| 8 Deploy | Workspace, Workflows, Asset Bundles | Databricks CLI / bundle, Jobs API | `bundle validate` only |
| 9 Operate | Pipelines/Jobs APIs, SQL warehouse, paging | Existing live tools | State file + DuckDB |

All connections use the no-op-unless-configured pattern and the credentials
in `agent/secrets.py`. Read-only connections (profiling, event logs, test
queries) are separate from write connections (deploy, grants), and write
connections only exist for the dev target unless a human approves prod.

## 3. What we have to give the agent

The agent can't invent any of these; a person has to supply them. Several
already exist in the repo for the synthetic source.

| # | Input | Example for this project | Used in stage | Have it today? |
|---|---|---|---|---|
| I1 | Source description: where the data comes from and how | Kafka topic `patient_events` (bootstrap servers), roster files on `gs://...` | 1, 2 | Yes (simulator, `databricks.yml` variables) |
| I2 | Sample data or schema docs per source | Simulator output; later MIMIC-IV extracts (stay local) | 1 | Yes (synthetic); MIMIC-IV pending PhysioNet |
| I3 | Event contract (fields, types, allowed values) | `common/contracts.py` | 1, 3 | Yes |
| I4 | Business questions the platform must answer | "Current ICU census", "avg HR by unit last hour", "census at 3am" | 4, 7 | Partly (in scenarios) |
| I5 | Metric definitions / business glossary | "Current census = encounters with status in-progress, by unit" | 4, 7 | **No** - catalog F1 |
| I6 | Data-quality rules and thresholds | Physical limits, hard-stop event types, acceptable drop rate (e.g. 1%) | 3 | Partly (expectations; no thresholds) |
| I7 | Governance policy | PHI columns, roles, unit-to-group mapping, minimum cell size 11 | 5 | Yes (`MASKED_COLUMNS`, UC SQL, `MIN_CELL_SIZE`) |
| I8 | Freshness and latency SLAs | Gold vitals within 2 minutes of the event; roster within 2 hours | 2, 3, 4, 9 | **No** |
| I9 | ML objective | Flag joint vital-sign anomalies; label source or "unsupervised"; acceptable alert rate | 6 | Partly (unsupervised IsolationForest; no target alert rate) |
| I10 | Golden questions with expected answers | ~100 Q -> expected value pairs on a fixed seed | 7 | **No** (14 eval scenarios only) |
| I11 | Workspace and environment config | Workspace URL, catalog, SQL warehouse ID, secret scope, dev vs prod | 2, 8 | Pending live workspace |
| I12 | Budget limits | Max DBU/day, cluster size cap, eval spend cap | 2, 6, 8, 9 | **No** |
| I13 | Approvers per gate | Who signs off data model, governance, prod deploy | All gates | **No** |

**The inputs to write first:** I5 metric definitions, I8 SLAs, I10 golden
questions, I12 budgets and I13 approvers. Without them several stage reports
have nothing to compare against: "is 2.3% dropped OK?" has no answer without
a threshold.

## 4. Stage by stage

Each stage gives: what the agent does, the tools it uses (exists / to
build), what its report contains, and its gate. "Local" means it can run
today on the simulator and DuckDB. "Workspace" means it needs the live
Databricks workspace.

### Stage 0 - Intake

- **Agent does:** checks inputs I1-I13 are present and consistent, and lists
  what's missing with the stages it blocks.
- **Tools:** a new input validator (schema-checks the inputs file).
- **Report:** an input checklist (present / missing / inconsistent), the
  open questions for people, and the stages that are blocked.
- **Gate:** a human confirms the inputs. **Runs:** local.

### Stage 1 - Source profiling and contract

- **Agent does:** profiles sample data per source (types, nulls,
  cardinality, value ranges, event-type mix, timestamp sanity) and compares
  it with the contract (I3). It proposes contract changes and flags likely
  PHI columns.
- **Tools:**
  - exists: `detect_schema_drift` (local), `plausible_vital_sql`;
  - to build: `profile_source` (DuckDB over a sample, PHI-safe output),
    a PHI column classifier (catalog C1/M9), timestamp checks (L1, L3, L5).
- **Report:**
  - per column: type, null %, distinct count, min/max (none for PHI
    columns);
  - event-type distribution; out-of-contract fields;
  - suspected PHI columns; timestamp anomalies (future, 1970, wrong offset);
  - sentinel values (0/-1/999) per vital;
  - proposed contract diff.
- **Gate:** a human approves the contract diff. **Runs:** local.

### Stage 2 - Ingestion (Kafka / Autoloader -> Bronze)

- **Agent does:** proposes ingest config (topic, starting offsets, schema
  location, trigger), runs it on dev, and verifies data lands.
- **Tools:**
  - exists: `restart_pipeline`, `check_job_status` (live, mock-tested);
  - to build: stream progress / lag (A1), corrupt-record rate (A3), offset
    gap check (A6).
- **Report:** rows landed, input vs processed rate, lag, corrupt-record %,
  first/last offsets, malformed samples (masked), and cost of the run.
- **Gate:** auto-pass on dev if corrupt % and lag are within I8/I6
  thresholds, otherwise a human. **Runs:** workspace (a local replay of
  simulator files into DuckDB could stand in).

### Stage 3 - Silver model

- **Agent does:**
  - proposes entities, keys and write patterns (CDC upsert vs append),
    watermarks and expectations from the profile and I6;
  - generates the DLT code as a diff;
  - runs it on sample data.
- **Tools:**
  - exists: the DLT dataset-graph test, the plausibility expectation test;
  - to build: a local Silver runner (the same SQL on DuckDB over a sample),
    Bronze -> Silver reconciliation (B8), dedup and late-drop counts (A4,
    A5), CDC sanity (B4, L7).
- **Report:**
  - model diagram (entities, keys, write pattern per table);
  - expectations with pass / drop / fail counts;
  - duplicates removed; late events dropped;
  - reconciliation (Bronze = Silver + dropped + filtered, per window);
  - CDC anomalies (ties, null keys, never-discharged encounters).
- **Gate:** a human approves the data model. **Runs:** local for logic,
  workspace for the real run.

### Stage 4 - Gold model

- **Agent does:** maps business questions (I4) and metric definitions (I5) to
  Gold tables, declares each table's grain, chooses SCD type 1 vs 2, and
  generates Gold code as a diff.
- **Tools:**
  - exists: `aggregate_gold_table`, `fct_encounter_history` / `as_of`;
  - to build: grain check (no duplicate keys at the declared grain),
    freshness check (B7), Silver -> Gold reconciliation, question coverage
    (each I4 question maps to a table and a metric).
- **Report:**
  - tables with grain, keys and SCD type;
  - metric definitions and which table serves each;
  - question coverage (answerable / not, and why);
  - row counts and reconciliation;
  - freshness against the SLA.
- **Gate:** a human approves the model and metric definitions.
  **Runs:** local for logic.

### Stage 5 - Governance

- **Agent does:** proposes column masks, row filters and grants for every
  Gold table from I7, then verifies them by querying as each group.
- **Tools:**
  - exists: `enforce_masking`, masked-filter refusal, small-cell
    suppression, UC SQL;
  - to build: governance coverage check (every Gold table has its masks,
    filters and grants - catalog N8), test-as-group queries (C7).
- **Report:**
  - per-table coverage matrix (masks / row filter / grants: expected vs
    actual);
  - test-as-group results (what each role can see);
  - PHI columns without masks (must be zero to pass).
- **Gate:** **always a human.** The agent never applies grants itself.
  **Runs:** checks are local against policy; verification needs the
  workspace.

### Stage 6 - ML model

- **Agent does:**
  - builds the feature set from Silver vitals and trains with a fixed seed;
  - evaluates against I9 and records a baseline for drift;
  - registers the model in MLflow, exports ONNX, and writes a model card.
- **Tools:**
  - exists: `ml/train_anomaly_model.py` (IsolationForest -> MLflow -> ONNX),
    `score_vitals_anomaly`;
  - to build: an evaluation report on held-out data with injected anomalies
    (precision/recall at the target alert rate), a feature baseline for
    drift (E1), and an ONNX-matches-registry check (H3).
- **Report:**
  - training data summary (rows, time range, feature distributions);
  - parameters; holdout metrics; alert rate vs target;
  - comparison to the previous version;
  - model card (intended use, limits, data source);
  - drift baseline.
- **Gate:** a human approves promotion. **Runs:** local.

### Stage 7 - Analyst readiness

- **Agent does:** runs the golden question set (I10) through DA mode and the
  scenario evals, and compares answers to expected values.
- **Tools:** exists: DA tools, eval harness (`evals/`); to build: the golden
  set and an accuracy grader.
- **Report:**
  - accuracy overall and per question type (counts, averages, as-of, trends);
  - suppressed-cell behaviour; PHI-leak checks (must be zero);
  - failed questions with the agent's tool calls;
  - cost per question.
- **Gate:** a human sets the pass bar, e.g. at least 95% accuracy and zero
  leaks. **Runs:** local.

### Stage 8 - Deploy and orchestrate

- **Agent does:** runs `databricks bundle validate` for each target, deploys
  to dev, runs the pipelines once, and runs the post-deploy governance check.
- **Tools:**
  - exists: bundle config, healthcheck job, `pipeline_health_live`;
  - to build: target diff (N1), post-deploy governance check (N8), a first
    run summary.
- **Report:** resources deployed (with IDs), config diff between targets,
  first update result per pipeline, governance check, and setup steps still
  missing (secret scope, volume - N6).
- **Gate:** dev is automatic after stages 3-7 are approved; **prod always
  needs a human.** **Runs:** workspace.

### Stage 9 - Operate

- **Agent does:** the existing DE healthcheck every 15 minutes and DA
  question answering, both using the problem catalog.
- **Tools:** exists: DE/DA tools, `lookup_problem`, healthcheck exit codes,
  audit log.
- **Report:**
  - a per-run healthcheck summary (exists as JSON on stdout);
  - a daily digest: incidents by catalog ID, remediations, escalations,
    freshness, cost.
- **Gate:** none; escalations page a human. **Runs:** workspace.

## 5. What exists vs. what to build

| Stage | Can run locally today | Main gaps |
|---|---|---|
| 0 Intake | - | Inputs file + validator; I5, I8, I10, I12, I13 not written yet |
| 1 Profiling | DuckDB, contract | `profile_source`, PHI classifier, timestamp checks |
| 2 Ingestion | - | Stream progress / lag, corrupt rate; needs workspace |
| 3 Silver | Graph test, plausibility SQL | Local Silver runner, reconciliation, dedup/late/CDC counts |
| 4 Gold | Aggregate tool, history table | Grain check, freshness, question coverage |
| 5 Governance | In-process masking | Coverage check, test-as-group; needs workspace to verify |
| 6 ML | Training + ONNX + MLflow | Evaluation with injected anomalies, drift baseline, model card |
| 7 Readiness | Eval harness, DA tools | Golden question set + accuracy grader |
| 8 Deploy | Bundle config | Target diff, post-deploy governance check; needs workspace |
| 9 Operate | Healthcheck, catalog lookup | Daily digest |
| All | Audit log | Stage state machine + gates, report templates (JSON + rendered) |

## 6. Suggested build order

1. **Stage framework:** the parts that connect the stages - each lives in
   the run manifest:
   - `run_id`;
   - the artifact store (versioned, hashed, approved/stale);
   - gate records in the audit log;
   - upstream links in every report;
   - stale propagation when an upstream artifact changes;
   - the report template (JSON + rendered page).

   Every stage plugs into it.
2. **Stage 0 + Stage 1:** an inputs file and profiling of simulator data.
   This exercises the framework end to end locally and produces the first
   real report.
3. **Stage 6:** the ML evaluation report. Mostly exists; it adds the model
   card and the comparison against the previous version.
4. **Stages 3 and 4 locally:** Silver/Gold reports on simulator data via
   DuckDB.
5. **Stage 7:** the golden set (needs I10 from us).
6. **Stages 2, 5, 8:** when the workspace is live.

## 7. Decisions needed before building

1. **Scope of "modeling":** both data modeling (Bronze/Silver/Gold design)
   and the ML model, as assumed here? Or one of them first?
2. **Agent authority:** may it deploy to **dev** on its own after a stage is
   approved, or should every change go through a pull request you merge?
   Prod and grants stay human-only either way.
3. **Report format and home:**
   - JSON plus markdown in the repo / UC volume;
   - JSON plus a PDF per stage;
   - or a dashboard page showing all stage reports and their gate status.
4. **Who supplies I5, I8, I10, I12, I13:** you write them, or the agent
   drafts them from the existing code and scenarios for you to correct.
