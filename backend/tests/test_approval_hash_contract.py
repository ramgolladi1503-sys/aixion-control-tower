from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    AgentProvider,
    ApprovalRequest,
    FileChange,
    RiskAssessment,
    RiskLevel,
)


ROOT = Path(__file__).parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


antigravity_hook = _load_module(
    "approval_hash_antigravity_hook",
    ROOT / "integrations" / "antigravity" / "aixion_pre_tool_hook.py",
)
codex_hook = _load_module(
    "approval_hash_codex_hook",
    ROOT / "integrations" / "codex" / "aixion_permission_request_hook.py",
)


def _approval(provider: AgentProvider) -> ApprovalRequest:
    return ApprovalRequest(
        project_id="project_hash_contract",
        workspace_id="workspace_hash_contract",
        title="Run reviewed command",
        summary="Test canonical hash parity",
        agent_name="connector-test",
        target_branch="main",
        files=[FileChange(path="README.md", diff="+reviewed")],
        test_plan=["pytest"],
        rollback_plan="Revert the patch.",
        source_provider=provider,
        source_agent_id="agent_hash_contract",
        source_agent_name="connector-test",
        source_session_id="session_hash_contract",
        source_invocation_id="invocation_hash_contract",
        risk=RiskAssessment(level=RiskLevel.HIGH, reasons=["writes files"]),
        approved_payload_hash_version=2,
    )


def test_backend_and_antigravity_v2_hashes_are_identical() -> None:
    approval = _approval(AgentProvider.ANTIGRAVITY)
    serialized = approval.model_dump(mode="json")

    assert antigravity_hook._payload_hash(serialized) == compute_approval_payload_hash(approval)


def test_backend_and_codex_v2_hashes_are_identical() -> None:
    approval = _approval(AgentProvider.CODEX)
    serialized = approval.model_dump(mode="json")

    assert codex_hook._hash(serialized) == compute_approval_payload_hash(approval)
