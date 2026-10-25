from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from integrations.antigravity import certify_phone_e2e
from integrations.antigravity.certify_phone_e2e import (
    EvidenceError,
    _computed_approved_payload_hash,
    verify_records,
)


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
        "title": "Antigravity approval: run_command",
        "summary": "Review the exact Antigravity tool and arguments before this invocation continues.",
        "agent_name": "Antigravity",
        "target_branch": "current-workspace",
        "test_plan": ["Confirm the tool and arguments match the intended task"],
        "rollback_plan": "Deny this request if uncertain. Approval authorizes only this displayed invocation.",
        "approved_payload_hash": None,
        "files": [{
            "path": "antigravity://tool/run_command",
            "change_type": "tool_call",
            "diff": '{"command":"mkdir /tmp/aixion-e2e-marker"}',
        }],
    }
    payload["approved_payload_hash"] = _computed_approved_payload_hash(payload)
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
    assert result["approved_payload_hash"] == evidence["approval"]["payload"]["approved_payload_hash"]
    assert result["delivery_claim_events"] == 1


def test_certifier_rejects_payload_hash_mismatch_even_when_audits_repeat_it(evidence: dict) -> None:
    changed = copy.deepcopy(evidence)
    changed["approval"]["payload"]["title"] = "Tampered after approval"
    repeated_hash = changed["approval"]["payload"]["approved_payload_hash"]
    changed["decision_events"][0]["details"]["approved_payload_hash"] = repeated_hash
    changed["claim_events"][0]["details"]["approved_payload_hash"] = repeated_hash

    with pytest.raises(EvidenceError, match="frozen payload hash"):
        _verify(changed)


def test_certifier_recomputes_workspace_and_risk_bound_v2_hash(evidence: dict) -> None:
    changed = copy.deepcopy(evidence)
    payload = changed["approval"]["payload"]
    payload.update({
        "approved_payload_hash_version": 2,
        "workspace_id": "workspace_test",
        "risk": {"level": "HIGH", "reasons": ["mutation"]},
    })
    payload["approved_payload_hash"] = _computed_approved_payload_hash(payload)
    changed["decision_events"][0]["details"]["approved_payload_hash"] = payload["approved_payload_hash"]
    changed["claim_events"][0]["details"]["approved_payload_hash"] = payload["approved_payload_hash"]

    assert _verify(changed)["approved_payload_hash"] == payload["approved_payload_hash"]

    payload["risk"]["level"] = "LOW"
    with pytest.raises(EvidenceError, match="frozen payload hash"):
        _verify(changed)


def test_database_verifier_sets_read_only_before_any_query(
    evidence: dict, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    approval = evidence["approval"]
    decision = evidence["decision_events"][0]
    claim = evidence["claim"]
    claim_event = evidence["claim_events"][0]
    queries: list[str] = []

    class FakeCursor:
        current_query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            if query.lstrip().upper().startswith("SELECT"):
                assert queries and queries[0].strip().upper() == "SET TRANSACTION READ ONLY"
            queries.append(query)
            self.current_query = query

        def fetchone(self):
            if "FROM approval_requests" in self.current_query:
                return (
                    approval["approval_id"], approval["source_agent_id"],
                    approval["status"], approval["payload"],
                )
            if "FROM approval_delivery_claims" in self.current_query:
                return (claim["approval_id"], claim["source_agent_id"], claim["source_invocation_id"])
            raise AssertionError(f"Unexpected fetchone query: {self.current_query}")

        def fetchall(self):
            if "approval.decision" in self.current_query:
                return [(decision["approval_id"], decision["event_type"], decision["details"])]
            if "approval.delivery_claimed" in self.current_query:
                return [(claim_event["approval_id"], claim_event["event_type"], claim_event["details"])]
            raise AssertionError(f"Unexpected fetchall query: {self.current_query}")

    class FakeConnection:
        info = SimpleNamespace(host="127.0.0.1", dbname="aixion_phone_e2e")

        def cursor(self):
            return FakeCursor()

        def rollback(self):
            pass

        def close(self):
            pass

    marker = tmp_path / "fresh-marker"
    marker.write_text("created by approved command", encoding="utf-8")
    monkeypatch.setattr(certify_phone_e2e, "_connect", lambda _dsn: FakeConnection())
    args = SimpleNamespace(
        dsn="postgresql://fake",
        approval_id=approval["approval_id"],
        agent_id="agent_test",
        project_id="project_test",
        tool_name="run_command",
        expected_arguments={"command": "mkdir /tmp/aixion-e2e-marker"},
        marker=str(marker),
        started_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )

    result = certify_phone_e2e.verify_database(args)

    assert result["result"] == "PASS_DURABLE_APPROVAL_AND_FRESH_MARKER"
    assert queries[0] == "SET TRANSACTION READ ONLY"
    assert len(queries) == 5


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
