"""scripts/setup_workspace.py: the governance SQL splits into the statements
the setup runs, and the account-security ones are held back."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import setup_workspace  # noqa: E402


def test_statements_drop_comments_and_split_on_line_end_semicolons():
    sql = "-- note; not a statement\nCREATE SCHEMA a;\nCREATE TABLE b (\n  x STRING -- no\n) COMMENT 'c;d';\n"
    assert setup_workspace.statements(sql) == [
        "CREATE SCHEMA a",
        "CREATE TABLE b (\n  x STRING -- no\n) COMMENT 'c;d'",
    ]


def test_governance_sql_has_objects_and_holds_back_groups_and_grants():
    plan = [
        s for f in setup_workspace.SQL_FILES for s in setup_workspace.statements(f.read_text(encoding="utf-8"))
    ]
    held = [s for s in plan if setup_workspace.needs_a_person(s)]
    run = [s for s in plan if not setup_workspace.needs_a_person(s)]
    assert run[0] == "CREATE CATALOG IF NOT EXISTS healthcare_agentic_de"
    assert any("ops.identity_decisions" in s for s in run)
    assert any("FUNCTION healthcare_agentic_de.gold.unit_row_filter" in s for s in run)
    assert held and all(s.split()[0] in ("CREATE", "GRANT") for s in held)
    assert all("GROUP" in s or s.startswith("GRANT") for s in held)


def test_dry_run_changes_nothing(capsys, monkeypatch):
    monkeypatch.setattr(setup_workspace, "_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no CLI")))
    assert setup_workspace.main(["--profile", "p", "--target", "t", "--dry-run"]) == 0
    assert "LEAVE (needs --with-groups) GRANT" in capsys.readouterr().out
