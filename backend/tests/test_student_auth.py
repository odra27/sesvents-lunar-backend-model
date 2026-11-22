"""ADR-051 D2, ADR-052, ADR-056: student email login, self-registration, refresh, GET/PATCH /api/student/me."""
from __future__ import annotations

import json
import logging

import pytest

from tests.admin_support import admin_auth  # noqa: F401 (fixture)
from tests.staff_support import PASSWORD, PETUGAS, staff_headers
from tests.student_support import (
    SISWA,
    SISWA_INACTIVE,
    FakeStudentAuthAdmin,
    FakeStudentDb,
    student_client,
    student_gotrue,
    student_headers,
)

INVALID = {"error": {"code": "INVALID_CREDENTIALS", "message": "Invalid credentials"}}


@pytest.fixture
def gotrue(admin_auth):
    return student_gotrue(admin_auth)


@pytest.fixture
def db():
    return FakeStudentDb()


@pytest.fixture
def auth_admin(gotrue, db):
    return FakeStudentAuthAdmin(gotrue, db)


@pytest.fixture
def client(admin_auth, db, gotrue, auth_admin):
    with student_client(
        admin_auth,
        db.open,
        auth_client_factory=gotrue.factory(),
        auth_admin_factory=lambda: auth_admin,
    ) as c:
        yield c


def login(c, email, password=PASSWORD, **extra):
    return c.post("/api/student/auth/login", json={"email": email, "password": password, **extra})


def test_login_with_email(client, gotrue):
    r = login(client, "  Nadia@Siswa.Test ")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["token_type"] == "bearer" and data["expires_in"] == 3600
    assert data["student"] == {
        "id": str(SISWA),
        "email": "nadia@siswa.test",
        "nis": "0012345678",
        "full_name": "Nadia Putri",
        "class_name": "5A",
        "grade": 5,
        "class_code": None,
        "avatar_url": None,
        "institution": {"id": "00000000-0000-0000-0000-00000000b001", "name": "SLB Negeri Contoh"},
    }
    assert json.loads(gotrue.requests[0].content) == {"email": "nadia@siswa.test", "password": PASSWORD}


