import json

import pytest

from agent.tools.data_query import query_gold_table

REPO_ROOT_SEED_SQL = "data/seed/gold_seed.sql"


@pytest.fixture
def duckdb_path(tmp_path):
    return tmp_path / "gold_test.duckdb"


def test_query_gold_table_returns_seeded_rows(duckdb_path):
    result = query_gold_table("dim_patients", db_path=duckdb_path, seed_sql_path=REPO_ROOT_SEED_SQL)
    rows = json.loads(result.content)
    assert len(rows) == 3
    patient_ids = {row["patient_id"] for row in rows}
    assert "pt_00001" in patient_ids


def test_query_gold_table_respects_filters(duckdb_path):
    result = query_gold_table(
        "dim_patients", filters={"patient_id": "pt_00002"}, db_path=duckdb_path, seed_sql_path=REPO_ROOT_SEED_SQL
    )
    rows = json.loads(result.content)
    assert len(rows) == 1
    assert rows[0]["patient_id"] == "pt_00002"


def test_query_gold_table_rejects_unknown_table(duckdb_path):
    result = query_gold_table("not_a_real_table", db_path=duckdb_path, seed_sql_path=REPO_ROOT_SEED_SQL)
    assert result.is_error is True


def test_query_gold_table_creates_duckdb_file_if_missing(duckdb_path):
    assert not duckdb_path.exists()
    query_gold_table("dim_providers", db_path=duckdb_path, seed_sql_path=REPO_ROOT_SEED_SQL)
    assert duckdb_path.exists()
