"""Phase 5B admin API: auth boundary, ADR-040 conventions, error mapping (fake repository)."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg.errors
import pytest

from app.db.admin import (
    AccountUsageItem,
    AccountUsageReport,
    AdminDevice,
    AdminLog,
    AdminPhrase,
    DailyActivityBucket,
    PeakHourBucket,
)
from tests.admin_support import FakeAdminDb, admin_auth, admin_client  # noqa: F401 (fixture)

P1 = UUID("00000000-0000-0000-0000-0000000000p1".replace("p", "a"))
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def phrase(**over):
    base = dict(id=P1, phrase_text="Lagi", category="Fallback", language_variant="BISINDO-Jakarta", active=True,
                model_phrase_id="label_05", sign_recognizable=True)
    base.update(over)
    return AdminPhrase(**base)


class FakeRepo:
    def __init__(self):
        self.calls = []
        self.phrases = {P1: phrase()}
        self.taken = set()
        self.fail_unique = False

    async def list_phrases(self, *, limit, offset):
        self.calls.append(("list_phrases", limit, offset))
        return list(self.phrases.values()), len(self.phrases)

    async def get_phrase(self, pid):
        self.calls.append(("get_phrase", pid))
        return self.phrases.get(pid)

    async def phrase_text_taken(self, text, *, exclude_id=None):
        self.calls.append(("taken", text, exclude_id))
        return text in self.taken

    async def create_phrase(self, *, phrase_text, category, active):
        self.calls.append(("create", phrase_text, category, active))
        if self.fail_unique:
            raise psycopg.errors.UniqueViolation("duplicate key value (model_phrase_id)=(label_05)")
        p = phrase(id=uuid4(), phrase_text=phrase_text, category=category,
                   active=True if active is None else active, model_phrase_id=None, sign_recognizable=False)
        self.phrases[p.id] = p
        return p

    async def update_phrase(self, pid, changes):
        self.calls.append(("update", pid, dict(changes)))
        if self.fail_unique:
            raise psycopg.errors.UniqueViolation("duplicate key value (x)=(y)")
        p = self.phrases.get(pid)
        if p is None:
            return None
        p = AdminPhrase(**{**vars(p), **changes})
        self.phrases[pid] = p
        return p

    async def list_logs(self, filters, *, limit, offset):
        self.calls.append(("list_logs", filters, limit, offset))
        return [AdminLog(uuid4(), uuid4(), "sign_to_text", None, Decimal("0.8499"), "escalated_to_jbi", NOW)], 1

    async def list_devices(self, *, limit, offset):
        self.calls.append(("list_devices", limit, offset))
        return [AdminDevice(uuid4(), uuid4(), "Kiosk 1", True)], 1

    async def get_account_usage(self, *, nis_list, since, tz_name):
        self.calls.append(("get_account_usage", nis_list, since, tz_name))
        sid_juri = UUID("00000000-0000-0000-0000-000000000101")
        sid_coba = UUID("00000000-0000-0000-0000-000000000102")
        accounts = [
            AccountUsageItem(
                student_id=sid_juri,
                nis="1001",
                full_name="Akun Juri",
                active=True,
                last_sign_in_at=NOW,
                login_count=5,
                token_refreshed_count=12,
                total_events=17,
            ),
            AccountUsageItem(
                student_id=sid_coba,
                nis="1002",
                full_name="Akun Coba",
                active=True,
                last_sign_in_at=None,
                login_count=10,
                token_refreshed_count=20,
                total_events=30,
            ),
        ]
        if nis_list is not None:
            accounts = [a for a in accounts if a.nis in nis_list]
        hours = [
            PeakHourBucket(h, 15 if h == 19 else 0, 32 if h == 19 else 0, 47 if h == 19 else 0)
            for h in range(24)
        ]
        days = [DailyActivityBucket(date(2026, 9, 29), 15, 32, 47)]
        return AccountUsageReport(accounts=accounts, peak_hours=hours, daily_activity=days)


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def api(admin_auth, repo):
    db = FakeAdminDb(repo)
    with admin_client(admin_auth, db.open) as c:
        yield c, admin_auth.headers(), db


def err(r, status, code):
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    return r.json()["error"]["message"]


# ---- auth boundary (ADR-039) ------------------------------------------------------------------

@pytest.mark.parametrize(("method", "path", "body"), [
    ("get", "/api/admin/phrases", None),
    ("post", "/api/admin/phrases", {"phrase_text": "x", "category": "Obat"}),
    ("patch", f"/api/admin/phrases/{P1}", {"active": False}),
    ("get", "/api/admin/logs", None),
    ("get", "/api/admin/devices", None),
    ("get", "/api/admin/account-usage", None),
])
def test_every_admin_endpoint_requires_admin(admin_auth, repo, method, path, body):
    db = FakeAdminDb(repo)
    with admin_client(admin_auth, db.open) as c:
        kw = {"json": body} if body is not None else {}
        err(getattr(c, method)(path, **kw), 401, "UNAUTHORIZED")
        err(getattr(c, method)(path, headers=admin_auth.non_admin_headers(), **kw), 403, "FORBIDDEN")
    assert db.opened == 0 and repo.calls == []


def test_auth_runs_before_validation(admin_auth, repo):
    db = FakeAdminDb(repo)
    with admin_client(admin_auth, db.open) as c:
        err(c.get("/api/admin/phrases?page=0&junk=1"), 401, "UNAUTHORIZED")
        err(c.post("/api/admin/phrases", json={"model_phrase_id": "label_05"}), 401, "UNAUTHORIZED")


# ---- pagination (ADR-040 §4–§8) ---------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/admin/phrases", "/api/admin/logs", "/api/admin/devices"])
def test_default_pagination_and_list_shape(api, path):
    c, h, _ = api
    r = c.get(path, headers=h)
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"items", "page", "page_size", "total"}
    assert (b["page"], b["page_size"], b["total"]) == (1, 50, len(b["items"]))


@pytest.mark.parametrize("path", ["/api/admin/phrases", "/api/admin/logs", "/api/admin/devices"])
def test_custom_page_passes_limit_offset(api, repo, path):
    c, h, _ = api
    r = c.get(f"{path}?page=3&page_size=100", headers=h)
    assert r.status_code == 200 and (r.json()["page"], r.json()["page_size"]) == (3, 100)
    call = repo.calls[-1]
    assert call[-2:] == (100, 200)  # limit, offset


@pytest.mark.parametrize("path", ["/api/admin/phrases", "/api/admin/logs", "/api/admin/devices"])
@pytest.mark.parametrize("qs", ["page=0", "page=-1", "page_size=0", "page_size=101", "page=abc", "page=1.5",
                                "sort=label", "order=desc", "junk=1"])
def test_invalid_pagination_or_unknown_query_is_422_not_clamped(api, repo, path, qs):
    c, h, db = api
    err(c.get(f"{path}?{qs}", headers=h), 422, "VALIDATION_ERROR")
    assert db.opened == 0


# ---- phrases (ADR-041) ------------------------------------------------------------------------

def test_list_phrases_fields(api):
    c, h, _ = api
    item = c.get("/api/admin/phrases", headers=h).json()["items"][0]
    assert set(item) == {"id", "phrase_text", "category", "language_variant", "active", "model_phrase_id",
                         "sign_recognizable"}
    assert "aliases" not in item


def test_create_phrase_201_with_defaults(api, repo):
    c, h, _ = api
    r = c.post("/api/admin/phrases", json={"phrase_text": "Saya sakit", "category": "Keluhan"}, headers=h)
    assert r.status_code == 201
    b = r.json()
    assert (b["phrase_text"], b["category"], b["active"], b["model_phrase_id"], b["sign_recognizable"]) == (
        "Saya sakit", "Keluhan", True, None, False)
    assert ("create", "Saya sakit", "Keluhan", None) in repo.calls


@pytest.mark.parametrize("field", ["model_phrase_id", "sign_recognizable", "language_variant", "id", "aliases",
                                   "created_at"])
def test_create_rejects_read_only_or_unknown_fields(api, repo, field):
    c, h, _ = api
    body = {"phrase_text": "x", "category": "Obat", field: "label_05" if field != "sign_recognizable" else True}
    err(c.post("/api/admin/phrases", json=body, headers=h), 422, "VALIDATION_ERROR")
    assert not any(k[0] == "create" for k in repo.calls)


@pytest.mark.parametrize("body", [
    pytest.param({"category": "Obat"}, id="missing text"),
    pytest.param({"phrase_text": "x"}, id="missing category"),
    pytest.param({"phrase_text": "", "category": "Obat"}, id="empty text"),
    pytest.param({"phrase_text": "   ", "category": "Obat"}, id="blank text"),
    pytest.param({"phrase_text": 5, "category": "Obat"}, id="text not string"),
    pytest.param({"phrase_text": "x", "category": "Umum"}, id="unknown category"),
    pytest.param({"phrase_text": "x", "category": "obat"}, id="category case"),
    pytest.param({"phrase_text": "x", "category": "Obat", "active": "yes"}, id="active not bool"),
    pytest.param({"phrase_text": "x", "category": "Obat", "active": None}, id="active null"),
])
def test_create_validation_422(api, body):
    c, h, _ = api
    err(c.post("/api/admin/phrases", json=body, headers=h), 422, "VALIDATION_ERROR")


def test_create_duplicate_text_409_without_echo(api, repo):
    c, h, _ = api
    repo.taken.add("Lagi")
    msg = err(c.post("/api/admin/phrases", json={"phrase_text": "Lagi", "category": "Fallback"}, headers=h),
              409, "CONFLICT")
    assert "Lagi" not in msg


def test_unique_violation_maps_to_409_without_db_details(api, repo):
    c, h, _ = api
    repo.fail_unique = True
    r = c.post("/api/admin/phrases", json={"phrase_text": "Baru", "category": "Obat"}, headers=h)
    err(r, 409, "CONFLICT")
    assert "label_05" not in r.text and "duplicate key" not in r.text


def test_patch_updates_only_sent_fields(api, repo):
    c, h, _ = api
    r = c.patch(f"/api/admin/phrases/{P1}", json={"active": False}, headers=h)
    assert r.status_code == 200 and r.json()["active"] is False
    assert r.json()["model_phrase_id"] == "label_05" and r.json()["sign_recognizable"] is True
    assert ("update", P1, {"active": False}) in repo.calls


def test_patch_text_and_category_keeps_model_phrase_id(api, repo):
    c, h, _ = api
    r = c.patch(f"/api/admin/phrases/{P1}", json={"phrase_text": "Ulangi", "category": "Keluhan"}, headers=h)
    assert r.status_code == 200
    assert (r.json()["phrase_text"], r.json()["category"], r.json()["model_phrase_id"]) == ("Ulangi", "Keluhan",
                                                                                          "label_05")
    assert ("taken", "Ulangi", P1) in repo.calls  # duplicate check excludes self


@pytest.mark.parametrize("body", [
    pytest.param({}, id="empty"),
    pytest.param({"model_phrase_id": "label_06"}, id="model_phrase_id immutable"),
    pytest.param({"sign_recognizable": False}, id="sign_recognizable immutable"),
    pytest.param({"language_variant": "BISINDO-Other"}, id="language_variant immutable"),
    pytest.param({"id": str(uuid4())}, id="id immutable"),
    pytest.param({"phrase_text": None}, id="null text"),
    pytest.param({"active": None}, id="null active"),
    pytest.param({"category": None}, id="null category"),
    pytest.param({"phrase_text": " "}, id="blank text"),
    pytest.param({"active": False, "sign_recognizable": True}, id="mixed with immutable"),
])
def test_patch_validation_422_and_no_write(api, repo, body):
    c, h, _ = api
    err(c.patch(f"/api/admin/phrases/{P1}", json=body, headers=h), 422, "VALIDATION_ERROR")
    assert not any(k[0] == "update" for k in repo.calls)


def test_patch_missing_phrase_404(api):
    c, h, _ = api
    err(c.patch(f"/api/admin/phrases/{uuid4()}", json={"active": False}, headers=h), 404, "PHRASE_NOT_FOUND")


def test_patch_invalid_uuid_422(api):
    c, h, _ = api
    err(c.patch("/api/admin/phrases/label_05", json={"active": False}, headers=h), 422, "VALIDATION_ERROR")


def test_patch_duplicate_text_409(api, repo):
    c, h, _ = api
    repo.taken.add("Maaf")
    err(c.patch(f"/api/admin/phrases/{P1}", json={"phrase_text": "Maaf"}, headers=h), 409, "CONFLICT")


def test_no_delete_endpoint(api):
    c, h, _ = api
    assert c.delete(f"/api/admin/phrases/{P1}", headers=h).status_code == 405


def test_references_endpoint_rejects_json_body(api):
    # Reference upload exists since ADR-046 and only accepts multipart (see test_admin_references.py).
    c, h, _ = api
    err(c.post(f"/api/admin/phrases/{P1}/references", headers=h, json={}), 422, "VALIDATION_ERROR")


# ---- logs (ADR-043) ---------------------------------------------------------------------------

def test_logs_fields_are_metadata_only(api):
    c, h, _ = api
    item = c.get("/api/admin/logs", headers=h).json()["items"][0]
    assert set(item) == {"id", "device_id", "direction", "predicted_phrase_id", "confidence", "resulted_action",
                         "created_at"}
    assert item["confidence"] == 0.8499
    text = json.dumps(item)
    for word in ("sequence", "landmark", "frames", "video", "audio", "payload"):
        assert word not in text


def test_logs_filters_are_passed(api, repo):
    c, h, _ = api
    d, p = uuid4(), uuid4()
    qs = (f"device_id={d}&predicted_phrase_id={p}&resulted_action=confirmed&direction=sign_to_text"
          "&created_from=2026-09-01T00:00:00Z&created_to=2026-09-30T00:00:00%2B07:00")
    assert c.get(f"/api/admin/logs?{qs}", headers=h).status_code == 200
    f = repo.calls[-1][1]
    assert (f.device_id, f.predicted_phrase_id, f.resulted_action, f.direction) == (d, p, "confirmed", "sign_to_text")
    assert f.created_from.tzinfo is not None and f.created_to.tzinfo is not None


@pytest.mark.parametrize("qs", [
    "device_id=not-a-uuid", "predicted_phrase_id=label_05", "resulted_action=pending_confirmation",
    "resulted_action=retry", "direction=sideways", "created_from=yesterday",
    "created_from=2026-09-01T00:00:00",  # naive datetime rejected
    "created_from=2026-09-30T00:00:00Z&created_to=2026-09-01T00:00:00Z",  # inverted range
])
def test_logs_invalid_filters_422(api, qs):
    c, h, db = api
    err(c.get(f"/api/admin/logs?{qs}", headers=h), 422, "VALIDATION_ERROR")
    assert db.opened == 0


# ---- devices (ADR-043) ------------------------------------------------------------------------

def test_devices_fields(api):
    c, h, _ = api
    item = c.get("/api/admin/devices", headers=h).json()["items"][0]
    assert set(item) == {"id", "institution_id", "label", "active"}


@pytest.mark.parametrize("method", ["post", "patch", "delete", "put"])
def test_devices_read_only(api, method):
    c, h, _ = api
    assert getattr(c, method)("/api/admin/devices", headers=h).status_code == 405


# ---- account usage (ADR-054) ------------------------------------------------------------------

def test_account_usage_defaults_and_shape(api, repo, monkeypatch):
    monkeypatch.delenv("TRACKED_STUDENT_NIS", raising=False)
    c, h, _ = api
    r = c.get("/api/admin/account-usage", headers=h)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["timezone"] == "Asia/Jakarta"
    assert b["days"] == 30
    assert len(b["accounts"]) == 2
    assert set(b["accounts"][0]) == {
        "student_id", "nis", "full_name", "active", "last_sign_in_at",
        "login_count", "token_refreshed_count", "total_events"
    }
    # 12:00 UTC converted to Asia/Jakarta (+07:00) → 19:00:00+07:00
    assert b["accounts"][0]["last_sign_in_at"] == "2026-09-29T19:00:00+07:00"
    assert b["accounts"][1]["last_sign_in_at"] is None
    assert len(b["peak_hours"]) == 24
    assert b["peak_hours"][19] == {"hour": 19, "login_count": 15, "token_refreshed_count": 32, "total_events": 47}
    assert b["daily_activity"] == [{"date": "2026-09-29", "login_count": 15, "token_refreshed_count": 32, "total_events": 47}]
    call_name, nis_list, since, tz_name = repo.calls[-1]
    assert (call_name, nis_list, tz_name) == ("get_account_usage", None, "Asia/Jakarta")
    assert since.tzinfo is not None


def test_account_usage_query_nis_and_env_fallback(api, repo, monkeypatch):
    c, h, _ = api
    monkeypatch.setenv("TRACKED_STUDENT_NIS", "1001, 1002")
    r_env = c.get("/api/admin/account-usage", headers=h)
    assert r_env.status_code == 200
    assert repo.calls[-1][1] == ["1001", "1002"]

    r_q = c.get("/api/admin/account-usage?nis=1001&days=7&tz=UTC", headers=h)
    assert r_q.status_code == 200
    assert r_q.json()["timezone"] == "UTC" and r_q.json()["days"] == 7
    assert [a["nis"] for a in r_q.json()["accounts"]] == ["1001"]
    assert repo.calls[-1][1] == ["1001"]


@pytest.mark.parametrize("qs", [
    "days=0",
    "days=-5",
    "days=366",
    "tz=Invalid/Zone",
    "tz=",
    "nis=",
    "nis=,",
    f"nis={'x' * 31}",
    "unknown_param=1",
])
def test_account_usage_invalid_query_422(api, qs):
    c, h, db = api
    err(c.get(f"/api/admin/account-usage?{qs}", headers=h), 422, "VALIDATION_ERROR")
    assert db.opened == 0

