"""ADR-055: Login Activity Tracking for Demo & Judge student accounts."""
from __future__ import annotations

from uuid import UUID

import pytest

from tests.admin_support import admin_auth  # noqa: F401 (fixture)
from tests.staff_support import PASSWORD, PETUGAS, staff_headers
from tests.student_support import (
    SISWA,
    SISWA_2,
    FakeStudentDb,
    student,
    student_client,
    student_gotrue,
    student_headers,
)

DEMO_ID = UUID("00000000-0000-0000-0000-00000000d300")
JUDGE_ID = UUID("00000000-0000-0000-0000-00000000d301")


@pytest.fixture
def tracked_env(admin_auth, monkeypatch):
    monkeypatch.delenv("DEMO_STUDENT_NIS", raising=False)
    monkeypatch.delenv("JUDGE_STUDENT_NIS", raising=False)
    db = FakeStudentDb()
    db.students.rows[DEMO_ID] = (
        "demo@siswa.test",
        student(DEMO_ID, nis="DEMO01", full_name="Akun Demo", account_type="demo"),
    )
    db.students.rows[JUDGE_ID] = (
        "judge@siswa.test",
        student(JUDGE_ID, nis="JUDGE01", full_name="Akun Juri", account_type="judge"),
    )
    gotrue = student_gotrue(admin_auth)
    gotrue.accounts["demo@siswa.test"] = (PASSWORD, DEMO_ID, "siswa")
    gotrue.accounts["judge@siswa.test"] = (PASSWORD, JUDGE_ID, "siswa")
    with student_client(admin_auth, db.open, auth_client_factory=gotrue.factory()) as c:
        yield c, db, admin_auth


def test_regular_student_is_never_tracked(tracked_env):
    c, db, auth = tracked_env
    r = c.post("/api/student/auth/login", json={"nis": "0012345678", "password": PASSWORD})
    assert r.status_code == 200
    h = student_headers(auth, SISWA)
    assert c.get("/api/student/me", headers=h).status_code == 200
    assert c.post("/api/student/session/heartbeat", headers=h).status_code == 204
    assert c.post("/api/student/auth/logout", headers=h).status_code == 204
    assert db.login_sessions.sessions == []
    assert db.login_sessions.calls == []


def test_demo_and_judge_logins_create_distinct_sessions_without_overwriting(tracked_env):
    c, db, _ = tracked_env
    r1 = c.post("/api/student/auth/login", json={"nis": "DEMO01", "password": PASSWORD})
    assert r1.status_code == 200
    # Login response contract stays unchanged (no extra fields).
    assert set(r1.json()) == {"access_token", "token_type", "expires_in", "refresh_token", "student"}
    db.login_sessions.advance(120)
    r2 = c.post("/api/student/auth/login", json={"nis": "DEMO01", "password": PASSWORD})
    assert r2.status_code == 200
    r3 = c.post("/api/student/auth/login", json={"nis": "JUDGE01", "password": PASSWORD})
    assert r3.status_code == 200

    assert len(db.login_sessions.sessions) == 3
    assert [s["account_type"] for s in db.login_sessions.sessions] == ["demo", "demo", "judge"]
    assert [s["user_id"] for s in db.login_sessions.sessions] == [DEMO_ID, DEMO_ID, JUDGE_ID]
    # Each login gets its own unique session_id.
    assert len({s["session_id"] for s in db.login_sessions.sessions}) == 3
    # No password or token is ever stored in session records.
    raw = str(db.login_sessions.sessions)
    assert PASSWORD not in raw and r1.json()["access_token"] not in raw and r1.json()["refresh_token"] not in raw


def test_env_fallback_identifies_demo_and_judge_without_db_column(tracked_env, monkeypatch):
    c, db, _ = tracked_env
    monkeypatch.setenv("DEMO_STUDENT_NIS", "0012345678")
    monkeypatch.setenv("JUDGE_STUDENT_NIS", "0099999999")
    assert c.post("/api/student/auth/login", json={"nis": "0012345678", "password": PASSWORD}).status_code == 200
    assert c.post("/api/student/auth/login", json={"nis": "0099999999", "password": PASSWORD}).status_code == 200
    assert [(s["user_id"], s["account_type"]) for s in db.login_sessions.sessions] == [
        (SISWA, "demo"),
        (SISWA_2, "judge"),
    ]


def test_activity_heartbeat_refresh_and_logout_lifecycle(tracked_env):
    c, db, auth = tracked_env
    assert c.post("/api/student/auth/login", json={"nis": "JUDGE01", "password": PASSWORD}).status_code == 200
    h = student_headers(auth, JUDGE_ID)

    # Background touch on authenticated endpoint (/me) after >= 60s
    db.login_sessions.advance(90)
    assert c.get("/api/student/me", headers=h).status_code == 200
    assert db.login_sessions.sessions[0]["session_duration_seconds"] == 90

    # Explicit lightweight heartbeat updates LastActivityAt immediately
    db.login_sessions.advance(30)
    hb = c.post("/api/student/session/heartbeat", headers=h)
    assert hb.status_code == 204 and hb.headers["cache-control"] == "no-store"
    assert db.login_sessions.sessions[0]["session_duration_seconds"] == 120

    # Token refresh also refreshes activity
    db.login_sessions.advance(60)
    assert c.post("/api/student/auth/refresh", json={"refresh_token": "refresh-judge@siswa.test"}).status_code == 200
    assert db.login_sessions.sessions[0]["session_duration_seconds"] == 180

    # Explicit logout ends the session and records logout_at + duration
    db.login_sessions.advance(45)
    lo = c.post("/api/student/auth/logout", headers=h)
    assert lo.status_code == 204 and lo.headers["cache-control"] == "no-store"
    s = db.login_sessions.sessions[0]
    assert s["logout_at"] is not None
    assert s["session_duration_seconds"] == 225


