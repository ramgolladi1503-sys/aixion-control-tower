# Staging Owner Decision and Cost Input Record

**Status:** Blank owner-input form. Every value is **PENDING** until supplied and reviewed by the accountable owner.
**Scope:** Slice 7 pre-billing handoff for the existing Aixion Control Tower.
**Authorization boundary:** Completing this record does not enable billing, authorize spending, enable APIs, grant IAM, create secrets/resources, run a cloud plan/apply, deploy, migrate data, or perform a live rehearsal. Each live cloud action requires separate explicit authorization after the applicable engineering gates pass.

Use this record with [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md), [CLOUD_PREWORK_STATUS_2026-10-22.md](CLOUD_PREWORK_STATUS_2026-10-22.md), [STAGING_HANDOFF_COST_MODEL.md](STAGING_HANDOFF_COST_MODEL.md), [STAGING_COST_SCENARIOS.md](STAGING_COST_SCENARIOS.md), and [STAGING_RETENTION_MATRIX.md](STAGING_RETENTION_MATRIX.md). Do not put credentials, secret payloads, payment details, personal approval data, or unredacted console screenshots in this repository. Store evidence in the approved restricted location and record only a sanitized reference here.

## 1. Accountable owner and project

| Decision | Owner input | Evidence/reference | Reviewer | Status/date |
| --- | --- | --- | --- | --- |
| Accountable staging project owner (name/role) | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Dedicated GCP staging project ID | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Project number, verified against selected project | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Billing account associated with that project (redacted identifier only) | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Association verification date and verifier | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Confirm project is dedicated to staging; identify shared services | **PENDING** | **PENDING** | **PENDING** | **PENDING** |

Do not infer a project from a local CLI default, account page, project name, or billing chart. A visible billing account alone does not prove the selected project is linked to it.

## 2. Region and data-residency decision

| Decision | Owner input | Evidence/reference | Reviewer | Status/date |
| --- | --- | --- | --- | --- |
| Approved Cloud Run region | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Approved Cloud SQL region and any cross-region placement | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Approved Scheduler, Artifact Registry, Logging, and Monitoring locations | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Data-residency requirement and rationale | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Firebase/FCM project boundary, if push is enabled | **PENDING** | **PENDING** | **PENDING** | **PENDING** |

`asia-south1`/Mumbai is a proposal only until approved here. Record a distinct value for each service where location choices differ.

## 3. Staging workload and topology inputs

Provide low, expected, and peak assumptions where applicable. State the measurement window and source; do not substitute a guess for an unknown.

| Input | Low | Expected | Peak | Basis/owner approval |
| --- | --- | --- | --- | --- |
| Test accounts/workspaces | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Approval requests per day/month | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| API requests per day/month | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Push/email notifications per day/month | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Push/email job cadence and latency target | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Average and maximum job runtime/invocations | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Cloud Run API CPU, memory, concurrency, timeout, min/max instances | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Cloud SQL edition/tier, vCPU/memory, and HA choice | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Database storage, growth, and autoscaling limit | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Backup/PITR schedule and retention | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Logs/metrics ingestion and retention | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Artifact Registry storage/retention | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Expected network ingress/egress and destinations | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Other billable services, support plans, or shared usage | **PENDING** | **PENDING** | **PENDING** | **PENDING** |

## 4. Dated cost estimate and exposure decision

### Pricing Calculator record

- Calculator URL/share reference: **PENDING**
- Export file/reference in approved restricted storage: **PENDING**
- Estimate date and pricing date: **PENDING**
- Project and region represented: **PENDING**
- Currency, taxes, discounts, and free-tier assumptions: **PENDING**
- Services/SKUs included and quantity assumptions: **PENDING**
- Monthly low / expected / peak totals: **PENDING**
- Excluded costs and reason for exclusion: **PENDING**
- Independent reviewer and review date: **PENDING**

The existing `$75/month` amount is only a proposal; it is not approved, a quote, or a project-wide hard ceiling. Do not report it as a guaranteed maximum or customer-facing price.

### Spend controls and uncapped maximum exposure

| Control/category | Owner decision and evidence |
| --- | --- |
| Approved total monthly exposure target (not represented as a universal hard cap) | **PENDING** |
| Cloud Run spend-cap eligibility, exact project/service scope, proposed amount, and verification plan | **PENDING** |
| Cloud SQL compute, storage, backups, and PITR maximum acceptable monthly exposure | **PENDING** |
| Other uncapped services (logging, monitoring, Artifact Registry, egress, taxes, etc.) and maximum exposure | **PENDING** |
| Total residual exposure after eligible caps, including enforcement lag/in-flight usage | **PENDING** |
| Authorized containment action and named operator for each uncapped category | **PENDING** |
| Budget notification thresholds, recipient, and response action | **PENDING** |

Budgets and eligible-service spend caps have limited scope and may lag; they do not guarantee a project-wide maximum. Record residual exposure explicitly.

