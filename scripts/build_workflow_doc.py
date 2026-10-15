"""Builds docs/workflow_and_tech_stack.pdf and .docx from one story.

    uv run --no-project --with reportlab --with python-docx --with pypdfium2 \\
        python scripts/build_workflow_doc.py

The content (workflows, tech stack, guardrails, status) is written once in
story(); the PDF and the Word file are two renderers of it, so they can't
drift apart. Update the content and SNAPSHOT here when the project changes.
"""

import html
import re
import sys
import tempfile
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors

REPO = Path(__file__).resolve().parent.parent
PDF_OUT = REPO / "docs" / "workflow_and_tech_stack.pdf"
DOCX_OUT = REPO / "docs" / "workflow_and_tech_stack.docx"
SNAPSHOT = "Status as of 15 Oct 2026"

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#52606d")
ACCENT = colors.HexColor("#1d4e89")
RULE = colors.HexColor("#cbd2d9")
HEAD_BG = colors.HexColor("#e4ecf5")
OURS = colors.HexColor("#eefaf1")
PLAT = colors.HexColor("#eef4fb")
GOV = colors.HexColor("#f3eefb")
HUMAN = colors.HexColor("#fff6e0")
OBS = colors.HexColor("#fdf3ea")
GREEN, YELLOW, RED, GREY = (colors.HexColor(c) for c in ("#d5f0dc", "#fdf1c7", "#fbe1e1", "#eceff1"))
# Status cell colours, matched by prefix.
STATUS = {
    "Runs": GREEN,
    "Measured": GREEN,
    "Tested locally": GREEN,
    "Mock-tested": YELLOW,
    "Advisory": YELLOW,
    "Partial": YELLOW,
    "Validated": YELLOW,
    "Not run": RED,
    "Never run": RED,
    "Planned": GREY,
}

# --- diagrams (reportlab drawings, used by both renderers) ----------------------


def _box(d, x, y, w, h, lines, fill):
    d.add(Rect(x, y, w, h, fillColor=fill, strokeColor=ACCENT, strokeWidth=0.8, rx=4, ry=4))
    top = y + h / 2 + (len(lines) - 1) * 5
    for i, line in enumerate(lines):
        bold = i == 0
        d.add(
            String(
                x + w / 2,
                top - i * 10,
                line,
                fontName="Helvetica-Bold" if bold else "Helvetica",
                fontSize=8 if bold else 7,
                fillColor=INK if bold else MUTED,
                textAnchor="middle",
            )
        )


def _arrow(d, x1, y1, x2, y2):
    d.add(Line(x1, y1, x2, y2, strokeColor=ACCENT, strokeWidth=1))
    if x2 > x1:
        pts = [x2, y2, x2 - 5, y2 + 3, x2 - 5, y2 - 3]
    elif x2 < x1:
        pts = [x2, y2, x2 + 5, y2 + 3, x2 + 5, y2 - 3]
    elif y2 < y1:
        pts = [x2, y2, x2 - 3, y2 + 5, x2 + 3, y2 + 5]
    else:
        pts = [x2, y2, x2 - 3, y2 - 5, x2 + 3, y2 - 5]
    d.add(Polygon(pts, fillColor=ACCENT, strokeColor=ACCENT))


def strip(steps, fills, width=760, box_h=46):
    """One row of boxes joined by arrows."""
    n = len(steps)
    gap = 16
    w = (width - 16 - gap * (n - 1)) / n
    d = Drawing(width, box_h + 8)
    for i, lines in enumerate(steps):
        x = 8 + i * (w + gap)
        _box(d, x, 4, w, box_h, lines, fills[i])
        if i:
            _arrow(d, x - gap, 4 + box_h / 2, x, 4 + box_h / 2)
    return d


def architecture():
    W, H = 760, 330
    d = Drawing(W, H)
    _box(d, 250, 285, 260, 36, ["People", "on-call data engineers, clinicians, analysts"], HUMAN)
    d.add(Rect(8, 120, W - 16, 150, fillColor=colors.HexColor("#f6fbf7"), strokeColor=RULE, rx=6, ry=6))
    d.add(
        String(
            18,
            256,
            "This project: the glue-and-guardrail layer (agent/)",
            fontName="Helvetica-Bold",
            fontSize=9,
            fillColor=INK,
        )
    )
    _box(d, 20, 200, 160, 44, ["Input guardrails", "injection scan", "refuse PHI requests"], OURS)
    _box(d, 200, 200, 170, 44, ["Router", "orchestrator + metrics layer", "+ problem catalog (104)"], OURS)
    _box(d, 390, 200, 175, 44, ["Output guardrails", "masking, small-count suppression,", "SQL scope + consistency"], OURS)
    _box(d, 585, 200, 160, 44, ["Audit log", "every question, SQL,", "action, approver"], OURS)
    _box(d, 20, 132, 160, 50, ["Metric path", "compute_metric (no LLM),", "query / aggregate Gold"], OURS)
    _box(d, 200, 132, 170, 50, ["Human approval gate", "agent/approvals.py", "+ earned autonomy"], HUMAN)
    _box(d, 390, 132, 175, 50, ["Evaluation", "golden set, rescore, EDA,", "human-work report"], OURS)
    _box(d, 585, 132, 160, 50, ["Observability", "OpenTelemetry -> Prometheus,", "Jaeger, Grafana"], OBS)
    _arrow(d, 180, 222, 200, 222)
    _arrow(d, 370, 222, 390, 222)
    _arrow(d, 565, 222, 585, 222)
    _arrow(d, 285, 200, 285, 182)
    _arrow(d, 665, 200, 665, 182)
    _arrow(d, 380, 285, 380, 244)
    _box(d, 20, 62, 220, 44, ["Genie space", "Conversation API", "answers questions"], PLAT)
    _box(d, 270, 62, 220, 44, ["Lakeflow pipelines", "event log, Pipelines / Jobs APIs", "approved actions only"], PLAT)
    _box(d, 520, 62, 225, 44, ["Agent Bricks (planned)", "runbook / how-to agent", ""], colors.HexColor("#f4f5f7"))
    # Router -> Genie, down the gap between the metric-path and approval boxes.
    d.add(Line(200, 206, 190, 206, strokeColor=ACCENT, strokeWidth=1))
    _arrow(d, 190, 206, 190, 106)
    _arrow(d, 285, 132, 380, 106)
    _box(
        d,
        20,
        6,
        725,
        44,
        [
            "Databricks on GCP (free trial, serverless)",
            "Autoloader files (prod: Kafka) -> Bronze -> Silver -> Gold  |  Unity Catalog masks, row filters  |  MLflow + ONNX",
            "",
        ],
        GOV,
    )
    _arrow(d, 130, 62, 130, 50)
    _arrow(d, 380, 62, 380, 50)
    return d


