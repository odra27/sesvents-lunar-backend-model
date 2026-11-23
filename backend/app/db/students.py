"""Student profile data access (ADR-051 D2, ADR-056). psycopg 3, parameterized SQL.

* `get_profile` loads the caller's own profile (with optional institution and auth.users email).
* `find_login_targets_by_email` resolves a lowercased email to student accounts plus the
  Supabase Auth email used for the password grant. The caller treats anything other than
  exactly one active match as a credential failure.
* `create_b2c_profile` inserts a self-registered B2C student profile (no NIS or institution
  required; grade and class_code can be filled in Step 2 via `update_profile`).
* `update_profile` updates `full_name`, `grade`, and/or `class_code` for the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection

_COLS = (
    "s.id, s.institution_id, i.name, s.nis, s.full_name, s.class_name, s.grade,"
    " s.avatar_url, s.active, s.account_type, u.email, s.class_code, s.phone"
)
_FROM = (
    "from student_profiles s"
    " left join institutions i on i.id = s.institution_id"
    " left join auth.users u on u.id = s.id"
)

WRITABLE_PROFILE_FIELDS = ("full_name", "grade", "class_code", "phone")


@dataclass(frozen=True)
class StudentProfile:
    id: UUID
    institution_id: UUID | None
    institution_name: str | None
    nis: str | None
    full_name: str
    class_name: str | None
    grade: int | None
    avatar_url: str | None
    active: bool
    account_type: str | None = None
    email: str | None = None
    class_code: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class StudentLoginTarget:
    email: str | None
    profile: StudentProfile


class StudentRepository(Protocol):
    async def get_profile(self, student_id: UUID) -> StudentProfile | None: ...
    async def find_login_targets(self, nis: str) -> list[StudentLoginTarget]: ...
    async def find_login_targets_by_email(self, email: str) -> list[StudentLoginTarget]: ...
    async def create_b2c_profile(self, *, user_id: UUID, full_name: str, phone: str) -> StudentProfile: ...
    async def update_profile(self, student_id: UUID, changes: dict[str, Any]) -> StudentProfile | None: ...


class PostgresStudentRepository:
    """PostgreSQL implementation of StudentRepository using parameterized psycopg 3 queries."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get_profile(self, student_id: UUID) -> StudentProfile | None:
        """Fetch a single student profile joined with optional institution and Auth email."""
        async with self._conn.cursor() as cur:
            await cur.execute(f"select {_COLS} {_FROM} where s.id = %s", (student_id,))
            row = await cur.fetchone()
        return StudentProfile(*row) if row else None

    async def find_login_targets(self, nis: str) -> list[StudentLoginTarget]:
        """Resolve up to two student accounts matching an exact legacy NIS."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"select u.email, {_COLS} {_FROM} where s.nis = %s limit 2",
                (nis,),
            )
            rows = await cur.fetchall()
        return [StudentLoginTarget(email=r[0], profile=StudentProfile(*r[1:])) for r in rows]

    async def find_login_targets_by_email(self, email: str) -> list[StudentLoginTarget]:
        """Resolve up to two student accounts matching a case-insensitive Auth email."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"select u.email, {_COLS} {_FROM} where lower(u.email) = lower(%s) limit 2",
                (email,),
            )
            rows = await cur.fetchall()
        return [StudentLoginTarget(email=r[0], profile=StudentProfile(*r[1:])) for r in rows]

    async def create_b2c_profile(self, *, user_id: UUID, full_name: str, phone: str) -> StudentProfile:
        """Insert a new self-registered B2C student profile tagged as account_type='student'."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                "insert into student_profiles (id, full_name, phone, account_type)"
                " values (%s, %s, %s, 'student')",
                (user_id, full_name, phone),
            )
        profile = await self.get_profile(user_id)
        if profile is None:
            raise RuntimeError("inserted student profile missing")
        return profile

    async def update_profile(self, student_id: UUID, changes: dict[str, Any]) -> StudentProfile | None:
        """Update allowlisted profile fields (`full_name`, `grade`, `class_code`, `phone`)."""
        cols = [c for c in WRITABLE_PROFILE_FIELDS if c in changes]
        if len(cols) != len(changes) or not cols:
            raise ValueError("only writable student profile fields may be updated")
        assignments = ", ".join(f"{c} = %s" for c in cols)
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"update student_profiles set {assignments} where id = %s",
                (*[changes[c] for c in cols], student_id),
            )
            if cur.rowcount != 1:
                return None
        return await self.get_profile(student_id)