## 5. Retention, recovery, and data-handling approvals

For every item, record an approved value or explicitly mark not applicable, policy owner, approver, effective date, enforcement mechanism, exception/legal-hold path, and cost impact. Until approved, do not enable destructive purge/lifecycle behavior or accept real approval data.

| Data/recovery class | Approved period/value | Policy owner/approver/date | Enforcement and verification evidence | Cost and rollback consequence |
| --- | --- | --- | --- | --- |
| Account/profile/deletion records | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Sessions/authentication records | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Approval/decision/audit records | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Connector credentials and credential audit | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Application/infrastructure logs | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| PostgreSQL automated backups and PITR/WAL | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Logical exports and recovery copies | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| RPO and measured RTO | **PENDING** | **PENDING** | **PENDING** | **PENDING** |

The local synthetic backup/restore drill is not evidence of Cloud SQL backup configuration or a cloud restore. A cloud restore rehearsal must target a new isolated instance and requires separate authorization.

## 6. Operators, alert recipients, and response

| Responsibility | Named person/role and contact reference (restricted if personal) | Coverage/response hours | Backup/escalation | Status/date |
| --- | --- | --- | --- | --- |
| Primary staging operator | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Backup staging operator | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Billing/cost alert recipient | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Reliability/security incident escalation | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Backup/restore operator | **PENDING** | **PENDING** | **PENDING** | **PENDING** |
| Approved alert channel and test-recipient reference | **PENDING** | **PENDING** | **PENDING** | **PENDING** |

Cloud alert policies, budgets, channels, and test notifications are not configured by filling this form. Their creation and end-to-end tests require separate live-cloud authorization.

## 7. Project-specific implementation review (blocked until gates pass)

| Gate | Evidence required | Status |
| --- | --- | --- |
| Slice 1 complete PostgreSQL application runtime and production startup gate | Exact-head tests and documented runtime authority/cutover evidence | **PENDING** |
| Project-specific immutable image built and scanned | CI artifact digest and provenance | **PENDING** |
| Project/region-specific Cloud Run and Jobs manifests | Reviewed values with no placeholders; offline validators pass | **PENDING** |
| IAM/API/secret bindings | Least-privilege reviewed plan and evidence; secrets never copied into this record | **PENDING** |
| Migration and rollback procedure | Rehearsed, versioned procedure with recovery evidence | **PENDING** |
| Cloud SQL backup/PITR and isolated restore | Separately authorized staging rehearsal and recorded result | **PENDING** |
| Health/readiness and approval-delivery alerts | Metrics emitted from selected production-authoritative runtime; alert tests | **PENDING** |
| Spend notification/cap and uncapped-resource stop/teardown | Separately authorized rehearsal and evidence | **PENDING** |
| Privacy/retention and data-flow review | Approved retention matrix and reviewer record | **PENDING** |

Until Slice 1 and the owner inputs above pass, keep manifests marked **DO NOT APPLY / NOT APPLY-READY**, and retain the production startup guard.

## 8. Separate authorization register for live cloud actions

No action is authorized by this document or by completing its owner-input fields. Before each live action, obtain separate explicit authorization that names the exact project, action, scope, operator, change record, expected cost/exposure, rollback/stop procedure, and time window. Authorization for one row does not authorize another.

| Live action requiring separate authorization | Exact project/scope | Authorizing owner/reviewer | Change record/time window | Status (default: NOT AUTHORIZED) |
| --- | --- | --- | --- | --- |
| Read-only project/billing/API/IAM inspection | **PENDING** | **PENDING** | **PENDING** | **NOT AUTHORIZED** |
| Enable billing or paid APIs | **PENDING** | **PENDING** | **PENDING** | **NOT AUTHORIZED** |
| Create/modify IAM bindings or secrets | **PENDING** | **PENDING** | **PENDING** | **NOT AUTHORIZED** |
| Create Cloud SQL, Artifact Registry, Cloud Run, Scheduler, Monitoring, or budget resources | **PENDING** | **PENDING** | **PENDING** | **NOT AUTHORIZED** |
| Run migration/import or move real data | **PENDING** | **PENDING** | **PENDING** | **NOT AUTHORIZED** |
| Deploy or route traffic / real approvals | **PENDING** | **PENDING** | **PENDING** | **NOT AUTHORIZED** |
| Cloud backup/restore, spend-control, rollback, or teardown rehearsal | **PENDING** | **PENDING** | **PENDING** | **NOT AUTHORIZED** |

## 9. Owner approval record

- Owner decision: **PENDING — NOT APPROVED**
- Owner name/role: **PENDING**
- Reviewer name/role: **PENDING**
- Decision date and record version: **PENDING**
- Conditions/exceptions: **PENDING**
- Restricted evidence references: **PENDING**

Approval of this handoff record means only that the recorded pre-billing assumptions and proposal are reviewed. It does not replace the separate action authorizations above, Slice 1 exit gate, security/privacy review, or live staging acceptance.
