from __future__ import annotations

from typing import Any

from .models import AuditEvent

REDACTED_DETAIL_VALUE = "[REDACTED]"
SENSITIVE_DETAIL_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "serverkey",
)


def _normalize_detail_key(key: str) -> str:
    return key.strip().lower().replace("_", "").replace("-", "")


def _is_sensitive_detail_key(key: str) -> bool:
    normalized = _normalize_detail_key(key)
    return any(marker in normalized for marker in SENSITIVE_DETAIL_KEY_MARKERS)


def redact_audit_details(value: Any) -> Any:
    """Return nested audit details with credential-like fields removed."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED_DETAIL_VALUE
            if _is_sensitive_detail_key(str(key))
            else redact_audit_details(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_audit_details(item) for item in value]
    return value


def redact_audit_event(event: AuditEvent) -> AuditEvent:
    return event.model_copy(update={"details": redact_audit_details(event.details)})
