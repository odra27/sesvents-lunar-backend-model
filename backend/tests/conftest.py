import os

import pytest

from tests.auth_support import fake_clock, jwks_server  # noqa: F401  (pytest fixtures)


@pytest.fixture(scope="session", autouse=True)
def supabase_auth_users_stub():
    """ADR-048 §6: stand-in for Supabase `auth.users` on a plain PostgreSQL test DB.

    Migrations reference `auth.users(id)` (ADR-047). Created only when the test DB has no
    such table; on a real Supabase database nothing is created or changed.
    """
    dsn = os.environ.get("BACKEND_TEST_DATABASE_URL")
    if not dsn:
        yield
        return
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("select to_regclass('auth.users')").fetchone()[0] is None:
            conn.execute("create schema if not exists auth")
            # `email` is read by the staff/student login resolver (ADR-047 D5, ADR-051 D2);
            # `last_sign_in_at` is read by account usage tracking (ADR-054).
            conn.execute(
                "create table if not exists auth.users ("
                "id uuid primary key, email text, last_sign_in_at timestamptz)"
            )
        else:
            conn.execute("alter table auth.users add column if not exists last_sign_in_at timestamptz")
        if conn.execute("select to_regclass('auth.audit_log_entries')").fetchone()[0] is None:
            conn.execute("create schema if not exists auth")
            conn.execute(
                "create table if not exists auth.audit_log_entries ("
                "instance_id uuid, id uuid primary key, payload json, created_at timestamptz, ip_address varchar(64) default '')"
            )
    yield
