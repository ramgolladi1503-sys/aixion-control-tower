# Staging Cloud Run Cost Scenarios

**Status:** Planning worksheet only; not a quote, a billing cap, or authorization to provision resources.
**Pricing checked:** 2026-10-30 against the official Google Cloud pricing pages linked below.
**Currency and period:** USD, 30-day month.
**Region:** Owner directed India for staging; Mumbai (`asia-south1`) is the selected planning default for Cloud Run and Cloud SQL. Published default rates below are reference rates, not a Mumbai calculator export.

## What these numbers cover

The compute scenarios estimate only Cloud Run request-based API compute/requests
and two Cloud Run Jobs. They exclude Cloud SQL, database storage and backups,
network egress, Artifact Registry, Cloud Logging/Monitoring, Secret Manager,
Scheduler, taxes, and any other workloads on the billing account. The worked
baseline below separately adds the planned HTTPS load balancer's fixed forwarding
rule fee, but still excludes load-balancer traffic processing and internet egress.
Cloud SQL runs continuously in the proposed topology and is therefore a separate
fixed monthly cost. No subtotal here is an all-in monthly cost or proof that the
proposed `$75/month` exposure is adequate.

Google's pricing page lists default request-based service rates of `$0.000024`
per vCPU-second, `$0.0000025` per GiB-second, and `$0.40` per million requests.
Its request-based free allowance is 180,000 vCPU-seconds, 360,000 GiB-seconds,
and 2 million requests per month. Cloud Run Jobs use instance-based rates of
`$0.000018` per vCPU-second and `$0.000002` per GiB-second, with 240,000
vCPU-seconds and 450,000 GiB-seconds of monthly free allowance. Free-tier usage
is shared across projects on the billing account. Prices depend on region, and
the free allowance is based on `us-central1` prices. See [Cloud Run pricing](https://cloud.google.com/run/pricing).

Cloud Run Jobs have a one-minute minimum billed instance duration. A job that
finishes in 10 or 30 seconds is still modeled as 60 billable seconds. See the
[Cloud Run pricing page](https://cloud.google.com/run/pricing), under “Billable
instance time for Cloud Run jobs.”

## Explicit scenario assumptions

- API service: request-based billing, one vCPU, 0.5 GiB memory, zero minimum instances.
- API active duration is the listed average per-request time; no idle/minimum-instance time is included.
- Two independent one-task Jobs (push and email) each run at the listed cadence. Cadences are modeling assumptions, not approved operational schedules.
- Each Job uses one vCPU and 0.5 GiB; actual run time is below or equal to 60 seconds, so each invocation is billed for 60 seconds.
- The “free tier available” column assumes no other project or service has consumed any of the relevant billing-account free allowance. That is an optimistic bound, not a guarantee.
- The “no free tier” column assumes all applicable free allowances have already been consumed elsewhere.
- All values are approximate arithmetic on published default USD rates. The pricing calculator must replace these figures after the owner chooses the project, billing account, region, and workload.

## Monthly compute and request estimates

| Scenario | API workload | Two Job cadence and run time | Cloud Run subtotal if free tiers are available | Cloud Run subtotal with free tiers exhausted |
| --- | --- | --- | ---: | ---: |
| Low | 100,000 requests/month; 0.25 sec average | Every 15 minutes; 10 sec actual per Job | **$1.90** | **$7.24** |
| Expected | 1,000,000 requests/month; 0.5 sec average | Every 5 minutes; 30 sec actual per Job | **$22.16** | **$32.72** |
| High | 5,000,000 requests/month; 0.5 sec average | Every 1 minute; 30 sec actual per Job | **$152.38** | **$163.62** |

Totals are API plus Jobs only. For example, the expected case comprises `$7.68`
of API compute after the request-based free allowance and `$14.48` of Job compute
after the Jobs free allowance. If those allowances are exhausted, the respective
reference costs are `$13.03` and `$19.70`.

For the low case with free allowances exhausted, API compute and requests are
approximately `$0.67` (including `$0.04` of request charges), and Job compute is
approximately `$6.57`, for a total of `$7.24` after rounding. The independent
Decimal recomputation in `backend/tests/test_staging_cost_scenarios.py` covers
the CPU, memory, request, and minimum Job duration charges.

The high case exceeds the proposed `$75/month` amount before adding the database
or supporting services. The low and expected cases also do not establish that an
all-in deployment fits that amount.

## Worked low-cost staging baseline (reference-rate subtotal)

This worked example makes the excluded-service problem concrete. It is an
account-independent **reference-rate subtotal**, not a Mumbai quote, invoice
forecast, or spend guarantee. The 30-day month uses 720 hours; where a published
rate is hourly it is multiplied by 720. Mumbai is the owner-directed planning
region, but the region-specific calculator export remains pending.

| Component | Explicit low-case quantity | Arithmetic / reference rate | Monthly reference cost |
| --- | --- | --- | ---: |
| Cloud Run API and two Jobs | Low scenario above; assume all free-tier allowances are exhausted | Existing low-case calculation | **$7.24** |
| Cloud SQL shared-core compute | One `db-g1-small`, always on for 720 hours | `720 × $0.035/hour`; default displayed table rate only, not confirmed for Mumbai | **$25.20** |
| Global external Application Load Balancer | One global forwarding rule, 720 hours; no traffic included | `720 × $0.025/hour`; published rate for the first five global forwarding rules | **$18.00** |
| Cloud Scheduler | Two continuously configured schedules; conservatively assume all three free jobs on the billing account are already consumed | `2 × $0.10 per 31 days × 30/31 days`; actual billing is per billable day | **$0.194 ≈ $0.20** |
| Artifact Registry | 5 GiB average retained image storage; assume account free allowance exhausted | `(5 − 0.5) GiB × $0.000136986/GiB-hour × 720 hours` | **$0.44** |
| Secret Manager | At most 6 active versions and 10,000 accesses in the billing account | Within published account free allowances; if exhausted, add version-hour and access charges | **$0.00 assumed** |
| Cloud Logging | At most 50 GiB/project/month, default retention only; no vended network logs | Within published project monthly free allowance | **$0.00 assumed** |
| Cloud Monitoring | Google Cloud metrics only; no custom metrics, Prometheus, uptime checks, or synthetic monitors | Assumed non-chargeable system metrics; confirm actual metric inventory | **$0.00 assumed** |
| **Known reference-rate subtotal** | Includes one global HTTPS forwarding rule; excludes unresolved items listed below | Sum of the rows above | **$51.08** |

The subtotal is **not all-in** and must not be represented as the all-in monthly
cost. It includes the known Cloud SQL compute line at a
non-regional reference rate and one global forwarding-rule fee, while Cloud SQL
storage, backup/PITR, load-balancer data processing, internet egress, taxes,
billing-account free-tier consumption, and the selected Mumbai rates are not
known. The $51.08 amount must therefore not be described as an affordable
monthly budget or maximum exposure. The previous `$75/month` proposal leaves
only `$23.92` over this incomplete subtotal; that difference is not a validated
reserve.

The scenario assumes Cloud Run request-based API billing with zero minimum
instances and the low API request/runtime values above; two separate one-task
Jobs every 15 minutes, one vCPU and 0.5 GiB each, with one-minute minimum billed
duration; one always-on `db-g1-small`; two scheduler definitions; and 5 GiB of
retained images. These are modeling assumptions only. Owner-selected traffic,
latency, job cadence, database tier/storage/backup retention, log volume,
metric mix, network egress, retention, region, taxes, and unrelated billing
account usage remain **PENDING**. Cloud SQL shared-core is a staging experiment
only and is not covered by the Cloud SQL SLA.

Official rates and limits used above: [Cloud Run pricing](https://cloud.google.com/run/pricing)
(request/Jobs rates, free-tier scope, regional pricing, and minimum Job duration);
[Cloud SQL pricing](https://cloud.google.com/sql/pricing)
(`db-g1-small` reference rate and regional selection for storage/network);
[Cloud Load Balancing pricing](https://cloud.google.com/load-balancing/pricing)
(`$0.025/hour` global forwarding-rule rate and traffic processing charges);
[Cloud Scheduler pricing](https://cloud.google.com/scheduler/pricing)
(`$0.10/job/31 days`, three free jobs per billing account);
[Artifact Registry pricing](https://cloud.google.com/artifact-registry/pricing)
(0.5 GiB free per billing account and storage rate);
[Secret Manager pricing](https://cloud.google.com/secret-manager/pricing)
(active-version/access allowances and rates); and
[Cloud Observability pricing](https://cloud.google.com/products/observability/pricing)
(Logging and Monitoring pricing). References checked 2026-10-30. The official
[Pricing Calculator](https://cloud.google.com/products/calculator) estimate
still requires owner-selected region/project/workload inputs and a saved dated
export; this arithmetic does not replace it.

### Proposed cost alert and stop plan (owner approval pending)

- Proposed whole-project monthly budget target: **$75 USD**, with alerts at
  **50% ($37.50), 80% ($60), and 100% ($75)** to the named billing and staging
  operators. These are alert/response thresholds only; an alerts-only budget
  does not stop usage.
- Proposed Cloud Run service-scoped spend cap: **$10 USD/month** on the staging
  project’s `run.googleapis.com` service, with Google's standard 50%, 80%, and
  100% notifications. This may pause Cloud Run API and Job usage when estimated
  gross cost reaches the target; it does not cap Cloud SQL or other products.
- At 50%, review billing export/usage and verify the cost-owner inventory. At
  80%, pause optional scheduled jobs and block new staging deployments. At 100%,
  suspend the staging API and execute the reviewed stop/teardown procedure for
  uncapped resources, after preserving any data required by the approved
  retention/recovery decision. Check residual charges afterward.
- **No project-wide hard monthly ceiling is currently evidenced.** Google
  documents spend caps for a single eligible service in one project; persistent
  resource charges, in-flight use, reporting/enforcement lag, and unlisted
  service charges can continue. The `$75` amount is therefore a proposed
  operator stop threshold, not a guaranteed maximum. The Cloud Run cap and
  project alerts must be configured and separately tested only after the owner
  authorizes those live actions.

Google references: [budgets and alerts](https://docs.cloud.google.com/billing/docs/how-to/budgets)
and [spend-cap eligibility and limitations](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps).

## Recalculation formulas

Let `R` be API requests/month, `T` average request seconds, and `N` the number of
scheduled invocations for **both** Jobs combined in the month. For the assumptions
above:

```text
API CPU seconds       = R * T * 1 vCPU
API memory GiB-sec    = R * T * 0.5 GiB
API requests charge   = max(R - 2,000,000, 0) / 1,000,000 * $0.40
Job CPU seconds       = N * 60 seconds * 1 vCPU
Job memory GiB-sec    = N * 60 seconds * 0.5 GiB
```

Apply the corresponding free allowances before multiplying remaining usage by
the rates above. These formulas omit minimum-instance idle charges, egress,
database and support-service charges, regional rate differences, discounts,
currency conversion, and taxes. Update them if the topology or price page changes.

## Cloud SQL and spend-control boundary

The Cloud SQL page displays a default `db-g1-small` reference rate of `$0.035`
per hour (`$25.20` for 720 hours), but the selected Mumbai tier price and required
storage/backup charges have not been exported from the calculator. Shared-core
tiers are not covered by the Cloud SQL SLA. Do not add `$25.20` to these scenarios
as if it were the Mumbai quote. See [Cloud SQL pricing](https://cloud.google.com/sql/pricing).

Google documents spend caps for a single eligible service in one project. Cloud
Run is eligible; Cloud SQL and the other excluded charges above are not covered by
that Cloud Run cap. In-flight usage and fixed charges for persistent resources
can continue after enforcement, and enforcement is not instantaneous. A cap is a
partial risk control, not a project-wide hard monthly ceiling. See [spend-cap
scope and limitations](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps)
and [Cloud Run billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings).

## Inputs required before the estimate can be accepted

1. Owner-confirmed GCP project ID, billing account, owner, and data-residency decision.
2. Approved API request/peak volume, average and high-percentile duration, concurrency, and maximum instance count.
3. Approved Job cadence, maximum runtime, and expected/peak outbox volume.
4. Selected Cloud SQL edition/tier, region, storage, backup retention, and recovery requirements.
5. Explicit log/metric retention, image retention, expected egress, and secret access volume.
6. Dated Pricing Calculator export with all selected SKUs, regional rates, currency, free-tier assumptions, and billing-account scope.
7. Approved maximum exposure for each uncapped category, plus a tested rollback/teardown procedure.

Until these are resolved and Slice 1's PostgreSQL application-runtime gate passes,
this worksheet remains a planning artifact. It authorizes no billing activation,
API enablement, IAM change, resource creation, deployment, or data migration.
