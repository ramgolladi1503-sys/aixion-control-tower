# Google Cloud Staging Handoff and Cost Model

**Status:** Proposed architecture only; no Google Cloud resource, API, billing account, or deployment has been changed.
**Prepared:** 2026-10-21
**Pricing references last checked:** 2026-10-21 (official Google Cloud pricing pages)
**Scope:** Draft input to Slice 7 in [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md)

## Current local artifact status — 2026-10-22

The backend container now has a deny-by-default `backend/.dockerignore` that allows only package metadata and runtime source, excluding local environment files, virtual environments, SQLite state, tests, and Python caches. A regression test pins this context allowlist. Docker is not installed in the current local workspace, so the image build itself remains to be verified by the repository's Docker-enabled CI job. The production Compose configuration is not a GCP deployment manifest. A Cloud Run/Cloud SQL manifest is intentionally not marked deployable while the production startup guard rejects the incomplete PostgreSQL application runtime; do not deploy the current image as a staging API.

## Recommendation

Use Google Cloud for the first hosted staging environment, with **Mumbai (`asia-south1`) as the proposed region** for the India-first owner and phone. This is a proposal, not a confirmed project or data-residency decision. The project ID, billing account, owner, data region, and ceiling require review before the billing cut line.

Keep staging small and disposable:

The API service manifest is supplemented by `deploy/gcp/cloud-run-jobs-staging.yaml`
and `deploy/gcp/cloud-scheduler-staging.tf`. Job schedules remain unresolved until
the owner approves notification latency and measured expected/peak outbox volume;
do not choose a cadence for convenience or treat a faster schedule as free. Each
trigger currently has zero configured Scheduler retries, and each Cloud Run Job
has zero task retries and processes at most one item. Cloud Scheduler remains an
at-least-once delivery system, so rare duplicate triggers are still possible;
database leasing and idempotency are required. Failed/ambiguous work depends on
durable outbox state and a later tick, not Scheduler's retry mechanism. Price the expected
and peak Job invocation/runtime profile separately from the request-billed API.

| Component | Proposed staging shape | Cost and reliability notes |
| --- | --- | --- |
| HTTPS API | Cloud Run, request-based billing, minimum instances 0, maximum 1 until load evidence supports more | Scales to zero when idle. Set request timeout, concurrency, CPU/memory, and max instances explicitly. Never run correctness-critical periodic dispatch solely in a request-scaled process. |
| PostgreSQL | Cloud SQL for PostgreSQL, single zone, encrypted connection, automated backups enabled, bounded storage | A shared-core `db-g1-small` is an inexpensive **staging smoke-test candidate only**. Google excludes shared-core types from the Cloud SQL SLA, so this is not a production database choice. Production needs a separately priced non-shared-core tier and recovery design. |
| Approval workers | Separate push and email Cloud Run Jobs invoked on owner-approved schedules after the complete application PostgreSQL cutover | `python scripts/run_postgres_push_dispatch_once.py` and `python scripts/run_postgres_email_dispatch_once.py` each attempt at most one due outbox item and exit. Each job has one task, parallelism one, zero task retries, and a separate runtime identity. Scheduler invokes the exact Jobs v2 `:run` endpoint with OAuth and job-scoped Invoker bindings. These are design templates only: the production startup guard is active, and current notification producers still use SQLite. |
| Images | Artifact Registry in the same region | Apply image retention and delete untagged build artifacts. |
| Secrets | Secret Manager, least-privilege runtime service account, versioned secret rotation | Store database, email, and push credentials outside images and source control. Secret/API usage and replication settings must be included in the final estimate. |
| Logs and metrics | Cloud Logging/Monitoring with explicit retention and alert limits | Set retention, log-volume alerts, and redaction before sending approval payloads. Avoid action arguments and credentials in logs. |
| Push | Firebase Cloud Messaging | FCM is a wake-up signal only; approval state remains authoritative in PostgreSQL. Real device delivery and any associated project usage remain unverified. |

See [`STAGING_SECRET_INVENTORY.md`](STAGING_SECRET_INVENTORY.md) for secret names,
purposes, consumers, intended access boundaries, rotation notes, and whether each
is needed for staging. It contains no values. Push and email dispatch Jobs have
separate runtime identities and secret references; FCM uses Google Application
Default Credentials from the push Job identity, not a downloaded service-account
key.