# --- content ----------------------------------------------------------------------

W5 = [26, 62, 70, 75, 34]

STACK = [
    ["Layer", "Technology", "Version", "What it does here", "Repo path", "Status"],
    ["Language / tooling", "Python, uv, ruff, pytest", "Python >=3.11; ruff 0.16.8; pytest 9.1.1", "uv-only package management, lint, 453 tests with zero network calls", "pyproject.toml, uv.lock, tests/", "Tested locally - passing in CI"],
    ["Data source", "Patient-event simulator (Synthea / MIMIC-IV-shaped, synthetic)", "-", "Admit / transfer / discharge + 6 vitals; Kafka publisher or JSON-lines files", "simulator/", "Runs - 103,301 events generated and ingested"],
    ["Transport (free trial)", "Autoloader over a Unity Catalog volume", "-", "Reads JSON-lines files; no extra service", "simulator/file_feed.py, pipeline/01_ingest/", "Runs on Databricks"],
    ["Transport (prod)", "Kafka / Redpanda", "confluent-kafka 2.15.1", "Production event stream; local broker via Docker", "docker-compose.yml, simulator/producer.py", "Tested locally - not run on Databricks"],
    ["Pipeline", "Lakeflow Declarative Pipelines (formerly DLT), serverless", "-", "Bronze -> contract gate -> Silver (CDC SCD1 + SCD2) -> Gold", "pipeline/, resources/dlt_pipeline.yml", "Runs on Databricks - layer counts reconcile"],
    ["Contract", "Pydantic + shared constants", "pydantic 2.13.5", "Schema, masked columns, vital ranges and physical limits, min cell size", "common/contracts.py", "Tested locally"],
    ["Governance", "Unity Catalog", "-", "Column masks and row filters declared in the pipeline; grants", "pipeline/04_gold/, governance/05_unity_catalog/", "Partial - masks verified (1,388 of 1,388); row filter and grants not tested"],
    ["Storage / query (live)", "Delta tables, serverless SQL warehouse", "-", "Gold tables; Statement Execution API", "agent/databricks_client.py", "Runs - golden set queried it live"],
    ["Storage / query (local)", "DuckDB", "duckdb 1.5.5", "Local Gold stand-in; EDA over the raw JSON lines", "data/seed/, scripts/eda_landing.py", "Tested locally"],
    ["Metrics layer", "One YAML of metric definitions", "pyyaml 6.0.3", "Feeds both Genie's instructions and compute_metric", "agent/knowledge/metrics.yaml, agent/tools/metrics.py", "Runs - live Genie space synced"],
    ["Platform agent (questions)", "Databricks Genie", "Conversation API", "Writes and runs SQL for analyst questions", "agent/tools/genie.py", "Measured - golden set, 2 live runs"],
    ["Platform agent (runbooks)", "Databricks Agent Bricks", "-", "Knowledge agent over the problem catalog", "-", "Planned"],
    ["Router / LLM", "Anthropic Claude via the Anthropic SDK", "anthropic 1.7.0", "Mode routing and tool loop; no agent framework", "agent/llm.py, agent/orchestrator.py", "Tested locally (scripted model) - real model needs a paid key"],
    ["Tool protocol", "Model Context Protocol (MCP)", "mcp 2.2.0", "DE tools served over stdio", "mcp_server/, agent/mcp_bridge.py", "Tested locally"],
    ["Knowledge", "Problem catalog + lookup_problem", "pyyaml 6.0.3", "104 real-time problems with expected response and autonomy level", "agent/knowledge/problem_catalog.yaml", "Tested locally"],
    ["ML", "IsolationForest -> MLflow -> ONNX; NEWS2", "scikit-learn 1.9.1, mlflow 3.16.1, onnxruntime 1.30.0", "Deterioration: rules, NEWS2, then model v2 at a calibrated threshold", "ml/, agent/tools/anomaly_score.py, early_warning.py", "Measured - v2 catches 61% vs NEWS2 47% at the same false alarms (simulated)"],
    ["Simulator physiology", "Per-patient baselines, unit acuity, deterioration episodes", "-", "Realistic, labelled vitals for training and evaluation", "simulator/physiology.py", "Tested locally - Databricks data predates it"],
    ["Human approval", "Approvals CLI + earned autonomy", "-", "Named approver, 4h expiry; promotion after 10 clean runs, auto-demotion", "agent/approvals.py, agent/autonomy.py", "Tested locally"],
    ["Observability", "OpenTelemetry SDK + Collector, Prometheus, Jaeger, Grafana", "otel-sdk 1.45.0; collector 0.111.0; Prometheus 2.54.1; Jaeger 1.62.0; Grafana 11.2.2", "Request and tool traces; human-work, guardrail and quality metrics; one dashboard", "agent/otel.py, agent/telemetry.py, observability/", "Runs locally (Docker) - on demo traffic"],
    ["LLM tracing / logs", "Langfuse, structlog", "langfuse 4.15.6, structlog 26.1.0", "LLM-call traces; structured logs", "agent/llm.py", "Tested locally (no-op path)"],
    ["Orchestration / deploy", "Databricks Asset Bundles + Workflows (serverless jobs)", "Databricks CLI v1.19.0", "Pipelines, roster job, health-check job", "databricks.yml, resources/", "Runs (free target) - health-check job runs serverless"],
    ["Evaluation", "Golden question set; scenario evals", "-", "27 live questions, 3 configurations; 14 scripted scenarios", "evals/golden/, evals/", "Measured (golden); scenarios not run on a real model"],
    ["Secrets", "GCP Secret Manager", "google-cloud-secret-manager 2.30.0", "No tokens in files; no-op unless configured", "agent/secrets.py", "Tested locally (no-op path)"],
    ["Infrastructure", "Terraform, Cloud Run (GCP)", "Terraform >=1.5", "GCS bucket, service account, Pub/Sub, Secret Manager", "infra/", "Never run"],
    ["CI", "GitHub Actions", "-", "Lint, guardrail tests, tests, pipeline integration, docs in sync; bundle validate/deploy", ".github/workflows/", "Runs - passing on GitHub"],
    ["Cost control", "stop_everything", "-", "Cancel runs, pause schedules, stop pipelines and warehouses", "scripts/stop_everything.py", "Validated - dry run on the workspace"],
]

