"""Proves the Genie glue + guardrails (agent/tools/genie.py and the Genie calls
in agent/databricks_client.py) against a mocked Genie Conversation API - zero
network calls. The fake follows the documented API shape; it is not
captured from a real Genie space (none exists in this environment).
"""

import json

import httpx
import pytest

from agent.databricks_client import DatabricksClient, DatabricksConfig
from agent.orchestrator import OrchestratorAgent
from agent.tools import genie
from agent.tools.genie import ask_genie
from common.contracts import MIN_CELL_SIZE

SPACE = "space-1"


class FakeGenie:
    """Answers start-conversation, then reports IN_PROGRESS `pending_polls`
    times before COMPLETED with a text attachment and a query attachment."""

    def __init__(self, *, text="", sql="SELECT 1", columns=(), rows=(), status="COMPLETED", pending_polls=1):
        self.text, self.sql, self.columns, self.rows = text, sql, list(columns), [list(r) for r in rows]
        self.status, self.pending_polls = status, pending_polls
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/start-conversation"):
            assert json.loads(request.content)["content"]
            return httpx.Response(200, json={"conversation_id": "c1", "message_id": "m1"})
        if path.endswith("/query-result"):
            schema = {"columns": [{"name": n, "type_name": t} for n, t in self.columns]}
            data = [[None if v is None else str(v) for v in row] for row in self.rows]
            return httpx.Response(
                200,
                json={"statement_response": {"manifest": {"schema": schema}, "result": {"data_array": data}}},
            )
        if path.endswith("/messages/m1"):
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return httpx.Response(200, json={"status": "EXECUTING_QUERY"})
            attachments = []
            if self.text:
                attachments.append({"attachment_id": "a0", "text": {"content": self.text}})
            if self.sql:
                attachments.append({"attachment_id": "a1", "query": {"query": self.sql, "description": ""}})
            body = {"status": self.status, "attachments": attachments}
            if self.status != "COMPLETED":
                body["error"] = {"error": "space misconfigured"}
            return httpx.Response(200, json=body)
        return httpx.Response(404)


def _client(fake: FakeGenie) -> DatabricksClient:
    config = DatabricksConfig(host="https://example.gcp.databricks.com", token="t", genie_space_id=SPACE)
    client = DatabricksClient(config, transport=httpx.MockTransport(fake.handler))
    original = client.genie_ask
    client.genie_ask = lambda q, **kw: original(q, sleep=lambda s: None, **kw)
    return client


def _payload(result):
    return json.loads(result.content)


def test_answer_flows_through_with_sql_and_typed_rows():
    fake = FakeGenie(
        text="There are 42 patients in the ICU.",
        sql="SELECT count(*) AS patient_count FROM healthcare_agentic_de.gold.fct_encounters WHERE unit = 'ICU'",
        columns=[("patient_count", "LONG")],
        rows=[[42]],
    )

    payload = _payload(ask_genie("How many patients are in the ICU?", client=_client(fake)))

    assert payload["ok"] is True
    assert payload["source"] == "genie"
    assert payload["rows"] == [{"patient_count": 42}]
    assert payload["answer_text"] == "There are 42 patients in the ICU."
    assert payload["guardrails"]["sql_schema_check"] == "passed"
    assert ("POST", f"/api/2.0/genie/spaces/{SPACE}/start-conversation") in fake.requests


def test_masked_columns_are_redacted_even_if_uc_masking_failed():
    fake = FakeGenie(
        text="The patient in bed 4 is Jane Alvarez (MRN-000123).",
        sql="SELECT full_name, mrn, unit FROM gold.dim_patients",
        columns=[("full_name", "STRING"), ("mrn", "STRING"), ("unit", "STRING")],
        rows=[["Jane Alvarez", "MRN-000123", "ICU"]],
    )

    payload = _payload(ask_genie("Who is in bed 4?", client=_client(fake)))

    assert payload["rows"] == [{"full_name": "***REDACTED***", "mrn": "***REDACTED***", "unit": "ICU"}]
    assert "Jane Alvarez" not in json.dumps(payload)
    assert "MRN-000123" not in json.dumps(payload)
    assert payload["guardrails"]["phi_values_redacted_from_text"] == 2
    assert payload["guardrails"]["masked_columns"] == ["full_name", "mrn"]