def test_heartbeat_and_logout_require_student_jwt_and_prevent_spoofing(tracked_env):
    c, db, auth = tracked_env
    for path in ("/api/student/session/heartbeat", "/api/student/auth/logout"):
        assert c.post(path).status_code == 401
        assert c.post(path, headers=staff_headers(auth, PETUGAS)).status_code == 403

    # Judge logs in; Demo calling logout cannot close Judge's session
    assert c.post("/api/student/auth/login", json={"nis": "JUDGE01", "password": PASSWORD}).status_code == 200
    assert c.post("/api/student/auth/logout", headers=student_headers(auth, DEMO_ID)).status_code == 204
    assert db.login_sessions.sessions[0]["logout_at"] is None


def test_tracking_failure_never_breaks_auth_flow(tracked_env):
    c, db, auth = tracked_env
    db.login_sessions.fail = True
    assert c.post("/api/student/auth/login", json={"nis": "DEMO01", "password": PASSWORD}).status_code == 200
    h = student_headers(auth, DEMO_ID)
    assert c.get("/api/student/me", headers=h).status_code == 200
    assert c.post("/api/student/auth/refresh", json={"refresh_token": "refresh-demo@siswa.test"}).status_code == 200
    assert c.post("/api/student/session/heartbeat", headers=h).status_code == 204
    assert c.post("/api/student/auth/logout", headers=h).status_code == 204


def test_anonymous_visitor_id_automatic_cookie_and_reuse_across_logins(tracked_env):
    """Automatic visitor identification via cookie without requiring any Frontend change."""
    c, db, _ = tracked_env
    c.cookies.clear()
    r1 = c.post("/api/student/auth/login", json={"nis": "DEMO01", "password": PASSWORD})
    assert r1.status_code == 200
    cookie_vid = r1.cookies.get("lunar_visitor_id")
    assert cookie_vid is not None
    vid_1 = UUID(cookie_vid)
    assert db.login_sessions.sessions[0]["visitor_id"] == vid_1

    # Same browser logs in again -> reuses existing lunar_visitor_id cookie, creates new session_id
    r2 = c.post("/api/student/auth/login", json={"nis": "DEMO01", "password": PASSWORD})
    assert r2.status_code == 200
    assert db.login_sessions.sessions[1]["visitor_id"] == vid_1
    assert db.login_sessions.sessions[1]["session_id"] != db.login_sessions.sessions[0]["session_id"]

    # Another visitor (different browser / cleared cookie) gets a different visitor_id
    c.cookies.clear()
    r3 = c.post("/api/student/auth/login", json={"nis": "DEMO01", "password": PASSWORD})
    assert r3.status_code == 200
    vid_2 = db.login_sessions.sessions[2]["visitor_id"]
    assert vid_2 is not None and vid_2 != vid_1


def test_anonymous_visitor_id_from_body_or_header_and_concurrent_visitor_isolation(tracked_env):
    """Explicit visitor_id from localStorage (body or X-Visitor-Id header) + concurrent visitors."""
    c, db, auth = tracked_env
    c.cookies.clear()
    v_alice = UUID("11111111-1111-4111-8111-111111111111")
    v_bob = UUID("22222222-2222-4222-8222-222222222222")

    # Alice logs in sending visitor_id in JSON body
    assert c.post(
        "/api/student/auth/login",
        json={"nis": "DEMO01", "password": PASSWORD, "visitor_id": str(v_alice)},
    ).status_code == 200

    # Bob logs in to the SAME Demo account sending X-Visitor-Id header
    c.cookies.clear()
    assert c.post(
        "/api/student/auth/login",
        json={"nis": "DEMO01", "password": PASSWORD},
        headers={"X-Visitor-Id": str(v_bob)},
    ).status_code == 200

    assert [s["visitor_id"] for s in db.login_sessions.sessions] == [v_alice, v_bob]

    # Alice logs out (identified by her X-Visitor-Id); Bob's session remains active!
    h_demo = {**student_headers(auth, DEMO_ID), "X-Visitor-Id": str(v_alice)}
    c.cookies.clear()
    assert c.post("/api/student/auth/logout", headers=h_demo).status_code == 204
    assert db.login_sessions.sessions[0]["logout_at"] is not None
    assert db.login_sessions.sessions[1]["logout_at"] is None


def test_malformed_visitor_id_in_header_or_cookie_is_replaced_and_body_validated(tracked_env):
    c, db, _ = tracked_env
    # Malformed visitor_id in body is rejected by Pydantic UUID validation (422)
    bad = c.post(
        "/api/student/auth/login",
        json={"nis": "DEMO01", "password": PASSWORD, "visitor_id": "not-a-uuid'; drop table--"},
    )
    assert bad.status_code == 422

    # Malformed visitor_id in header/cookie is safely ignored and replaced with a fresh UUID
    c.cookies.set("lunar_visitor_id", "malicious-cookie-value")
    ok = c.post(
        "/api/student/auth/login",
        json={"nis": "DEMO01", "password": PASSWORD},
        headers={"X-Visitor-Id": "not-a-uuid"},
    )
    assert ok.status_code == 200
    assert isinstance(db.login_sessions.sessions[0]["visitor_id"], UUID)
