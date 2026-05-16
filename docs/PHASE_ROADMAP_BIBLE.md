# Aixion Control Tower — Phase Roadmap & Product Scope Bible

Status: roadmap control document  
Owner: product/operator  
Repository: `ramgolladi1503-sys/aixion-control-tower`  
Applies to: Phase 0 local validation, Phase 1 paid production-shaped setup, Phase 2 production hardening

This document is the execution bible for the remaining Mobile Approval Console / Aixion Control Tower roadmap. It exists to stop scope drift, stop fake readiness claims, and make every future PR map to a clear phase, acceptance gate, and release objective.

## 1. Product definition

Aixion Control Tower is a mobile approval console for AI-agent and automation work.

The product allows a user to review, approve, reject, or revise high-risk agent actions from Android before those actions reach GitHub, MCP child servers, connector workflows, or other mutating execution paths.

The core value is not a pretty dashboard. The core value is this control guarantee:

> No mutating agent or connector action should execute unless the right user can see the context, understand the risk, and approve the exact request.

## 2. Non-negotiable product rules

These rules override convenience.

1. No fake production claims.
   - If backend is local-only, call it local-only.
   - If public pages still contain placeholders, call them placeholders.
   - If signed AAB workflow exists but secrets are not configured, do not claim Play readiness.

2. No silent mock fallback in authenticated product paths.
   - Backend failures must show explicit errors.
   - Empty data must not be replaced with fake approvals/work orders.

3. One PR must do one meaningful thing.
   - Avoid mega-PRs that mix infra, UI, DB, auth, policy, and docs.
   - Every PR must have acceptance gates.

4. Every phase must be testable.
   - Phase 0 must be proven on laptop + real Android phone on same Wi-Fi.
   - Phase 1 must be proven through public HTTPS/review/prod-style infrastructure.
   - Phase 2 must be proven through evidence bundles, rollback drills, and hardening checks.

5. Approval safety beats speed.
   - Approve/reject/revise flows must remain auditable.
   - MCP/agent/connector mutation paths must stay gated.
   - Broker-like, destructive, privileged, or write actions must never bypass the approval lifecycle.

## 3. Current baseline

Latest verified merged before this bible:

```text
PR #144 — Add public page URL verifier
```

Important correction:

The roadmap numbering changed during execution. Earlier planning mapped Phase 1 to PR #137–#144, but actual PR #137–#144 work mostly covered public pages, signing scaffolds, Play Store copy, and URL verification. That work is useful, but it did not finish the originally intended paid production infra work.

Therefore, this bible treats PR #145 as the roadmap reset/control document and continues missing Phase 1 infra from PR #146 onward.

## 4. Phase model

```text
Phase 0 — Local testable product
Goal: prove the product works on a real Android phone connected to laptop backend over same Wi-Fi LAN.
Money: no required paid infra.

Phase 1 — Paid production-shaped setup
Goal: move from LAN demo to public/review/prod-style hosted setup with real domain, DB, email, push, signed AAB smoke, and Play closed-testing readiness.
Money: domain, hosting, database, Play Console, maybe email provider.

Phase 2 — Production hardening and release confidence
Goal: make the product safer, more observable, less fragile, and defensible as a real release candidate.
Money: ongoing infra plus optional monitoring/email/storage costs.
```

## 5. Phase 0 — Local testable product scope

### 5.1 Objective

Phase 0 proves the product locally before spending money.

The expected setup:

```text
Laptop:
- backend running locally
- repository checked out
- test/demo data available
- agent/MCP/connector approval path available

Android phone:
- installed debug/release test APK
- connected to same Wi-Fi as laptop
- API base URL points to laptop LAN IP, not emulator localhost

Network:
- phone can reach backend over http://<laptop-lan-ip>:<port>
```

### 5.2 Phase 0 user journey

The minimum demonstration:

1. User opens Android app.
2. User registers/logs in.
3. App validates session against backend.
4. App loads approvals/work orders from real backend.
5. Agent/MCP/connector work creates a pending approval.
6. Android phone displays the pending approval.
7. User approves or rejects from phone.
8. Backend records the decision.
9. Downstream worker/agent/MCP path continues only if approved.
10. Audit/event trail shows what happened.

### 5.3 Phase 0 merged PR baseline

These PRs form the Phase 0 local testable product baseline:

