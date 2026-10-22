# Cloud Prework Status — 2026-10-22

**Status:** Local pre-billing preparation only. No Cloud project, billing, API, IAM, resource, secret, or deployment settings were changed.
**Scope:** Evidence update for Slice 7 in [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md).
**Related design:** [STAGING_HANDOFF_COST_MODEL.md](STAGING_HANDOFF_COST_MODEL.md), [PRE_BILLING_ALERT_MATRIX.md](PRE_BILLING_ALERT_MATRIX.md), [CLOUD_RUN_MANIFEST_PREFLIGHT.md](CLOUD_RUN_MANIFEST_PREFLIGHT.md), and [POSTGRES_BACKUP_RESTORE_RUNBOOK.md](POSTGRES_BACKUP_RESTORE_RUNBOOK.md).

## Read-only account and project check

On 2026-10-22, the open Google Cloud console showed:

- A billing overview labelled **Free trial account**.
- Current-month cost of **₹0.00** for October 1–22, 2026.
- A free-trial banner stating that prepayment is required.
- A payment-account warning. The payment view showed ₹0.00 balance and no transactions.

The page was a billing-account view. It did not show a selected project ID or prove which project, if any, is linked to that billing account. The local `gcloud` CLI is not installed, so project discovery through the authenticated CLI was unavailable. No project ID, project owner, billing association, or project-level billing status is confirmed by this check. Account email and billing-account identifiers are intentionally not recorded here.

A subsequent read-only inspection of the local gcloud configuration file found
`upstox-oauth` as its configured default project. That identifier is not
confirmed as an Aixion staging project or as linked to the billing account
above, so it is not adopted for the staging plan. No gcloud command or cloud
setting was changed.

The zero displayed cost is an observation from this billing page and reporting window; it is not evidence that a specific project is free of resources or that future usage will cost zero.

## Current design and offline checks

The repository already contains a proposed Cloud Run and Cloud SQL topology, a draft manifest validator, a local synthetic PostgreSQL backup/restore runbook, retention guidance, and an alert matrix. These remain design artifacts. The manifest is deliberately not apply-ready while the production application path is not fully PostgreSQL-backed.

The manifest preflight validates local YAML shape and selected configuration invariants only. It cannot verify project ownership, IAM, APIs, secrets, billing, connectivity, delivery, or cost. The local backup/restore drill used synthetic data and does not certify Cloud SQL backup configuration or restore behavior. The current alert matrix is unconfigured and identifies missing application-level approval-delivery metrics.

## Pricing and spend-control findings

Official Google Cloud documentation was rechecked on 2026-10-22:

