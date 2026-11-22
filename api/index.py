"""Vercel Serverless ASGI entrypoint for LUNAR Backend + Modeling.

Adds `backend/` and `ml/src/` to `sys.path`, stubs `sklearn.metrics` when `scikit-learn` is omitted
from the slim serverless bundle (`Predictor` only uses pure-NumPy `softmax` and `known_confidence`),
and eagerly warms `app.state` (~90 ms) so requests work even if the serverless adapter skips ASGI
lifespan startup.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "backend", ROOT / "ml" / "src"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

# Stub `sklearn.metrics` if scikit-learn/scipy are excluded from the serverless bundle.
try:
    import sklearn.metrics  # noqa: F401
except ModuleNotFoundError:
    _sk = types.ModuleType("sklearn")
    _sk_metrics = types.ModuleType("sklearn.metrics")
    _sk_metrics.confusion_matrix = None  # type: ignore[attr-defined]
    _sk_metrics.f1_score = None  # type: ignore[attr-defined]
    _sk_metrics.roc_auc_score = None  # type: ignore[attr-defined]
    _sk.metrics = _sk_metrics  # type: ignore[attr-defined]
    sys.modules.setdefault("sklearn", _sk)
    sys.modules.setdefault("sklearn.metrics", _sk_metrics)

from app.auth.jwt_verifier import JwtVerifier  # noqa: E402
from app.auth.supabase_auth import SupabaseAuthClient  # noqa: E402
from app.chat.model import OfflineChatModel, chat_model_from_env  # noqa: E402
from app.errors import ModelingUnavailable  # noqa: E402
from app.main import app  # noqa: E402
from app.modeling.client import load_modeling_client  # noqa: E402
from app.provisioning.supabase_admin import SupabaseAdminClient  # noqa: E402
from app.storage.supabase import SupabaseStorage  # noqa: E402


def _ensure_state() -> None:
    if getattr(app.state, "modeling", None) is None:
        try:
            app.state.modeling = load_modeling_client()
        except ModelingUnavailable:
            app.state.modeling = None
    if getattr(app.state, "jwt_verifier", None) is None:
        try:
            app.state.jwt_verifier = JwtVerifier.from_env()
        except Exception:
            app.state.jwt_verifier = None
    if getattr(app.state, "storage", None) is None:
        try:
            app.state.storage = SupabaseStorage.from_env()
        except Exception:
            app.state.storage = None
    if getattr(app.state, "auth_client", None) is None:
        try:
            app.state.auth_client = SupabaseAuthClient.from_env()
        except Exception:
            app.state.auth_client = None
    if getattr(app.state, "auth_admin", None) is None:
        try:
            app.state.auth_admin = SupabaseAdminClient.from_env()
        except Exception:
            app.state.auth_admin = None
    if getattr(app.state, "chat_model", None) is None:
        try:
            app.state.chat_model = chat_model_from_env()
        except Exception:
            app.state.chat_model = OfflineChatModel()


_ensure_state()
