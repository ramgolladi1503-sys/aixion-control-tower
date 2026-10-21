from __future__ import annotations

import hashlib
import json
import stat
import sqlite3

import pytest

from app.trust_crypto import sha256_hex
from app.trust_models import TrustEvent, TrustEventType
from scripts import audit_legacy_sqlite
from scripts.audit_legacy_sqlite import audit, main


def _create_db(path, rows, *, seed_workspace_owners=True):
    rows = list(rows)
    if seed_workspace_owners:
        existing_user_ids = {
            entity_id for entity_type, entity_id, _ in rows if entity_type == "user"
        }
        for entity_type, workspace_id, payload in tuple(rows):
            if entity_type != "workspace":
                continue
            owner_id = payload.get("owner_user_id")
            if owner_id and owner_id not in existing_user_ids:
                rows.append(
                    (
                        "user",
                        owner_id,
                        {
                            "id": owner_id,
                            "email": f"{owner_id}@example.test",
                            "password_hash": "hash",
                            "password_salt": "salt",
                            "workspace_id": workspace_id,
                        },
                    )
                )
                existing_user_ids.add(owner_id)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE kv_store (entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(entity_type, entity_id))"
        )
        conn.execute(
            "CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            ("0001_baseline_kv_store", "Create baseline key-value persistence table", "2026-01-01T00:00:00+00:00"),
        )
        conn.executemany(
            "INSERT INTO kv_store VALUES (?, ?, ?)",
            [(kind, entity_id, json.dumps(payload)) for kind, entity_id, payload in rows],
        )


def _valid_trust_event_payload():
    event = TrustEvent(
        sequence=1,
        event_type=TrustEventType.ACTION_ALLOWED,
        entity_type="approval_request",
        entity_id="a1",
        workspace_id="ws1",
        actor="reviewer@example.test",
        payload={"decision": "ALLOW"},
        previous_hash=None,
        event_hash="pending",
    )
    material = {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": event.actor,
        "correlation_id": event.correlation_id,
        "payload": event.payload,
        "previous_hash": event.previous_hash,
        "created_at": event.created_at,
    }
    event.event_hash = sha256_hex(material)
    return event.model_dump(mode="json")


def test_audit_is_read_only_and_flags_unmapped_legacy_user(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1", "created_at": "2026-01-01T00:00:00Z"}),
        ("user", "u1", {"id": "u1", "email": "owner@example.com", "password_hash": "hash", "password_salt": "salt", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}),
        ("project", "p1", {"id": "p1", "name": "Project", "workspace_id": "ws1", "created_at": "2026-01-01T00:00:00Z"}),
        ("approval_request", "a1", {"id": "a1", "project_id": "p1", "title": "Review", "summary": "safe", "target_branch": "main", "files": [], "risk": {"level": "LOW"}}),
        ("user", "legacy", {"id": "legacy", "email": "old@example.com", "password_hash": "hash", "password_salt": "salt"}),
    ])
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    result = audit(path)

    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after
    assert result["read_only"] is True
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["ownership_review_counts"] == {"user": 1}
    assert result["record_findings"] == [{
        "entity_type": "user",
        "entity_id": "legacy",
        "explicit_workspace_id": None,
        "inferred_workspace_id": None,
        "findings": ["workspace_unresolved"],
    }]
    assert "ownership_requires_operator_mapping" in result["findings"]


def test_structural_review_pass_never_authorizes_production_import(tmp_path):
    path = tmp_path / "structurally_valid.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {
            "id": "ws1", "name": "Account", "owner_user_id": "owner1",
        }),
        ("project", "p1", {
            "id": "p1", "name": "Project", "workspace_id": "ws1",
        }),
    ])

    result = audit(path)

    assert result["status"] == "STRUCTURAL_REVIEW_PASSED", result["findings"]
    assert result["scope"] == "read_only_structural_review_only"
    assert result["migration_authorized"] is False


