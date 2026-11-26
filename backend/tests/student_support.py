"""Shared helpers for student login / chatbot tests (ADR-051). No real keys, tokens or network."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient

from app.chat.model import ChatTurn
from app.db.chat import ChatActivity, ChatDocument, ChatMessage, ChatSession, StudentDocument
from app.db.session import RequestDb
from app.db.students import StudentLoginTarget, StudentProfile
from app.main import create_app
from app.modeling.client import LibraryModelingClient
from tests.admin_support import AdminAuth
from tests.staff_support import PASSWORD, FakeGoTrue
from tests.test_modeling_client import FakePredictor

SCHOOL = UUID("00000000-0000-0000-0000-00000000b001")
SISWA = UUID("00000000-0000-0000-0000-0000000006a1")
SISWA_2 = UUID("00000000-0000-0000-0000-0000000006a2")
SISWA_INACTIVE = UUID("00000000-0000-0000-0000-0000000006a3")


def student(
    sid: UUID,
    *,
    nis: str | None = None,
    email: str | None = None,
    full_name: str = "Nadia Putri",
    active: bool = True,
    account_type: str | None = None,
    grade: int | None = 5,
    class_code: str | None = None,
    phone: str | None = None,
    institution_id: UUID | None = SCHOOL,
    institution_name: str | None = "SLB Negeri Contoh",
) -> StudentProfile:
    return StudentProfile(
        id=sid,
        institution_id=institution_id,
        institution_name=institution_name,
        nis=nis,
        full_name=full_name,
        class_name="5A" if institution_id else None,
        grade=grade,
        avatar_url=None,
        active=active,
        account_type=account_type,
        email=email,
        class_code=class_code,
        phone=phone,
    )


def default_students() -> dict[UUID, tuple[str | None, StudentProfile]]:
    return {
        SISWA: (
            "nadia@siswa.test",
            student(SISWA, nis="0012345678", email="nadia@siswa.test"),
        ),
        SISWA_2: (
            "raka@siswa.test",
            student(SISWA_2, nis="0099999999", email="raka@siswa.test", full_name="Raka"),
        ),
        SISWA_INACTIVE: (
            "inactive@siswa.test",
            student(SISWA_INACTIVE, nis="0011111111", email="inactive@siswa.test", active=False),
        ),
    }


class FakeStudentRepo:
    def __init__(self, rows=None) -> None:
        self.rows = rows if rows is not None else default_students()
        self.fail_create = False

    async def get_profile(self, student_id: UUID) -> StudentProfile | None:
        row = self.rows.get(student_id)
        return row[1] if row else None

    async def find_login_targets(self, nis: str) -> list[StudentLoginTarget]:
        return [StudentLoginTarget(email=e, profile=p) for e, p in self.rows.values() if p.nis == nis][:2]

    async def find_login_targets_by_email(self, email: str) -> list[StudentLoginTarget]:
        target = email.strip().lower()
        return [
            StudentLoginTarget(email=e, profile=p)
            for e, p in self.rows.values()
            if e and e.lower() == target
        ][:2]

    async def create_b2c_profile(self, *, user_id: UUID, full_name: str, phone: str) -> StudentProfile:
        if self.fail_create:
            raise RuntimeError("db insert failed")
        email = getattr(self, "_last_created_email", None)
        p = student(
            user_id,
            nis=None,
            email=email,
            full_name=full_name,
            grade=None,
            class_code=None,
            phone=phone,
            institution_id=None,
            institution_name=None,
        )
        self.rows[user_id] = (email, p)
        return p

    async def update_profile(self, student_id: UUID, changes: dict) -> StudentProfile | None:
        row = self.rows.get(student_id)
        if row is None:
            return None
        email, p = row
        updated = StudentProfile(**{**vars(p), **changes})
        self.rows[student_id] = (email, updated)
        return updated


class FakeChatRepo:
    """In-memory ChatRepository with the same ownership semantics as Postgres."""

    def __init__(self) -> None:
        # id → (student_id, session)
        self.sessions: dict[UUID, tuple[UUID, ChatSession]] = {}
        self.messages: list[ChatMessage] = []
        self.documents: list[tuple[ChatDocument, str]] = []  # (metadata, text)
        self._clock = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)

    def _now(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def _own(self, student_id, session_id):
        row = self.sessions.get(session_id)
        return row[1] if row and row[0] == student_id else None

    async def list_sessions(self, student_id, *, limit, offset):
        own = sorted((s for sid, s in self.sessions.values() if sid == student_id),
                     key=lambda s: (s.updated_at, str(s.id)), reverse=True)
        return own[offset:offset + limit], len(own)

    async def create_session(self, student_id, title):
        now = self._now()
        s = ChatSession(id=uuid.uuid4(), title=title,
                        created_at=now, updated_at=now)
        self.sessions[s.id] = (student_id, s)
        return s

    async def get_session(self, student_id, session_id):
        return self._own(student_id, session_id)

    async def rename_session(self, student_id, session_id, title):
        s = self._own(student_id, session_id)
        if s is None:
            return None
        s = ChatSession(id=s.id, title=title,
                        created_at=s.created_at, updated_at=s.updated_at)
        self.sessions[s.id] = (student_id, s)
        return s

    async def delete_session(self, student_id, session_id):
        if self._own(student_id, session_id) is None:
            return False
        del self.sessions[session_id]
        self.messages = [
            m for m in self.messages if m.session_id != session_id]
        self.documents = [
            d for d in self.documents if d[0].session_id != session_id]
        return True

    async def list_messages(self, session_id, *, limit):
        own = [m for m in self.messages if m.session_id == session_id]
        return own[-limit:]

    async def append_exchange(self, session_id, *, user_content, input_mode, assistant_content, new_title,
                              video_id=None, video_t_sec=None):
        student_id, s = self.sessions[session_id]
        user = ChatMessage(uuid.uuid4(), session_id, "user",
                           user_content, input_mode, self._now(), video_id, video_t_sec)
        bot = ChatMessage(uuid.uuid4(), session_id, "assistant",
                          assistant_content, None, self._now())
        self.messages += [user, bot]
        s = ChatSession(id=s.id, title=new_title or s.title,
                        created_at=s.created_at, updated_at=bot.created_at)
        self.sessions[session_id] = (student_id, s)
        return user, bot, s

    async def add_document(self, session_id, *, filename, page_count, content):
        doc = ChatDocument(uuid.uuid4(), session_id, filename,
                           page_count, len(content), self._now())
        self.documents.append((doc, content))
        return doc

    async def session_documents(self, session_id):
        return [d for d, _ in self.documents if d.session_id == session_id]

    async def document_texts(self, session_id, *, limit):
        own = [(d.filename, t)
               for d, t in self.documents if d.session_id == session_id]
        return list(reversed(own))[:limit]

    async def list_documents(self, student_id, *, limit):
        out = []
        for d, _ in reversed(self.documents):
            row = self.sessions.get(d.session_id)
            if row and row[0] == student_id:
                out.append(StudentDocument(d, row[1].title))
        return out[:limit]

    async def activity(self, student_id, since):
        own = {sid for sid, (stu, _) in self.sessions.items()
               if stu == student_id}
        q = sum(1 for m in self.messages
                if m.session_id in own and m.role == "user" and m.video_id and m.created_at >= since)
        d = sum(
            1 for doc, _ in self.documents if doc.session_id in own and doc.created_at >= since)
        return ChatActivity(video_questions=q, documents=d)

    async def count_recent_user_messages(self, student_id, *, window_seconds):
        cutoff = self._clock - timedelta(seconds=window_seconds)
        own = {sid for sid, (stu, _) in self.sessions.items() if stu == student_id}
        return sum(
            1
            for m in self.messages
            if m.session_id in own and m.role == "user" and m.created_at >= cutoff
        )


class FakeLoginSessionRepo:
    """In-memory LoginSessionRepository (ADR-055)."""

    def __init__(self) -> None:
        self.sessions: list[dict] = []
        self.calls: list[tuple] = []
        self.fail: bool = False
        self._now = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)

    def advance(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)

    def _find_active(
        self,
        user_id: UUID,
        auth_session_id: str | None,
        visitor_id: UUID | None,
        inactivity_minutes: int,
    ) -> dict | None:
        cutoff = self._now - timedelta(minutes=inactivity_minutes)
        if auth_session_id is not None:
            for s in reversed(self.sessions):
                if s["user_id"] == user_id and s["auth_session_id"] == auth_session_id and s["logout_at"] is None:
                    return s
        if visitor_id is not None:
            for s in reversed(self.sessions):
                if (
                    s["user_id"] == user_id
                    and s["visitor_id"] == visitor_id
                    and s["logout_at"] is None
                    and s["last_activity_at"] >= cutoff
                ):
                    return s
        for s in reversed(self.sessions):
            if s["user_id"] == user_id and s["logout_at"] is None and s["last_activity_at"] >= cutoff:
                return s
        return None

    async def create_session(
        self,
        *,
        user_id: UUID,
        account_type: str,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
    ) -> UUID:
        self.calls.append(("create_session", user_id, account_type, auth_session_id, visitor_id))
        if self.fail:
            raise RuntimeError("tracking db down")
        sid = uuid.uuid4()
        self.sessions.append({
            "session_id": sid,
            "user_id": user_id,
            "account_type": account_type,
            "auth_session_id": auth_session_id,
            "visitor_id": visitor_id,
            "login_at": self._now,
            "last_activity_at": self._now,
            "logout_at": None,
            "session_duration_seconds": 0,
        })
        return sid

    async def touch_activity(
        self,
        *,
        user_id: UUID,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
        min_interval_seconds: int = 0,
        inactivity_minutes: int = 30,
    ) -> bool:
        self.calls.append(("touch_activity", user_id, auth_session_id, visitor_id, min_interval_seconds))
        if self.fail:
            raise RuntimeError("tracking db down")
        s = self._find_active(user_id, auth_session_id, visitor_id, inactivity_minutes)
        if s is None:
            return False
        if min_interval_seconds > 0 and (self._now - s["last_activity_at"]).total_seconds() < min_interval_seconds:
            return False
        s["last_activity_at"] = self._now
        s["session_duration_seconds"] = max(0, int((self._now - s["login_at"]).total_seconds()))
        return True

    async def end_session(
        self,
        *,
        user_id: UUID,
        auth_session_id: str | None = None,
        visitor_id: UUID | None = None,
        inactivity_minutes: int = 30,
    ) -> bool:
        self.calls.append(("end_session", user_id, auth_session_id, visitor_id))
        if self.fail:
            raise RuntimeError("tracking db down")
        s = self._find_active(user_id, auth_session_id, visitor_id, inactivity_minutes)
        if s is None:
            return False
        s["logout_at"] = self._now
        s["last_activity_at"] = self._now
        s["session_duration_seconds"] = max(0, int((self._now - s["login_at"]).total_seconds()))
        return True


class FakeStudentDb:
    def __init__(self) -> None:
        self.students = FakeStudentRepo()
        self.chat = FakeChatRepo()
        self.login_sessions = FakeLoginSessionRepo()
        self.opened = 0

    @asynccontextmanager
    async def open(self):
        self.opened += 1
        yield RequestDb(
            devices=None,
            phrases=None,
            logs=None,
            students=self.students,
            chat=self.chat,
            login_sessions=self.login_sessions,
        )


class FakeChatModel:
    name = "openrouter"

    def __init__(self) -> None:
        self.calls: list[list[ChatTurn]] = []
        self.contexts: list[str | None] = []
        self.answer = "Siklus air adalah perjalanan air yang terus berulang."
        self.error: Exception | None = None

    async def reply(self, history, *, context=None):
        self.calls.append(list(history))
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.answer

    async def aclose(self):
        return None


class FakeStudentAuthAdmin:
    """In-memory stand-in for SupabaseAdminClient during student self-registration tests (ADR-056)."""

    def __init__(self, gotrue: FakeGoTrue, db: FakeStudentDb) -> None:
        self.gotrue = gotrue
        self.db = db
        self.created: list[UUID] = []
        self.deleted: list[UUID] = []
        self.weak_password = False

    async def create_user(self, *, email: str, password: str, role: str) -> UUID:
        from app.errors import Conflict
        from app.provisioning.supabase_admin import WeakPassword

        if self.weak_password:
            raise WeakPassword("weak")
        if email.lower() in {k.lower() for k in self.gotrue.accounts}:
            raise Conflict("exists")
        uid = uuid.uuid4()
        self.gotrue.accounts[email.lower()] = (password, uid, role)
        self.db.students._last_created_email = email.lower()
        self.created.append(uid)
        return uid

    async def delete_user(self, user_id: UUID) -> None:
        self.deleted.append(user_id)
        for k, v in list(self.gotrue.accounts.items()):
            if v[1] == user_id:
                del self.gotrue.accounts[k]

    async def aclose(self) -> None:
        return None


def student_gotrue(auth: AdminAuth) -> FakeGoTrue:
    g = FakeGoTrue(auth)
    g.accounts = {
        "nadia@siswa.test": (PASSWORD, SISWA, "siswa"),
        "raka@siswa.test": (PASSWORD, SISWA_2, "siswa"),
        "inactive@siswa.test": (PASSWORD, SISWA_INACTIVE, "siswa"),
    }
    return g


def _not_configured():
    raise RuntimeError("not configured in this test")


def student_client(
    auth: AdminAuth,
    open_db,
    *,
    auth_client_factory=_not_configured,
    auth_admin_factory=_not_configured,
    chat_model=None,
) -> TestClient:
    app = create_app(
        modeling_factory=lambda: LibraryModelingClient(FakePredictor()),
        open_db=open_db,
        jwt_verifier_factory=auth.verifier,
        storage_factory=_not_configured,
        auth_client_factory=auth_client_factory,
        auth_admin_factory=auth_admin_factory,
        chat_model_factory=(
            lambda: chat_model) if chat_model is not None else _not_configured,
    )
    return TestClient(app, raise_server_exceptions=False)


def student_headers(auth: AdminAuth, sid: UUID, role="siswa", **over) -> dict[str, str]:
    app_metadata = {"provider": "email",
                    "role": role} if role is not None else {"provider": "email"}
    return auth.headers(sub=str(sid), app_metadata=app_metadata, **over)
