"""Policy for the GitHub Actions workflows themselves. The pinning rule is
the one Databricks enforces in bundle-examples (conftest,
pinned_actions.rego), checked here with the rest of the tests instead of a
separate tool."""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_there_are_workflows():
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "bundle.yml", "workflow-lint.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_external_actions_are_pinned_to_a_commit_with_the_version_noted(path):
    """A tag like @v4 can be moved to another commit; a full SHA can't. The
    trailing "# vX.Y.Z" comment keeps the pin readable (and is what
    Dependabot updates)."""
    for line in path.read_text(encoding="utf-8").splitlines():
        match = USES.match(line)
        if not match or match.group(1).startswith("./"):
            continue
        ref, rest = match.group(1), match.group(2)
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), f"{path.name}: {ref} is not pinned to a commit SHA"
        assert re.search(r"#\s*v\d", rest), f"{path.name}: {ref} has no '# vX.Y.Z' comment"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_token_permissions_are_read_only_by_default(path):
    assert _load(path).get("permissions") == {"contents": "read"}, path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_are_never_echoed(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"echo[^\n]*\$\{\{\s*secrets\.", text), path.name


def test_workspace_jobs_never_overlap_and_never_start_on_their_own():
    jobs = _load(REPO / ".github" / "workflows" / "bundle.yml")["jobs"]
    for name in ("deploy", "integration"):
        assert jobs[name]["concurrency"]["group"] == "bundle-deploy-free"
        assert jobs[name]["concurrency"]["cancel-in-progress"] is False
    assert "vars.DEPLOY_ON_MERGE == 'true'" in jobs["deploy"]["if"]
    assert "workflow_dispatch" in jobs["integration"]["if"]
    # The integration run always stops the warehouse and pipelines.
    last = jobs["integration"]["steps"][-1]
    assert last["if"] == "always()" and "stop_everything.py" in last["run"]


def test_guardrail_job_runs_every_guardrail_test_file():
    step = _load(REPO / ".github" / "workflows" / "ci.yml")["jobs"]["guardrails"]["steps"][-1]["run"]
    named = re.findall(r"tests/\S+\.py", step)
    assert set(named) == {
        "tests/test_masking_guard.py",
        "tests/test_prompt_injection_guard.py",
        "tests/test_mcp_server_tools.py",
        "tests/test_kill_switch.py",
        "tests/test_escalation_ceiling.py",
        "tests/test_audit_trail.py",
    }
    assert all((REPO / f).exists() for f in named)


def test_dependabot_waits_two_weeks_before_proposing_a_release():
    config = _load(REPO / ".github" / "dependabot.yml")
    assert {u["package-ecosystem"] for u in config["updates"]} == {"github-actions", "uv"}
    assert all(u["cooldown"]["default-days"] >= 14 for u in config["updates"])