def test_audit_verifies_valid_trust_event_hash_chain(tmp_path):
    path = tmp_path / "trust_event_chain.sqlite3"
    event = _valid_trust_event_payload()
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("project", "p1", {"id": "p1", "name": "Project", "workspace_id": "ws1"}),
        ("approval_request", "a1", {
            "id": "a1", "project_id": "p1", "title": "Review", "summary": "safe",
            "target_branch": "main", "files": [], "risk": {"level": "LOW"},
            "workspace_id": "ws1",
        }),
        ("trust_event", event["id"], event),
    ])

    result = audit(path)

    assert result["status"] == "STRUCTURAL_REVIEW_PASSED", result["findings"]
    assert result["trust_event_chain_verification"] == {
        "valid": True,
        "event_count": 1,
        "first_invalid_sequence": None,
        "reason": "",
    }


def test_audit_blocks_tampered_trust_event_chain(tmp_path):
    path = tmp_path / "tampered_trust_event_chain.sqlite3"
    event = _valid_trust_event_payload()
    event["payload"]["decision"] = "BLOCK"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("project", "p1", {"id": "p1", "name": "Project", "workspace_id": "ws1"}),
        ("approval_request", "a1", {
            "id": "a1", "project_id": "p1", "title": "Review", "summary": "safe",
            "target_branch": "main", "files": [], "risk": {"level": "LOW"},
            "workspace_id": "ws1",
        }),
        ("trust_event", event["id"], event),
    ])

    result = audit(path)

    assert result["status"] == "REVIEW_REQUIRED"
    assert "trust_event_chain_invalid" in result["findings"]
    assert result["trust_event_chain_verification"]["reason"] == "Event payload hash mismatch."


def test_audit_resolves_agent_registration_and_revocation_to_agent_workspace(tmp_path):
    path = tmp_path / "agent_audit_scope.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("external_agent", "agent1", {
            "id": "agent1",
            "provider": "ANTIGRAVITY",
            "display_name": "Antigravity",
            "workspace_id": "ws1",
        }),
        ("audit_event", "audit_registered", {
            "id": "audit_registered",
            "event_type": "agent.registered",
            "entity_id": "agent1",
            "actor": "owner@example.test",
            "details": {},
        }),
        ("audit_event", "audit_revoked", {
            "id": "audit_revoked",
            "event_type": "agent.token_revoked",
            "entity_id": "agent1",
            "actor": "owner@example.test",
            "details": {},
        }),
    ])

    result = audit(path)

    assert result["status"] == "STRUCTURAL_REVIEW_PASSED"
    assert result["ownership_review_counts"] == {}
    assert result["migration_authorized"] is False


def test_audit_keeps_agent_event_unresolved_without_exact_agent_parent(tmp_path):
    path = tmp_path / "orphan_agent_audit_scope.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("audit_event", "audit_orphan", {
            "id": "audit_orphan",
            "event_type": "agent.token_revoked",
            "entity_id": "missing-agent",
            "actor": "owner@example.test",
            "details": {},
        }),
    ])

    result = audit(path)

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["ownership_review_counts"] == {"audit_event": 1}
    assert result["record_findings"][0]["findings"] == ["workspace_unresolved"]


def test_cli_writes_private_report_and_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    database = tmp_path / "valid.sqlite3"
    report_path = tmp_path / "audit.json"
    _create_db(database, [
        ("workspace", "ws1", {
            "id": "ws1", "name": "Account", "owner_user_id": "owner1",
        }),
        ("project", "p1", {
            "id": "p1", "name": "Project", "workspace_id": "ws1",
        }),
    ])
    monkeypatch.setattr(
        "sys.argv",
        ["audit_legacy_sqlite.py", "--database", str(database), "--output", str(report_path)],
    )

    assert main() == 0
    report = json.loads(report_path.read_text())
    assert stat.S_IMODE(report_path.stat().st_mode) & 0o077 == 0
    assert report["migration_authorized"] is False
    assert report["status"] == "STRUCTURAL_REVIEW_PASSED"

    report_path.write_text("preserve-existing-report")
    assert main() == 2
    assert report_path.read_text() == "preserve-existing-report"
    assert "FileExistsError" in capsys.readouterr().err


def test_audit_reports_wal_presence_without_certifying_snapshot(tmp_path):
    path = tmp_path / "wal_source.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {
            "id": "ws1", "name": "Account", "owner_user_id": "owner1",
        }),
    ])
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
        ("future_entity", "wal-row", json.dumps({"value": "uncheckpointed"})),
    )
    writer.commit()
    assert path.with_name(path.name + "-wal").exists()

    result = audit(path)

    assert result["wal_sidecar_present"] is True
    assert result["snapshot_certified"] is False
    assert result["migration_authorized"] is False
    assert result["entity_counts"]["future_entity"] == 1
    writer.close()