| PR | Status | Scope |
|---:|---|---|
| #124 | Merged | Auth-first app routing and session gate |
| #125 | Merged | Registration email verification flow |
| #126 | Merged | Android auth UX polish |
| #127 | Merged | Remove authenticated mock fallbacks |
| #128 | Merged | Android retry actions |
| #129 | Merged | WorkOrder provenance and scoped agent creation |
| #130 | Merged | Show WorkOrder source provenance on Android |
| #131 | Merged | Split Android DTOs by domain |
| #132 | Merged | Android release smoke checklist |
| #133 | Merged | Android signed release and Play Store readiness guardrails |
| #134 | Merged | Privacy policy/data safety drafts and account deletion backend foundation |
| #135 | Merged | Android privacy and account removal controls |
| #136 | Merged | Public privacy and account deletion pages |

### 5.4 Phase 0 completion gate

Phase 0 is not complete merely because PRs are merged. It is complete only when this real-device gate passes:

```text
[ ] Backend starts on laptop and binds to LAN-reachable host/port.
[ ] Android app points to laptop LAN IP.
[ ] Fresh install opens auth screen.
[ ] User can register/verify/login.
[ ] Invalid/expired session is rejected.
[ ] Protected screens do not open before auth.
[ ] Approvals load from backend without mock fallback.
[ ] Work orders load from backend without mock fallback.
[ ] Pending MCP/agent approval is visible on Android.
[ ] Approve action updates backend state.
[ ] Reject action updates backend state.
[ ] Audit trail shows source/provenance and decision.
[ ] Retry buttons recover from temporary backend/network errors.
[ ] Demo can be repeated after app restart.
```

### 5.5 Phase 0 explicit non-goals

Phase 0 does not require:

- public domain
- paid backend hosting
- production database
- real email provider
- Play Store release
- production FCM
- legal/privacy review
- external customer use

## 6. Phase 1 — Paid production-shaped setup

### 6.1 Objective

Phase 1 converts the product from LAN demo into a reviewable, internet-accessible, production-shaped system.

This is the phase where money starts to matter.

Phase 1 must not be treated as “just deployment.” It is the point where the product must stop depending on laptop-only assumptions.

### 6.2 Phase 1 target architecture

```text
Android app
  -> public HTTPS backend URL
  -> hosted backend service
  -> hosted production/review database
  -> email provider for verification/resend
  -> FCM push setup for approval notifications
  -> public privacy/account deletion pages
  -> signed AAB artifact
  -> Play Console closed testing track
```

### 6.3 Expected paid or possibly paid items

Exact pricing must be checked before purchase. Do not hardcode vendor prices into release claims.

| Item | Why needed | Paid? |
|---|---|---|
| Domain | Stable public identity and URLs | Usually yes |
| DNS / hosting routing | Public backend and policy URL routing | Usually free/low-cost at start |
| Backend hosting | Public review/prod API | Usually yes |
| Hosted database | Durable state outside laptop | Usually yes |
| Play Console | Closed testing / app distribution | Usually one-time paid account |
| Email provider | Production verification/resend | Free tier possible, paid later |
| Monitoring/logging | Production diagnostics | Free tier possible, paid later |
| FCM | Push notifications | Usually free at start |

### 6.4 Domain and URL plan

Preferred public identity:

```text
Root/company domain:
aixionlabs.com

Product/review backend:
api.controltower.aixionlabs.com

Product landing/support page:
controltower.aixionlabs.com

Privacy policy URL:
https://controltower.aixionlabs.com/privacy-policy

Account deletion URL:
https://controltower.aixionlabs.com/account-deletion
```

Fallback if domain is not bought yet:

```text
Use provider URLs only for temporary review smoke tests.
Do not call provider URLs final public identity.
Do not submit Play Console public URLs until they are stable and placeholder-free.
```

### 6.5 Phase 1 continuation PR roadmap

Because PR #137–#144 were consumed by public-page/signing/Play-prep work, the missing Phase 1 infra continues from PR #146 onward. PR #145 is this bible/control PR.

| Planned PR | Scope | Phase | Spend gate |
|---:|---|---|---|
| #145 | Add Phase Roadmap & Product Scope Bible | Phase 1 control | No spend |
| #146 | Deploy hosted backend review environment | Phase 1 | Backend hosting may start |
| #147 | Add production database deployment path and backup/restore validation | Phase 1 | Hosted DB may start |
| #148 | Wire domain/DNS/HTTPS routing documentation and env config | Phase 1 | Domain/DNS spend may start |
| #149 | Replace public-page placeholders with real release values | Phase 1 | Requires real operator/support/legal inputs |
| #150 | Integrate real email verification provider | Phase 1 | Email provider may start |
| #151 | Configure and validate production FCM push path | Phase 1 | Usually no direct spend |
| #152 | Android production backend config and signed AAB smoke evidence | Phase 1 | Depends on signing secrets and hosted backend |
| #153 | Play Console closed-testing readiness package | Phase 1 | Play developer account required |
| #154 | Phase 1 evidence gate and release decision record | Phase 1 | No new spend, validates all spend |

