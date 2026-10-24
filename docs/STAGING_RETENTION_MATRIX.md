# Staging Retention Matrix

**Status:** Unapproved engineering proposal. This is not the published privacy policy, legal advice, or authorization to delete data.
**Scope:** Offline preparation for Slice 7 in [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md).
**Prepared:** 2026-10-22.

## Decision rule

No retention period below is approved for production. Where the repository exposes a configured lifetime or a recommended audit duration, that value is implementation evidence—not an owner/legal decision and not proof of deletion. Do not enable a purge, archival, or destructive lifecycle rule while the applicable period, scope, exceptions, and recovery requirements remain unapproved. Keep data access disabled after an account-deletion request while an operator handles the request under an approved policy.

## Data-class matrix

| Data class | Proposed duration / current value | Enforcement and evidence in this checkout | Deletion, export, and recovery behavior | Approval still required |
| --- | --- | --- | --- | --- |
| Pending approvals | Request expiry is currently controlled by `agent_approval_ttl_seconds` (default 600 seconds); review the configured value for the target environment. | API creates an expiry and expiry processing exists in `backend/app/approval_expiry.py`; selected routes also assign the configured deadline in `backend/app/main.py` and `backend/app/agent_routes.py`. This is request-lifecycle expiry, not a data-retention purge. | Expiry changes lifecycle state; it does not establish deletion of the request or its audit record. Export is available through the audit export API, subject to its authorization and limits. | Owner must approve the pending-request TTL and whether/when expired request payloads may be removed or minimized. Legal/security reviewers must decide whether decision evidence must be retained. |
| Final approvals and audit events | **Unresolved.** `/audit/retention-policy` reports recommended 365 days online and 2,555 days archived; those are recommendations only. | `backend/app/audit_routes.py` defines `AUDIT_RETENTION_POLICY_VERSION`, these recommended values, and `deletion_job_enabled=false`. The endpoint explicitly says retention is not automatically enforced. Audit export and recovery export are available to authorized roles; reads fail closed when the PostgreSQL runtime lacks unified audit/recovery binding. | No automatic archive or deletion is enabled. Audit export is bounded (maximum 1,000 events per request). Recovery snapshot export is not a retention or purge mechanism. Preserve audit integrity and do not infer deletion from an approval's terminal state. | Owner, privacy/legal, security, and incident-response reviewers must approve online/archive periods, event classes/exceptions, anonymization rules, legal holds, export access, and whether deletion can ever be enabled. |
| Account profiles, sessions, and deletion requests | **Unresolved** for profile/deletion records. Session expiry is implemented separately and is not the account-data retention period. | `POST /auth/account-deletion-request` records a request, revokes active sessions, and disables account access; `backend/app/auth_routes.py` states final deletion/anonymization is an operator action. No automated completion/purge path was found in this implementation. | The API returns `RECEIVED`; no completion SLA, account export, or automated profile/anonymization behavior is established by that route. The public [account-deletion page](public/account-deletion.html) still contains TODOs for exact timelines and final deletion/anonymization rules. Backups may retain data until their separately approved window expires. | Owner and privacy/legal reviewers must set request completion SLA, profile/session/deletion-record periods, export process, dependent-record handling, anonymization rules, exceptions, and customer-facing wording before publication or account onboarding. |
| Application and infrastructure logs | **Unresolved.** No approved log-retention duration is configured by this proposal. | [STAGING_HANDOFF_COST_MODEL.md](STAGING_HANDOFF_COST_MODEL.md) and [PRE_BILLING_ALERT_MATRIX.md](PRE_BILLING_ALERT_MATRIX.md) identify log retention as an input. The alert matrix says logs must be redacted, but no deployed Cloud Logging bucket/sink/lifecycle setting is evidenced. | No Cloud Logging lifecycle or deletion behavior is configured or verified. Log retention must not be assumed from application log rotation or from Cloud Run defaults; confirm the actual selected service/bucket behavior during an authorized staging review. | Owner, privacy/legal, and incident-response operator must approve durations by log class, access, redaction, legal hold, export, and deletion. Cloud cost estimate must include the approved retention and ingestion assumptions. |
| PostgreSQL backups, PITR/WAL, and logical exports | **Unresolved.** The local drill has no production retention interval. | [POSTGRES_BACKUP_RESTORE_RUNBOOK.md](POSTGRES_BACKUP_RESTORE_RUNBOOK.md) requires the staging owner to decide backup schedule, retention window, PITR/WAL retention, RPO/RTO, and export controls. Its recorded drill is local and synthetic; no Cloud SQL retention setting or cloud restore has been verified. | The runbook requires an isolated restore target and checksum/migration/data verification. The [spend-stop and teardown checklist](STAGING_SPEND_STOP_AND_TEARDOWN.md) requires explicit decisions before deleting instances, automated/on-demand backups, PITR/WAL, or exports. | Owner, recovery operator, and privacy/legal reviewers must approve retention, RPO/RTO, encryption/location, access, export lifecycle, restore evidence, and maximum recurring storage cost. |
| Connector credentials and credential audit records | **Credential validity:** default maximum 30 days (`AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS`, bounded 300–2,592,000 seconds). **Credential-record retention after expiry/revocation:** unresolved. | [EXTERNAL_AGENT_CREDENTIAL_LIFETIME.md](EXTERNAL_AGENT_CREDENTIAL_LIFETIME.md) documents expiry checks in SQLite and PostgreSQL and rotation invalidation. Revocation/expiry prevents authentication; it does not prove that the credential row/hash or its audit event is deleted. [STAGING_SECRET_INVENTORY.md](STAGING_SECRET_INVENTORY.md) calls for old secret versions to be disabled only after rollback/recovery needs are covered. | Raw connector tokens are returned only at registration/rotation; stored credential material is hashed. Rotation/revocation and host secure-store removal are required operationally. No automatic cleanup period for credential records, audit events, or Secret Manager versions is established here. | Owner and security/privacy reviewers must approve token lifetime for the deployment, post-revocation record retention, audit linkage, secure-store removal, secret-version disable/delete windows, and emergency-revocation procedure. |