def test_audit_reports_unknown_entity_and_invalid_payload_without_exposing_payload(tmp_path):
    path = tmp_path / "invalid.sqlite3"
    secret_marker = "DO_NOT_PRINT_SECRET"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("project", "bad", {"id": "bad", "workspace_id": "ws1", "name": secret_marker, "mode": "NOT_A_MODE"}),
        ("future_entity", "x", {"secret": secret_marker}),
    ])

    result = audit(path)
    rendered = json.dumps(result)

    assert result["invalid_payload_counts"] == {"project": 1}
    assert result["unknown_entity_counts"] == {"future_entity": 1}
    assert result["status"] == "REVIEW_REQUIRED"
    assert {"entity_type": "future_entity", "entity_id": "x"} == {
        key: result["record_findings"][0][key]
        for key in ("entity_type", "entity_id")
    }
    assert secret_marker not in rendered


def test_aggregate_migration_findings_are_represented_in_record_ledger(tmp_path):
    path = tmp_path / "finding_ledger.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {
            "id": "ws1", "name": "Account", "owner_user_id": "owner1",
        }),
        ("user", "owner1", {
            "id": "owner1", "email": "owner@example.test", "password_hash": "hash",
            "password_salt": "salt", "workspace_id": "ws1",
        }),
        ("user", "duplicate", {
            "id": "duplicate", "email": "OWNER@example.test", "password_hash": "hash2",
            "password_salt": "salt2", "workspace_id": "ws1",
        }),
        ("user", "unassigned", {
            "id": "unassigned", "email": "unassigned@example.test",
            "password_hash": "hash3", "password_salt": "salt3",
        }),
        ("project", "p1", {
            "id": "p1", "name": "Project", "workspace_id": "ws1",
        }),
        ("project", "invalid-project", {
            "id": "invalid-project", "name": "Invalid", "workspace_id": "ws1",
            "mode": "NOT_A_MODE",
        }),
        ("approval_request", "bad-approval", {
            "id": "bad-approval", "project_id": "p1", "work_order_id": "missing-work-order",
            "workspace_id": "ws1", "title": "Review", "summary": "Change",
            "target_branch": "main", "files": [], "risk": {"level": "LOW"},
        }),
        ("future_entity", "unknown1", {"value": "unknown"}),
    ])

    result = audit(path)

    def count_by_entity(code):
        counts = {}
        for record in result["record_findings"]:
            if code in record["findings"]:
                counts[record["entity_type"]] = counts.get(record["entity_type"], 0) + 1
        return dict(sorted(counts.items()))

    assert count_by_entity("invalid_model_payload") == result["invalid_payload_counts"]
    assert count_by_entity("unknown_entity_type") == result["unknown_entity_counts"]
    assert {
        entity_type: count
        for entity_type, count in (
            (name, count_by_entity("workspace_unresolved").get(name, 0)
             + count_by_entity("workspace_not_registered").get(name, 0))
            for name in set(result["ownership_review_counts"])
        )
    } == result["ownership_review_counts"]
    assert count_by_entity("workspace_parent_mismatch") == result["ownership_conflict_counts"]
    assert count_by_entity("duplicate_normalized_email") == result["identity_conflict_counts"]
    assert count_by_entity("parent_relationship_conflict") == result["parent_relationship_conflict_counts"]


def test_integrity_check_failure_blocks_structural_review_pass(tmp_path, monkeypatch):
    path = tmp_path / "integrity_failure.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {
            "id": "ws1", "name": "Account", "owner_user_id": "owner1",
        }),
    ])
    monkeypatch.setattr(audit_legacy_sqlite, "_integrity_check", lambda _conn: ["corrupt"])

    result = audit(path)

    assert result["integrity_check"] == ["corrupt"]
    assert "sqlite_integrity_check_failed" in result["findings"]
    assert result["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize(
    ("migration_id", "expected_finding"),
    [
        (None, "missing_migration_table"),
        ("9999_future_migration", "unknown_migrations"),
    ],
)
def test_missing_or_unknown_schema_migrations_block_structural_pass(
    tmp_path, migration_id, expected_finding
):
    path = tmp_path / f"migration_{migration_id or 'missing'}.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {
            "id": "ws1", "name": "Account", "owner_user_id": "owner1",
        }),
    ])
    with sqlite3.connect(path) as conn:
        if migration_id is None:
            conn.execute("DROP TABLE schema_migrations")
        else:
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (migration_id, "Future migration", "2026-10-18T00:00:00+00:00"),
            )

    result = audit(path)

    assert expected_finding in result["findings"]
    assert result["status"] == "REVIEW_REQUIRED"


