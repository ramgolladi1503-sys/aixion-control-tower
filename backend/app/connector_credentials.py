from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime

from pydantic import BaseModel

from .connector_models import AgentConnector, ConnectorAuthType, ConnectorHealthStatus, ConnectorPublic, ConnectorStatus
from .models import new_id, now_utc


class ConnectorSecretIssueRequest(BaseModel):
    note: str = ""


class ConnectorSecretIssueResponse(BaseModel):
    connector: ConnectorPublic
    secret: str | None = None
    secret_hint: str | None = None


class ConnectorCredentialStatus(BaseModel):
    connector_id: str
    auth_type: ConnectorAuthType
    secret_configured: bool
    secret_revoked_at: datetime | None = None
    secret_rotated_at: datetime | None = None
    last_used_at: datetime | None = None
    last_health_check_at: datetime | None = None
    failed_auth_count: int = 0
    last_error: str | None = None
    health_status: ConnectorHealthStatus
    status: ConnectorStatus


def hash_connector_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def issue_connector_secret(connector: AgentConnector) -> tuple[str, str, str]:
    raw_secret = f"aixion_connector_{secrets.token_urlsafe(40)}"
    secret_hash = hash_connector_secret(raw_secret)
    secret_ref = f"connector_secret_{new_id('secret')}"
    connector.secret_ref = secret_ref
    connector.config = {
        **connector.config,
        "secret_hash": secret_hash,
        "secret_hint": raw_secret[-8:],
        "secret_revoked_at": None,
        "secret_rotated_at": now_utc().isoformat(),
    }
    connector.updated_at = now_utc()
    return raw_secret, secret_ref, raw_secret[-8:]


def revoke_connector_secret(connector: AgentConnector) -> None:
    connector.secret_ref = None
    connector.config = {
        **connector.config,
        "secret_hash": None,
        "secret_hint": None,
        "secret_revoked_at": now_utc().isoformat(),
    }
    connector.updated_at = now_utc()


def record_connector_auth_success(connector: AgentConnector) -> None:
    connector.last_used_at = now_utc()
    connector.last_error = None
    if connector.status == ConnectorStatus.ENABLED:
        connector.health_status = ConnectorHealthStatus.HEALTHY
    connector.updated_at = now_utc()


def record_connector_auth_failure(connector: AgentConnector, reason: str) -> None:
    connector.failed_auth_count += 1
    connector.last_error = reason
    if connector.status == ConnectorStatus.ENABLED:
        connector.health_status = ConnectorHealthStatus.DEGRADED if connector.failed_auth_count < 3 else ConnectorHealthStatus.FAILING
    connector.updated_at = now_utc()


def mark_connector_health_check(connector: AgentConnector, *, healthy: bool, reason: str = "") -> None:
    connector.last_health_check_at = now_utc()
    connector.last_error = "" if healthy else reason
    if connector.status == ConnectorStatus.DISABLED:
        connector.health_status = ConnectorHealthStatus.DISABLED
    else:
        connector.health_status = ConnectorHealthStatus.HEALTHY if healthy else ConnectorHealthStatus.DEGRADED
    connector.updated_at = now_utc()


def connector_credential_status(connector: AgentConnector) -> ConnectorCredentialStatus:
    return ConnectorCredentialStatus(
        connector_id=connector.id,
        auth_type=connector.auth_type,
        secret_configured=bool(connector.secret_ref and connector.config.get("secret_hash")),
        secret_revoked_at=_parse_optional_datetime(connector.config.get("secret_revoked_at")),
        secret_rotated_at=_parse_optional_datetime(connector.config.get("secret_rotated_at")),
        last_used_at=connector.last_used_at,
        last_health_check_at=connector.last_health_check_at,
        failed_auth_count=connector.failed_auth_count,
        last_error=connector.last_error,
        health_status=connector.health_status,
        status=connector.status,
    )


def verify_connector_secret(connector: AgentConnector, secret: str) -> bool:
    stored_hash = connector.config.get("secret_hash")
    if not stored_hash:
        return False
    return safe_equal(stored_hash, hash_connector_secret(secret))


def _parse_optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
