# Real-time stack: flow and where each piece lives in the repo

Repository: <https://github.com/sumanthreddy369/databricks-agentic-de>

Each box shows the technology and the repo path to pull it from. Line
styles:

- solid boxes run (locally or on the Databricks trial workspace);
- dashed boxes are written but have never run (Terraform, Cloud Run).

```mermaid
flowchart TD
    subgraph SRC["1. Sources"]
        SIM["Patient event simulator<br/>simulator/producer.py<br/>simulator/population.py<br/>simulator/domain.py"]
        ROSTER["Provider roster files<br/>simulator/autoloader_feed.py"]
        CHAOS["Failure injectors (tests)<br/>simulator/chaos.py"]
    end

    subgraph STREAM["2. Streaming transport"]
        KAFKA["Kafka / Redpanda<br/>docker-compose.yml (local)<br/>topic: patient_events"]
        GCS["GCS landing bucket<br/>infra/terraform/"]
    end

    CONTRACT["Event contract - single source of truth<br/>common/contracts.py<br/>pipeline/common/schemas.py"]

    subgraph DLT["3. Databricks Delta Live Tables - resources/dlt_pipeline.yml"]
        ING["Ingest<br/>pipeline/01_ingest/kafka_patient_events.py<br/>pipeline/01_ingest/autoloader_provider_roster.py"]
        BRONZE["Bronze - keeps every row<br/>pipeline/02_bronze/bronze_patient_events.py<br/>pipeline/02_bronze/bronze_provider_roster.py"]
        GATE["Silver contract gate - hard stop<br/>pipeline/03_silver/silver_patient_events.py"]
        SILVER["Silver - CDC upsert + SCD2 history + watermark dedup<br/>pipeline/03_silver/silver_encounters.py<br/>pipeline/03_silver/silver_vitals.py<br/>pipeline/03_silver/silver_providers.py"]
        GOLD["Gold - healthcare_agentic_de.gold<br/>pipeline/04_gold/gold_encounters.py<br/>pipeline/04_gold/gold_vitals.py<br/>pipeline/04_gold/gold_providers.py"]
    end

    UC["4. Unity Catalog - masks, row filters, grants<br/>governance/05_unity_catalog/catalog_and_grants.sql<br/>governance/05_unity_catalog/row_filters_and_masking.sql"]

    subgraph ML["5. ML lifecycle"]
        TRAIN["MLflow training - IsolationForest<br/>ml/train_anomaly_model.py"]
        ONNX["ONNX model<br/>ml/models/vitals_anomaly.onnx"]
    end

    subgraph SERVE["6. Serving / query"]
        WH["Databricks SQL warehouse + Pipelines API<br/>agent/databricks_client.py"]
        DUCK["Local Gold stand-in - DuckDB<br/>data/seed/gold_seed.sql"]
    end

    subgraph AGENT["7. Orchestrator agent - Claude"]
        ORCH["Mode routing + guardrails<br/>agent/orchestrator.py<br/>agent/llm.py, agent/prompts.py"]
        GUARD["Masking + injection scan<br/>agent/tools/governance_guard.py"]
        MCP["MCP tool server + client<br/>mcp_server/server.py<br/>agent/mcp_bridge.py"]
        DE["DE tools<br/>agent/tools/pipeline_health.py<br/>agent/tools/pipeline_health_live.py<br/>agent/tools/anomaly_score.py"]
        DA["DA tools - query + aggregate<br/>agent/tools/data_query.py"]
        KB["Problem catalog + lookup_problem<br/>agent/knowledge/problem_catalog.yaml<br/>agent/tools/problem_catalog.py"]
    end

    subgraph OPS["8. Orchestration + platform"]
        WF["Databricks Workflows + Asset Bundle<br/>databricks.yml, resources/workflows.yml"]
        HC["Scheduled healthcheck job<br/>agent/healthcheck.py"]
        GCP["GCP: Secret Manager, OpenTelemetry, Terraform, Cloud Run<br/>agent/secrets.py, agent/otel.py<br/>infra/terraform/, infra/cloudrun/"]
        CI["CI<br/>.github/workflows/ci.yml<br/>.github/workflows/bundle-validate.yml"]
        EVAL["Evals: golden question set + scenarios<br/>evals/golden/, evals/"]
    end

    subgraph OBSV["10. Observability"]
        TEL["Traces + metrics (OpenTelemetry)<br/>agent/otel.py, agent/telemetry.py"]
        COL["OTel Collector -> Prometheus + Jaeger<br/>observability/otel-collector.yaml"]
        GRAF["Grafana dashboard<br/>observability/grafana/"]
    end

    USERS["9. Users<br/>on-call data engineers - pages<br/>clinicians / analysts - questions"]

    SIM --> KAFKA
    ROSTER --> GCS
    KAFKA --> ING
    GCS --> ING
    CONTRACT -.-> ING
    CONTRACT -.-> GATE
    ING --> BRONZE --> GATE --> SILVER --> GOLD
    GOLD --> UC
    SILVER --> TRAIN --> ONNX
    UC --> WH
    DUCK -. "local stand-in for" .-> WH
    WH --> DA
    WH --> DE
    ONNX --> DE
    DA --> GUARD
    DE --> MCP
    KB --> MCP
    GUARD --> ORCH
    MCP --> ORCH
    ORCH --> USERS
    WF --> DLT
    WF --> HC --> ORCH
    GCP -.-> ORCH
    CI -.-> AGENT
    EVAL -.-> ORCH
    CHAOS -.-> EVAL
    ORCH --> TEL --> COL --> GRAF --> USERS

    classDef unrun stroke-dasharray: 6 4
    classDef localonly stroke-dasharray: 2 2
    class GCS unrun
```

