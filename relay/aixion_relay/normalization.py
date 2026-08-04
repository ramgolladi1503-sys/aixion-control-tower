from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from .contracts import ActionProposal


def _relative_workspace_path(raw: str, workspace: Path) -> tuple[str, bool]:
    cleaned = raw.strip()
    if not cleaned:
        return cleaned, False
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        normalized = Path(os.path.normpath(cleaned))
        if normalized.parts and normalized.parts[0] == "..":
            return normalized.as_posix(), True
        return normalized.as_posix(), False
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(workspace)
    except ValueError:
        return resolved.as_posix(), True
    return relative.as_posix(), False


def _domain(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return cleaned
    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    host = parsed.hostname
    return host.lower().rstrip(".") if host else cleaned.lower().rstrip(".")


def normalize_action_proposal(
    proposal: ActionProposal,
    *,
    workspace_path: str,
) -> ActionProposal:
    workspace = Path(workspace_path).expanduser().resolve(strict=False)
    normalized_paths: list[str] = []
    outside_paths: list[str] = []
    for raw in proposal.paths:
        normalized, outside = _relative_workspace_path(raw, workspace)
        if normalized and normalized not in normalized_paths:
            normalized_paths.append(normalized)
        if outside:
            outside_paths.append(raw)

    domains: list[str] = []
    for raw in proposal.network_domains:
        normalized = _domain(raw)
        if normalized and normalized not in domains:
            domains.append(normalized)

    return proposal.model_copy(
        update={
            "paths": normalized_paths,
            "network_domains": domains,
            "metadata": {
                **proposal.metadata,
                "workspace_path": str(workspace),
                "original_paths": list(proposal.paths),
                "outside_workspace_paths": outside_paths,
                "original_network_destinations": list(proposal.network_domains),
            },
        }
    )