GUARDRAILS = [
    ["Guardrail", "What it stops", "Where", "Proof"],
    ["PHI masking before any model", "Names / MRNs reaching the LLM, transcript, or audit log", "governance_guard, orchestrator _dispatch", "tests/test_masking_guard.py"],
    ["PHI-request refusal", "Asking Genie or the fallback for identifiers", "agent/tools/genie.py, data_query", "tests/test_genie_gateway.py; golden set 3/3 probes"],
    ["Masking backstop on Genie output", "A misconfigured UC mask leaking values, incl. into Genie's text", "agent/tools/genie.py", "tests/test_genie_gateway.py"],
    ["SQL scope check", "Genie results from tables outside Gold", "agent/tools/genie.py", "tests/test_genie_gateway.py"],
    ["Question/SQL consistency", "Genie answering a different question (PICU asked, ICU answered)", "agent/tools/genie.py", "Found by golden run 1; regression test"],
    ["Small-cell suppression", "Counts over fewer than 11 patients, incl. Genie's own text", "aggregate_gold_table (exact), ask_genie (COUNT columns)", "Found by golden run 2; tests/test_genie_gateway.py"],
    ["Prompt-injection scan", "Instructions hidden in free text", "governance_guard.scan_for_injection", "tests/test_prompt_injection_guard.py"],
    ["Rules, then NEWS2, then a calibrated model", "A model's miss hiding a dangerous reading (v2 scores SpO2 80 as normal)", "agent/tools/anomaly_score.py", "ml/evaluate_anomaly_model.py; tests/test_anomaly_score_tool.py"],
    ["Human approval + earned autonomy", "The agent changing pipelines on its own before it has earned it", "agent/approvals.py, agent/autonomy.py", "tests/test_human_approval.py, test_earned_autonomy.py"],
    ["Kill switch + escalation ceiling", "Remediation loops; fixing past a hard stop", "orchestrator", "tests/test_kill_switch.py, test_escalation_ceiling.py"],
    ["Audit log + traces", "Untraceable actions; spans never carry request text", "orchestrator, agent/otel.py", "tests/test_audit_trail.py, test_telemetry.py"],
    ["Graceful degradation", "LLM outage crashing callers or looking green", "orchestrator.handle, healthcheck exit 1", "tests/test_healthcheck.py"],
]

REPO_MAP = [
    ["Folder / file", "Contents"],
    ["agent/", "The glue-and-guardrail layer: orchestrator, LLM wrapper, approvals, autonomy, human-work report, telemetry, healthcheck, Databricks client, MCP bridge"],
    ["agent/tools/", "genie (Genie gateway), metrics, data_query, pipeline_health (+ _live), problem_catalog, governance_guard, anomaly_score, knowledge_search"],
    ["agent/knowledge/", "metrics.yaml (metrics layer), problem_catalog.yaml (104 problems)"],
    ["pipeline/, resources/, databricks.yml", "Lakeflow pipeline code and the Asset Bundle (dev target on the trial workspace)"],
    ["governance/", "Unity Catalog SQL; genie/space.json (generated Genie space)"],
    ["evals/", "golden/ (live question set, run, rescore, scoring) and scripted scenarios"],
    ["ml/", "Anomaly model training, evaluation, committed ONNX file"],
    ["observability/", "OTel Collector, Prometheus, Grafana provisioning and dashboard"],
    ["scripts/", "eda_landing, demo_traffic, build_* (Genie space, dashboard, images, docs), stop_everything"],
    ["docs/", "plan, architecture, eda/, comparisons/ (Genie results), images/, this document"],
    ["tests/", "453 tests, one file per concern, zero network calls"],
]

