"""Student authentication, self-registration, session tracking, and profile endpoints.

Routes (ADR-051 D2, ADR-052, ADR-055, ADR-056, ADR-057, ADR-058):
    POST /api/student/auth/login
        {email, password, visitor_id?} → verify Supabase Auth password grant + `siswa` role
        → record session in `user_login_sessions` → 200 {access_token, refresh_token, student}.
    POST /api/student/auth/register
        {full_name, email, phone (^0[0-9]{7,14}$), password (min 8), visitor_id?}
        → create Supabase Auth user (role `siswa`) + B2C `student_profiles` row (with rollback)
        → sign in and record session → 201 {access_token, refresh_token, student}.
    POST /api/student/auth/forgot-password
        {email} → trigger Supabase recovery email for active students; always returns 204
        (anti-enumeration).
    POST /api/student/auth/refresh
        {refresh_token} → rotate token pair, re-verify `siswa` role + active profile, touch session.
    POST /api/student/auth/logout & POST /api/student/session/heartbeat
        `require_student` → close or touch the caller's login session (best-effort) → 204.
    GET /api/student/me & PATCH /api/student/me
        `require_student` → read or update own profile (`full_name`, `grade`, `class_code`, `phone`).

Security:
    All auth responses carry `Cache-Control: no-store`. Passwords and tokens are never logged.
"""
from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from app.api.errors import VALIDATION_ERROR_STATUS
from app.auth.staff import subject_uuid
from app.auth.student import StudentPrincipal, is_student, require_student
from app.db.login_sessions import (
    VISITOR_COOKIE_MAX_AGE_S,
    VISITOR_COOKIE_NAME,
    extract_visitor_id,
    jwt_session_id,
    resolve_account_type,
    resolve_or_create_visitor_id,
)
from app.db.students import StudentProfile
from app.errors import AuthUnavailable, BackendError, Conflict, Forbidden, InvalidCredentials, Unauthorized
from app.provisioning.supabase_admin import WeakPassword

router = APIRouter()

_INVALID = "Invalid credentials"
_UNAVAILABLE = "Authentication service unavailable"
_CONFLICT = "Email is already registered"
_NO_STORE = {"Cache-Control": "no-store"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^0[0-9]{7,14}$")


class _ValidationRejected(BackendError):
    code = "VALIDATION_ERROR"
    http_status = VALIDATION_ERROR_STATUS


def _normalize_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("email must be a valid email address")
    return v


def _validate_phone(v: str) -> str:
    if not _PHONE_RE.match(v):
        raise ValueError("phone must start with 0 and contain 8 to 15 digits only (no spaces or dashes)")
    return v


_DEFAULT_INSTITUTION_ID = UUID("00000000-0000-0000-0000-00000000b001")
_DEFAULT_INSTITUTION_NAME = "LUNAR"


class StudentLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=320)
    password: StrictStr = Field(min_length=1, max_length=1024)
    visitor_id: UUID | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)


class StudentRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(min_length=3, max_length=320)
    phone: StrictStr = Field(min_length=8, max_length=15)
    password: StrictStr = Field(min_length=8, max_length=1024)
    visitor_id: UUID | None = None

    @field_validator("full_name")
    @classmethod
    def _full_name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("full_name must not be blank")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return _validate_phone(v)


class StudentProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    phone: StrictStr | None = Field(default=None, min_length=8, max_length=15)

    @model_validator(mode="after")
    def _check_fields(self) -> "StudentProfilePatch":
        if not self.model_fields_set:
            raise ValueError("at least one of full_name, phone is required")
        if "full_name" in self.model_fields_set:
            if self.full_name is None or not self.full_name.strip():
                raise ValueError("full_name must not be null or blank")
            self.full_name = " ".join(self.full_name.split())
        if "phone" in self.model_fields_set:
            if self.phone is None:
                raise ValueError("phone must not be null")
            self.phone = _validate_phone(self.phone)
        return self

    def changes(self) -> dict:
        return {name: getattr(self, name) for name in self.model_fields_set}


class InstitutionOut(BaseModel):
    id: UUID
    name: str


class StudentOut(BaseModel):
    id: UUID
    email: str | None = None
    nis: str | None = None
    full_name: str
    phone: str | None = None
    class_name: str | None = None
    grade: int | None = None
    class_code: str | None = None
    avatar_url: str | None = None
    institution: InstitutionOut | None = None


class StudentLoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str
    student: StudentOut


def student_out(p: StudentProfile) -> StudentOut:
    """Map a database StudentProfile record to the public StudentOut API schema."""
    institution = (
        InstitutionOut(id=p.institution_id, name=p.institution_name)
        if p.institution_id is not None and p.institution_name is not None
        else InstitutionOut(id=_DEFAULT_INSTITUTION_ID, name=_DEFAULT_INSTITUTION_NAME)
    )
    return StudentOut(
        id=p.id,
        email=p.email,
        nis=p.nis if p.nis is not None else (p.email or ""),
        full_name=p.full_name,
        phone=p.phone,
        class_name=p.class_name,
        grade=p.grade,
        class_code=p.class_code,
        avatar_url=p.avatar_url,
        institution=institution,
    )


