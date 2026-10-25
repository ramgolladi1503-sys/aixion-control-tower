from __future__ import annotations

import copy

import pytest

from integrations.antigravity.certify_phone_e2e import EvidenceError, verify_records


@pytest.fixture
def evidence() -> dict:
    invocation = "antigravity:v1:" + "a" * 64
    payload = {
        "id": "approval_0123456789ab",
        "status": "APPROVED",
        "source_provider": "ANTIGRAVITY",
        "source_agent_id": "agent_test",
        "source_invocation_id": invocation,
        "project_id": "project_test",
        "approved_payload_hash": "f" * 64,
        "files": [{
            "path": "antigravity://tool/run_command",
            "change_type": "tool_call",
            "diff": '{"command":"mkdir /tmp/aixion-e2e-marker"}',
        }],
    }
    return {
        "approval": {
            "approval_id": payload["id"],
            "source_agent_id": "agent_test",
            "status": "APPROVED",
            "payload": payload,
        },
        "decision_events": [{
            "approval_id": payload["id"],
            "event_type": "approval.decision",
            "details": {"new_status": "APPROVED", "approved_payload_hash": payload["approved_payload_hash"]},
        }],
        "claim": {
            "approval_id": payload["id"],
            "source_agent_id": "agent_test",
            "source_invocation_id": invocation,
        },
        "claim_events": [{
            "approval_id": payload["id"],
            "event_type": "approval.delivery_claimed",
            "details": {
                "source_agent_id": "agent_test",
                "source_invocation_id": invocation,
                "approved_payload_hash": payload["approved_payload_hash"],
            },
        }],
    }


def _verify(evidence: dict) -> dict:
    return verify_records(
        **evidence,
        expected_approval_id="approval_0123456789ab",
        expected_agent_id="agent_test",
        expected_project_id="project_test",
        tool_name="run_command",
        expected_arguments={"command": "mkdir /tmp/aixion-e2e-marker"},
    )


def test_certifier_accepts_exact_approved_and_claimed_invocation(evidence: dict) -> None:
    result = _verify(evidence)

    assert result["source_invocation_id"] == evidence["claim"]["source_invocation_id"]
    assert result["approved_payload_hash"] == "f" * 64
    assert result["delivery_claim_events"] == 1


@pytest.mark.parametrize("mutation", ["changed_arguments", "different_invocation", "missing_claim", "duplicate_claim_event"])
def test_certifier_rejects_incomplete_or_mismatched_e2e_evidence(
    evidence: dict, mutation: str
) -> None:
    changed = copy.deepcopy(evidence)
    if mutation == "changed_arguments":
        changed["approval"]["payload"]["files"][0]["diff"] = '{"command":"rm -rf /tmp"}'
    elif mutation == "different_invocation":
        changed["claim"]["source_invocation_id"] += ":other"
    elif mutation == "missing_claim":
        changed["claim"] = None
    else:
        changed["claim_events"].append(copy.deepcopy(changed["claim_events"][0]))

    with pytest.raises(EvidenceError):
        _verify(changed)
