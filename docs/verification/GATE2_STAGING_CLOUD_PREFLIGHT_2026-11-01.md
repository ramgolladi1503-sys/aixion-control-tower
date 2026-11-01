# Gate 2 staging Cloud preflight — 2026-11-01

## Result

**NOT DEPLOYABLE.** The existing gcloud credential is authenticated as the staging account, but the configured default project is not the intended staging project. The intended staging project has billing disabled and Cloud Run Admin API is not enabled. No API enablement, billing link, IAM change, deployment, DNS update, or other cloud resource change was performed.

## Read-only observations

- Active gcloud account: `madhuri.kalidindi03@gmail.com`.
- Configured default project: `project-bbf1eaa4-b9d8-4b2d-b86`.
- Visible staging project: `aixion-ct-staging-20261030` (`Aixion Control Tower Staging`, project number `753634339500`).
- `gcloud billing projects describe aixion-ct-staging-20261030` returned `billingEnabled=false`.
- A read-only Cloud Run service-list request for `asia-south1` reported `run.googleapis.com` disabled. The command did not enable the API or create a service.
- The production project `aixion-control-tower-prod-2026` was not present in the current account's visible project list. This credential is the staging account, not the production account.

The Google Cloud CLI was installed from Google's ARM64 versioned archive under the external TradeBot volume. The downloaded archive matched the SHA-256 published in Google's versioned-archive documentation. The SDK is running inside an APFS sparse image on that external volume because extracting directly onto the external filesystem produced AppleDouble sidecars that the SDK component scanner misread. The sidecars and the original verified archive were left intact.

## Required before staging deployment

1. Confirm the intended staging service topology and cost envelope.
2. In the staging billing account, link billing to `aixion-ct-staging-20261030` only after the billing owner accepts the expected charges.
3. Enable the required APIs for the approved topology, beginning with Cloud Run and the APIs required by the checked-in Terraform.
4. Set the gcloud default project explicitly to `aixion-ct-staging-20261030` before any deployment command.
5. Apply the reviewed staging infrastructure, deploy the API only after Gate 1's PostgreSQL runtime/cutover prerequisites are satisfied, then verify the HTTPS endpoint and push flow.

Production deployment additionally requires credentials for the production Google account/project and a separately reviewed production plan.

## Validation and change record

- gcloud auth/project listing, billing-project describe, and Cloud Run service listing were read-only.
- The Cloud Run query returned a disabled-service error; no prompt was accepted to enable it.
- No app source, credential, database, cloud resource, billing setting, DNS record, or deployment was changed.
- Local config keys: none added.
- Tests: none run; this is an environment preflight record.
- Rollout: none.
