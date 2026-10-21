# Recovery Snapshots

Aixion Control Tower now has a safe recovery snapshot foundation for the SQLite MVP backend.

This is intentionally not a full credential backup system. It exports operational product state that is useful for demo recovery, validation, manual inspection, and future restore tooling.

## Endpoints

Owner-only endpoints are exposed through the audit router:

```text
GET  /audit/recovery/export
POST /audit/recovery/validate
```

## What export includes

The recovery snapshot includes:

```text
projects
mcp_child_servers
ideas
work_orders
approval_requests
mcp_pending_requests
test_runs
notifications
audit_events
applied_migrations
collection counts
format_version
generated_at
```

## What export deliberately excludes

The snapshot deliberately excludes sensitive credential/session collections:

```text
users
sessions
invites
external_agents
external_agent_credentials
agent_connectors
device_registrations
capability_leases
reviewer_attestations
proposed_actions
policy_decisions
action_authorizations
action_consumptions
credential_grants
trust_events
```

Why: those collections can contain password hashes, session hashes, invitation state, agent secrets, device tokens, or authorization material. A casual full export endpoint would be a security foot-gun.

Other operational collections are also excluded and are now listed separately
in the snapshot's `excluded_collections` field:

```text
workspaces
agent_tasks
agent_task_events
agent_runs
agent_run_steps
agent_run_events
agent_run_faults
```

This export is partial operational data, not a complete account backup. The
exclusion inventory must be reviewed before any migration or restore use.

## Export workflow

Use the export endpoint before risky maintenance, migration experiments, or demo reset work:

```bash
curl -H "Authorization: Bearer <owner-token>" \
  http://localhost:8000/audit/recovery/export > recovery-snapshot.json
```

Store the snapshot somewhere private. Even though credential/session collections are excluded, operational approval data and audit data may still contain sensitive project details.

## Validation workflow

Validate a snapshot before using it for manual recovery or future restore tooling:

```bash
curl -X POST \
  -H "Authorization: Bearer <owner-token>" \
  -H "Content-Type: application/json" \
  -d @validate-payload.json \
  http://localhost:8000/audit/recovery/validate
```

Payload shape:

```json
{
  "snapshot": {
    "format_version": "aixion-control-tower-recovery-v1",
    "generated_at": "2026-05-12T00:00:00Z",
    "applied_migrations": [],
    "counts": {},
    "excluded_collections": [],
    "excluded_sensitive_collections": [],
    "entities": {}
  }
}
```

## Validation checks

Validation checks:

```text
format_version is supported
all expected collections are present and lists
declared collection counts match actual rows
entity IDs are unique within each collection
entity payloads validate against current Pydantic models
unknown collections are rejected
the excluded-collection inventory is compared with the current backend schema
missing migration metadata creates a warning
sensitive exclusion creates a warning
```

Export captures collections and derives their counts while holding the store's
writer lock. This keeps the artifact internally consistent for the current
single-process SQLite profile. It does not make the export a transactionally
consistent PostgreSQL backup or a recoverable physical database snapshot.

## What this does not solve yet

This PR does not add a live restore endpoint.

That is deliberate. A live overwrite endpoint can destroy user/session state and create security problems if rushed.

This PR also does not add:

```text
SQLite physical file backup
scheduled backup jobs
encrypted backup storage
cloud object storage
point-in-time recovery
PostgreSQL restore workflow
credential/session restore
restore drills in CI
```

## Manual recovery use

For now, recovery snapshots are best used as:

```text
pre-maintenance export evidence
manual inspection artifact
future restore input contract
demo state recovery source
migration sanity comparison
```

Hard truth: this is recovery discipline, not disaster recovery. Real production still needs encrypted physical backups, restore drills, database hosting strategy, retention policy, and incident runbooks.