## Component -> repo path

| Stage | Technology | Repo path | Status |
|---|---|---|---|
| Sources | Python event simulator (patient admit/transfer/discharge + vitals) | `simulator/` | Tested locally |
| Sources | Provider roster batch files | `simulator/autoloader_feed.py` | Tested locally |
| Transport | Kafka / Redpanda (local broker + console) | `docker-compose.yml` | Runs locally |
| Transport | GCS landing bucket | `infra/terraform/` | Never applied |
| Contract | Event schema, masked columns, physical vital limits, cell size | `common/contracts.py`, `pipeline/common/schemas.py` | Tested locally |
| Ingest | Autoloader (cloudFiles) on the trial; Kafka in prod | `pipeline/01_ingest/` | Runs on Databricks (Autoloader); Kafka path not run there |
| Bronze | Lakeflow streaming tables | `pipeline/02_bronze/` | Runs on Databricks |
| Silver | Lakeflow `apply_changes` (SCD1 + SCD2), expectations | `pipeline/03_silver/` | Runs on Databricks |
| Gold | Tables and materialized views in `healthcare_agentic_de.gold`, masks declared | `pipeline/04_gold/` | Runs on Databricks |
| Pipeline config | Asset Bundle pipelines | `resources/dlt_pipeline.yml` | Deployed (dev) |
| Governance | Unity Catalog masks, row filters, grants | `pipeline/04_gold/`, `governance/05_unity_catalog/` | Masks verified on the workspace; row filter and grants not tested |
| ML | IsolationForest -> MLflow -> ONNX | `ml/`, `agent/tools/anomaly_score.py` | Evaluated: advisory only; rules decide (`docs/eda/`) |
| Serving | Databricks SQL Statement Execution API + Pipelines API; Genie | `agent/databricks_client.py`, `agent/tools/genie.py` | SQL and Genie run live (golden set); Pipelines API mock-tested |
| Serving (local) | DuckDB Gold stand-in | `data/seed/gold_seed.sql` | Tested locally |
| Agent | Claude tool loop, mode routing, guardrails | `agent/orchestrator.py`, `agent/llm.py`, `agent/prompts.py` | Tested locally |
| Agent | PHI masking + prompt-injection scan | `agent/tools/governance_guard.py` | Tested locally |
| Agent | MCP server + client (DE tools) | `mcp_server/server.py`, `agent/mcp_bridge.py` | Tested locally |
| Agent | DE tools (local + live) | `agent/tools/pipeline_health.py`, `agent/tools/pipeline_health_live.py` | Local tested; live mock-tested |
| Agent | DA tools (query + aggregate, small-cell suppression) | `agent/tools/data_query.py` | Tested locally |
| Agent | Problem catalog + `lookup_problem` | `agent/knowledge/problem_catalog.yaml`, `agent/tools/problem_catalog.py` | Tested locally |
| Agent | Metrics layer (`compute_metric`, Genie instructions) | `agent/knowledge/metrics.yaml`, `agent/tools/metrics.py` | Runs (live Genie space synced) |
| Agent | Human approval + earned autonomy | `agent/approvals.py`, `agent/autonomy.py` | Tested locally |
| Agent | Vector Search knowledge tool | `agent/tools/knowledge_search.py` | Stubbed |
| Orchestration | Workflows: hourly roster job, 15-min agent healthcheck | `resources/workflows.yml`, `agent/healthcheck.py` | CLI tested; job never run |
| Platform | GCP Secret Manager | `agent/secrets.py` | No-op unless configured |
| Observability | OpenTelemetry Collector, Prometheus, Jaeger, Grafana | `agent/otel.py`, `agent/telemetry.py`, `observability/` | Runs locally in Docker (demo traffic) |
| Platform | Terraform, Cloud Run | `infra/` | Never applied |
| Quality | CI (lint + tests), bundle validate | `.github/workflows/` | CI passing on GitHub |
| Quality | Golden question set (live Genie) + scenario evals | `evals/golden/`, `evals/` | Golden set measured (2 live runs); scenarios not run on a real model |
| Docs | Problem catalog PDF + generator | `docs/agent_problem_catalog.pdf`, `scripts/build_problem_catalog.py` | Pushed |

## Pulling it

Clone the whole repo:

```bash
gh repo clone sumanthreddy369/databricks-agentic-de
```

Install everything and run the tests:

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

Start the local real-time stream (Redpanda, then the simulator):

```bash
docker compose up -d
```

```bash
uv run python -m simulator.producer --patients 1000 --duration 60
```

To pull only one part, a sparse checkout works. For example, just the
pipeline and its contract:

```bash
git clone --filter=blob:none --sparse https://github.com/sumanthreddy369/databricks-agentic-de.git
```

```bash
git -C databricks-agentic-de sparse-checkout set pipeline common resources
```
