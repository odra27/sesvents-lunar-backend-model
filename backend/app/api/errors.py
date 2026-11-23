"""Approved application error envelope (Phase 3 error contract).

Every application error, including validation errors, is returned as:

    {"error": {"code": "<STABLE_CODE>", "message": "<text>"}}

No retry metadata in the body. Messages never echo request values: pydantic
validation details are reduced to field location + message (the offending
`input`, which may be patient landmarks, is dropped — ADR-012).

These functions return (http_status, body); the route layer only serializes them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from app.errors import BackendError

VALIDATION_ERROR_STATUS = 422
_MAX_VALIDATION_ITEMS = 5


def error_body(code: str, message: str) -> dict[str, Any]:
    """Build the standard `{"error": {"code", "message"}}` response envelope."""
    return {"error": {"code": code, "message": message}}


def from_backend_error(exc: BackendError) -> tuple[int, dict[str, Any]]:
    """Convert a domain BackendError into an `(http_status, error_body)` tuple."""
    return exc.http_status, error_body(exc.code, exc.message)


def from_error_list(errors: Sequence[Mapping[str, Any]]) -> tuple[int, dict[str, Any]]:
    """422 envelope from pydantic-style error dicts. Only `loc` and `msg` are read;
    `input` and `ctx` (which may carry request values) are never used."""
    parts = []
    for err in list(errors)[:_MAX_VALIDATION_ITEMS]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "body"
        parts.append(f"{loc}: {err.get('msg', 'invalid value')}")
    more = len(errors) - len(parts)
    if more > 0:
        parts.append(f"(+{more} more)")
    return VALIDATION_ERROR_STATUS, error_body("VALIDATION_ERROR", "Invalid request. " + "; ".join(parts))


def from_validation_error(exc: ValidationError) -> tuple[int, dict[str, Any]]:
    """Convert a Pydantic ValidationError into a 422 envelope without echoing input values."""
    return from_error_list(exc.errors(include_input=False, include_url=False, include_context=False))


def from_unexpected_error(_exc: BaseException) -> tuple[int, dict[str, Any]]:
    """Return a generic 500 envelope so unexpected internal/DB errors never leak details."""
    return 500, error_body("INTERNAL_ERROR", "Internal server error")
