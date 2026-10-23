# Staging Cloud Run Cost Scenarios

**Status:** Planning worksheet only; not a quote, a billing cap, or authorization to provision resources.
**Pricing checked:** 2026-10-23 against the official Google Cloud pricing pages linked below.
**Currency and period:** USD, 30-day month.
**Region:** `asia-south1` is proposed, not owner-approved. Published default rates below are reference rates, not a Mumbai calculator export.

## What these numbers cover

These scenarios estimate only Cloud Run request-based API compute/requests and two
Cloud Run Jobs. They exclude Cloud SQL, database storage and backups, network
egress, Artifact Registry, Cloud Logging/Monitoring, Secret Manager, Scheduler,
taxes, and any other workloads on the billing account. Cloud SQL runs continuously
in the proposed topology and is therefore a separate fixed monthly cost. The
scenario total must not be represented as the all-in monthly cost or as proof that
the proposed `$75/month` exposure is adequate.

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
| Low | 100,000 requests/month; 0.25 sec average | Every 15 minutes; 10 sec actual per Job | **$1.90** | **$7.20** |
| Expected | 1,000,000 requests/month; 0.5 sec average | Every 5 minutes; 30 sec actual per Job | **$22.16** | **$32.72** |
| High | 5,000,000 requests/month; 0.5 sec average | Every 1 minute; 30 sec actual per Job | **$152.38** | **$163.62** |

Totals are API plus Jobs only. For example, the expected case comprises `$7.68`
of API compute after the request-based free allowance and `$14.48` of Job compute
after the Jobs free allowance. If those allowances are exhausted, the respective
reference costs are `$13.03` and `$19.70`.

For the low case with free allowances exhausted, API compute and requests are
approximately `$0.63`, and Job compute is approximately `$6.57`, for a total of
`$7.20` after rounding. The prior `$7.24` table value was an arithmetic error.

The high case exceeds the proposed `$75/month` amount before adding the database
or supporting services. The low and expected cases also do not establish that an
all-in deployment fits that amount.

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