## Required sign-off record before staging data

For every row, record the exact policy value or explicitly mark it not applicable, the policy/version owner, approver(s), effective date, enforcement mechanism, verification evidence, exceptions/legal-hold path, and rollback/recovery consequence. Reconcile the signed matrix with:

- The API response from authenticated `GET /audit/retention-policy` and its `deletion_job_enabled` value.
- The public account-deletion and privacy pages, including removal of publication TODOs only after review.
- PostgreSQL backup/PITR configuration, logical-export storage lifecycle, and a measured isolated restore drill.
- Cloud Logging bucket/sink retention and redaction controls.
- Connector credential expiry/revocation behavior and secret-version retirement procedure.
- The cost calculator and spend-stop/teardown inventory.

If a value or enforcement is unknown, keep it **UNRESOLVED** and block staging use with real user data. This matrix intentionally chooses no legal retention period and enables no destructive behavior.

## References

- [Audit retention policy implementation](../backend/app/audit_routes.py)
- [Account deletion request implementation](../backend/app/auth_routes.py)
- [Account deletion publication page](public/account-deletion.html)
- [PostgreSQL backup and restore runbook](POSTGRES_BACKUP_RESTORE_RUNBOOK.md)
- [Staging spend stop and teardown checklist](STAGING_SPEND_STOP_AND_TEARDOWN.md)
- [External-agent credential lifetime](EXTERNAL_AGENT_CREDENTIAL_LIFETIME.md)
- [Staging secret inventory](STAGING_SECRET_INVENTORY.md)
- [Staging alert matrix](PRE_BILLING_ALERT_MATRIX.md)
- [Staging handoff and cost model](STAGING_HANDOFF_COST_MODEL.md)
