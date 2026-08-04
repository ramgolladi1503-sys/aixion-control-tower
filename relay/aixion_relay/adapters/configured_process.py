from __future__ import annotations

import json
import os
from typing import Any

from ..contracts import RelayProvider
from .generic_process import GenericProcessAdapter


class ConfiguredProcessError(ValueError):
    pass


def argv_from_environment(name: str) -> list[str] | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfiguredProcessError(f"{name} must be a JSON string array.") from error
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfiguredProcessError(f"{name} must be a non-empty JSON string array.")
    return value


def configured_process_adapter(
    *,
    environment_name: str,
    adapter_id: str,
    provider: RelayProvider,
    display_name: str,
    environment: dict[str, str] | None = None,
) -> GenericProcessAdapter | None:
    argv = argv_from_environment(environment_name)
    if argv is None:
        return None
    return GenericProcessAdapter(
        adapter_id=adapter_id,
        provider=provider,
        display_name=display_name,
        argv=argv,
        environment=environment,
    )


def render_argv(template: list[str], values: dict[str, Any]) -> list[str]:
    rendered: list[str] = []
    for item in template:
        value = item
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", str(replacement))
        rendered.append(value)
    return rendered