async def _record_login_session(
    request: Request,
    response: Response,
    profile: StudentProfile,
    claims: dict,
    body_visitor_id: UUID | None,
) -> None:
    account_type = resolve_account_type(profile)
    visitor_id = resolve_or_create_visitor_id(request, body_visitor_id)
    try:
        async with request.app.state.open_db() as db:
            if db.login_sessions is not None:
                await db.login_sessions.create_session(
                    user_id=profile.id,
                    account_type=account_type,
                    auth_session_id=jwt_session_id(claims),
                    visitor_id=visitor_id,
                )
    except Exception:
        pass
    response.set_cookie(
        key=VISITOR_COOKIE_NAME,
        value=str(visitor_id),
        max_age=VISITOR_COOKIE_MAX_AGE_S,
        path="/",
        httponly=True,
        samesite="lax",
    )


@router.post("/api/student/auth/login", response_model=StudentLoginResponse)
async def student_login(payload: StudentLoginRequest, request: Request, response: Response) -> StudentLoginResponse:
    """Authenticate an active student via email + password and record a login session."""
    auth_client = getattr(request.app.state, "auth_client", None)
    verifier = getattr(request.app.state, "jwt_verifier", None)
    if auth_client is None or verifier is None:
        raise AuthUnavailable(_UNAVAILABLE)

    async with request.app.state.open_db() as db:
        if db.students is None:
            raise RuntimeError("student repository missing")
        targets = await db.students.find_login_targets_by_email(payload.email)
    if len(targets) != 1:
        raise InvalidCredentials(_INVALID)
    target = targets[0]
    if target.profile.active is not True or not target.email:
        raise InvalidCredentials(_INVALID)

    grant = await auth_client.password_grant(target.email, payload.password)

    try:
        claims = await run_in_threadpool(verifier.verify, grant.access_token)
    except Unauthorized:
        # Supabase issued a token our verifier rejects: configuration problem, not the user's.
        raise AuthUnavailable(_UNAVAILABLE) from None
    if not is_student(claims) or subject_uuid(claims) != target.profile.id:
        raise InvalidCredentials(_INVALID)

    await _record_login_session(request, response, target.profile, claims, payload.visitor_id)

    response.headers.update(_NO_STORE)
    return StudentLoginResponse(
        access_token=grant.access_token,
        expires_in=grant.expires_in,
        refresh_token=grant.refresh_token,
        student=student_out(target.profile),
    )


@router.post("/api/student/auth/register", response_model=StudentLoginResponse, status_code=201)
async def student_register(
    payload: StudentRegisterRequest, request: Request, response: Response
) -> StudentLoginResponse:
    """B2C student self-registration (ADR-056): creates Supabase Auth user + student_profiles row,
    then signs the student in immediately so the FE can continue to Step 2 (Lengkapi profil)."""
    auth_admin = getattr(request.app.state, "auth_admin", None)
    auth_client = getattr(request.app.state, "auth_client", None)
    verifier = getattr(request.app.state, "jwt_verifier", None)
    if auth_admin is None or auth_client is None or verifier is None:
        raise AuthUnavailable(_UNAVAILABLE)

    try:
        user_id = await auth_admin.create_user(
            email=payload.email, password=payload.password, role="siswa"
        )
    except WeakPassword:
        raise _ValidationRejected("Invalid request. password: does not meet password policy") from None
    except Conflict:
        raise Conflict(_CONFLICT) from None

    try:
        async with request.app.state.open_db() as db:
            if db.students is None:
                raise RuntimeError("student repository missing")
            profile = await db.students.create_b2c_profile(
                user_id=user_id, full_name=payload.full_name, phone=payload.phone
            )
    except Exception as exc:
        try:
            await auth_admin.delete_user(user_id)
        except Exception:
            pass
        if isinstance(exc, UniqueViolation):
            raise Conflict(_CONFLICT) from None
        raise

    grant = await auth_client.password_grant(payload.email, payload.password)
    try:
        claims = await run_in_threadpool(verifier.verify, grant.access_token)
    except Unauthorized:
        raise AuthUnavailable(_UNAVAILABLE) from None
    if not is_student(claims) or subject_uuid(claims) != profile.id:
        raise AuthUnavailable(_UNAVAILABLE)

    await _record_login_session(request, response, profile, claims, payload.visitor_id)

    response.headers.update(_NO_STORE)
    return StudentLoginResponse(
        access_token=grant.access_token,
        expires_in=grant.expires_in,
        refresh_token=grant.refresh_token,
        student=student_out(profile),
    )


class StudentRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: StrictStr = Field(min_length=1, max_length=4096)


class StudentForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)


