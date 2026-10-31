# Gate 2 Staging Hosting Precheck — 2026-10-31

## Result

**HOSTED STAGING API NOT DEPLOYED — BILLING AND HOSTING PREREQUISITES REMAIN OPEN.**

The selected staging project is `aixion-ct-staging-20261030`. Google Cloud Console
reports that this project is not linked to a billing account. The Cloud Run Admin
API (`run.googleapis.com`) is not enabled. The API catalog offers an Enable action,
but it was not selected. No Cloud Run service, load balancer, Cloud SQL instance,
or other billable resource was created.

Public DNS lookup returned no A record for `api.aixionlab.com`. The Spaceship DNS
manager is open, but no record was added or changed because there is no deployed
service or load-balancer address to target.

## Required owner decisions and sequence

1. Decide whether to link a billing account to the staging project. Linking a
   billing account enables charges; obtain and review a project-specific cost
   estimate first, including the external Application Load Balancer and database.
2. After billing is explicitly approved, enable the required Google APIs and deploy
   the API to the India region with the approved staging database and secrets.
3. Create the external Application Load Balancer and health check, verify HTTPS,
   then add the exact assigned target to `api.aixionlab.com` in Spaceship.
4. Configure push credentials and deep links, then verify notification receipt,
   open, approve, and deny on a locked phone from an off-LAN connection.

Do not point DNS at the website's root A record or an unverified address. Production
deployment remains separately gated on Gate 1 PostgreSQL authority and cutover.

## Validation boundary

These observations came from read-only Cloud Console, the Spaceship UI, and public
DNS lookup. This note does not assert that an API is reachable, that a service is
deployed, that HTTPS is configured, or that push notifications work. No billing,
project API, IAM, DNS, or hosting configuration was changed.
