from __future__ import annotations

import json
import os
import platform
import socket
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from pydantic import BaseModel, Field, field_validator

from .contracts import AdapterManifest


class RelayConfig(BaseModel):
    api_base_url: str
    relay_id: str
    relay_token_secret_name: str
    worker_id: str
    poll_seconds: float = Field(default=1.0, ge=0.2, le=60.0)
    heartbeat_seconds: float = Field(default=20.0, ge=5.0, le=60.0)
    command_lease_seconds: int = Field(default=180, ge=30, le=3600)
    request_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    workspace_roots: list[str] = Field(default_factory=list)
    adapters: list[AdapterManifest] = Field(default_factory=list)
    max_parallel_sessions: int = Field(default=4, ge=1, le=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not (
            cleaned.startswith("https://")
            or cleaned.startswith("http://127.0.0.1")
            or cleaned.startswith("http://localhost")
        ):
            raise ValueError("Aixion API must use HTTPS except for local development")
        return cleaned

    @field_validator("workspace_roots")
    @classmethod
    def validate_roots(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for raw in values:
            path = str(Path(raw).expanduser().resolve())
            if path not in result:
                result.append(path)
        return result


def default_config_path() -> Path:
    return Path(user_config_dir("aixion-relay", "Aixion")) / "config.json"


def load_config(path: Path | None = None) -> RelayConfig:
    target = path or Path(os.getenv("AIXION_RELAY_CONFIG", default_config_path()))
    if not target.exists():
        raise FileNotFoundError(
            f"Relay configuration not found at {target}. Run aixion-relay init first."
        )
    payload = json.loads(target.read_text(encoding="utf-8"))
    return RelayConfig.model_validate(payload)


def save_config(config: RelayConfig, path: Path | None = None) -> Path:
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def default_registration_payload(
    *,
    name: str,
    workspace_roots: list[str],
    adapters: list[AdapterManifest],
    repositories: list[str],
) -> dict[str, Any]:
    system = platform.system().upper()
    platform_name = {
        "DARWIN": "MACOS",
        "LINUX": "LINUX",
        "WINDOWS": "WINDOWS",
    }.get(system, "OTHER")
    fingerprint_seed = "|".join(
        [socket.gethostname(), platform.platform(), platform.machine()]
    )
    import hashlib

    fingerprint = hashlib.sha256(fingerprint_seed.encode("utf-8")).hexdigest()
    return {
        "name": name,
        "platform": platform_name,
        "hostname": socket.gethostname(),
        "machine_fingerprint": fingerprint,
        "workspace_roots": [str(Path(root).expanduser().resolve()) for root in workspace_roots],
        "allowed_repositories": repositories,
        "adapters": [adapter.model_dump(mode="json") for adapter in adapters],
        "metadata": {"runtime": "aixion-agent-relay"},
    }
