from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "runtime" / "mcp_approval_demo_validation.sqlite3"

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_DB_PATH", str(DB_PATH))

if Path(os.environ["AIXION_DB_PATH"]) == DB_PATH and DB_PATH.exists():
    DB_PATH.unlink()

sys.path.insert(0, str(ROOT))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def post(client: TestClient, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post(path, json=payload or {})
    require(response.status_code == 200, f"POST {path} failed: {response.status_code} {response.text}")
    return response.json()


def get(client: TestClient, path: str) -> Any:
    response = client.get(path)
    require(response.status_code == 200, f"GET {path} failed: {response.status_code} {response.text}")
    return response.json()


def log(message: str) -> None:
    print(f"[mcp-demo] {message}")


def main() -> None:
    from app.main import app
    from app.mcp_gateway import FORWARDED_AFTER_APPROVAL
    from app.mcp_gateway_routes import child_received_count_for_test, reset_gateway_runtime_for_test
    from app.models import MCPPendingStatus
    from app.store import store

    client = TestClient(app)
    store.reset()
    reset_gateway_runtime_for_test()

    project = post(
        client,
        "/projects",
        {
            "name": "MCP approval demo validation",
            "description": "Proof that mutating MCP calls wait for approval.",
            "mode": "STRICT",
            "rules": ["Mutating MCP tools require explicit approval"],
        },
    )
    project_id = project["id"]
    log(f"created project: {project_id}")

    submitted = post(
        client,
        "/mcp-gateway/requests",
        {
            "project_id": project_id,
            "request": {
                "server_name": "child-test-server",
                "tool_name": "update_config",
                "arguments": {"key": "demo", "value": "approved-value"},
                "session_id": "demo-validation-session",
                "requested_by": "demo-mcp-client",
                "mutating": True,
            },
        },
    )
    approval_id = submitted["approval_request_id"]
    require(submitted["forwarded"] is False, "Gateway forwarded before approval")
    require(submitted["approval_required"] is True, "Gateway did not require approval")
    require(approval_id is not None, "Gateway did not create an approval")
    require(child_received_count_for_test() == 0, "Downstream server received call before approval")
    log(f"gateway created approval: {approval_id}")
    log("downstream received count before approval: 0")

    pending_before = get(client, f"/mcp-gateway/pending-requests?approval_request_id={approval_id}")
    require(len(pending_before) == 1, "Expected one pending request")
    require(pending_before[0]["status"] == MCPPendingStatus.WAITING_FOR_APPROVAL, "Pending request is not waiting")
    log(f"queue status before approval: {pending_before[0]['status']}")

    approval = post(
        client,
        f"/approvals/{approval_id}/decision",
        {"decision": "approve", "reason": "Demo approval path validation"},
    )
    require(approval["status"] == "APPROVED", "Approval decision did not approve")
    require(bool(approval["approved_payload_hash"]), "Approved payload hash missing")
    require(child_received_count_for_test() == 0, "Downstream server received call before gateway resolve")
    log("approval status: APPROVED")
    log("approved payload hash: present")

    resolved = post(client, f"/mcp-gateway/approvals/{approval_id}/resolve")
    require(resolved["forwarded"] is True, f"Gateway did not forward after approval: {resolved}")
    require(child_received_count_for_test() == 1, "Downstream server did not receive exactly one call")
    log("gateway resolve forwarded: true")
    log("downstream received count after resolve: 1")

    pending_after = get(client, f"/mcp-gateway/pending-requests?approval_request_id={approval_id}")
    require(len(pending_after) == 1, "Expected one pending request after resolve")
    require(pending_after[0]["status"] == MCPPendingStatus.FORWARDED, "Pending request did not become FORWARDED")
    log(f"queue status after resolve: {pending_after[0]['status']}")

    second_resolve = post(client, f"/mcp-gateway/approvals/{approval_id}/resolve")
    require(second_resolve["forwarded"] is False, "Second resolve forwarded again")
    require(child_received_count_for_test() == 1, "Second resolve duplicated downstream call")
    log("second resolve forwarded: false")
    log("downstream received count after second resolve: 1")

    audit_events = get(client, "/audit")
    event_types = [event["event_type"] for event in audit_events]
    require("mcp.approval_requested" in event_types, "Missing mcp.approval_requested audit event")
    require("approval.decision" in event_types, "Missing approval.decision audit event")
    require(FORWARDED_AFTER_APPROVAL in event_types, "Missing forwarding audit event")
    require(event_types.count(FORWARDED_AFTER_APPROVAL) == 1, "Forwarding audit event was duplicated")
    log("audit contains request, decision, and exactly-one forwarding event")

    print("\nMCP approval demo validation PASSED")


if __name__ == "__main__":
    main()