- [Cloud SQL pricing](https://cloud.google.com/sql/pricing) includes regional pricing selectors, including Mumbai (`asia-south1`). The page's default shared-core table displays `db-g1-small` at USD $0.035/hour; that displayed amount must not be treated as a Mumbai quote without selecting Mumbai and preserving a dated estimate. Shared-core instance types are not covered by the Cloud SQL SLA.
- [Cloud Run pricing](https://cloud.google.com/run/pricing) lists Mumbai (`asia-south1`) as a Tier 1 region and states that prices vary by region and billing configuration. Its free tier is based on `us-central1` pricing and is aggregated across projects under a billing account; it is not a guaranteed allowance for this project.
- [Google Cloud spend caps](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps) apply to one eligible service in one project. The documented eligible services include Cloud Run, Cloud Run functions, Gemini API, and Gemini Enterprise Agent Platform. A cap pauses eligible service usage; it does not stop ongoing fixed charges for persistent resources such as databases and storage. Enforcement can lag, in-flight usage can finish, and resulting overage remains billable.
- FCM HTTP v1 sends require the `cloudmessaging.messages.create` permission in the target Firebase project. Firebase documents `roles/firebasecloudmessaging.admin`; Google lists the send permission as supported in custom roles, so a custom one-permission role is a least-privilege candidate for owner review. This is documentation research only; no Firebase/FCM project or effective IAM binding has been inspected. [FCM HTTP v1 authorization](https://firebase.google.com/docs/cloud-messaging/send/v1-api), [FCM IAM roles and permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/firebasecloudmessaging), [custom-role permission support](https://docs.cloud.google.com/iam/docs/custom-roles-permissions-support).

The existing **$75/month** figure is therefore a proposed review amount, not an approved budget or hard project-wide maximum. It does not bound Cloud SQL, backup/storage, Artifact Registry, logging, monitoring, network, tax, or other excluded costs. Do not use it as a billing authorization or user-facing cost promise.

A workload-specific calculator export is still required. It must identify the selected project/region and show the chosen database edition and tier, storage, backup/PITR retention, Cloud Run resource and billing settings, scheduled job frequency/runtime, logs/metrics retention, Artifact Registry storage, network egress, taxes/currency, and all applicable discounts or free-tier assumptions. Preserve the calculator URL/export and its pricing date in the reviewed cost record.

## Pre-billing gate status

| Requirement | Current evidence | Status |
| --- | --- | --- |
| Dedicated staging project and accountable owner | No project ID or owner confirmed | **Open** |
| Billing account linked to selected project | Billing account page visible; project link not established | **Open** |
| Data region and residency decision | `asia-south1` is proposed only | **Open** |
| Workload-specific calculator estimate | Existing cost model is an indicative proposal; no dated export | **Open** |
| Maximum exposure for uncapped services | `$75/month` is not a project-wide ceiling; owner decision absent | **Open** |
| Named primary/backup operator and alert recipient | Alert matrix has requirements but no named recipients | **Open** |
| Complete PostgreSQL application runtime | Production startup guard remains; full route/worker cutover is incomplete | **Open; blocks deployment** |
| Cloud SQL backups, PITR, and restore rehearsal | Local synthetic PostgreSQL drill only | **Open; requires separately authorized staging** |
| Cloud alert policies and budget controls | Design only; nothing configured | **Open; requires separately authorized staging** |
| IAM, API, and secret configuration | Least-privilege proposal only; no project was inspected | **Open** |
| Manifest and container readiness | Manifest intentionally contains placeholders; Docker is unavailable locally and CI is the available image-build evidence | **Open for project-specific manifest; offline validator exists** |
| Rollback and teardown | Procedures are documented but not tested against cloud resources | **Open; requires separately authorized staging** |

## Remaining pre-billing work

Before the billing cut line, the owner/reviewer must provide or approve:

1. The dedicated staging project ID and named project owner; verify that the selected billing account is linked to that project.
2. The region and data-residency decision. Mumbai remains a proposal, not a confirmed requirement.
3. A workload assumption set for the calculator: expected test accounts, approval volume, API request volume, job frequency/runtime, database tier/storage, backup/PITR retention, log retention, and egress.
4. A dated Pricing Calculator export with every resource in the topology, including taxes and excluded/uncapped categories.
5. A maximum acceptable exposure for resources not protected by an eligible spend cap, plus the operator authorized to stop and tear down those resources.
6. Named primary and backup operators, alert destination, and response hours.
7. Review and approval of RPO/RTO, retention, and restore-cost assumptions.
8. Completion of the PostgreSQL application-runtime gate and local failure certification before the manifest can be made deployment-ready.

These decisions are not inferred from a visible account, an empty billing chart, or a proposed cost model. Do not enable billing, enable APIs, create IAM bindings, provision resources, place a payment, or deploy as part of this prework.

## Configuration, migration, validation, and rollout

- **New configuration keys:** None.
- **Database migrations:** None.
- **Cloud mutations:** None.
- **Validation performed:** Read-only console inspection; read-only `gcloud` availability check; review of the current repository's cost, manifest, alert, retention, and backup/restore documents; official Google Cloud pricing and spend-cap documentation review.
- **Local rollout:** None. Keep the Cloud Run manifest blocked and retain the production startup guard.
- **Next cloud activity:** After the project, cost exposure, operators, and workload estimate are approved—and after the PostgreSQL/runtime gate passes—prepare a project-specific manifest for review. Cloud billing, APIs, IAM, resources, deployment, and cloud restore drills require separate explicit authorization.
