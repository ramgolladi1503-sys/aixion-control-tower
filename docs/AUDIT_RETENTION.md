# Audit Export and Retention Controls

Aixion Control Tower now has a first-pass audit export and retention policy foundation.

This is not a compliance system. It is the minimum operational discipline needed before audit logs grow without control or are casually exported without limits.

## Endpoints

Maintainer-or-owner endpoints:

```text
GET /audit/export
GET /audit/retention-policy
```

Reviewer access remains available for normal audit event lookup:

```text
GET /audit/events
GET /audit/events/{audit_event_id}
```

## Export filters

The export endpoint supports the same core filters as audit lookup, plus created-time windows:

```text
event_type
entity_id
actor
detail_key
detail_value
created_after
created_before
limit
```

Example:

```bash
curl -H "Authorization: Bearer <maintainer-token>" \
  "http://localhost:8000/audit/export?event_type=approval.created&limit=100"
```

Created-time window example:

```bash
curl -H "Authorization: Bearer <maintainer-token>" \
  "http://localhost:8000/audit/export?created_after=2026-05-01T00:00:00Z&created_before=2026-05-31T23:59:59Z"
```

## Export limits

Exports are intentionally bounded:

```text
default limit: 1000
maximum limit: 1000
minimum limit: 1
```

The response includes:

```text
generated_at
count
limit
truncated
filters
retention_policy
events
```

If more events match than the limit allows, `truncated` is true.

## Redaction behavior

Export redacts sensitive detail keys before returning audit events.

Sensitive key markers include:

```text
password
secret
token
apikey
authorization
credential
serverkey
```

This applies recursively to nested dictionaries and lists.

Example:

```json
{
  "agent_token": "[REDACTED]",
  "nested": {
    "api_secret": "[REDACTED]",
    "safe": "visible"
  }
}
```

Hard truth: redaction by key marker is a useful guardrail, not a guarantee. Engineers must still avoid writing raw credentials into audit details in the first place.

## Retention policy foundation

The policy endpoint returns the current recommended policy:

```text
policy_version: aixion-audit-retention-v1
recommended_online_retention_days: 365
recommended_archive_retention_days: 2555
deletion_job_enabled: false
```

The deletion job is deliberately disabled in this foundation PR.

## What this does not do yet

This PR does not add:

```text
automatic audit deletion
automatic archival
cloud object storage
signed export artifacts
encrypted export files
compliance-specific legal hold
admin UI for audit export
Android audit export screen
```

## Operational guidance

Use audit export for:

```text
incident review
release validation evidence
operator handoff
manual compliance review
recovery investigation
agent behavior review
```

Do not use audit export as a database backup. Recovery snapshots and database backups are separate concerns.

## Why deletion is not included yet

Deleting audit events is dangerous if done before the product has clear incident, recovery, legal, and operator requirements.

The safe sequence is:

```text
1. bounded export
2. redaction
3. retention policy visibility
4. archive strategy
5. restore/review process
6. deletion job
```

This PR implements steps 1 through 3 only.
