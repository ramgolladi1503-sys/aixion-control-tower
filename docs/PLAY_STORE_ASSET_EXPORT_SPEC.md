# Play Store Asset Export Specification

This document defines the screenshot, icon, feature graphic, and export rules for the first Google Play release of **Aixion Control Tower**.

Status: **draft export spec**. Do not publish assets until the app is connected to a review-safe backend, demo data is clean, and public policy pages are finalized.

## Goals

The Play Store assets must communicate one thing clearly:

```text
Aixion Control Tower is a calm mobile approval console for human-reviewed connected-agent actions.
```

The assets should avoid hype. The app is not ready to claim broad enterprise security, universal AI-agent support, or full production automation coverage.

## Required Google Play assets

### App icon

```text
Required size: 512 x 512 px
Format: PNG
Color space: sRGB
Transparency: allowed, but avoid transparent-only composition
```

Recommended icon concept:

```text
A simple control-tower or shield-check symbol inside a rounded square.
```

Avoid:

```text
Tiny text
GitHub logo
OpenAI/Google/Anthropic logos
Robot face cliché
Overloaded gradients
Fake security seals
```

### Feature graphic

```text
Required size: 1024 x 500 px
Format: PNG or JPEG
Color space: sRGB
```

Recommended feature graphic message:

```text
Human approval for connected AI agents
```

Recommended layout:

```text
Left: Aixion Control Tower wordmark/title
Center: phone mockup showing approval dashboard
Right: simple approval gate and workflow nodes
```

Avoid:

```text
Unverifiable security claims
Partner logos
Screenshots with TODO values
Dense technical diagrams
Small unreadable UI
```

### Phone screenshots

Recommended for first release:

```text
Minimum: 6 screenshots
Preferred: 8 screenshots
Orientation: portrait
Format: PNG or JPEG
Color space: sRGB
```

Recommended device frame:

```text
Use one consistent Android phone frame across all screenshots.
Keep the UI centered and readable.
Avoid mixing random device sizes.
```

## Screenshot set

### Screenshot 1 — Sign in

Purpose:

```text
Show that the app is authenticated and operator-focused.
```

Suggested caption:

```text
Secure access for approval operators
```

Screen requirements:

```text
Use demo email only
No real user email
No localhost URL
No debug labels
No stack traces
```

### Screenshot 2 — Approval dashboard

Purpose:

```text
Show the main value: pending approval review from mobile.
```

Suggested caption:

```text
Review pending agent actions from your phone
```

Screen requirements:

```text
At least one pending approval
Clear status labels
Readable project/task context
No real repository secrets or tokens
```

### Screenshot 3 — Approval detail

Purpose:

```text
Show inspection before action.
```

Suggested caption:

```text
Inspect request details before approving
```

Screen requirements:

```text
Show request title
Show status
Show safe demo metadata
Show approve/reject actions if applicable
```

### Screenshot 4 — Approve/reject decision

Purpose:

```text
Show human-in-the-loop control.
```

Suggested caption:

```text
Approve or reject with clear operator intent
```

Screen requirements:

```text
Use confirmation state if available
Avoid destructive-looking wording unless intentional
No fake production impact claims
```

### Screenshot 5 — Execution/result tracking

Purpose:

```text
Show that approval has lifecycle visibility after the decision.
```

Suggested caption:

```text
Track execution state after approval
```

Screen requirements:

```text
Show READY_FOR_PR, EXECUTING, FAILED, or completed-style demo state
No real commit secrets
No private repository names unless demo-safe
```

### Screenshot 6 — Audit/history

Purpose:

```text
Show accountability and traceability.
```

Suggested caption:

```text
Follow lifecycle and audit history
```

Screen requirements:

```text
Use demo actor names
Use realistic timestamps
Avoid exposing real emails
```

### Screenshot 7 — Account and privacy controls

Purpose:

```text
Show Play Store review that privacy/data controls exist in app.
```

Suggested caption:

```text
Access privacy and account controls anytime
```

Screen requirements:

```text
Show privacy/data controls panel
Show account removal entry point
No TODO text
No broken policy links
```

### Screenshot 8 — Account removal flow

Purpose:

```text
Show account deletion/removal readiness.
```

Suggested caption:

```text
Request account removal from the app
```

Screen requirements:

```text
Show safe confirmation/result state
Do not claim instant deletion if backend process is request-based
Match public account deletion page wording
```

## Demo data rules

Use only demo-safe values.

Allowed examples:

```text
operator.demo@example.com
Demo Project
Approval Request #1042
Connected Agent Task
Review backend configuration update
Generate release evidence bundle
```

Avoid examples:

```text
real emails
real access tokens
real repository secrets
real customer names
real API keys
localhost URLs
private production URLs
personal phone numbers
```

## Visual style direction

### Tone

```text
Calm
Focused
Trustworthy
Operational
Readable
```

### Layout

```text
Prefer large readable text
Prefer clean spacing
Avoid cluttered data-dense screenshots
Keep status chips readable
Keep primary actions visible but not aggressive
```

### Color direction

```text
Neutral background
Blue/indigo accent
Green only for success/approved states
Red only for reject/failure/destructive states
Avoid neon colors
Avoid high-stress warning-heavy visuals
```

## Export naming convention

Use deterministic names for final exported assets.

```text
playstore/icon-512.png
playstore/feature-graphic-1024x500.png
playstore/screenshots/01-sign-in.png
playstore/screenshots/02-approval-dashboard.png
playstore/screenshots/03-approval-detail.png
playstore/screenshots/04-approval-decision.png
playstore/screenshots/05-execution-tracking.png
playstore/screenshots/06-audit-history.png
playstore/screenshots/07-account-privacy-controls.png
playstore/screenshots/08-account-removal-flow.png
```

These paths describe final export names. They do not require committing generated binary images unless the release process explicitly decides to version store assets.

## Pre-export checklist

Before exporting screenshots:

```text
Review/demo backend is HTTPS
Demo account works
Policy URLs are live
Account deletion URL is live
No TODO values are visible
No debug labels are visible
No real data appears
App version is release-like
Screens are stable and not empty
Signed or release build is used where possible
```

## Submission blocker checklist

Do not upload store assets until these are resolved:

```text
Public privacy policy values replaced
Public account deletion values replaced
GitHub Pages URLs verified live
Production/review backend available over HTTPS
Reviewer test account created
Signed AAB generated
Signed AAB smoke validated
Final SDK/permission/provider review completed
Legal/privacy review completed
```

## Claims guardrail

Do not place these claims in screenshots or graphics:

```text
Enterprise-grade security
SOC 2 ready
GDPR compliant
Works with every AI agent
Zero-risk automation
Fully autonomous control
Bank-grade security
```

Safer copy:

```text
Review connected-agent actions
Approve requests from mobile
Track approval lifecycle
Human-in-the-loop control
Audit-focused visibility
```

## Review handoff notes

The final Play Console package should include:

```text
Signed AAB artifact
Feature graphic
App icon
Phone screenshots
Short description
Full description
Privacy policy URL
Account deletion URL
Reviewer login credentials
Review/demo backend URL
Data Safety answers
App access instructions
```

## Relationship to other docs

Keep this document aligned with:

```text
docs/PLAY_STORE_LISTING_DRAFT.md
docs/PLAY_STORE_DATA_SAFETY_DRAFT.md
docs/PRIVACY_POLICY_DRAFT.md
docs/PUBLIC_PAGE_RELEASE_VALUES.md
docs/ANDROID_SIGNED_AAB_CI.md
docs/ANDROID_RELEASE_PROCESS.md
```