def test_issued_token_works_for_me(client):
    token = login(client, "nadia@siswa.test").json()["access_token"]
    r = client.get("/api/student/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["email"] == "nadia@siswa.test"
    assert r.headers["cache-control"] == "no-store"


def test_wrong_password(client):
    assert login(client, "nadia@siswa.test", password="salah").json() == INVALID


@pytest.mark.parametrize("email", ["tidakada@siswa.test", "lain@example.com"])
def test_unknown_email_never_calls_supabase(client, gotrue, email):
    r = login(client, email)
    assert r.status_code == 401 and r.json() == INVALID
    assert gotrue.requests == []


def test_inactive_student_never_calls_supabase(client, gotrue):
    assert login(client, "inactive@siswa.test").json() == INVALID
    assert gotrue.requests == []


@pytest.mark.parametrize("role", ["petugas", "admin", "Siswa", None])
def test_account_without_student_role_is_rejected(client, gotrue, role):
    gotrue.accounts["nadia@siswa.test"] = (PASSWORD, SISWA, role) if role else (PASSWORD, SISWA, "x")
    assert login(client, "nadia@siswa.test").json() == INVALID


def test_token_subject_must_match_profile(client, gotrue):
    gotrue.accounts["nadia@siswa.test"] = (PASSWORD, SISWA_INACTIVE, "siswa")
    assert login(client, "nadia@siswa.test").json() == INVALID


def test_missing_auth_config_is_503(admin_auth, db):
    with student_client(admin_auth, db.open) as c:
        r = login(c, "nadia@siswa.test")
    assert r.status_code == 503 and r.json()["error"]["code"] == "AUTH_UNAVAILABLE"


@pytest.mark.parametrize("payload", [
    {},
    {"email": "nadia@siswa.test"},
    {"password": PASSWORD},
    {"email": "  ", "password": PASSWORD},
    {"email": "bad@email", "password": PASSWORD},
    {"nis": "x" * 31, "password": PASSWORD},
    {"email": 12345678, "password": PASSWORD},
    {"email": "nadia@siswa.test", "password": ""},
    {"email": "nadia@siswa.test", "password": PASSWORD, "role": "admin"},
])
def test_validation(client, gotrue, payload):
    r = client.post("/api/student/auth/login", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert gotrue.requests == []


def test_login_backward_compatible_with_nis_field(client):
    """Existing Frontend sends `{nis, password}`; accepts both NIS value and email inside `nis`."""
    r_nis = client.post("/api/student/auth/login", json={"nis": "0012345678", "password": PASSWORD})
    assert r_nis.status_code == 200
    r_email_in_nis = client.post("/api/student/auth/login", json={"nis": "nadia@siswa.test", "password": PASSWORD})
    assert r_email_in_nis.status_code == 200


def test_password_never_echoed_or_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Unique-Student-Pass-5521"
    assert secret not in login(client, "nadia@siswa.test", password=secret).text
    assert secret not in caplog.text


# ---- POST /api/student/auth/register & PATCH /api/student/me (ADR-056) -----------------------

def test_register_b2c_and_complete_profile(client):
    r = client.post(
        "/api/student/auth/register",
        json={"full_name": "  Nadia  ", "email": "Baru@Gmail.com", "password": "Password123"},
    )
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["student"]["full_name"] == "Nadia"
    assert data["student"]["email"] == "baru@gmail.com"
    assert data["student"]["nis"] == "baru@gmail.com"
    assert data["student"]["institution"] == {"id": "00000000-0000-0000-0000-00000000b001", "name": "LUNAR"}
    assert data["student"]["grade"] is None
    assert data["student"]["class_code"] is None

    # Step 2: Lengkapi profil (pilih SD 5 + kode kelas opsional)
    h = {"Authorization": f"Bearer {data['access_token']}"}
    p = client.patch("/api/student/me", headers=h, json={"grade": 5, "class_code": " LNR-5A21 "})
    assert p.status_code == 200, p.text
    assert p.json()["grade"] == 5 and p.json()["class_code"] == "LNR-5A21"

    # Skip / clear class_code
    p2 = client.patch("/api/student/me", headers=h, json={"class_code": "   "})
    assert p2.status_code == 200 and p2.json()["class_code"] is None


def test_register_duplicate_email_409_and_rollback_on_db_failure(client, db, auth_admin):
    dup = client.post(
        "/api/student/auth/register",
        json={"full_name": "Nadia", "email": "nadia@siswa.test", "password": "Password123"},
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "CONFLICT"

    db.students.fail_create = True
    fail = client.post(
        "/api/student/auth/register",
        json={"full_name": "Gagal", "email": "gagal@gmail.com", "password": "Password123"},
    )
    assert fail.status_code == 500
    assert len(auth_admin.deleted) == 1 and auth_admin.deleted[0] == auth_admin.created[-1]


@pytest.mark.parametrize("payload", [
    {},
    {"full_name": " ", "email": "a@b.com", "password": "Password123"},
    {"full_name": "A", "email": "not-email", "password": "Password123"},
    {"full_name": "A", "email": "a@b.com", "password": "short"},  # < 8 chars
    {"full_name": "A", "email": "a@b.com", "password": "Password123", "role": "admin"},
])
def test_register_validation_422(client, payload):
    r = client.post("/api/student/auth/register", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("payload", [
    {},
    {"grade": None},
    {"grade": 0},
    {"grade": 13},
    {"full_name": "   "},
    {"nis": "1234"},  # immutable / forbidden
])
def test_patch_me_validation_422(client, admin_auth, payload):
    r = client.patch("/api/student/me", headers=student_headers(admin_auth, SISWA), json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


# ---- POST /api/student/auth/forgot-password (ADR-056) ----------------------------------------

def test_forgot_password_triggers_recover_for_active_student_without_enumeration(client, gotrue):
    r1 = client.post("/api/student/auth/forgot-password", json={"email": "  Nadia@Siswa.Test "})
    assert r1.status_code == 204 and r1.headers["cache-control"] == "no-store"
    assert len(gotrue.requests) == 1
    assert gotrue.requests[0].url.path.endswith("/recover")
    assert json.loads(gotrue.requests[0].content) == {"email": "nadia@siswa.test"}

    # Unknown or inactive email also returns 204 without calling Supabase (anti-enumeration)
    r2 = client.post("/api/student/auth/forgot-password", json={"email": "unknown@siswa.test"})
    r3 = client.post("/api/student/auth/forgot-password", json={"email": "inactive@siswa.test"})
    assert r2.status_code == 204 and r3.status_code == 204
    assert len(gotrue.requests) == 1


@pytest.mark.parametrize("payload", [{}, {"email": "not-an-email"}, {"email": "a@b.com", "extra": 1}])
def test_forgot_password_validation_422(client, gotrue, payload):
    r = client.post("/api/student/auth/forgot-password", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert gotrue.requests == []


# ---- /api/student/me authorization -----------------------------------------------------------

def test_me_requires_token(client):
    assert client.get("/api/student/me").status_code == 401
    assert client.patch("/api/student/me", json={"grade": 5}).status_code == 401


def test_me_rejects_staff_token(client, admin_auth):
    r = client.get("/api/student/me", headers=staff_headers(admin_auth, PETUGAS))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_me_rejects_inactive_student(client, admin_auth):
    assert client.get("/api/student/me", headers=student_headers(admin_auth, SISWA_INACTIVE)).status_code == 403


def test_student_token_cannot_use_staff_endpoints(client, admin_auth):
    r = client.get("/api/me", headers=student_headers(admin_auth, SISWA))
    assert r.status_code == 403


# ---- POST /api/student/auth/refresh (ADR-052) ----------------------------------------------------

def refresh(c, token):
    return c.post("/api/student/auth/refresh", json={"refresh_token": token})


def test_refresh_returns_new_tokens_and_profile(client, gotrue):
    r = refresh(client, "refresh-nadia@siswa.test")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["student"]["email"] == "nadia@siswa.test" and data["access_token"].count(".") == 2
    req = gotrue.requests[0]
    assert req.url.params["grant_type"] == "refresh_token"
    assert json.loads(req.content) == {"refresh_token": "refresh-nadia@siswa.test"}
    me = client.get("/api/student/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200


def test_refresh_with_revoked_token(client):
    r = refresh(client, "revoked")
    assert r.status_code == 401 and r.json() == INVALID


def test_refresh_rejects_inactive_student(client):
    assert refresh(client, "refresh-inactive@siswa.test").json() == INVALID


def test_refresh_rejects_non_student_role(client, gotrue):
    gotrue.accounts["nadia@siswa.test"] = (PASSWORD, SISWA, "petugas")
    assert refresh(client, "refresh-nadia@siswa.test").json() == INVALID


@pytest.mark.parametrize("status", [500, 503])
def test_refresh_supabase_unavailable(client, gotrue, status):
    gotrue.status = status
    assert refresh(client, "refresh-nadia@siswa.test").status_code == 503


@pytest.mark.parametrize("payload", [{}, {"refresh_token": ""}, {"refresh_token": 5}, {"refresh_token": "x", "email": "a@b.c"}])
def test_refresh_validation(client, gotrue, payload):
    r = client.post("/api/student/auth/refresh", json=payload)
    assert r.status_code == 422
    assert gotrue.requests == []