NEXT = [
    ["#", "Step", "Needs", "Outcome"],
    ["1", "Connect the alert webhook (Slack / Teams) as a Databricks secret; unpause the 15-minute schedule", "You: create the webhook and the secret", "Alerts in chat, once per problem"],
    ["2", "Unit row filter tested with real workspace groups (a nurse sees only their unit)", "Groups in the workspace", "Row-level access shown, not just column masks"],
    ["3", "Split shared patient_ids at source (re-assign the second person's admissions) after a steward confirms", "Source-system access", "Shared ids resolved, not just flagged"],
    ["4", "Use runbook search (14/15 held-out) inside the agent's lookup_problem when the workspace is reachable", "-", "Better problem matching in health checks"],
    ["5", "Make the best-scoring hosted model the default planner; more turns or examples where models fall short", "-", "Free-form requests handled without a paid key"],
    ["6", "Turn on the GitHub -> Databricks stages (validate, deploy on merge, integration run)", "You: DATABRICKS_HOST / DATABRICKS_TOKEN repo secrets", "Every merge checked against the real workspace"],
    ["7", "Cancel the old GCP trial subscription (that workspace is locked)", "You, in GCP Marketplace", "No charges"],
]


def story(fx):
    """The whole document, written once. fx supplies the builders."""
    p, bullets, grid, img, space, br = fx.p, fx.bullets, fx.grid, fx.img, fx.space, fx.pagebreak
    s = [
        p("Workflow and Tech Stack", "TITLE"),
        p(
            "databricks-agentic-de - a real-time healthcare data platform on Databricks where the platform's agents do "
            "the work and this project builds the glue and guardrails around them",
            "SUB",
        ),
        space(),
        p(f"{SNAPSHOT}. Repository: github.com/sumanthreddy369/databricks-agentic-de", "SMALL"),
        space(),
        p("In one paragraph", "H2"),
        p(
            "Patient events flow from a simulator into Databricks, through Bronze, Silver and Gold tables governed by "
            "Unity Catalog. Databricks' own agents do the data work: Genie answers analysts' questions and the pipeline "
            "APIs act on pipelines. This project is the layer around them that companies build themselves: it routes "
            "requests, checks everything going in and coming out, holds every change for a named human until that kind "
            "of change has earned autonomy, records everything, and measures it - answer quality, safety, and how much "
            "work still needed a person. The goal is to cut routine data-engineering and analyst work as fast as is safe."
        ),
        space(),
        p("Status at a glance", "H2"),
        grid(
            [
                ["Area", "State"],
                ["Pipelines on Databricks (Free Edition; earlier GCP trial)", "Runs - 103,359 simulated events (v5): 306 impossible vitals dropped; four identity checks catch 78/78 planted cases; a steward review queue decides"],
                ["Unity Catalog masks", "Runs - 1,388 of 1,388 names and MRNs masked in Gold; row filter and grants not tested"],
                ["Genie behind the guardrails", "Measured - 27-question golden set, 3 live runs; run 3 (Free Edition): 21/21 answers, 6/6 safety probes"],
                ["Metrics layer", "Runs - one definition per metric for Genie and compute_metric"],
                ["Human approval, earned autonomy, human-work report", "Tested locally - no real week of operation yet"],
                ["Observability (OTel, Prometheus, Jaeger, Grafana)", "Runs locally on demo traffic through the real orchestrator"],
                ["Data and deterioration detection", "Measured - EDA done; with realistic physiology, model v2 + NEWS2 + rules catch 66% at 3.8% false alarms (simulated)"],
                ["Not yet", "Claude as router (paid key), restart through the live API, Agent Bricks, Terraform, unit row filter with real groups"],
            ],
            [120, 147],
            status_col=1,
        ),
        br(),
        p("1. Architecture", "H1"),
        p(
            "Three tiers: people at the top, this project's layer in the middle, Databricks' agents and platform "
            "underneath. Every request passes the input guardrails, every result the output guardrails, every action the "
            "approval gate; everything is audited, traced and measured."
        ),
        space(),
        img("architecture", architecture()),
        space(),
        bullets(
            [
                "<b>Platform does the work:</b> Genie (questions), Lakeflow pipelines and Jobs APIs (actions), Agent Bricks planned (runbooks).",
                "<b>We build glue + guardrails:</b> routing, the Genie gateway, the metrics layer, guardrails, approvals and earned autonomy, audit, evaluation, observability.",
                "<b>Metric path = baseline:</b> compute_metric answers routine metrics with no LLM in about 1.3 s; Genie (about 13 s) is measured against it.",
            ]
        ),
        br(),
        p("2. Workflows", "H1"),
    ]

    def workflow(title, intro, drawing_name, steps, fills, rows, notes=None):
        parts = [p(title, "H2"), p(intro), space(), img(drawing_name, strip(steps, fills)), space(), grid(rows, W5, 4)]
        if notes:
            parts += [space(), bullets(notes)]
        return parts + [br()]

    s += workflow(
        "W1. Data flow: source to Gold",
        "How a vital-sign reading becomes a governed Gold row. The free trial uses files in a Unity Catalog volume "
        "instead of Kafka; both feed identical columns to Bronze.",
        "w1",
        [
            ["Simulator", "simulator/"],
            ["Files in UC volume", "(prod: Kafka topic)"],
            ["Ingest", "Autoloader", "01_ingest/"],
            ["Bronze", "keeps every row", "02_bronze/"],
            ["Contract gate", "hard stop on", "unknown event types"],
            ["Silver", "CDC SCD1 + SCD2,", "impossible values dropped"],
            ["Gold", "gold schema", "04_gold/"],
            ["Unity Catalog", "masks, row filters"],
        ],
        [PLAT] * 7 + [GOV],
        [
            ["Step", "What happens", "Rules", "Code", "Status"],
            ["Generate", "10 min of events for 1,388 patients on a simulated clock", "One file per simulated minute", "simulator/file_feed.py", "Runs - 103,301 events"],
            ["Ingest + Bronze", "Autoloader reads JSON lines; flatten, add ingestion time", "Never drops a row", "pipeline/01_ingest, 02_bronze", "Runs on Databricks - raw = Bronze = 103,301"],
            ["Silver", "Contract gate, encounters (current + history), patients, vitals", "Unknown event type fails the pipeline; 323 impossible vitals dropped", "pipeline/03_silver/", "Runs on Databricks - 99,213 vitals"],
            ["Gold", "dim_patients, fct_encounters, fct_encounter_history, fct_vitals, dim_providers, live vitals", "Readings attributed to the unit at reading time", "pipeline/04_gold/", "Runs on Databricks"],
            ["Govern", "Masks on full_name / mrn declared in the pipeline; unit row filter", "Agent never sees raw identifiers", "pipeline/04_gold/, governance/", "Partial - masks verified; row filter untested"],
        ],
        notes=["EDA of the same 103,301 events (docs/eda/): no duplicates, late events or broken lifecycles; 13 patient_ids shared by two people (random id collisions) - now flagged in Silver."],
    )
    s += workflow(
        "W2. Analyst question",
        "A clinician asks a plain-English question. Routine metrics go to the metric path; anything else to Genie. "
        "Our layer decides whether the question may be asked and what of the answer may be shown.",
        "w2",
        [
            ["Question", "'How many patients", "are in the ICU?'"],
            ["Input check", "refuse names / MRNs"],
            ["Metric path or Genie", "compute_metric, or", "Genie writes SQL"],
            ["Scope + consistency", "Gold only; filters", "the question asked"],
            ["Output guard", "mask PHI, suppress", "groups < 11 patients"],
            ["Audit + trace", "question, SQL, outcome"],
            ["Answer", "+ which source answered"],
        ],
        [HUMAN, OURS, PLAT, OURS, OURS, OBS, HUMAN],
        [
            ["Step", "What happens", "If it goes wrong", "Code", "Status"],
            ["Input check", "Requests for patient identifiers refused before anything is sent", "Refusal returned; nothing reaches Genie", "agent/tools/genie.py", "Measured - 3/3 probes refused"],
            ["Metric path", "compute_metric over the metrics layer; same definitions as Genie", "Unknown value -> refusal listing allowed values", "agent/tools/metrics.py", "Measured - 20/20 correct, 1.3 s"],
            ["Genie", "Conversation API: start, poll, fetch result", "Failure / timeout -> tool error", "agent/databricks_client.py", "Measured - 21/21 correct, 13 s median"],
            ["Scope + consistency", "SQL must read only Gold and filter only on values the question named", "Result withheld", "agent/tools/genie.py", "Measured - caught PICU -> ICU"],
            ["Output guard", "Masked columns redacted; COUNT cells under 11 suppressed and Genie's text withheld", "Suppressed with a reason", "agent/tools/genie.py", "Measured - caught a count of 1"],
        ],
    )
    s += workflow(
        "W3. Pipeline incident",
        "A health check asks the agent to look at the pipelines. The problem catalog decides how far the agent may go.",
        "w3",
        [
            ["Health check", "every 15 min (prod)"],
            ["Checks", "expectations, jobs,", "schema drift"],
            ["lookup_problem", "catalog ID +", "autonomy level"],
            ["L1: needs a person", "notify_and_page"],
            ["L2: fixable", "fix requested"],
            ["Approval gate", "unless the class", "earned autonomy"],
            ["Fix runs", "kill switch + ceiling", "still apply"],
        ],
        [PLAT, OURS, OURS, HUMAN, OURS, HUMAN, PLAT],
        [
            ["Step", "What happens", "Rules", "Code", "Status"],
            ["Check", "Expectation failures, job states, schema drift", "Read-only; each result names its backend", "agent/tools/pipeline_health.py, _live.py", "Runs live on Free Edition (expectations, job status)"],
            ["Match", "Symptom matched to one of 104 catalog problems", "No match = new problem -> escalate", "agent/tools/problem_catalog.py", "Tested locally"],
            ["Escalate", "Contract breaks, data loss, schema drift go to a person", "Never auto-fixed", "notify_and_page", "Tested locally"],
            ["Request fix", "Quarantine / restart become pending requests", "Approval required by default", "agent/orchestrator.py", "Tested locally"],
            ["Scheduled job", "Serverless job; rules planner when no model key; exit codes 0-3", "Non-zero fails the run -> email to the owner", "agent/healthcheck.py, agent/rules_planner.py, resources/workflows.yml", "Runs - Free Edition: 3 findings, 2 paged, 1 fix queued for approval"],
        ],
    )
    s += workflow(
        "W4. Human approval and earned autonomy",
        "How a requested fix becomes an executed one - or doesn't - and how a proven kind of fix stops needing a person.",
        "w4",
        [
            ["Agent requests", "quarantine / restart"],
            ["Pending request", "4h expiry;", "duplicates merged"],
            ["Person decides", "approve / reject", "--by name"],
            ["Runs, audited", "kill switch, ceiling,", "guardrails apply"],
            ["10 clean runs", "class is promotable"],
            ["Person promotes", "approvals promote"],
            ["First failure", "auto-demoted"],
        ],
        [OURS, OURS, HUMAN, PLAT, OURS, HUMAN, OURS],
        [
            ["Command", "Effect", "Notes", "Code", "Status"],
            ["python -m agent.approvals list", "Pending, unexpired requests", "Read-only", "agent/approvals.py", "Tested locally"],
            ["... approve / reject <id> --by \"Name\"", "Executes or closes the request", "Both audited with the person's name", "agent/approvals.py", "Tested locally"],
            ["... classes", "Each action class: policy, streak, eligibility", "Streak = consecutive approved, successful runs", "agent/autonomy.py", "Tested locally"],
            ["... promote <class> --by \"Name\"", "Class runs without approval from now on", "Needs 10 clean runs; quarantine/restart only", "agent/autonomy.py", "Tested locally"],
            ["Automatic demotion", "First failed automatic run puts the class back on approval", "Reason recorded", "agent/orchestrator.py", "Tested locally"],
        ],
    )
    s += workflow(
        "W5. Deploy and run on Databricks Free Edition",
        "From laptop to a running pipeline on Databricks Free Edition (serverless, no cost, doesn't expire), and back "
        "to nothing running. The GCP trial workspace was used first; it locked sign-in when the trial ended.",
        "w5",
        [
            ["Login", "auth login --profile free"],
            ["Catalog + schemas", "SQL on the warehouse"],
            ["Upload dataset", "to a UC volume"],
            ["bundle deploy -t free", "pipelines + jobs"],
            ["Run pipeline", "triggered, serverless"],
            ["Genie space", "create from the", "generated space.json"],
            ["Stop everything", "scripts/stop_everything.py"],
        ],
        [OURS, PLAT, PLAT, PLAT, PLAT, PLAT, OURS],
        [
            ["Step", "Command", "Cost", "Notes", "Status"],
            ["Login", "databricks auth login --profile free", "Free", "OAuth in the browser; the account owner approves", "Runs"],
            ["Catalog, data", "CREATE CATALOG / SCHEMA / VOLUME; databricks fs cp", "Free", "Same catalog name as before, so no code changes", "Runs"],
            ["Deploy + run", "databricks bundle deploy -t free; bundle run streaming_patient_pipeline", "Free (quota)", "Dev mode pauses schedules; triggered", "Runs - counts match the local prediction"],
            ["Evidence", "scripts/workspace_snapshot.py <pipeline_id>", "Free", "Row counts + event-log expectations into docs/", "Runs"],
            ["Genie + golden set", "databricks genie create-space; python -m evals.golden.run", "Free (quota)", "Run 3: guarded 27/27", "Runs"],
            ["Stop", "uv run python scripts/stop_everything.py --profile free", "-", "Pre-approved after every session", "Runs"],
        ],
        notes=["The old GCP trial: only the account owner can cancel its Databricks subscription in GCP Marketplace, which guarantees it never bills."],
    )
    s += workflow(
        "W6. Measure: answers, safety, data, work",
        "Nothing is claimed without a measurement. Each run's findings become guardrail changes with regression tests.",
        "w6",
        [
            ["Golden set", "27 questions, truth", "SQL on Gold"],
            ["Three paths", "Genie, Genie+guard,", "metric path"],
            ["Score", "deterministic,", "no LLM judge"],
            ["Findings -> fixes", "each with a test"],
            ["Rescore", "same answers,", "new guard"],
            ["EDA + model test", "local, free"],
            ["Human-work report", "audit + approvals"],
        ],
        [OURS, PLAT, OURS, OURS, OURS, OURS, OURS],
        [
            ["Measurement", "How", "Result so far", "Code", "Status"],
            ["Golden question set", "python -m evals.golden.run (live), .rescore (offline replay)", "Run 2 with current guard: 21/21 answers, 6/6 probes; Genie alone failed 1 probe", "evals/golden/", "Measured"],
            ["Latency", "Same runs", "Genie p50 13.4 s, p90 17.1 s; metric path 1.3 s", "evals/golden/run.py", "Measured"],
            ["EDA", "scripts/eda_landing.py (DuckDB over raw JSON)", "Input clean; 13 patient-id collisions; vitals not physiological", "docs/eda/", "Measured"],
            ["Detectors", "python -m ml.evaluate_anomaly_model", "Round 1: model v1 worse than the range rule. Round 2: v2 61% vs NEWS2 47% at equal false alarms", "ml/evaluate_anomaly_model.py", "Measured - v2 promoted as a calibrated third layer"],
            ["Human work", "python -m agent.human_work", "Counts requests, who needed a person, decision times", "agent/human_work.py", "Tested locally - no real usage yet"],
        ],
    )
    s += workflow(
        "W7. Observability",
        "Every request is a trace; the records become metrics; one dashboard shows human work, guardrails and quality. "
        "Everything runs locally and free; moving to Datadog, Dynatrace or Grafana Cloud is a collector config change.",
        "w7",
        [
            ["Orchestrator", "agent.request +", "agent.tool spans"],
            ["telemetry.py", "metrics from audit log,", "approvals, reports"],
            ["OTel Collector", "OTLP :4318"],
            ["Prometheus", "metrics :9090"],
            ["Jaeger", "traces :16686"],
            ["Grafana", "dashboard :3000"],
            ["Person", "sees human work,", "guardrails, quality"],
        ],
        [OURS, OURS, OBS, OBS, OBS, OBS, HUMAN],
        [
            ["Piece", "Command", "Notes", "Code", "Status"],
            ["Start the stack", "docker compose --profile observability up -d", "Collector, Prometheus, Jaeger, Grafana (pinned images)", "docker-compose.yml, observability/", "Runs locally"],
            ["Metrics exporter", "python -m agent.telemetry", "16 gauges recomputed from the records every 15 s", "agent/telemetry.py", "Runs locally"],
            ["Traces", "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318", "Spans carry ids, tool names and outcomes - never request text", "agent/otel.py, agent/orchestrator.py", "Runs locally"],
            ["Demo traffic", "python scripts/demo_traffic.py", "Real orchestrator and guardrails, scripted tool choices, no API cost", "scripts/demo_traffic.py", "Runs locally - demo data, not real usage"],
            ["Dashboard", "python scripts/build_grafana_dashboard.py", "Generated; a test fails if a panel queries an unknown metric", "observability/grafana/", "Runs locally"],
        ],
    )
    s += workflow(
        "W8. Development and quality",
        "How a change gets in. The repo's credibility rests on an honest split between runs, measured, tested locally, "
        "mock-tested and never run.",
        "w8",
        [
            ["Change code", "glue or guardrail only"],
            ["ruff check", "lint"],
            ["pytest", "453 tests, no network,", "no API key"],
            ["Guardrail tests", "masking, injection, MCP", "when core files change"],
            ["Commit + push", "master"],
            ["GitHub Actions CI", "lint + tests"],
            ["Live measurement", "golden set,", "on the workspace"],
        ],
        [OURS, OURS, OURS, OURS, PLAT, PLAT, HUMAN],
        [
            ["Practice", "Why", "Where", "Enforced by", "Status"],
            ["No real secrets in the repo", "Public repo", ".gitignore, agent/secrets.py", "Review; Secret Manager", "Tested locally"],
            ["Status labels in every doc", "Never claim untested work works", "README feature status, this document", "Review", "Tested locally"],
            ["Generated artifacts checked", "Genie space and dashboard can't drift from their source", "tests/test_metrics_layer.py, test_telemetry.py", "CI", "Tested locally"],
            ["Live paths mock-tested", "No network in CI", "tests/conftest.py clears DATABRICKS_*", "conftest fixture", "Tested locally"],
            ["Findings become tests", "A fixed failure stays fixed", "tests/ (golden, EDA findings)", "CI", "Tested locally"],
        ],
    )
    s += [
        p("3. Tech stack", "H1"),
        p("Versions are the ones pinned in uv.lock and docker-compose.yml. Status colours: green runs, measured or tested locally; yellow partial, advisory or mock-tested; red never run; grey planned."),
        space(),
        grid(STACK, [30, 50, 42, 62, 50, 33], status_col=5),
        br(),
        p("4. Guardrails", "H1"),
        p("Enforced in code on every request, not just asked for in a prompt."),
        space(),
        grid(GUARDRAILS, [48, 80, 72, 67]),
        br(),
        p("5. Where everything lives", "H1"),
        grid(REPO_MAP, [70, 197]),
        space(),
        p("Pull it", "H2"),
        bullets(
            [
                "<font name='Courier'>gh repo clone sumanthreddy369/databricks-agentic-de</font>",
                "<font name='Courier'>uv sync --extra dev</font> then <font name='Courier'>uv run python -m pytest</font>",
            ]
        ),
        br(),
        p("6. What's next", "H1"),
        grid(NEXT, [10, 120, 62, 75]),
    ]
    return s