@router.post("/api/student/auth/forgot-password", status_code=204, response_class=Response)
async def student_forgot_password(
    payload: StudentForgotPasswordRequest, request: Request
) -> Response:
    """Trigger password recovery email via Supabase Auth (ADR-056).

    Anti-enumeration: always returns 204 No Content regardless of whether the email exists
    or is active in `student_profiles`. Only calls Supabase `/recover` when an active student
    profile matches.
    """
    auth_client = getattr(request.app.state, "auth_client", None)
    if auth_client is None:
        raise AuthUnavailable(_UNAVAILABLE)

    async with request.app.state.open_db() as db:
        if db.students is None:
            raise RuntimeError("student repository missing")
        targets = await db.students.find_login_targets_by_email(payload.email)
    if len(targets) == 1 and targets[0].profile.active is True and targets[0].email:
        await auth_client.recover_password(targets[0].email)
    return Response(status_code=204, headers=_NO_STORE)


@router.post("/api/student/auth/refresh", response_model=StudentLoginResponse)
async def student_refresh(payload: StudentRefreshRequest, request: Request, response: Response) -> StudentLoginResponse:
    """Exchange a refresh token for a new token pair (ADR-052), so a student who already logged in
    on this device goes straight in. The new token must still be a `siswa` token of an active
    profile; anything else → 401 INVALID_CREDENTIALS (the FE then shows the login form)."""
    auth_client = getattr(request.app.state, "auth_client", None)
    verifier = getattr(request.app.state, "jwt_verifier", None)
    if auth_client is None or verifier is None:
        raise AuthUnavailable(_UNAVAILABLE)

    grant = await auth_client.refresh_grant(payload.refresh_token)
    try:
        claims = await run_in_threadpool(verifier.verify, grant.access_token)
    except Unauthorized:
        raise AuthUnavailable(_UNAVAILABLE) from None
    user_id = subject_uuid(claims)
    if not is_student(claims) or user_id is None:
        raise InvalidCredentials(_INVALID)
    async with request.app.state.open_db() as db:
        if db.students is None:
            raise RuntimeError("student repository missing")
        profile = await db.students.get_profile(user_id)
        if profile is None or profile.active is not True:
            raise InvalidCredentials(_INVALID)
        # ADR-055 / ADR-057: token refresh counts as session activity for all student accounts (best-effort).
        if db.login_sessions is not None:
            try:
                await db.login_sessions.touch_activity(
                    user_id=user_id,
                    auth_session_id=jwt_session_id(claims),
                    visitor_id=extract_visitor_id(request),
                    min_interval_seconds=0,
                )
            except Exception:
                pass

    response.headers.update(_NO_STORE)
    return StudentLoginResponse(
        access_token=grant.access_token,
        expires_in=grant.expires_in,
        refresh_token=grant.refresh_token,
        student=student_out(profile),
    )


@router.post("/api/student/auth/logout", status_code=204, response_class=Response)
async def student_logout(
    request: Request, principal: StudentPrincipal = Depends(require_student)
) -> Response:
    """Record explicit logout for student accounts (ADR-055 / ADR-057)."""
    try:
        async with request.app.state.open_db() as db:
            if db.login_sessions is not None:
                await db.login_sessions.end_session(
                    user_id=principal.user_id,
                    auth_session_id=principal.auth_session_id,
                    visitor_id=principal.visitor_id,
                )
    except Exception:
        pass
    return Response(status_code=204, headers=_NO_STORE)


@router.post("/api/student/session/heartbeat", status_code=204, response_class=Response)
async def student_session_heartbeat(
    request: Request, principal: StudentPrincipal = Depends(require_student)
) -> Response:
    """Lightweight heartbeat to update LastActivityAt for student sessions (ADR-055 / ADR-057)."""
    try:
        async with request.app.state.open_db() as db:
            if db.login_sessions is not None:
                await db.login_sessions.touch_activity(
                    user_id=principal.user_id,
                    auth_session_id=principal.auth_session_id,
                    visitor_id=principal.visitor_id,
                    min_interval_seconds=0,
                )
    except Exception:
        pass
    return Response(status_code=204, headers=_NO_STORE)


@router.get("/api/student/me", response_model=StudentOut)
async def student_me(response: Response, principal: StudentPrincipal = Depends(require_student)) -> StudentOut:
    """Return the authenticated student's own profile."""
    response.headers.update(_NO_STORE)
    return student_out(principal.profile)


@router.patch("/api/student/me", response_model=StudentOut)
async def update_student_me(
    payload: StudentProfilePatch,
    request: Request,
    response: Response,
    principal: StudentPrincipal = Depends(require_student),
) -> StudentOut:
    """Update own student profile (`full_name`, `phone`)."""
    async with request.app.state.open_db() as db:
        if db.students is None:
            raise RuntimeError("student repository missing")
        updated = await db.students.update_profile(principal.user_id, payload.changes())
    if updated is None:
        raise Forbidden("Forbidden")
    response.headers.update(_NO_STORE)
    return student_out(updated)
