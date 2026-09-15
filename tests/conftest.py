"""Shared fixtures. Postgres tests need DATABASE_URL (Compose default works locally and in CI)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

DEFAULT_URL = "postgresql://sovrag:sovrag@localhost:5432/sovrag"
TEST_DB = "sovrag_test"


def _test_url() -> str:
    base = os.environ.get("DATABASE_URL", DEFAULT_URL)
    return base.rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    base = os.environ.get("DATABASE_URL", DEFAULT_URL)
    admin_url = base.rsplit("/", 1)[0] + "/postgres"
    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute(
                "select 1 from pg_database where datname = %s", (TEST_DB,)
            ).fetchone()
            if not exists:
                conn.execute(f'create database "{TEST_DB}"')
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable at {admin_url}: {exc}")
    return _test_url()


@pytest.fixture
def db(test_database_url: str) -> Iterator[psycopg.Connection]:
    from sovrag.index.store import init_schema

    with psycopg.connect(test_database_url, autocommit=True) as conn:
        init_schema(conn)
        conn.execute("truncate chunks, documents restart identity cascade")
        yield conn
