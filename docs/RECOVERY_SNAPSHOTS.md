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
device_registrations
```

Why: those collections can contain password hashes, session hashes, invitation state, agent secret hashes, or device tokens. A casual full export endpoint would be a security foot-gun.

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
    "excluded_sensitive_collections": [],
    "entities": {}
  }
}
```

## Validation checks

Validation checks:

```text
format_version is supported
expected collections are lists
entity payloads validate against current Pydantic models
unknown collections are rejected
missing migration metadata creates a warning
sensitive exclusion creates a warning
```

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