# --- PDF renderer -------------------------------------------------------------------


def build_pdf(out: Path) -> None:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=ss["BodyText"], fontSize=9.5, leading=13, textColor=INK)
    styles = {
        "H1": ParagraphStyle("H1", parent=ss["Heading1"], fontSize=18, leading=22, textColor=ACCENT, spaceAfter=6),
        "H2": ParagraphStyle("H2", parent=ss["Heading2"], fontSize=13, leading=16, textColor=ACCENT, spaceBefore=8, spaceAfter=4),
        "BODY": body,
        "SMALL": ParagraphStyle("Small", parent=body, fontSize=8.5, leading=11.5, textColor=MUTED),
        "TITLE": ParagraphStyle("Title", parent=ss["Heading1"], fontSize=26, leading=31, textColor=ACCENT),
        "SUB": ParagraphStyle("Sub", parent=body, fontSize=11.5, leading=16, textColor=MUTED),
    }
    cell = ParagraphStyle("Cell", parent=body, fontSize=8, leading=10.2)
    cellh = ParagraphStyle("CellH", parent=cell, fontName="Helvetica-Bold")
    bul = ParagraphStyle("Bul", parent=body, leftIndent=12, bulletIndent=2, spaceAfter=2)

    def pdf_grid(rows, widths, status_col=None):
        data = [[Paragraph(c, cellh if r == 0 else cell) for c in row] for r, row in enumerate(rows)]
        t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if status_col is not None:
            for r, row in enumerate(rows[1:], start=1):
                fill = next((c for key, c in STATUS.items() if row[status_col].startswith(key)), None)
                if fill is not None:
                    style.append(("BACKGROUND", (status_col, r), (status_col, r), fill))
        t.setStyle(TableStyle(style))
        return t

    class fx:
        p = staticmethod(lambda text, style="BODY": Paragraph(text, styles[style]))
        bullets = staticmethod(lambda items: [Paragraph(i, bul, bulletText="-") for i in items])
        grid = staticmethod(pdf_grid)
        img = staticmethod(lambda name, drawing: drawing)
        space = staticmethod(lambda: Spacer(1, 6))
        pagebreak = staticmethod(PageBreak)

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 8 * mm, f"databricks-agentic-de - Workflow and tech stack - {SNAPSHOT}")
        canvas.drawRightString(doc.pagesize[0] - 15 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    flat = list(_flatten(story(fx)))
    if flat and isinstance(flat[-1], PageBreak):
        flat.pop()
    doc = SimpleDocTemplate(
        str(out),
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=13 * mm,
        bottomMargin=14 * mm,
        title="databricks-agentic-de: Workflow and Tech Stack",
        author="sumanthreddy369",
    )
    doc.build(flat, onFirstPage=on_page, onLaterPages=on_page)


# --- Word renderer --------------------------------------------------------------------


def build_docx(out: Path) -> None:
    import pypdfium2 as pdfium
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor
    from reportlab.graphics import renderPDF

    accent, muted = RGBColor(0x1D, 0x4E, 0x89), RGBColor(0x52, 0x60, 0x6D)
    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Mm(297), Mm(210)
    section.left_margin = section.right_margin = Mm(15)
    section.top_margin, section.bottom_margin = Mm(13), Mm(14)
    usable_mm = 297 - 30
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10)
    for name, size in (("Title", 26), ("Heading 1", 18), ("Heading 2", 13)):
        doc.styles[name].font.color.rgb = accent
        doc.styles[name].font.size = Pt(size)
        doc.styles[name].font.name = "Calibri"
    footer = section.footer.paragraphs[0]
    footer.text = f"databricks-agentic-de - Workflow and tech stack - {SNAPSHOT}"
    footer.runs[0].font.size = Pt(8)
    footer.runs[0].font.color.rgb = muted

    token = re.compile(r"(<b>|</b>|<font name='Courier'>|</font>)")

    def add_runs(paragraph, text, *, size=None, color=None, bold=False):
        state = {"b": bold, "code": False}
        for part in token.split(text):
            if part in ("<b>", "</b>"):
                state["b"] = part == "<b>" or bold
            elif part in ("<font name='Courier'>", "</font>"):
                state["code"] = part != "</font>"
            elif part:
                run = paragraph.add_run(html.unescape(part))
                run.bold = state["b"]
                if state["code"]:
                    run.font.name = "Consolas"
                if size:
                    run.font.size = Pt(size)
                if color:
                    run.font.color.rgb = color

    def shade(cell, fill):
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill.hexval()[2:])
        cell._tc.get_or_add_tcPr().append(shd)

    tmp = Path(tempfile.mkdtemp())

    class fx:
        @staticmethod
        def p(text, style="BODY"):
            if style == "TITLE":
                add_runs(doc.add_paragraph(style="Title"), text)
            elif style in ("H1", "H2"):
                add_runs(doc.add_heading(level=1 if style == "H1" else 2), text)
            elif style in ("SMALL", "SUB"):
                add_runs(doc.add_paragraph(), text, size=9 if style == "SMALL" else 12, color=muted)
            else:
                add_runs(doc.add_paragraph(), text)

        @staticmethod
        def bullets(items):
            for text in items:
                add_runs(doc.add_paragraph(style="List Bullet"), text)

        @staticmethod
        def grid(rows, widths, status_col=None):
            total = sum(widths)
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            table.autofit = False
            for r, row in enumerate(rows):
                for c, text in enumerate(row):
                    cell = table.cell(r, c)
                    cell.width = Mm(widths[c] / total * usable_mm)
                    cell.paragraphs[0].paragraph_format.space_after = Pt(0)
                    add_runs(cell.paragraphs[0], text, size=8.5, bold=(r == 0))
                    if r == 0:
                        shade(cell, HEAD_BG)
                    elif status_col is not None and c == status_col:
                        fill = next((v for k, v in STATUS.items() if text.startswith(k)), None)
                        if fill is not None:
                            shade(cell, fill)
            header = OxmlElement("w:tblHeader")
            header.set(qn("w:val"), "true")
            table.rows[0]._tr.get_or_add_trPr().append(header)
            doc.add_paragraph().paragraph_format.space_after = Pt(2)

        @staticmethod
        def img(name, drawing):
            pdf = tmp / f"{name}.pdf"
            renderPDF.drawToFile(drawing, str(pdf))
            png = tmp / f"{name}.png"
            pdfium.PdfDocument(str(pdf))[0].render(scale=3).to_pil().save(png)
            doc.add_picture(str(png), width=Mm(usable_mm))

        space = staticmethod(lambda: None)

        @staticmethod
        def pagebreak():
            doc.add_page_break()

    # The builders write to the document as story() calls them, in order.
    story(fx)
    doc.core_properties.title = "databricks-agentic-de: Workflow and Tech Stack"
    doc.core_properties.author = "sumanthreddy369"
    doc.save(str(out))


def _flatten(items):
    for item in items:
        if isinstance(item, list):
            yield from _flatten(item)
        elif item is not None:
            yield item


def main() -> int:
    build_pdf(PDF_OUT)
    build_docx(DOCX_OUT)
    print(f"wrote {PDF_OUT}\nwrote {DOCX_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