def test_small_counts_are_suppressed():
    fake = FakeGenie(
        sql="SELECT unit, count(*) AS patient_count FROM gold.fct_encounters GROUP BY unit",
        columns=[("unit", "STRING"), ("patient_count", "LONG")],
        rows=[["ICU", 42], ["Burn Unit", 3]],
    )

    payload = _payload(ask_genie("Patients per unit?", client=_client(fake)))

    assert payload["rows"][0] == {"unit": "ICU", "patient_count": 42}
    assert payload["rows"][1] == {"unit": "Burn Unit", "patient_count": None, "patient_count_suppressed": True}
    assert payload["guardrails"]["suppressed_cells"] == 1
    assert payload["guardrails"]["min_cell_size"] == MIN_CELL_SIZE


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM healthcare_agentic_de.silver.silver_dim_patients",
        "SELECT * FROM gold.fct_encounters e JOIN bronze.bronze_patient_events b USING (encounter_id)",
        "SELECT * FROM other_catalog.gold.fct_encounters",
    ],
)
def test_results_from_sql_outside_gold_are_withheld(sql):
    fake = FakeGenie(sql=sql, columns=[("x", "STRING")], rows=[["secret"]])

    payload = _payload(ask_genie("Anything", client=_client(fake)))

    assert payload["withheld"] is True
    assert "secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    "question",
    [
        "What is the full name of patient pt_00001?",
        "List the MRNs in the ICU",
        "Give me the names of all patients",
    ],
)
def test_phi_requests_are_refused_before_reaching_genie(question):
    fake = FakeGenie()

    payload = _payload(ask_genie(question, client=_client(fake)))

    assert payload["refused"] is True
    assert fake.requests == []


def test_not_configured_points_to_the_fallback_tools():
    payload = _payload(ask_genie("How many patients are in the ICU?"))

    assert payload == {
        "ok": False,
        "configured": False,
        "message": payload["message"],
    }
    assert "aggregate_gold_table" in payload["message"]


def test_genie_failure_is_a_tool_error():
    fake = FakeGenie(status="FAILED")

    result = ask_genie("How many patients?", client=_client(fake))

    assert result.is_error is True
    assert "FAILED" in _payload(result)["error"]


def test_genie_timeout_is_a_tool_error():
    fake = FakeGenie(pending_polls=10_000)
    client = _client(fake)
    original = DatabricksClient.genie_ask
    client.genie_ask = lambda q, **kw: original(client, q, sleep=lambda s: None, timeout_s=10, poll_s=2)

    result = ask_genie("How many patients?", client=client)

    assert result.is_error is True
    assert "did not answer" in _payload(result)["error"]


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def test_orchestrator_routes_ask_genie_and_audits_it(tmp_path, monkeypatch):
    fake = FakeGenie(
        sql="SELECT full_name FROM gold.dim_patients", columns=[("full_name", "STRING")], rows=[["Jane Alvarez"]]
    )
    client = _client(fake)
    monkeypatch.setattr(genie, "ask_genie", lambda question: ask_genie(question, client=client))
    audit = tmp_path / "audit.jsonl"
    orchestrator = OrchestratorAgent(claude=_NoopClaude(), audit_log_path=audit)

    result = orchestrator._dispatch("ask_genie", {"question": "Who is admitted?"})

    assert "Jane Alvarez" not in result.content
    entry = json.loads(audit.read_text().splitlines()[-1])
    assert entry["tool"] == "ask_genie"
    assert "Jane Alvarez" not in audit.read_text()


def test_provider_counts_are_not_suppressed():
    """Found by the golden-set run: suppression protects patients, so small
    provider counts must pass through unchanged."""
    fake = FakeGenie(
        sql="SELECT specialty, count(*) AS provider_count FROM gold.dim_providers GROUP BY 1",
        columns=[("specialty", "STRING"), ("provider_count", "LONG")],
        rows=[["Cardiology", 4], ["Critical Care", 9]],
    )

    payload = _payload(ask_genie("How many providers per specialty?", client=_client(fake)))

    assert payload["rows"] == [
        {"specialty": "Cardiology", "provider_count": 4},
        {"specialty": "Critical Care", "provider_count": 9},
    ]
    assert payload["guardrails"]["suppressed_cells"] == 0