### 6.6 PR #146 — Deploy hosted backend review environment

Purpose:

Move backend from laptop-only to public HTTPS review environment.

Scope:

- Add deployment documentation for selected provider.
- Add production/review environment variable table.
- Add backend health endpoint validation for deployed environment if missing.
- Add deployment smoke checklist.
- Confirm no secrets are committed.
- Confirm local/dev behavior still works.

Acceptance gate:

```text
[ ] Hosted backend URL exists.
[ ] Health check returns expected response.
[ ] Required env vars are documented.
[ ] Unsafe defaults fail in production/review mode.
[ ] Local demo mode still works.
[ ] No secrets or tokens are committed.
```

Out of scope:

- database migration to hosted DB if separated into PR #147
- real email provider
- Play Console submission

### 6.7 PR #147 — Production database deployment path and backup/restore validation

Purpose:

Move persistent product state from local SQLite assumptions toward hosted durable DB readiness.

Scope:

- Document chosen database provider.
- Add production DB connection env contract.
- Add migration/startup notes for hosted database.
- Add backup policy and restore drill documentation.
- Add tests or scripts that verify DB URL/config validation.
- Preserve local SQLite/dev path if still needed.

Acceptance gate:

```text
[ ] Hosted DB connection configuration is explicit.
[ ] Production mode refuses unsafe/missing DB config.
[ ] Migration path is documented.
[ ] Backup process is documented.
[ ] Restore drill is documented.
[ ] Local dev DB path remains usable.
```

Hard rule:

Do not claim “production database ready” until a restore path is documented and tested at least once.

### 6.8 PR #148 — Domain, DNS, and HTTPS routing

Purpose:

Make public URLs stable and reviewable.

Scope:

- Document DNS records for backend and public pages.
- Document HTTPS verification steps.
- Add environment config examples for public URLs.
- Add validation checklist for privacy/account-deletion URLs.
- Keep provider temporary URLs separate from final product URLs.

Acceptance gate:

```text
[ ] Domain/subdomain plan is documented.
[ ] Backend HTTPS URL is documented.
[ ] Privacy policy URL is documented.
[ ] Account deletion URL is documented.
[ ] URL verifier can be run against deployed URLs.
[ ] Temporary provider URLs are clearly marked temporary.
```

### 6.9 PR #149 — Public-page real release values

Purpose:

Replace placeholder policy/account-deletion values with real operator-approved values.

Scope:

- Fill final developer/operator name.
- Fill support/privacy email.
- Fill actual backend/provider details.
- Fill retention windows.
- Fill account deletion handling language.
- Run local public page readiness guard.
- Run deployed URL verifier if pages are hosted.

Acceptance gate:

```text
[ ] No TODO placeholders remain in public privacy/account deletion pages.
[ ] Public privacy policy URL loads without login.
[ ] Public account deletion URL loads without login.
[ ] Retention windows are stated.
[ ] Support/privacy contact is real.
[ ] Data Safety draft matches actual app behavior.
[ ] Legal/privacy review is marked required before external launch.
```

Hard rule:

Do not invent legal/operator/privacy values. If values are unknown, keep the PR blocked.

### 6.10 PR #150 — Real email provider integration

Purpose:

Replace dev-only verification code behavior with a production-capable email verification/resend path.

Scope:

- Add provider abstraction if not already present.
- Configure production email provider env vars.
- Keep dev/test provider deterministic.
- Add resend rate-limit protection if missing.
- Add audit event for email verification send/resend if appropriate.
- Add provider failure behavior.

Acceptance gate:

```text
[ ] Production mode sends verification email through configured provider.
[ ] Dev/test mode does not require external email provider.
[ ] Missing provider config fails safely in production mode.
[ ] Resend cannot be abused without limits.
[ ] Verification code/token is not logged in production.
[ ] Backend tests cover success and provider failure paths.
```

### 6.11 PR #151 — Production FCM push validation

Purpose:

Validate mobile push notification path for pending approval visibility.

Scope:

- Document FCM project/app setup.
- Confirm Android manifest/config expectations.
- Confirm backend device-token registration path.
- Add push send smoke documentation.
- Add failure logging policy without exposing secrets/tokens.

Acceptance gate:

```text
[ ] Device token can be registered from Android.
[ ] Backend can trigger a test approval notification.
[ ] Notification opens or routes user to relevant approval path where possible.
[ ] Missing/invalid FCM config fails safely.
[ ] Token privacy/retention is documented.
```