Do not add a load balancer, Cloud NAT, Redis, multi-region database, or high-availability database to this first staging footprint unless a measured requirement justifies the recurring cost. Use the Cloud Run HTTPS endpoint directly and Cloud SQL's supported authenticated connector path; do not open PostgreSQL to `0.0.0.0/0`.

## Indicative monthly cost envelope

The following is a planning estimate, **not a quote or a billing cap**. The Cloud SQL page's displayed default price table has listed `db-g1-small` at **$0.035/hour**; 720 hours would be **$25.20/month for database compute alone** at that displayed rate. The page does not establish that as the `asia-south1` rate. This is a shared-core staging candidate only and is excluded from the Cloud SQL SLA. Storage, backups, network, logs, taxes, and regional price differences are extra. Treat the database cost as **unpriced for Mumbai** until a dated calculator export records the exact edition, tier, region, storage, backup retention, and network assumptions. [Cloud SQL pricing](https://cloud.google.com/sql/pricing)

Google's current Cloud Run page lists Mumbai (`asia-south1`) as an available region, but prices depend on region and billing mode. The default USD request-billing table lists active CPU at **$0.000024/vCPU-second**, active memory at **$0.0000025/GiB-second**, and requests at **$0.40/million**; the page's request-based free tier is 180,000 vCPU-seconds, 360,000 GiB-seconds, and 2 million requests monthly, based on `us-central1` pricing and aggregated by billing account. These are reference rates, **not a Mumbai quote**. Model Cloud Run Jobs separately: the current page gives Jobs a different free-tier allowance and rate table from request-based services. Free tiers can be consumed by unrelated projects on the same billing account and must not be treated as guaranteed zero cost. [Cloud Run pricing](https://cloud.google.com/run/pricing), [Cloud Run billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings)

| Cost category | Planning treatment |
| --- | --- |
| Cloud SQL shared-core staging compute | The page's displayed default `db-g1-small` rate implies **$25.20/month** at 720 hours, but this is **not established as the Mumbai rate**. Keep the Mumbai line item unpriced until a calculator export is saved. |
| Cloud SQL storage, backup, and network | Variable; set low initial storage, retain only required backups, and price them separately. Do not omit from final approval. |
| Cloud Run API | Default published request-based reference rates: **$0.000024/vCPU-second active CPU**, **$0.0000025/GiB-second active memory**, **$0.40/million requests**. Regional rate, billed CPU/memory time, networking, and billing-account-wide free-tier availability require the Mumbai calculator estimate. |
| Scheduled jobs | Cloud Run Jobs have a separate free tier and rate table. Price by invocation/runtime only after the bounded one-shot worker is deployable; the current lifespan loops are not costed as reliable scheduled jobs. |
| Artifact Registry | First **0.5 GiB** storage is free per billing account; then **$0.000136986/GiB-hour** (about **$0.099/GiB-month** at 720 hours). Network transfer and optional scanning are separate. Co-locate with the runtime and expire old images. |
| Cloud Logging | Standard log ingestion: first **50 GiB/project/month** free, then **$0.50/GiB**; retention beyond 30 days is **$0.01/GiB-month**. Vended network logs and other observability products have separate treatment. Redaction and volume limits remain required. |
| Secret Manager | First **6 active secret versions** and **10,000 accesses/month** are free per billing account; beyond that, **$0.000082192/version-hour** and **$0.03/10,000 accesses**. Replication location and version lifecycle affect cost. |
| Metrics, data transfer, DNS, taxes | Usage/configuration-dependent or not yet priced. Enter explicit quantities in the calculator; do not assume these are zero. |
| Production-grade database | **Not estimated in this staging envelope.** A non-shared-core, possibly HA database has a materially different recurring cost and requires a separate production estimate. |

### Workload inputs for the dated calculator estimate

Do not convert an unknown input to zero. Complete this worksheet with the project owner and attach evidence (measurement, test plan, or an explicitly approved estimate) before exporting a price calculation. The calculator export must record its timestamp, currency, region, selected SKUs, free-tier assumptions, and billing-account scope.

| Input | Unit / definition | Value | Evidence or owner decision |
| --- | --- | --- | --- |
| GCP project and billing account | Exact project ID and billing account used for the estimate | **PENDING** | Owner confirmation; do not infer from the local `gcloud` default |
| Region | Region for Cloud Run, Cloud SQL, Artifact Registry, and regional logs where supported | **PENDING** | Owner/data-residency approval; `asia-south1` is only a proposal |
| API calls | Expected and peak requests per day, plus active days per month | **PENDING** | Instrumented test plan or owner-approved workload |
| API compute | vCPU and GiB memory per instance; mean and p95 active seconds per request | **PENDING** | Cloud Run configuration and measured/requested profile |
| API concurrency and scale | Max concurrency, max instances, min instances, and expected idle hours | **PENDING** | Reviewed service limits; do not assume scale-to-zero if availability requires a warm instance |
| PostgreSQL compute | Exact edition/tier, instance-hours per month, and single-zone or HA | **PENDING** | Application/runtime design and approved availability target |
| PostgreSQL storage | Provisioned data GiB, growth allowance, backup/PITR GiB, and retention days | **PENDING** | Synthetic staging data estimate and approved recovery policy |
| Scheduled jobs | Invocations per day, active seconds per invocation, vCPU, memory, retries, and active days | **PENDING** | Deployed one-shot worker design; price separately from API requests |
| Artifact Registry | Retained image count, average compressed GiB, and retention period | **PENDING** | Build cadence and immutable-image retention policy |
| Logs and metrics | Ingested GiB per month and retained GiB-days beyond included retention | **PENDING** | Redacted event volume estimate and approved retention policy |
| Secrets | Active secret versions, replication location, and accesses per month | **PENDING** | Secret rotation and expected connector/runtime access pattern |
| Network and egress | Expected GiB by source/destination and any connector/Cloud SQL transfer | **PENDING** | Same-region design and observed/test workload; include nonzero estimate where applicable |
| Operations reserve | Taxes, monitoring products, image scanning, DNS, and other usage-dependent charges | **PENDING** | Calculator line items or documented reason for exclusion |
| Maximum exposure | Approved total monthly amount and maximum for services outside any eligible spend cap | **PENDING** | Owner approval plus reviewed teardown plan; alerts alone are not a ceiling |

Calculate request-based Cloud Run API and Cloud Run Jobs on their respective pricing modes; do not blend their free tiers. For Cloud SQL, storage, backups/PITR, egress, logging, secrets, and registry, enter separate line items using the selected region and configuration. Record both the expected-month and conservative peak-month totals. If a charge cannot be priced from the selected calculator inputs, label it **UNPRICED** and keep the handoff open. A proposed $75/month amount is not an approved exposure limit and does not fill any pending input above.

### Proposed spend limit and enforcement boundary

For the first, time-bounded staging exercise, propose a **$75 USD per calendar month project spend cap**, with notifications at 50%, 80%, and 100%. This number is a review proposal, not approved spending authority. It is intended to cover the shared-core staging database plus low-volume API and observability usage; the final estimate must be recalculated for Mumbai and actual retention/network assumptions before any resource is created.

An alerts-only budget is **not** a spending ceiling: Google states that it does not automatically cap service usage. Google's currently documented spend-cap eligibility list names Cloud Run, Cloud Run functions, Gemini API, and Gemini Enterprise Agent Platform. It scopes a spend cap to one eligible service in one project. Cloud Run billing documentation says a Cloud Run cap automatically applies to all Cloud Run resource types, including Jobs. Cloud SQL, Artifact Registry, Secret Manager, Cloud Logging/Monitoring, network egress, and taxes are not in that eligibility list; do not count Cloud Run's cap as protection for those charges. [Eligible services and limitations](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps#eligible-services), [Cloud Run billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings), [Google Cloud budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets)

Therefore, **$75 is not a hard project-wide ceiling** for the proposed topology. Even if a Cloud Run service cap is configured, the Cloud SQL instance and other supporting services can continue to incur charges. Before any billing/resource action, either (a) revise the topology to avoid uncapped recurring resources during the initial connector test, or (b) approve an explicit maximum exposure for uncapped services and rehearse an operator shutdown/teardown path that includes Cloud SQL, storage, backups, artifacts, logs, and network resources. Google notes that spend caps do not delete data/resources, in-flight use may finish, cost reporting can lag, and ongoing fixed costs can remain after a cap triggers; a spend cap is a control on eligible service usage, not a universal billing stop. [Spend-cap limitations](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps#limitations)

The proposed operator response is: at 50%, review usage; at 80%, stop nonessential jobs and deployments; at 100%, suspend the staging service and execute the reviewed teardown of uncapped resources, including the database where safe, then investigate before re-enabling. Keep enough separate recovery access to tear down resources even if the application is unavailable. Residual storage, backup, artifact, and already-incurred usage can remain billable after compute is stopped, so the operator procedure must include those resources. This procedure is not verified until rehearsed in a separately authorized disposable project.

## Project, identity, and permissions proposal

- **Project:** a dedicated staging project, ID not yet selected or verified. Do not use a personal development project with unrelated workloads.
- **Region:** `asia-south1` (Mumbai), subject to owner confirmation and service availability. Keep Cloud Run, Cloud SQL, Artifact Registry, and logs in the same region where supported.
- **Human owner:** the account owner retains billing-account administration and project ownership; deployment identities receive only resource-specific roles.
- **CI deploy identity:** Artifact Registry Writer for the target repository and Cloud Run Developer/Service Account User only for the staging service identity. Prefer Workload Identity Federation over a long-lived service-account key.
- **Runtime identity:** Cloud Run service account with Cloud SQL Client, read access to named Secret Manager versions, and only required logging/monitoring permissions. No project Editor/Owner role.
- **Migration identity:** separate, short-lived identity allowed to connect and apply versioned schema migrations; not available to the runtime service.
- **Operations identity:** a named operator who can inspect billing, pause services, restore a database, and rotate secrets. Keep a second reviewed break-glass principal.
- **APIs to consider after authorization:** Cloud Run, Cloud SQL Admin, Artifact Registry, Secret Manager, Cloud Logging, Cloud Monitoring, Cloud Scheduler, and the required Firebase/FCM APIs. Enable only the APIs required by the approved manifest.

These role names are a least-privilege starting point; verify exact permissions against the chosen deployment workflow before granting them. Do not create service accounts, grant roles, enable APIs, or activate billing during this pre-billing phase. The draft Scheduler service account is a dedicated identity with `roles/run.invoker` on each named job through job-level IAM members. It must not receive project-wide Invoker, secret access, Cloud SQL access, job administration, or image publication.

### Generic role and API mapping (account-independent)

The following matrix is a permission-design baseline, not a grant request. Exact
permissions vary with the deployment mechanism and must be checked against the
selected Cloud Run, Cloud SQL connector, Secret Manager, and CI documentation
before assigning roles. Prefer repository/resource-level grants where supported.
Do not grant project `Owner` or `Editor` to automation identities.

| Principal | Candidate role boundary | Allowed purpose | Explicitly excluded |
| --- | --- | --- | --- |
| Human project/billing owner | Project IAM administration and billing-account association, held by named human administrators only | Select project, associate approved billing account, approve IAM and spend controls | Routine application runtime; shared credentials; CI use |
| CI image publisher | `roles/artifactregistry.writer` on the single target repository | Push immutable, reviewed container images | Deploy service; read runtime secrets; connect to database; project-wide Artifact Registry administration |
| CI deployer | `roles/run.developer` scoped to the staging service where supported; `roles/iam.serviceAccountUser` on only the selected runtime service account; image read access only if required by deployment | Deploy a reviewed image and service configuration | Secret payload access; database data access; IAM policy administration; billing administration |
| Cloud Run runtime identity | `roles/cloudsql.client` only if the selected Cloud SQL connector path requires it; Secret Manager Secret Accessor on individually named secrets/versions; narrowly required log/metric write permissions | Connect to the selected database and read runtime secrets; emit operational telemetry | Image publishing/deployment; secret version creation; migration DDL; project IAM changes; billing; broad database administration |
| Migration identity | A separate short-lived identity with the minimum Cloud SQL connection permission and database DDL grants required by the reviewed migration runner | Apply the exact, reviewed migration set during a controlled window | Runtime service use; secret administration; project IAM/billing changes; interactive application access |
| Scheduler invoker | `roles/run.invoker` on only `aixion-push-dispatch` and `aixion-email-dispatch` through separate job-level IAM members | Trigger the scheduled dispatcher after the durable runtime gate passes | Project-wide Invoker; read application secrets; access Cloud SQL; deploy or alter the worker |
| Operations primary and break-glass operators | Individually scoped operational roles for service inspection/disablement, database backup/restore, secret rotation, and alert/budget visibility; grant each capability separately | Respond to alerts, stop spend, execute an approved restore or rotation | Permanent project Owner/Editor; shared operator identity; routine use of break-glass access |

The API list below describes when an API might be needed. Enabling an API can
change billing eligibility and is a post-authorization action. The exact service
agent roles created by Google during enablement must be reviewed as part of the
change record.

| API / service | Enable only when | Identity/resource boundary |
| --- | --- | --- |
| Cloud Run Admin API (`run.googleapis.com`) | The reviewed manifest deploys the HTTPS API or one-shot worker to Cloud Run | Deployment identity operates only the approved staging service/job; runtime identity is separate |
| Cloud SQL Admin API (`sqladmin.googleapis.com`) | The approved topology includes Cloud SQL or its managed connection path | Provisioning/migration principals are separate from the application runtime; do not expose a public database address |
| Artifact Registry API (`artifactregistry.googleapis.com`) | CI publishes or Cloud Run pulls the container from Artifact Registry | Writer scoped to the target repository; runtime/deployer receives read access only if required |
| Secret Manager API (`secretmanager.googleapis.com`) | A reviewed service configuration references Secret Manager versions | Runtime reads only named secrets; rotation principal manages versions; CI must not receive secret payloads |
| Cloud Logging API (`logging.googleapis.com`) | Managed services export logs or an operator queries configured log sinks | Enforce payload redaction and approved retention; no credentials or raw tool arguments |
| Cloud Monitoring API (`monitoring.googleapis.com`) | The approved alert matrix and metrics policies are implemented | Alert policy administration is separate from runtime metric-write permissions |
| Cloud Scheduler API (`cloudscheduler.googleapis.com`) | A reviewed Cloud Scheduler job triggers the one-shot worker | Scheduler uses an invoker identity scoped to the target only; do not use API-process polling loops for correctness-critical work |
| Firebase Cloud Messaging API (`fcm.googleapis.com`) | The approved mobile push path sends FCM messages | Sender identity receives only required FCM send permission; mobile tokens remain protected data |
| Service Usage API (`serviceusage.googleapis.com`) | A named administrator must enable the specifically approved APIs | Restricted to the human/platform provisioning principal; never grant API-enablement permission to runtime or ordinary CI |

#### Post-authorization sequence

These steps are deliberately not performed by this pre-billing handoff. Start only
after the owner records the exact project ID, billing association, region/data
residency, approved monthly exposure (including uncapped services), workload
estimate, operators, and rollback/teardown authority—and after the production
PostgreSQL runtime gate in Slice 1 passes:

1. Have the owner verify the project and billing association in the console. Do
   not infer either from a local CLI default or a billing-account screen.
2. Review the dated cost estimate and approve both the total exposure and each
   uncapped resource category. An alert budget is not a hard spend limit.
3. Freeze the project-specific manifest, immutable image reference, secret-name
   map, region, retention settings, and API list. Reject unresolved placeholders.
4. Enable only the APIs mapped above and required by that frozen manifest; record
   the operator, time, API name, and resulting service-agent changes.
5. Create separate CI, runtime, migration, scheduler, and operations identities.
   Bind each role to the narrowest supported resource scope; review effective
   permissions before deploying. Use Workload Identity Federation for CI where
   supported and avoid downloadable service-account keys.
6. Create secrets through the approved secure channel, grant runtime access to
   named secrets only, and verify that CI logs, images, and repository contain no
   secret values.
7. Apply infrastructure from the reviewed manifest, then run migration,
   readiness, backup/isolated-restore, alert-delivery, cost-notification, and
   teardown rehearsals with synthetic staging data before real approval traffic.

At every step, stop if the effective project, principal, permissions, expected
charges, or target resource differs from the reviewed record. No API enablement,
IAM binding, billing association, or resource creation is authorized by this
document.

## Data, retention, and rollback constraints

- Store account, workspace, connector, approval, decision, audit, and outbox state in PostgreSQL only after Slice 1's complete application-runtime gate passes. Never dual-write SQLite and PostgreSQL as competing authorities.
- Keep staging data synthetic or explicitly approved test data. Do not import production or personal approval history in the first staging deployment.
- Define database backup retention and demonstrate restore to an isolated instance before staging can be used for real approvals. A backup job without a restore drill is insufficient.
- Redact hook tokens, passwords, raw push tokens, and tool argument payloads from logs. Keep only correlation IDs and approved operational metadata.
- Rollback means stop API/worker traffic, preserve a final encrypted database backup and migration ledger, revert the container to the prior immutable image, and restore the previous schema only through a reviewed forward/backward migration plan. Never drop the database as a routine application rollback.
- Retain audit and approval records according to the repository's retention policy; confirm deletion/export behavior and legal review before inviting external users.

## Required pre-billing follow-up

This document does not satisfy Slice 7's exit gate yet. Before the billing boundary, complete and review:

1. Confirm the GCP project ID, project owner, billing account, region, data residency, and **$75/month proposed cap**.
2. Produce a dated Google Pricing Calculator export for `asia-south1` including database compute/storage/backups, Cloud Run, jobs, logs/metrics, secrets, Artifact Registry, egress, and taxes.
3. Confirm spend-cap eligibility for the selected billing account. Current documentation says Cloud Run spend caps cover all Cloud Run resource types, including Jobs, but only Cloud Run usage in one project. Cloud SQL and several supporting services remain uncapped; choose a topology and approve maximum exposure for those charges. Configure any eligible-service cap and test notification plus shutdown/teardown in a disposable project only after separate authorization.
4. Finish PostgreSQL application routing, one-shot worker design, migration/rollback automation, backup/restore drill, health/readiness checks, retention configuration, and alerts.
5. Review the exact manifest, IAM bindings, secrets, data flow, and teardown procedure. A reviewer must sign off before billing/API/resource actions.

The new Jobs and Scheduler files close only the account-independent manifest
design omission. Their templates intentionally retain unresolved project, region,
image digest, secret identity, and schedule values, so the offline validator must
report **NOT APPLY-READY**. The validator does not contact GCP or verify Terraform
provider schema/effective IAM. The one-shot workers now have strict worker-scoped
production settings validation: each requires PostgreSQL and only its own provider
configuration, while the API-wide production settings validator still refuses
startup until the complete app is PostgreSQL-authoritative. This permits isolated
worker configuration without weakening the API guard, but does not close Slice 1:
outbox producer consistency and full app PostgreSQL routing remain prerequisites.
Do not bypass the production guard or share every API secret with both Jobs.

## Source pricing references

- [Cloud SQL pricing](https://cloud.google.com/sql/pricing) — shared-core hourly table and SLA exclusion.
- [Cloud SQL region availability](https://docs.cloud.google.com/sql/docs/postgres/region-availability-overview) — PostgreSQL is available in Mumbai; availability does not establish the selected SKU's regional price.
- [Cloud Run pricing](https://cloud.google.com/run/pricing) and [billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings) — request-based and Jobs pricing, free-tier terms, and spend-cap coverage for all Cloud Run resource types in a project.
- [Artifact Registry pricing](https://cloud.google.com/artifact-registry/pricing) — storage allowance, overage rate, and data transfer/scanning categories.
- [Cloud Logging pricing](https://cloud.google.com/logging) — ingestion, retention, and free allowance.
- [Secret Manager pricing](https://cloud.google.com/secret-manager/pricing) — active versions and access-operation charges.
- [Google Cloud budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets) — alerts do not automatically stop usage.
- [Spend-cap budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps) — behavior and supported-service qualification.

**Pricing-control recheck (2026-10-21):** reviewed Google's spend-cap eligibility, Cloud Run request/Jobs tables, Cloud SQL price and PostgreSQL region availability, Artifact Registry, Cloud Logging, Secret Manager, and backup-retention documentation. The published pages confirm cost categories and several default/reference rates, but do not provide a saved workload-specific Mumbai estimate. Cloud Run's request-based free allowance is based on `us-central1` rates and shared across projects on the billing account; Cloud Run rates depend on region and billing mode. The Cloud SQL shared-core `db-g1-small` displayed rate remains **not a Mumbai quote**. The proposed **$75/month** remains unapproved and is not a project-wide hard limit for this topology; Cloud SQL and several supporting charges are outside Cloud Run spend-cap coverage. A dated Mumbai Pricing Calculator export, confirmed billing owner/project, approved maximum exposure for uncapped services, and an uncapped-resource teardown rehearsal remain outstanding. No billing, API, IAM, or resource action was performed.
