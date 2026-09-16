"""Smoke test for the warehouse lane itself: DSN, scratch schema fixture, dbt-postgres importable."""

import pytest

pytestmark = pytest.mark.warehouse


def test_scratch_schema_exists_and_is_empty(pg_dsn, scratch_schema):
    import psycopg2

    with psycopg2.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from information_schema.tables where table_schema = %s", (scratch_schema,))
        assert cur.fetchone() == (0,)
        cur.execute("select 1 from information_schema.schemata where schema_name = %s", (scratch_schema,))
        assert cur.fetchone() == (1,)


def test_dbt_postgres_is_installed():
    import dbt.adapters.postgres  # noqa: F401