def test_audit_resolves_child_ownership_and_flags_parent_workspace_mismatch(tmp_path):
    path = tmp_path / "relations.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account 1", "owner_user_id": "u1"}),
        ("workspace", "ws2", {"id": "ws2", "name": "Account 2", "owner_user_id": "u2"}),
        ("user", "u1", {"id": "u1", "email": "one@example.com", "password_hash": "hash", "password_salt": "salt", "workspace_id": "ws1"}),
        ("user", "u2", {"id": "u2", "email": "two@example.com", "password_hash": "hash", "password_salt": "salt", "workspace_id": "ws2"}),
        ("project", "p1", {"id": "p1", "name": "Project", "workspace_id": "ws1"}),
        ("agent_task", "t1", {"id": "t1", "title": "Task", "goal": "Run", "project_id": "p1", "workspace_id": "ws1"}),
        ("agent_task_event", "te1", {"id": "te1", "task_id": "t1"}),
        ("approval_request", "a1", {"id": "a1", "project_id": "p1", "workspace_id": "ws2", "title": "Review", "summary": "safe", "target_branch": "main", "files": [], "risk": {"level": "LOW"}}),
    ])

    result = audit(path)

    assert result["ownership_conflict_counts"] == {"approval_request": 1}
    assert result["ownership_review_counts"] == {}
    assert "ownership_parent_mismatch" in result["findings"]


def test_audit_flags_same_workspace_parent_project_mismatches(tmp_path):
    path = tmp_path / "parent_mismatches.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("project", "p1", {"id": "p1", "name": "Approval project", "workspace_id": "ws1"}),
        ("project", "p2", {"id": "p2", "name": "Parent project", "workspace_id": "ws1"}),
        ("idea", "i2", {"id": "i2", "project_id": "p2", "workspace_id": "ws1", "title": "Other project", "raw_text": "Idea"}),
        ("work_order", "wo2", {"id": "wo2", "project_id": "p2", "idea_id": "i2", "workspace_id": "ws1", "goal": "Other project work"}),
        ("work_order", "wo_bad", {"id": "wo_bad", "project_id": "p1", "idea_id": "i2", "workspace_id": "ws1", "goal": "Mismatched idea"}),
        ("approval_request", "a1", {"id": "a1", "project_id": "p1", "work_order_id": "wo2", "workspace_id": "ws1", "title": "Mismatched work order", "summary": "Review", "target_branch": "main", "files": [], "risk": {"level": "LOW"}}),
    ])

    result = audit(path)

    assert result["parent_relationship_conflict_counts"] == {
        "approval_request": 1,
        "work_order": 1,
    }
    assert "parent_relationship_conflicts" in result["findings"]


def test_audit_allows_workspace_level_task_linked_to_project_approval(tmp_path):
    path = tmp_path / "workspace_task.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("project", "p1", {"id": "p1", "name": "Project", "workspace_id": "ws1"}),
        ("approval_request", "a1", {"id": "a1", "project_id": "p1", "workspace_id": "ws1", "title": "Review", "summary": "Review", "target_branch": "main", "files": [], "risk": {"level": "LOW"}}),
        ("agent_task", "t1", {"id": "t1", "title": "Workspace task", "goal": "Link to approval", "workspace_id": "ws1", "approval_request_id": "a1"}),
    ])

    result = audit(path)

    assert result["parent_relationship_conflict_counts"] == {}
    assert "parent_relationship_conflicts" not in result["findings"]