### 6.12 PR #152 — Android production backend config and signed AAB smoke evidence

Purpose:

Prove signed Android build can talk to the hosted backend.

Scope:

- Add production/review API base URL build config notes.
- Run signed AAB workflow with real secrets when available.
- Capture smoke evidence path.
- Confirm release build does not point to laptop/localhost.
- Confirm auth/session/approval flows work against hosted backend.

Acceptance gate:

```text
[ ] Signed AAB artifact is generated.
[ ] Release/review build points to hosted HTTPS backend.
[ ] Fresh install auth flow works.
[ ] Approval list loads from hosted backend.
[ ] Approve/reject works from physical device.
[ ] No debug/dev verification secrets are exposed in release build.
```

### 6.13 PR #153 — Play Console closed-testing readiness package

Purpose:

Prepare closed testing honestly, without pretending full public launch readiness.

Scope:

- Add Play closed-test checklist.
- Add tester instructions.
- Add known limitations.
- Add privacy/account deletion/public URL evidence references.
- Add signed artifact upload notes.
- Add release notes for testers.

Acceptance gate:

```text
[ ] Play listing draft is complete enough for closed test.
[ ] Public URLs are stable and placeholder-free.
[ ] Signed AAB is available.
[ ] Tester instructions exist.
[ ] Known limitations are documented.
[ ] Data Safety draft is reviewed against actual artifact behavior.
```

### 6.14 PR #154 — Phase 1 evidence gate and release decision record

Purpose:

Create a clear go/no-go record for moving into Phase 2.

Scope:

- Add Phase 1 evidence checklist.
- Link hosted backend URL evidence.
- Link database backup/restore evidence.
- Link public page verifier output.
- Link signed AAB evidence.
- Link real-device smoke evidence.
- Record open blockers honestly.

Acceptance gate:

```text
[ ] Phase 0 LAN gate passed or explicitly marked stale.
[ ] Hosted backend gate passed.
[ ] Hosted DB gate passed.
[ ] Domain/public URL gate passed.
[ ] Email provider gate passed.
[ ] FCM gate passed or intentionally deferred.
[ ] Signed AAB gate passed.
[ ] Closed-test readiness gate passed.
[ ] Remaining blockers are documented with owner/action.
```

## 7. Phase 2 — Production hardening roadmap

### 7.1 Objective

Phase 2 turns the Phase 1 hosted system into a safer release candidate.

Phase 2 is not where the product becomes “feature bigger.” It is where the product becomes harder to break, harder to misuse, and easier to audit.

### 7.2 Planned Phase 2 PRs

| Planned PR | Scope | Why it matters |
|---:|---|---|
| #155 | Secrets/session/rate-limit hardening | Prevent obvious auth/session abuse |
| #156 | Retention, anonymization, and account deletion executor | Convert policy docs into actual lifecycle behavior |
| #157 | Agent/MCP/connector policy guardrail hardening | Reduce risk of dangerous mutations slipping through |
| #158 | Database migration rollback and restore drill | Prove recoverability, not just migration forward path |
| #159 | Observability and error telemetry without PII leakage | Debug production safely |
| #160 | Android offline/session-expiry/retry polish | Improve real-world mobile reliability |
| #161 | Release candidate evidence bundle | Centralize proof for reviewers/recruiters/users |
| #162 | v1.0 release candidate tag and launch decision | Freeze a defensible release state |

### 7.3 PR #155 — Secrets/session/rate-limit hardening

Acceptance gate:

```text
[ ] Production secrets are documented and not committed.
[ ] Session expiry and revocation are tested.
[ ] Login/register/verification resend rate limits exist or are documented as blockers.
[ ] Unsafe debug behavior is blocked in production mode.
[ ] Security-sensitive logs avoid tokens/passwords/codes.
```

### 7.4 PR #156 — Retention, anonymization, and account deletion executor

Acceptance gate:

```text
[ ] Account deletion request lifecycle is explicit.
[ ] User-facing deletion request status is auditable.
[ ] Retained audit/security data is justified.
[ ] Anonymization/deletion behavior is implemented or deliberately queued.
[ ] Retention policy matches public pages.
```

### 7.5 PR #157 — Agent/MCP/connector guardrail hardening

Acceptance gate:

```text
[ ] Mutating tool calls remain approval-gated.
[ ] Read-only calls cannot secretly mutate state.
[ ] High-risk operations carry risk labels.
[ ] Approval payload hash/integrity behavior remains enforced.
[ ] Rejected/expired approvals cannot execute later.
```