def test_small_patient_counts_are_still_suppressed_when_joined_with_providers():
    fake = FakeGenie(
        sql=(
            "SELECT p.specialty, count(*) AS patient_count FROM gold.fct_encounters e "
            "JOIN gold.dim_providers p ON e.attending_provider_id = p.provider_id GROUP BY 1"
        ),
        columns=[("specialty", "STRING"), ("patient_count", "LONG")],
        rows=[["Cardiology", 4]],
    )

    payload = _payload(ask_genie("Patients per attending specialty?", client=_client(fake)))

    assert payload["rows"][0]["patient_count"] is None


@pytest.mark.parametrize(
    ("question", "sql", "withheld"),
    [
        # Found by the golden-set run: asked about the PICU, Genie answered for the ICU.
        (
            "How many patients are currently in the PICU?",
            "SELECT count(*) AS n FROM gold.fct_encounters WHERE unit = 'ICU'",
            True,
        ),
        (
            "How many patients are in intensive care?",
            "SELECT count(*) AS n FROM gold.fct_encounters WHERE unit = 'ICU'",
            False,
        ),
        (
            "How many patients in the emergency room?",
            "SELECT count(*) AS n FROM gold.fct_encounters WHERE `unit` = 'ED'",
            False,
        ),
        (
            "What's the census in med-surg?",
            "SELECT count(*) AS n FROM gold.fct_encounters WHERE unit = 'MedSurg'",
            False,
        ),
        (
            "How many patients are admitted?",
            "SELECT count(*) AS n FROM gold.fct_encounters WHERE status = 'in-progress'",
            False,
        ),
        ("Census by unit?", "SELECT unit, count(*) AS n FROM gold.fct_encounters GROUP BY unit", False),
    ],
)
def test_filters_the_question_never_asked_for_are_withheld(question, sql, withheld):
    fake = FakeGenie(sql=sql, columns=[("n", "LONG")], rows=[[34]])

    payload = _payload(ask_genie(question, client=_client(fake)))

    assert bool(payload.get("withheld")) is withheld, payload


def test_small_count_under_a_generic_alias_is_suppressed_text_included():
    """Found by golden run 2: Genie aliased COUNT(...) as `value` and wrote
    "There are **1** distinct patients" - the name heuristic let both through."""
    fake = FakeGenie(
        text="There are **1** distinct patients currently admitted in the **ICU** with gender = **nonbinary**.",
        sql=(
            "SELECT COUNT(DISTINCT `f`.`patient_id`) AS `value`\n"
            "FROM `healthcare_agentic_de`.`gold`.`fct_encounters` AS `f`\n"
            "INNER JOIN `healthcare_agentic_de`.`gold`.`dim_patients` AS `p`\n"
            "  ON `f`.`patient_id` = `p`.`patient_id`\n"
            "WHERE `f`.`unit` = 'ICU' AND `p`.`gender` = 'nonbinary'"
        ),
        columns=[("value", "LONG")],
        rows=[[1]],
    )

    payload = _payload(ask_genie("How many nonbinary patients are in the ICU right now?", client=_client(fake)))

    assert payload["rows"] == [{"value": None, "value_suppressed": True}]
    assert "**1**" not in payload["answer_text"]
    assert "too small to report" in payload["answer_text"]


def test_averages_under_a_generic_alias_are_not_suppressed():
    fake = FakeGenie(
        sql="SELECT AVG(`value`) AS `value` FROM healthcare_agentic_de.gold.fct_vitals WHERE `itemid` = 'spo2'",
        columns=[("value", "DOUBLE")],
        rows=[[3]],
    )

    payload = _payload(ask_genie("What is the average SpO2?", client=_client(fake)))

    assert payload["rows"] == [{"value": 3}]
    assert payload["guardrails"]["suppressed_cells"] == 0
