# Staging Secret and Credential Inventory

**Status:** Design inventory only. It contains no secret values and authorizes no secret creation, IAM grant, billing, API enablement, or deployment.
**Scope:** Slice 7 pre-billing preparation. Staging project, identity names, secret versions, and operators remain unselected.

This inventory separates protected values from non-secret configuration. Exact Secret Manager resource names and access bindings must be reviewed after the owner selects the project. Use numeric pinned versions in deployment definitions; rotate by publishing and reviewing a new version, updating consumers, verifying health, then disabling the old version only after rollback and recovery needs are covered. Never place values in Git, Terraform state, shell history, command output, logs, container layers, or screenshots.

| Name / configuration key | Purpose | Consumer | Access boundary | Rotation / recovery | Needed for staging |
| --- | --- | --- | --- | --- | --- |
| `AIXION_POSTGRES_DSN` / `aixion-postgres-dsn` | PostgreSQL connection, using verified TLS (`sslmode=verify-full`) or the approved Cloud SQL connector path | API, push job, email job; separate short-lived migration identity | Each runtime identity reads this one named secret; migration identity is separate. Database grants are separate from Google IAM. | Rotate database credential through a reviewed database-role change, publish a new secret version, test consumers, and retain a rollback principal until verified. | Required only after Slice 1 and the project-specific database design pass. |
| `AIXION_LEASE_SIGNING_KEY` | Signs agent lease/trust material | API only | Secret access only for API runtime; not scheduler or dispatcher jobs. | Follow the trust-key rotation runbook and assess existing signed leases before retiring prior key material. | Required if the selected API runtime uses signed leases. |
| `AIXION_AUTH_RATE_LIMIT_HMAC_KEY` | HMAC identifiers for durable authentication rate limits | API only | Secret access only for API runtime; not scheduler or dispatcher jobs. | Preserve key continuity for imported counters or perform an explicit counter migration/reset decision before rotation. | Required if PostgreSQL authentication rate limits are enabled. |
| `AIXION_METRICS_TOKEN` | Authenticates the protected metrics scrape | API and the approved metrics scraper | API reads the token; scraper receives only the same token in its protected secret store. Do not pass to jobs or ordinary dashboards. | Rotate both copies together, verify scrape success, then revoke the prior token. | Required only if the staging metrics scrape is enabled. |
| `AIXION_EMAIL_SMTP_USERNAME` / `aixion-smtp-username` | SMTP authentication identity | Email job; API only if an inline email sender is explicitly enabled in an approved local profile | Email runtime service account reads only its username secret. The push job must not receive it. | Rotate with the SMTP provider, update the paired password version, send a controlled delivery check, then revoke the old credential. | Required if staging email delivery is enabled. |
| `AIXION_EMAIL_SMTP_PASSWORD` / `aixion-smtp-password` | SMTP authentication credential | Email job; API only if inline email is explicitly enabled | Email runtime service account reads only its password secret. The push job must not receive it. | Rotate with username as one change; never log SMTP exception text or recipient data. | Required if staging email delivery is enabled. |
| `AIXION_EMAIL_OUTBOX_KEY` / `aixion-email-outbox-key` | Encrypts verification codes stored in the email outbox | Email job and any explicitly approved producer that encrypts those rows | Read only by the email dispatcher and the producer that must encrypt the same outbox data. Do not pass to push job. | Preserve old key decryption capability until all rows encrypted under it have expired or been safely re-encrypted; use a reviewed key-version migration before removal. | Required if email outbox delivery is enabled. |
| Google service identity for FCM (no downloaded key file) | Obtains short-lived OAuth credentials for FCM HTTP v1 sends through Application Default Credentials | Push job runtime identity only | The required send permission is `cloudmessaging.messages.create` on the target Firebase project. Firebase documents `roles/firebasecloudmessaging.admin`; prefer a custom role with only the send permission if the target project accepts it. Do not mount a service-account JSON key. Verify the effective binding before staging. | Rotate/revoke IAM binding or service identity through reviewed IAM change; Google-managed credentials are not copied into the repo. | Required if push dispatch is enabled. |
| `AIXION_FCM_PROJECT_ID` | Selects the Firebase project endpoint; this is configuration, not a credential | Push job and API push producer | Plain configuration may be present only in processes that send FCM; no FCM admin key is used. | Change only with a reviewed Firebase/project migration and device registration plan. | Required if push dispatch is enabled. |
| `AIXION_EMAIL_SMTP_HOST`, `AIXION_EMAIL_SMTP_PORT`, `AIXION_EMAIL_FROM` | SMTP endpoint, port, and verified sender; not passwords | Email job | Plain configuration for email dispatcher only; sender domain ownership and SMTP TLS behavior must be verified by the owner. | Change by approved email provider/sender configuration update; verify delivery and SPF/DKIM/DMARC separately. | Required if email dispatch is enabled. |
| `AIXION_GITHUB_TOKEN` or GitHub App private key plus App/installation identifiers | Optional GitHub connector/API mutations; app credentials are sensitive while IDs are configuration | API integration only; never dispatcher jobs | Prefer a narrowly scoped GitHub App installation token flow where supported. Do not grant to push/email jobs or CI image publisher. | Rotate/revoke through GitHub with a tested permission set and audit. | Only if the selected staging feature set exercises GitHub operations; not needed by dispatch jobs. |
| Android release keystore/upload key and passwords | Signs distributed Android release artifacts | Restricted release operator or approved signing pipeline only | Keep in a dedicated signing vault/CI secret store; never in the repository, backend runtime, Cloud Run job, application asset, or ordinary build logs. | Follow Android signing-key recovery and Play upload-key rotation procedures; maintain protected recovery access. | Not needed by Cloud Run staging. Needed only for a signed app release. |

## Configuration that is not secret

Project IDs, project numbers, region, Cloud SQL instance connection name, service account email addresses, Artifact Registry image digest, schedule, timezone, SMTP host/port/sender, metrics workspace ID, and Firebase project ID are configuration or identifiers. They still require owner review and should not be treated as authorization or proof of ownership. Secret resource names are identifiers only; secret payload values must remain in the selected secret manager.

## Current worker-code status

The push and email one-shot dispatchers now use a strict worker-scoped
`validate_outbox_worker_environment()` path. Each requires the production profile
and scheduled mode, rejects an explicitly configured SQLite path, and validates
PostgreSQL plus only the selected worker's provider settings. The API-wide
`validate_startup_environment()` remains unchanged and continues to fail closed
until complete PostgreSQL application binding exists. This removes the prior
requirement to give each job unrelated API secrets, but does not make the jobs
deployable: producer/dispatcher consistency and the whole-app PostgreSQL runtime
remain open Slice 1 gates. Keep these manifests marked **DO NOT APPLY**. Do not use
a demo/local profile or share every secret with both jobs.

## Review before staging

- Assign a human owner and rotation operator for each secret class.
- Confirm retention, deletion, and audit-log policy for versions; destroy obsolete versions only after recovery and rollback windows close.
- Confirm workload identity and exact per-secret Secret Accessor grants at the narrowest supported resource scope.
- Keep push and email job service accounts separate; keep the Scheduler service account limited to invoking the two named jobs.
- Verify CI cannot read secret payloads and that secret values do not appear in images, IaC state, logs, test output, or artifacts.