### 7.6 PR #158 — Migration rollback and restore drill

Acceptance gate:

```text
[ ] Migration forward path is tested.
[ ] Unknown newer DB version fails safely.
[ ] Backup procedure exists.
[ ] Restore procedure is tested in a non-production environment.
[ ] Failure playbook exists.
```

### 7.7 PR #159 — Observability without PII leakage

Acceptance gate:

```text
[ ] Health/readiness endpoints are clear.
[ ] Error logging policy exists.
[ ] Sensitive fields are redacted.
[ ] Operational events are useful for debugging.
[ ] No tokens, verification codes, passwords, or secrets appear in logs.
```

### 7.8 PR #160 — Android offline/session-expiry/retry polish

Acceptance gate:

```text
[ ] Offline state is understandable.
[ ] Expired session routes user back to auth.
[ ] Retry actions are consistent.
[ ] Approval actions cannot appear successful if backend failed.
[ ] App restart preserves/clears state correctly.
```

### 7.9 PR #161 — Release candidate evidence bundle

Acceptance gate:

```text
[ ] Evidence bundle links validation docs.
[ ] Evidence bundle links release smoke results.
[ ] Evidence bundle links privacy/public URL checks.
[ ] Evidence bundle links security/hardening decisions.
[ ] Known limitations are explicit.
```

### 7.10 PR #162 — v1.0 release candidate tag and launch decision

Acceptance gate:

```text
[ ] Release candidate tag is documented.
[ ] Go/no-go decision is recorded.
[ ] Open risks are accepted or blocked.
[ ] Rollback path is documented.
[ ] Next post-release roadmap is separated from launch blockers.
```

## 8. Execution discipline for every future PR

Every PR after this bible must include:

```text
## Summary
What changed in plain English.

## Phase mapping
Phase 0 / Phase 1 / Phase 2.

## Scope
What is included.

## Out of scope
What is intentionally not included.

## Acceptance gate
Checkboxes proving the PR is done.

## Validation
Commands, manual checks, or evidence required.

## Risk notes
What could break or what remains unfinished.
```

## 9. Required validation commands by area

Backend typical validation:

```bash
cd backend
python -m pytest
```

Android typical validation:

```bash
cd mobile/android
./gradlew testDebugUnitTest
./gradlew assembleDebug
```

Android UI/device validation when relevant:

```bash
cd mobile/android
./gradlew connectedDebugAndroidTest
```

Public page validation when relevant:

```bash
python scripts/validate_public_pages_ready.py
python scripts/verify_public_page_urls.py --privacy-url <url> --account-deletion-url <url>
```

Signed AAB validation when relevant:

```text
Run manual signed AAB GitHub Actions workflow with real signing secrets configured.
Download artifact.
Install/test through intended review/closed-test path.
```

## 10. Release claim policy

Allowed claims:

```text
Local demo-ready
LAN-testable
Review-environment ready
Closed-test candidate
Release candidate
```

Forbidden claims unless evidence exists:

```text
Production-ready
Play Store ready
Secure
Compliant
Enterprise-ready
Fully private
Fully hardened
```

Use conservative wording until the evidence exists.

## 11. Spending gate

Do not spend money just because the roadmap says Phase 1.

Spend only when the related gate is ready:

```text
Domain:
Buy only when public URL plan is final.

Backend hosting:
Start only when deployment env contract is ready.

Hosted database:
Start only when DB connection, migration, backup, and restore expectations are clear.

Email provider:
Start only when verification provider abstraction/config path is ready.

Play Console:
Use only when signed AAB, public pages, privacy/data safety, and tester plan are close enough for closed testing.
```

## 12. Definition of done by phase

### Phase 0 done

```text
A real Android phone on same Wi-Fi as laptop can login, load real backend data, approve/reject pending work, and show audit/provenance without mock fallback.
```

### Phase 1 done

```text
A signed Android build can connect to a hosted HTTPS backend backed by durable DB, use real auth/email behavior, expose public privacy/account deletion URLs, pass URL/readiness checks, and be prepared honestly for Play closed testing.
```

### Phase 2 done

```text
The hosted product has evidence-backed hardening for secrets, sessions, retention, audit, DB recovery, observability, mobile reliability, and release candidate decision-making.
```

## 13. Hard truth checkpoint

The repo has moved fast. That is good, but speed creates a dangerous illusion: lots of merged PRs can look like production readiness even when paid infra, real hosted DB, real email, real public URLs, and real-device evidence are missing.

This bible exists to prevent that mistake.

The next correct move is not random feature work. The next correct move is to follow the roadmap from PR #146 onward and close the paid-infra gap honestly.
