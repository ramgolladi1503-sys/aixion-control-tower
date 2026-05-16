# Play Store Listing Draft

This document is the working Play Store listing draft for **Aixion Control Tower**.

Status: **draft only**. Do not submit this copy to Google Play until the privacy policy values, production backend URL, signed AAB, screenshots, and legal review are complete.

## Product identity

### App name

Primary candidate:

```text
Aixion Control Tower
```

Alternative candidates:

```text
Aixion Approval Console
Aixion Mobile Control
Aixion Agent Control Tower
```

Recommended final name: **Aixion Control Tower**.

Reason: it communicates oversight, approval, and operational control without sounding like a generic task app.

## Short description

Google Play short descriptions are limited. Final copy should stay concise.

```text
Review, approve, and track AI agent actions from your phone.
```

Alternative:

```text
Mobile approvals and audit visibility for connected AI agents.
```

## Full description

```text
Aixion Control Tower is a mobile approval console for teams and operators who want human control over connected AI agent actions.

Instead of allowing automation to mutate code, execute tasks, or trigger sensitive workflows without review, Aixion gives operators a focused mobile interface to inspect pending requests, approve safe actions, reject risky ones, retry failed work, and track execution results.

The app is designed for approval-first workflows where human review, auditability, and operational control matter.

Key capabilities:

• Review pending approval requests from connected agents
• Inspect request status, project context, and execution state
• Approve or reject actions from a mobile-first interface
• Retry failed approval or execution flows when allowed
• Track audit events and lifecycle transitions
• Use authenticated sessions with role-aware access
• Access privacy and account removal controls

Aixion Control Tower is not a general chat app. It is an operator control surface for approval-gated automation.

Best suited for:

• Developers supervising AI coding agents
• QA and release operators reviewing automation output
• Technical teams that need approval gates before execution
• Builders experimenting with human-in-the-loop agent workflows

Important notes:

• A backend deployment is required for production use.
• Connected-agent behavior depends on configured integrations.
• Some advanced workflows require repository, MCP, or execution-worker setup.
• This release focuses on approval review, operator visibility, account controls, and release readiness foundations.
```

## Feature list

Use these as Play Store feature bullets, screenshot captions, and review notes.

```text
Mobile approval review
Approval lifecycle tracking
Connected-agent request visibility
Approve, reject, and retry actions
Authenticated operator access
Role-aware owner and reviewer flows
Audit event visibility
Privacy and account removal controls
Release signing and AAB readiness foundation
```

## Review notes for Google Play

Use this section to prepare the Play Console review instructions.

```text
Aixion Control Tower is a mobile control app for reviewing and approving automation requests created by a backend service.

Reviewer access requires a test account connected to a review/demo backend.

Core review flow:
1. Sign in using the provided test account.
2. Open the approval dashboard.
3. Review pending approval requests.
4. Approve or reject a request.
5. View status and audit/history information.
6. Open Account > Privacy/Data Controls to view account removal controls.

No financial transactions are available in this app.
No user-generated public social content is available in this app.
No ads are displayed in this app.
```

### Required before submission

Replace this section with actual Play Console values before review.

```text
Test username/email: TODO
Test password: TODO
Demo backend URL: TODO
Privacy policy URL: TODO
Account deletion URL: TODO
Support email: TODO
```

## Screenshot checklist

Recommended minimum screenshots for the first closed/internal testing release:

```text
1. Auth / sign-in screen
2. Approval dashboard with pending requests
3. Approval detail screen
4. Approve/reject confirmation state
5. Execution/result status screen
6. Audit/history screen
7. Account screen with privacy/data controls
8. Account removal request flow
```

### Screenshot style requirements

```text
Use clean demo data only.
Avoid real tokens, real repositories, real customer names, real emails, or real secrets.
Show calm, simple screens with readable contrast.
Prefer stable states over empty screens.
Use consistent device frame and aspect ratio.
Avoid showing debug banners, local hostnames, stack traces, or TODO text.
```

## App icon requirements

Required assets:

```text
512 x 512 px Play Store icon
Adaptive Android launcher icon foreground/background
No transparent-only icon
No tiny unreadable text
No copyrighted third-party logos
```

Recommended visual direction:

```text
Calm control-tower motif
Shield or approval-check motif
Blue/indigo/neutral enterprise palette
Simple geometry that remains readable at small sizes
```

## Feature graphic requirements

Required asset:

```text
1024 x 500 px Play Store feature graphic
```

Recommended content:

```text
Product name: Aixion Control Tower
Message: Human approval for connected AI agents
Visual motif: mobile screen + approval gate + agent/workflow nodes
No fake partner logos
No claims such as "secure" or "enterprise-grade" unless backed by release evidence
```

## Store category candidates

Primary category candidate:

```text
Productivity
```

Alternative category candidates:

```text
Business
Tools
```

Recommended category: **Productivity** for first release.

## Data safety notes

This listing draft must stay aligned with:

```text
docs/PLAY_STORE_DATA_SAFETY_DRAFT.md
docs/PRIVACY_POLICY_DRAFT.md
docs/public/privacy-policy.html
docs/public/account-deletion.html
```

Do not claim data deletion, retention, or anonymization behavior beyond what the backend and public policy actually support.

## Compliance warnings

Do not submit until these blockers are resolved:

```text
Public privacy policy TODO values replaced
Public account deletion TODO values replaced
GitHub Pages URLs verified live
Production/review HTTPS backend available
Reviewer test account available
Signed AAB generated and smoke validated
Final SDK/permission/provider review complete
Legal/privacy review complete
Store screenshots and graphics exported
```

## Current first-release positioning

Use this positioning internally when reviewing copy:

```text
Aixion Control Tower is not a replacement for CI/CD, GitHub, ChatGPT, Codex, or MCP servers.
It is the mobile approval and audit layer between humans and connected automation.
```

## Claims to avoid

Avoid these until stronger evidence exists:

```text
Enterprise-grade security
SOC 2 ready
GDPR compliant
Fully autonomous agent control
Works with every AI agent
Production-ready for all teams
Zero-risk automation
```

Safer alternatives:

```text
approval-gated automation
human-in-the-loop control
operator review workflow
audit-focused visibility
connected-agent approval console
```
