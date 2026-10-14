"""Generates governance/genie/space.json from the metrics layer.

Genie's instructions, example SQL and sample questions all come from
agent/knowledge/metrics.yaml, the same definitions compute_metric uses, so
Genie and the fallback agent can't define "current census" differently.
tests/test_metrics_layer.py fails if space.json is out of date.

    uv run python scripts/build_genie_space.py            # rewrite space.json
    uv run python scripts/build_genie_space.py --check    # exit 1 if stale

Pushing it to the live space is a separate, explicit step (see
scripts/sync_genie_space.py).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.tools.metrics import load, render_sql  # noqa: E402
from common.contracts import GOLD_SCHEMA, MIN_CELL_SIZE  # noqa: E402

SPACE_PATH = REPO_ROOT / "governance" / "genie" / "space.json"
TITLE = "Healthcare Agentic DE - Clinical Ops (Gold)"
DESCRIPTION = (
    "Genie over the governed Gold tables of databricks-agentic-de (synthetic data). Patient identifiers are "
    "masked by Unity Catalog. Metric definitions are generated from agent/knowledge/metrics.yaml, shared with "
    "the project's fallback agent. Questions reach this space through the guardrail layer (agent/tools/genie.py)."
)
TABLES = [
    "dim_patients",
    "dim_providers",
    "fct_encounter_history",
    "fct_encounters",
    "fct_vitals",
    "gold_live_vitals_by_unit",
]


def _id(*parts: str) -> str:
    # Stable 32-hex ids so regenerating without changes produces an identical file.
    return hashlib.md5("|".join(parts).encode()).hexdigest()  # noqa: S324 - not security-related


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def build() -> dict:
    layer = load()
    dims, metrics = layer["dimensions"], layer["metrics"]

    instructions = ["Metric definitions - use exactly these; they are shared with the project's fallback agent:\n"]
    for m in metrics.values():
        filters = ", ".join(f"{k} = '{v}'" for k, v in m.get("filters", {}).items())
        line = f"* {m['name']} ({m['id']}): {m['description'].strip()} Table {GOLD_SCHEMA}.{m['table']}"
        if filters:
            line += f"; always filter {filters}"
        if m.get("requires_as_of"):
            line += "; needs a point in time: valid_from <= T AND (valid_to IS NULL OR valid_to > T)"
        line += f". Break down only by: {', '.join(m.get('dimensions', []))}.\n"
        instructions.append(line)
    instructions.append(
        "Dimension values - compare with = against these exact values; never LIKE / ILIKE patterns:\n"
    )
    for name, spec in dims.items():
        if not spec.get("values"):
            continue
        line = f"* {name}: {', '.join(spec['values'])}"
        if spec.get("synonyms"):
            line += " (synonyms: " + ", ".join(f"'{k}' means {v}" for k, v in spec["synonyms"].items()) + ")"
        instructions.append(line + "\n")
    instructions += [
        "Governance:\n",
        "* full_name and mrn are masked by policy and show ***REDACTED***. Never try to reveal, infer or "
        "reconstruct them.\n",
        f"* Don't report a group that covers fewer than {MIN_CELL_SIZE} patients; "
        "say it is too small to report.\n",
        "* The newest window in gold_live_vitals_by_unit may still be filling; say so when reporting it.\n",
        "* All data is synthetic (simulated), not real patients.",
    ]

    examples = []
    for m in metrics.values():
        sql = render_sql(
            m["id"],
            group_by=m.get("example_group_by"),
            filters=m.get("example_filters"),
            as_of=m.get("example_as_of"),
        )
        examples.append({"id": _id("sql", m["id"]), "question": [m["example_question"]], "sql": _lines(sql)})

    samples = [m["example_question"] for m in metrics.values()][:4]
    return {
        "title": TITLE,
        "description": DESCRIPTION,
        "space": {
            "version": 2,
            # Genie rejects these lists unless they are sorted by id.
            "config": {
                "sample_questions": sorted(
                    ({"id": _id("q", q), "question": [q]} for q in samples), key=lambda x: x["id"]
                )
            },
            "data_sources": {"tables": [{"identifier": f"{GOLD_SCHEMA}.{t}"} for t in TABLES]},
            "instructions": {
                "text_instructions": [{"id": _id("instructions"), "content": instructions}],
                "example_question_sqls": sorted(examples, key=lambda x: x["id"]),
            },
        },
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if space.json is out of date")
    args = parser.parse_args(argv)
    content = render()
    if args.check:
        current = SPACE_PATH.read_text(encoding="utf-8") if SPACE_PATH.exists() else ""
        if current != content:
            print(f"{SPACE_PATH} is out of date; run scripts/build_genie_space.py", file=sys.stderr)
            return 1
        print("space.json is up to date")
        return 0
    SPACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPACE_PATH.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {SPACE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