def test_audit_flags_agent_and_connector_project_scopes_outside_workspace(tmp_path):
    path = tmp_path / "principal_scope_mismatch.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account 1", "owner_user_id": "u1"}),
        ("workspace", "ws2", {"id": "ws2", "name": "Account 2", "owner_user_id": "u2"}),
        ("project", "p1", {"id": "p1", "name": "Account 1 project", "workspace_id": "ws1"}),
        ("project", "p2", {"id": "p2", "name": "Account 2 project", "workspace_id": "ws2"}),
        ("external_agent", "agent1", {"id": "agent1", "provider": "ANTIGRAVITY", "display_name": "Agent", "workspace_id": "ws1", "allowed_project_ids": ["p2"]}),
        ("agent_connector", "connector1", {"id": "connector1", "name": "Connector", "workspace_id": "ws1", "allowed_project_ids": ["p2"]}),
    ])

    result = audit(path)

    assert result["parent_relationship_conflict_counts"] == {
        "agent_connector": 1,
        "external_agent": 1,
    }
    assert result["record_findings"] == [
        {
            "entity_type": "agent_connector",
            "entity_id": "connector1",
            "explicit_workspace_id": "ws1",
            "inferred_workspace_id": None,
            "findings": ["parent_relationship_conflict"],
        },
        {
            "entity_type": "external_agent",
            "entity_id": "agent1",
            "explicit_workspace_id": "ws1",
            "inferred_workspace_id": None,
            "findings": ["parent_relationship_conflict"],
        },
    ]
    assert "parent_relationship_conflicts" in result["findings"]


def test_record_findings_are_payload_free_and_deterministically_sorted(tmp_path):
    path = tmp_path / "record_findings.sqlite3"
    secret = "DO_NOT_INCLUDE_PAYLOAD_OR_SECRET"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "u1"}),
        ("project", "z-project", {"id": "z-project", "name": secret, "workspace_id": "missing-workspace"}),
        ("future_entity", "a-record", {"secret": secret}),
    ])

    result = audit(path)
    serialized_findings = json.dumps(result["record_findings"])

    assert [(item["entity_type"], item["entity_id"]) for item in result["record_findings"]] == [
        ("future_entity", "a-record"),
        ("project", "z-project"),
    ]
    assert secret not in serialized_findings
    assert "payload" not in serialized_findings


def test_audit_flags_session_device_and_invite_owner_mismatches(tmp_path):
    path = tmp_path / "account_credential_mismatch.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account 1", "owner_user_id": "owner1"}),
        ("workspace", "ws2", {"id": "ws2", "name": "Account 2", "owner_user_id": "owner2"}),
        ("user", "reviewer1", {"id": "reviewer1", "email": "reviewer@example.test", "password_hash": "hash", "password_salt": "salt", "workspace_id": "ws1"}),
        ("session", "session1", {"id": "session1", "user_id": "missing-user", "token_hash": "hash", "expires_at": "2027-01-01T00:00:00Z"}),
        ("device_registration", "device1", {"id": "device1", "user_id": "reviewer1", "workspace_id": "ws2", "token": "device-token"}),
        ("invite", "invite1", {"id": "invite1", "email": "invitee@example.test", "token_hash": "hash", "expires_at": "2027-01-01T00:00:00Z", "created_by_user_id": "reviewer1", "workspace_id": "ws2"}),
    ])

    result = audit(path)

    assert result["parent_relationship_conflict_counts"] == {
        "device_registration": 1,
        "invite": 1,
        "session": 1,
    }


