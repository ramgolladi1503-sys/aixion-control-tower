# Public Play Store Pages

This folder contains static pages intended to be hosted over public HTTPS before Google Play submission.

## Pages

```text
privacy-policy.html
account-deletion.html
```

## Hosting requirement

These pages must be hosted at public HTTPS URLs that are:

```text
accessible without login
active and reachable
not geofenced
not a PDF
not editable by visitors
stable enough for Google Play review
```

Acceptable hosting options:

```text
GitHub Pages
Netlify
Cloudflare Pages
Vercel
company website/static hosting
```

## GitHub Pages workflow

This repo includes a GitHub Actions workflow:

```text
.github/workflows/public-pages.yml
```

The workflow publishes only this folder:

```text
docs/public
```

Expected GitHub Pages URLs after the workflow runs successfully:

```text
Privacy Policy URL:
https://ramgolladi1503-sys.github.io/aixion-control-tower/privacy-policy.html

Account Deletion URL:
https://ramgolladi1503-sys.github.io/aixion-control-tower/account-deletion.html
```

Repository setting that may still be required:

```text
GitHub repo -> Settings -> Pages -> Build and deployment -> Source: GitHub Actions
```

If GitHub Pages is disabled or set to a branch source, the workflow may not publish correctly.

## Not publishable until TODOs are replaced

Do not submit these pages to Google Play until all TODOs are resolved:

```text
developer/operator name
privacy/support email
last updated date
account deletion response/completion timeline
exact retention periods
backup retention window
audit anonymization/deletion rules
backend hosting/provider details
legal/privacy review
```

## Hard rule

A page existing in the repo is not enough. Google Play needs the actual deployed public URL.

A workflow existing in the repo is also not enough. The workflow must run successfully, and the resulting URLs must load in a browser without login.
