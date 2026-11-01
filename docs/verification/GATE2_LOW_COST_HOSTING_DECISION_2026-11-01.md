# Gate 2 low-cost hosting decision — 2026-11-01

## Scope

This note records a lower-cost deployment path for the existing Gate 2 objective:
host the Control Tower API in India, provide HTTPS to the Android app and
connectors, use `api.aixionlab.com` for the branded production endpoint, and
prove remote push delivery. It does not change the four production gates or
authorize billing, DNS, IAM, or cloud-resource changes.

## Verified constraints

- The selected India region is Cloud Run Mumbai, `asia-south1`.
- One Google Cloud global forwarding rule currently costs USD $0.025/hour,
  about USD $18.25 for a 730-hour month, before load-balancer traffic processing
  and internet egress. The HTTPS load balancer is therefore a recurring floor,
  even at very low request volume.
- Cloud Run request-based compute has a monthly free allowance, subject to the
  published limits and billing-account aggregation. Mumbai is a Tier 1 Cloud
  Run region. This does not make the whole deployment free: database, storage,
  image registry, logs, and network egress can still be billable.
- Cloud Run domain mapping is not available in Mumbai. Google documents it as a
  preview feature that is not production-ready, so it is not a suitable
  production shortcut for `api.aixionlab.com`.
- Firebase Hosting can rewrite requests to Cloud Run in Mumbai and provides
  HTTPS for a connected custom domain, but the rewrite request limit is 60
  seconds. The current MCP JSON-RPC route can hold one HTTP request while
  waiting for a human approval, up to the configured approval TTL (currently
  900 seconds). A catch-all Firebase Hosting rewrite would therefore break a
  supported API path.
- The current staging project has no active billing account or deployed Cloud
  Run service. No production endpoint or live DNS target exists. This is not a
  dated workload estimate for either project.

## Recommended cost-controlled sequence

1. **Staging E2E without custom DNS:** after staging billing is explicitly
   enabled, deploy the reviewed API to Cloud Run in Mumbai with request-based
   billing and zero minimum instances. Use its default HTTPS `run.app` URL for
   the controlled staging connector and phone tests. This avoids the fixed
   load-balancer forwarding-rule charge while preserving the long-running MCP
   request path.
2. **Keep `api.aixionlab.com` unpointed during staging:** do not create DNS
   records until a real service target and the cost model are approved. The
   existing endpoint must remain fail-closed until PostgreSQL and release
   gates are met.
3. **Choose the branded production front door after API transport review:**
   either keep the external HTTPS load balancer for the current synchronous MCP
   route, or design and verify a separately versioned asynchronous MCP approval
   protocol before considering Firebase Hosting for the whole API. Do not use a
   Firebase catch-all rewrite with the current synchronous route.
4. **Keep PostgreSQL durability in the estimate:** do not replace the planned
   managed database with a single-host database just to hide its recurring
   cost. A single VM running API and PostgreSQL may be considered only as an
   explicitly limited demo/pilot with tested off-site backups and an accepted
   single-host outage risk; it is not equivalent to the production recovery
   gate.
5. **Price the complete shape before enabling resources:** preserve a dated
   Mumbai estimate covering Cloud SQL sizing, storage/backups, Cloud Run, jobs,
   Artifact Registry, logs, egress, and applicable tax. Configure alerts and any
   eligible service spend cap, while treating them as controls rather than a
   universal hard ceiling.

## Decision and current status

The least-cost suitable next experiment is Cloud Run Mumbai on its generated
HTTPS URL, with no load balancer and no custom DNS. The branded production
endpoint remains unresolved because the lowest-cost custom-domain proxy has a
request timeout shorter than the current synchronous MCP wait.

No service, database, load balancer, Firebase Hosting site, API, billing link,
secret, DNS record, or deployment was created or changed. Gate 2 remains open.

## Source links

- [Cloud Run pricing](https://cloud.google.com/run/pricing)
- [Cloud Run locations and domain-mapping availability](https://docs.cloud.google.com/run/docs/locations)
- [Cloud Run custom-domain options and domain-mapping production limitations](https://docs.cloud.google.com/run/docs/mapping-custom-domains)
- [Firebase Hosting rewrites to Cloud Run and request timeout](https://firebase.google.com/docs/hosting/cloud-run)
- [External Application Load Balancer pricing](https://cloud.google.com/load-balancing/pricing)