def test_audit_flags_workspace_owner_that_is_missing(tmp_path):
    path = tmp_path / "orphan_workspace_owner.sqlite3"
    _create_db(
        path,
        [("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "missing-owner"})],
        seed_workspace_owners=False,
    )

    result = audit(path)

    assert result["parent_relationship_conflict_counts"] == {"workspace": 1}


def test_audit_quarantines_multiple_workspaces_for_one_unassigned_owner(tmp_path):
    path = tmp_path / "duplicate_workspace_owner.sqlite3"
    _create_db(
        path,
        [
            ("workspace", "ws1", {"id": "ws1", "name": "Account 1", "owner_user_id": "owner1"}),
            ("workspace", "ws2", {"id": "ws2", "name": "Account 2", "owner_user_id": "owner1"}),
            ("user", "owner1", {
                "id": "owner1",
                "email": "owner@example.test",
                "password_hash": "hash",
                "password_salt": "salt",
            }),
            ("device_registration", "device1", {
                "id": "device1", "user_id": "owner1", "token": "device-token",
            }),
        ],
        seed_workspace_owners=False,
    )

    result = audit(path)

    assert result["parent_relationship_conflict_counts"] == {
        "device_registration": 1,
        "workspace": 2,
    }
    assert result["ownership_review_counts"] == {"device_registration": 1, "user": 1}
    assert "workspace_owner_has_multiple_workspaces" in result["findings"]
    assert result["record_findings"] == [
        {
            "entity_type": "device_registration",
            "entity_id": "device1",
            "explicit_workspace_id": None,
            "inferred_workspace_id": None,
            "findings": ["parent_relationship_conflict", "workspace_unresolved"],
        },
        {
            "entity_type": "user",
            "entity_id": "owner1",
            "explicit_workspace_id": None,
            "inferred_workspace_id": None,
            "findings": ["owner_has_multiple_workspaces", "workspace_unresolved"],
        },
        {
            "entity_type": "workspace",
            "entity_id": "ws1",
            "explicit_workspace_id": None,
            "inferred_workspace_id": None,
            "findings": ["owner_has_multiple_workspaces", "parent_relationship_conflict"],
        },
        {
            "entity_type": "workspace",
            "entity_id": "ws2",
            "explicit_workspace_id": None,
            "inferred_workspace_id": None,
            "findings": ["owner_has_multiple_workspaces", "parent_relationship_conflict"],
        },
    ]


def test_audit_reports_malformed_workspace_owner_without_crashing(tmp_path):
    path = tmp_path / "malformed_workspace_owner.sqlite3"
    _create_db(
        path,
        [("workspace", "ws1", {
            "id": "ws1",
            "name": "Account",
            "owner_user_id": ["not", "a", "user-id"],
        })],
        seed_workspace_owners=False,
    )

    result = audit(path)

    assert result["invalid_payload_counts"] == {"workspace": 1}
    assert result["parent_relationship_conflict_counts"] == {"workspace": 1}
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["record_findings"] == [{
        "entity_type": "workspace",
        "entity_id": "ws1",
        "explicit_workspace_id": None,
        "inferred_workspace_id": None,
        "findings": ["invalid_model_payload", "parent_relationship_conflict"],
    }]


def test_audit_flags_case_variant_duplicate_account_emails_without_exposing_payload(tmp_path):
    path = tmp_path / "duplicate_user_email.sqlite3"
    _create_db(
        path,
        [
            ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "owner1"}),
            ("user", "owner1", {
                "id": "owner1", "email": "Owner@example.test", "password_hash": "secret-hash",
                "password_salt": "salt", "workspace_id": "ws1",
            }),
            ("user", "duplicate", {
                "id": "duplicate", "email": " owner@EXAMPLE.test ", "password_hash": "other-secret",
                "password_salt": "other-salt", "workspace_id": "ws1",
            }),
        ],
        seed_workspace_owners=False,
    )

    result = audit(path)
    rendered = json.dumps(result)

    assert result["identity_conflict_counts"] == {"user": 2}
    assert "duplicate_normalized_user_email" in result["findings"]
    assert [item["entity_id"] for item in result["record_findings"]] == ["duplicate", "owner1"]
    assert all("duplicate_normalized_email" in item["findings"] for item in result["record_findings"])
    assert "secret-hash" not in rendered
    assert "other-secret" not in rendered


def test_audit_does_not_infer_invite_or_device_membership_for_unassigned_user(tmp_path):
    path = tmp_path / "unassigned_account_principal.sqlite3"
    _create_db(path, [
        ("workspace", "ws1", {"id": "ws1", "name": "Account", "owner_user_id": "owner1"}),
        ("user", "owner1", {"id": "owner1", "email": "owner@example.test", "password_hash": "hash", "password_salt": "salt", "workspace_id": "ws1"}),
        ("user", "unassigned", {"id": "unassigned", "email": "legacy@example.test", "password_hash": "hash", "password_salt": "salt"}),
        ("device_registration", "device1", {"id": "device1", "user_id": "unassigned", "workspace_id": "ws1", "token": "device-token"}),
        ("invite", "invite1", {"id": "invite1", "email": "invitee@example.test", "token_hash": "hash", "expires_at": "2027-01-01T00:00:00Z", "created_by_user_id": "unassigned", "workspace_id": "ws1"}),
    ])

    result = audit(path)

    assert result["parent_relationship_conflict_counts"] == {
        "device_registration": 1,
        "invite": 1,
    }
