"""Login session tracking for Demo & Judge student accounts (ADR-055). psycopg 3, parameterized SQL.

Only students identified as 'demo' or 'judge' (via `student_profiles.account_type` or backend env
`DEMO_STUDENT_NIS` / `JUDGE_STUDENT_NIS`) are recorded. Regular student accounts are untouched.

Anonymous Visitor Identification:
* Visitors using the shared Demo or Judge accounts are distinguished by an anonymous UUID
  (`visitor_id`), persisted in the browser via the `lunar_visitor_id` cookie (automatic, no FE
  change required) or passed explicitly by the client via `visitor_id` body field / `X-Visitor-Id`
  header (from `localStorage`).
* Non-UUID values are rejected/ignored and replaced with a fresh server-generated UUID v4.
* `visitor_id` is never used for authentication or authorization.

Security & privacy:
* Never stores passwords, access tokens, refresh tokens, browser fingerprints, or IP addresses.
* `auth_session_id` is read only from the verified Supabase JWT `session_id` claim when present.
* All writes are called best-effort so tracking never disrupts authentication.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import Request
from psycopg import AsyncConnection

from app.db.students import StudentProfile

AccountType = Literal["demo", "judge"]

# Sessions without logout and without activity beyond this window are treated as expired.
INACTIVITY_TIMEOUT_MINUTES = 30
# Minimum interval between automatic background activity touches (keeps DB writes lightweight).
ACTIVITY_TOUCH_INTERVAL_S = 60

VISITOR_COOKIE_NAME = "lunar_visitor_id"
VISITOR_HEADER_NAME = "x-visitor-id"
VISITOR_COOKIE_MAX_AGE_S = 365 * 24 * 3600  # 1 year


def _parse_env_nis_set(env_var: str) -> set[str]:
    raw = os.environ.get(env_var, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def resolve_account_type(profile: StudentProfile) -> AccountType | None:
    """Resolve whether a student profile is a tracked 'demo' or 'judge' account."""
    if profile.account_type in ("demo", "judge"):
        return profile.account_type  # type: ignore[return-value]
    if profile.nis in _parse_env_nis_set("JUDGE_STUDENT_NIS"):
        return "judge"
    if profile.nis in _parse_env_nis_set("DEMO_STUDENT_NIS"):
        return "demo"
    return None


def jwt_session_id(claims: dict[str, Any]) -> str | None:
    """Extract Supabase Auth's `session_id` claim from verified JWT claims, if present."""
    sid = claims.get("session_id")
    if isinstance(sid, str):
        sid = sid.strip()
        if 1 <= len(sid) <= 128:
            return sid
    return None


def _parse_uuid(raw: str | None) -> UUID | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > 64:
        return None
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


def extract_visitor_id(request: Request, body_visitor_id: UUID | None = None) -> UUID | None:
    """Read an existing valid visitor UUID from body, `X-Visitor-Id` header, or `lunar_visitor_id` cookie."""
    if body_visitor_id is not None:
        return body_visitor_id
    from_header = _parse_uuid(request.headers.get(VISITOR_HEADER_NAME))
    if from_header is not None:
        return from_header
    return _parse_uuid(request.cookies.get(VISITOR_COOKIE_NAME))


def resolve_or_create_visitor_id(request: Request, body_visitor_id: UUID | None = None) -> UUID:
    """Return the visitor's existing UUID or generate a fresh UUID v4 if none/invalid."""
    existing = extract_visitor_id(request, body_visitor_id)
    return existing if existing is not None else uuid4()


@dataclass(frozen=True)
class LoginSessionRecord:
    session_id: UUID
    user_id: UUID
    account_type: AccountType
    auth_session_id: str | None
    visitor_id: UUID | None
    login_at: datetime
    last_activity_at: datetime
    logout_at: datetime | None
    session_duration_seconds: int


class LoginSessionRepository(Protocol):
    async def create_session(
        self,
        *,
        user_id: UUID,
        account_type: AccountType,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
    ) -> UUID: ...

    async def touch_activity(
        self,
        *,
        user_id: UUID,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
        min_interval_seconds: int = 0,
        inactivity_minutes: int = INACTIVITY_TIMEOUT_MINUTES,
    ) -> bool: ...

    async def end_session(
        self,
        *,
        user_id: UUID,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
        inactivity_minutes: int = INACTIVITY_TIMEOUT_MINUTES,
    ) -> bool: ...


class PostgresLoginSessionRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def create_session(
        self,
        *,
        user_id: UUID,
        account_type: AccountType,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
    ) -> UUID:
        async with self._conn.cursor() as cur:
            await cur.execute(
                "insert into user_login_sessions (user_id, account_type, auth_session_id, visitor_id)"
                " values (%s, %s, %s, %s)"
                " returning session_id",
                (user_id, account_type, auth_session_id, visitor_id),
            )
            return (await cur.fetchone())[0]

    async def touch_activity(
        self,
        *,
        user_id: UUID,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
        min_interval_seconds: int = 0,
        inactivity_minutes: int = INACTIVITY_TIMEOUT_MINUTES,
    ) -> bool:
        async with self._conn.cursor() as cur:
            if auth_session_id is not None:
                await cur.execute(
                    "with target as ("
                    "  select session_id from user_login_sessions"
                    "  where user_id = %s and auth_session_id = %s"
                    "    and logout_at is null"
                    "    and last_activity_at >= now() - make_interval(mins => %s)"
                    "  order by login_at desc limit 1"
                    ")"
                    " update user_login_sessions uls"
                    " set last_activity_at = now(),"
                    "     session_duration_seconds = greatest(0, extract(epoch from (now() - uls.login_at))::integer)"
                    " from target"
                    " where uls.session_id = target.session_id"
                    "   and (%s <= 0 or uls.last_activity_at <= now() - make_interval(secs => %s))",
                    (user_id, auth_session_id, inactivity_minutes, min_interval_seconds, min_interval_seconds),
                )
                if cur.rowcount == 1:
                    return True
            if visitor_id is not None:
                await cur.execute(
                    "with target as ("
                    "  select session_id from user_login_sessions"
                    "  where user_id = %s and visitor_id = %s"
                    "    and logout_at is null"
                    "    and last_activity_at >= now() - make_interval(mins => %s)"
                    "  order by login_at desc limit 1"
                    ")"
                    " update user_login_sessions uls"
                    " set last_activity_at = now(),"
                    "     session_duration_seconds = greatest(0, extract(epoch from (now() - uls.login_at))::integer)"
                    " from target"
                    " where uls.session_id = target.session_id"
                    "   and (%s <= 0 or uls.last_activity_at <= now() - make_interval(secs => %s))",
                    (user_id, visitor_id, inactivity_minutes, min_interval_seconds, min_interval_seconds),
                )
                if cur.rowcount == 1:
                    return True
            await cur.execute(
                "with target as ("
                "  select session_id from user_login_sessions"
                "  where user_id = %s and logout_at is null"
                "    and last_activity_at >= now() - make_interval(mins => %s)"
                "  order by login_at desc limit 1"
                ")"
                " update user_login_sessions uls"
                " set last_activity_at = now(),"
                "     session_duration_seconds = greatest(0, extract(epoch from (now() - uls.login_at))::integer)"
                " from target"
                " where uls.session_id = target.session_id"
                "   and (%s <= 0 or uls.last_activity_at <= now() - make_interval(secs => %s))",
                (user_id, inactivity_minutes, min_interval_seconds, min_interval_seconds),
            )
            return cur.rowcount == 1

    async def end_session(
        self,
        *,
        user_id: UUID,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
        inactivity_minutes: int = INACTIVITY_TIMEOUT_MINUTES,
    ) -> bool:
        async with self._conn.cursor() as cur:
            if auth_session_id is not None:
                await cur.execute(
                    "with target as ("
                    "  select session_id from user_login_sessions"
                    "  where user_id = %s and auth_session_id = %s"
                    "    and logout_at is null"
                    "  order by login_at desc limit 1"
                    ")"
                    " update user_login_sessions uls"
                    " set logout_at = now(),"
                    "     last_activity_at = now(),"
                    "     session_duration_seconds = greatest(0, extract(epoch from (now() - uls.login_at))::integer)"
                    " from target"
                    " where uls.session_id = target.session_id",
                    (user_id, auth_session_id),
                )
                if cur.rowcount == 1:
                    return True
            if visitor_id is not None:
                await cur.execute(
                    "with target as ("
                    "  select session_id from user_login_sessions"
                    "  where user_id = %s and visitor_id = %s"
                    "    and logout_at is null"
                    "    and last_activity_at >= now() - make_interval(mins => %s)"
                    "  order by login_at desc limit 1"
                    ")"
                    " update user_login_sessions uls"
                    " set logout_at = now(),"
                    "     last_activity_at = now(),"
                    "     session_duration_seconds = greatest(0, extract(epoch from (now() - uls.login_at))::integer)"
                    " from target"
                    " where uls.session_id = target.session_id",
                    (user_id, visitor_id, inactivity_minutes),
                )
                if cur.rowcount == 1:
                    return True
            await cur.execute(
                "with target as ("
                "  select session_id from user_login_sessions"
                "  where user_id = %s and logout_at is null"
                "    and last_activity_at >= now() - make_interval(mins => %s)"
                "  order by login_at desc limit 1"
                ")"
                " update user_login_sessions uls"
                " set logout_at = now(),"
                "     last_activity_at = now(),"
                "     session_duration_seconds = greatest(0, extract(epoch from (now() - uls.login_at))::integer)"
                " from target"
                " where uls.session_id = target.session_id",
                (user_id, inactivity_minutes),
            )
            return cur.rowcount == 1
