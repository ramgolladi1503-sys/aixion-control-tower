# Gate 2 Read-Only Hosting Discovery — 2026-10-31

## Scope

Read-only inspection of the configured Google Cloud staging project and the
`api.aixionlab.com` DNS name. The requested hosting region is Mumbai
(`asia-south1`). No cloud resources, APIs, billing settings, DNS records, or
deployments were changed.

## Verified findings

- The authenticated Google Cloud account is
  `madhuri.kalidindi03@gmail.com`.
- That account can list the staging project:
  `aixion-ct-staging-20261030` (`Aixion Control Tower Staging`, `ACTIVE`).
- The enabled API list for staging does not include Cloud Run
  (`run.googleapis.com`), Cloud SQL Admin (`sqladmin.googleapis.com`), or
  Compute Engine (`compute.googleapis.com`). Read-only attempts to list Cloud
  Run services, Cloud SQL instances, and forwarding rules reported those APIs
  as disabled.
- DNS lookups returned no A, AAAA, or CNAME answer for `api.aixionlab.com`.
- A read-only production project-list attempt under
  `ramgolladi1503@gmail.com` failed with `invalid_grant`; the saved credential
  needs reauthentication before production project state can be checked.
- Billing state was not verified. The `gcloud beta billing` command group is
  not installed; its attempted non-interactive invocation did not install it
  or change project settings.

## Gate status

Gate 2 remains open. There is no verified hosted API, HTTPS endpoint, or
remote locked-phone push/decision proof.

This discovery does not establish a safe monthly cost estimate. The next
hosting step must first verify billing and the exact staging budget, then
enable only the required APIs and provision the approved resources in
`asia-south1`. DNS should point to a real deployed endpoint only after that
endpoint and its HTTPS configuration are verified.
