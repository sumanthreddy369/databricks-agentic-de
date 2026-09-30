# Access policy notes

Short, human-readable companion to the DDL in this directory — the grants
themselves are the enforcement point; this file explains the policy behind
them.

## Minimum-necessary-access: `orchestrator_agent`

The `orchestrator_agent` Unity Catalog group (the orchestrator agent's own
identity for both DE-mode pipeline-health tool calls and DA-mode
`query_gold_table` calls) is granted **exactly**:

```sql
GRANT USE CATALOG ON CATALOG healthcare_agentic_de TO `orchestrator_agent`;
GRANT USE SCHEMA, SELECT ON SCHEMA healthcare_agentic_de.gold TO `orchestrator_agent`;
GRANT USE SCHEMA ON SCHEMA healthcare_agentic_de.ops TO `orchestrator_agent`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME healthcare_agentic_de.ops.agent_state TO `orchestrator_agent`;
```

— see [`catalog_and_grants.sql`](catalog_and_grants.sql). That's the whole
grant: `USE CATALOG`, `USE SCHEMA, SELECT` on `gold`, and read/write on the
`ops.agent_state` volume, which holds only the scheduled agent job's own
state file and audit log (kill switch, escalation counters, incidents) so
they survive between runs on ephemeral clusters. No `MODIFY` or `CREATE
TABLE` on any schema, no access to `bronze`/`silver`, and no membership in
`phi_unmasked` (the break-glass group that sees unmasked PHI) — that
membership is never granted anywhere in this file, and `catalog_and_grants.sql`
says so explicitly at the grant site as a standing reminder not to add it.

This is deliberate minimum-necessary access, not an oversight: the
orchestrator agent's actual job (answer questions over Gold, check/remediate
pipeline health, keep its own state file) never requires write access to any
Unity Catalog table, or read access below Gold, or unmasked PHI. Narrowing the
grant to exactly what's used means a prompt-injection attempt that somehow
got an LLM call to *want* to do something destructive (drop a table, read
Silver/Bronze, read unmasked PHI) still can't, at the database layer,
independently of whatever `agent/tools/governance_guard.py` and
`agent/orchestrator.py`'s guardrails do in-process. Two independent layers,
not one — see docs/architecture.md's "Guardrails" section, row
"Minimum-necessary-access."

## PHI retention/deletion policy (policy-only, not code-enforced)

**This is a stated policy, not an enforced mechanism** — nothing in this
repository implements automatic retention/deletion, and it is labeled here
honestly for exactly that reason, consistent with this project's convention
of marking Databricks-only/not-actually-running pieces as such rather than
silently implying they work.

Stated policy this project would follow in a real deployment:

- Raw Bronze `patient_events` payloads (which carry PHI in the `patient`
  struct) are retained no longer than clinically/operationally necessary —
  typically bounded by the hosting institution's medical-record retention
  requirement, not indefinitely.
- A patient/record deletion request (e.g. a right-to-erasure or
  record-correction request) would be actioned via a `DELETE`/`MERGE` against
  the affected `dim_patients`/`fct_encounters` rows plus a `VACUUM` to
  physically remove the deleted Delta file versions, and would need to be
  applied consistently across Bronze/Silver/Gold, not just Gold.
- Enforcing this automatically (a scheduled retention job, a verified erasure
  workflow) is out of scope for this portfolio project and is not
  implemented — treat this section as documentation of the intended policy
  for a real deployment, not a feature of the code here.
