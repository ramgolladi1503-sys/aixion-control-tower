# Public Page URL Smoke Workflow

This document explains how to run the manual public page URL verification workflow before Google Play submission.

Workflow:

```text
.github/workflows/public-page-url-smoke.yml
```

Script used by the workflow:

```text
scripts/verify_public_page_urls.py
```

## Purpose

Google Play needs public HTTPS URLs for the privacy policy and account deletion page. Local files in `docs/public` are not enough.

This workflow verifies that the deployed URLs:

```text
use HTTPS
are not PDFs
return HTTP 200
serve HTML
contain required Aixion Control Tower page phrases
optionally fail on TODO placeholders
```

## Default URLs

```text
Privacy policy:
https://ramgolladi1503-sys.github.io/aixion-control-tower/privacy-policy.html

Account deletion:
https://ramgolladi1503-sys.github.io/aixion-control-tower/account-deletion.html
```

## How to run

In GitHub:

```text
Actions -> Verify public page URLs -> Run workflow
```

Inputs:

```text
privacy_url
account_deletion_url
allow_placeholders
```

## Smoke mode

Use this before final public page values exist:

```text
allow_placeholders=true
```

This checks deployment reachability and required page structure, but it does **not** mean the pages are Play-submission ready.

## Final Play submission mode

Use this only after all public page TODO values are replaced:

```text
allow_placeholders=false
```

The run must pass before the public URLs are placed into Google Play Console.

## Important warning

A passing smoke run with `allow_placeholders=true` proves only this:

```text
the pages are deployed and structurally reachable
```

It does not prove:

```text
privacy policy is legally ready
account deletion wording is final
retention values are correct
provider disclosures are complete
Google Play Data Safety answers are correct
```

## Required order

Use this order for Phase 0 release readiness:

```text
1. Publish docs/public through GitHub Pages.
2. Run this workflow with allow_placeholders=true.
3. Complete docs/PUBLIC_PAGE_VALUES_INTAKE.md.
4. Replace public page TODO values.
5. Run scripts/validate_public_pages_ready.py locally/CI.
6. Run this workflow with allow_placeholders=false.
7. Use the verified URLs in Google Play Console.
```

## Failure handling

If the workflow fails, inspect the printed reason:

```text
URL must use HTTPS
expected HTTP 200
response must be HTML
missing required phrase
unresolved TODO placeholder
```

Fix the page deployment, URL input, or page content before using the URLs for Play review.
