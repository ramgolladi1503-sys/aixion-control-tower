# Aixion Control Tower — Phase Roadmap & Product Scope Bible

Status: roadmap control document  
Repository: `ramgolladi1503-sys/aixion-control-tower`  
Applies to: Phase 0 local validation, Phase 1 paid production-shaped setup, Phase 2 production hardening

This document is the execution bible for the Mobile Approval Console / Aixion Control Tower roadmap. It exists to stop scope drift, stop fake readiness claims, and make every future PR map to a clear phase, acceptance gate, and release objective.

## 1. Product definition

Aixion Control Tower is a mobile approval console for AI-agent and automation work.

The product allows a user to review, approve, reject, or revise high-risk agent actions from Android before those actions reach GitHub, MCP child servers, connector workflows, or other mutating execution paths.

Core guarantee:

> No mutating agent or connector action should execute unless the right user can see the context, understand the risk, and approve the exact request.

## 2. Non-negotiable rules

1. No fake production claims.
   - If backend is local-only, call it local-only.
   - If public pages still contain placeholders, call them placeholders.
   - If signed build automation exists but real release configuration is absent, do not claim store readiness.

2. No silent mock fallback in authenticated product paths.
   - Backend failures must show explicit errors.
   - Empty backend data must not be replaced with fake approvals or work orders.

3. One PR must do one meaningful thing.
   - Avoid mega-PRs that mix infrastructure, UI, database, auth, policy, and docs.
   - Every PR must have an acceptance gate.

4. Every phase must be testable.
   - Phase 0 must be proven on laptop plus real Android phone on same Wi-Fi.
   - Phase 1 must be proven through public HTTPS/review/production-shaped infrastructure.
   - Phase 2 must be proven through evidence bundles, recovery drills, and hardening checks.

5. Approval safety beats speed.
   - Approve, reject, and revise flows must remain auditable.
   - MCP, agent, and connector mutation paths must stay gated.
   - Privileged or write actions must never bypass the approval lifecycle.

## 3. Current baseline and Phase 0 lock

Latest verified roadmap-control baseline:

```text
PR #147 — Add Phase Roadmap Product Scope Bible
PR #148 — Correct phase roadmap bible numbering
```

Correction:

Earlier planning mapped Phase 1 to PR #137–#144, but actual PR #137–#144 mostly covered public pages, signing scaffolds, Play Store copy, and URL verification. Useful work, but not the originally intended paid production infrastructure.

Decision after PR #148:

```text
Do not enter paid Phase 1 until Phase 0 is fully proven with real-device LAN evidence.
PR #149 is reserved for Phase 0 LAN completion gate and evidence template.
Phase 1 hosted backend work starts after Phase 0 evidence is accepted.
```

## 4. Phase model

```text
Phase 0 — Local testable product
Goal: prove the app works on a real Android phone connected to laptop backend over same Wi-Fi LAN.
Money: no required paid infrastructure.

Phase 1 — Paid production-shaped setup
Goal: move from LAN demo to public/review/production-shaped setup with domain, hosted backend, hosted database, email, push, signed build smoke, and Play closed-testing readiness.
Money: domain, hosting, database, Play Console, maybe email provider.

Phase 2 — Production hardening and release confidence
Goal: make the product safer, more observable, less fragile, and defensible as a release candidate.
Money: ongoing infrastructure plus optional monitoring/email/storage costs.
```

## 5. Phase 0 — Local testable product

### Objective

Phase 0 proves the product locally before spending money.

Expected setup:

```text
Laptop:
- backend running locally
- repository checked out
- test/demo data available
- agent/MCP/connector approval path available

Android phone:
- installed debug or release-test APK
- connected to same Wi-Fi as laptop
- API base URL points to laptop LAN IP, not emulator localhost

Network:
- phone can reach backend over http://<laptop-lan-ip>:<port>
```

Detailed gate:

```text
docs/PHASE0_LAN_COMPLETION_GATE.md
```

### Phase 0 user journey

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

### Phase 0 merged PR baseline

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
| #149 | Planned | Phase 0 LAN completion gate and evidence template |

### Phase 0 completion gate

Phase 0 is not complete merely because PRs are merged. It is complete only when this real-device gate passes and evidence is recorded:

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

### Phase 0 non-goals

Phase 0 does not require public domain, paid backend hosting, production database, real email provider, Play Store release, production push notifications, legal/privacy review, or external customer use.

## 6. Phase 1 — Paid production-shaped setup

### Objective

Phase 1 converts the product from LAN demo into an internet-accessible review/production-shaped system.

Target architecture:

```text
Android app
  -> public HTTPS backend URL
  -> hosted backend service
  -> hosted database
  -> email provider for verification/resend
  -> push setup for approval notifications
  -> public privacy/account deletion pages
  -> signed Android artifact
  -> Play Console closed testing track
```

### Expected paid or possibly paid items

| Item | Why needed | Paid? |
|---|---|---|
| Domain | Stable public identity and URLs | Usually yes |
| DNS / hosting routing | Public backend and policy URL routing | Free or low-cost at start |
| Backend hosting | Public review/prod API | Usually yes |
| Hosted database | Durable state outside laptop | Usually yes |
| Play Console | Closed testing / app distribution | Usually one-time paid account |
| Email provider | Production verification/resend | Free tier possible, paid later |
| Monitoring/logging | Production diagnostics | Free tier possible, paid later |
| Push service | Approval notifications | Usually free at start |

### Domain and URL plan

Preferred public identity:

```text
Root/company domain: aixionlabs.com
Product/review backend: api.controltower.aixionlabs.com
Product landing/support page: controltower.aixionlabs.com
Privacy policy URL: https://controltower.aixionlabs.com/privacy-policy
Account deletion URL: https://controltower.aixionlabs.com/account-deletion
```

Fallback:

```text
Use provider URLs only for temporary review smoke tests.
Do not call provider URLs final public identity.
Do not submit Play Console public URLs until they are stable and placeholder-free.
```

### Phase 1 continuation roadmap

Phase 1 starts only after Phase 0 LAN completion evidence is accepted.

| Planned PR | Scope | Phase | Spend gate |
|---:|---|---|---|
| #150 | Deploy hosted backend review environment | Phase 1 | Backend hosting may start |
| #151 | Add production database deployment path and backup/restore validation | Phase 1 | Hosted DB may start |
| #152 | Wire domain/DNS/HTTPS routing documentation and env config | Phase 1 | Domain/DNS spend may start |
| #153 | Replace public-page placeholders with real release values | Phase 1 | Requires real operator/support/legal inputs |
| #154 | Integrate real email verification provider | Phase 1 | Email provider may start |
| #155 | Configure and validate production push notification path | Phase 1 | Usually no direct spend |
| #156 | Android production backend config and signed AAB smoke evidence | Phase 1 | Depends on signing config and hosted backend |
| #157 | Play Console closed-testing readiness package | Phase 1 | Play developer account required |
| #158 | Phase 1 evidence gate and release decision record | Phase 1 | No new spend, validates all spend |

### PR #150 — Deploy hosted backend review environment

Purpose: move backend from laptop-only to public HTTPS review environment.

Acceptance gate:

```text
[ ] Hosted backend URL exists.
[ ] Health check returns expected response.
[ ] Required environment values are documented.
[ ] Unsafe defaults fail in production/review mode.
[ ] Local demo mode still works.
[ ] No sensitive values are committed.
```

### PR #151 — Production database deployment path and backup/restore validation

Purpose: move persistent product state from local assumptions toward hosted durable DB readiness.

Acceptance gate:

```text
[ ] Hosted DB configuration is explicit.
[ ] Production mode refuses unsafe/missing DB config.
[ ] Migration path is documented.
[ ] Backup process is documented.
[ ] Restore drill is documented.
[ ] Local dev DB path remains usable.
```

Hard rule: do not claim production database readiness until restore is documented and tested at least once.

### PR #152 — Domain, DNS, and HTTPS routing

Purpose: make public URLs stable and reviewable.

Acceptance gate:

```text
[ ] Domain/subdomain plan is documented.
[ ] Backend HTTPS URL is documented.
[ ] Privacy policy URL is documented.
[ ] Account deletion URL is documented.
[ ] URL verifier can be run against deployed URLs.
[ ] Temporary provider URLs are clearly marked temporary.
```

### PR #153 — Public-page real release values

Purpose: replace placeholder policy/account-deletion values with real operator-approved values.

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

Hard rule: do not invent legal/operator/privacy values. If values are unknown, keep the PR blocked.

### PR #154 — Real email provider integration

Purpose: replace dev-only verification behavior with production-capable email verification/resend.

Acceptance gate:

```text
[ ] Production mode sends verification email through configured provider.
[ ] Dev/test mode does not require external provider.
[ ] Missing provider config fails safely in production mode.
[ ] Resend cannot be abused without limits.
[ ] Verification credential is not exposed in production logs.
[ ] Backend tests cover success and provider failure paths.
```

### PR #155 — Production push notification validation

Purpose: validate mobile push notification path for pending approval visibility.

Acceptance gate:

```text
[ ] Device registration path works from Android.
[ ] Backend can trigger a test approval notification.
[ ] Notification opens or routes user to relevant approval path where possible.
[ ] Missing/invalid push config fails safely.
[ ] Notification identifier privacy/retention is documented.
```

### PR #156 — Android production backend config and signed AAB smoke evidence

Purpose: prove signed Android build can talk to the hosted backend.

Acceptance gate:

```text
[ ] Signed Android artifact is generated.
[ ] Release/review build points to hosted HTTPS backend.
[ ] Fresh install auth flow works.
[ ] Approval list loads from hosted backend.
[ ] Approve/reject works from physical device.
[ ] Debug-only behavior is absent from release build.
```

### PR #157 — Play Console closed-testing readiness package

Purpose: prepare closed testing honestly, without pretending full public launch readiness.

Acceptance gate:

```text
[ ] Play listing draft is complete enough for closed test.
[ ] Public URLs are stable and placeholder-free.
[ ] Signed Android artifact is available.
[ ] Tester instructions exist.
[ ] Known limitations are documented.
[ ] Data Safety draft is reviewed against actual artifact behavior.
```

### PR #158 — Phase 1 evidence gate and release decision record

Purpose: create a clear go/no-go record for moving into Phase 2.

Acceptance gate:

```text
[ ] Phase 0 LAN gate passed or explicitly marked stale.
[ ] Hosted backend gate passed.
[ ] Hosted DB gate passed.
[ ] Domain/public URL gate passed.
[ ] Email provider gate passed.
[ ] Push gate passed or intentionally deferred.
[ ] Signed build gate passed.
[ ] Closed-test readiness gate passed.
[ ] Remaining blockers are documented with owner/action.
```

## 7. Phase 2 — Production hardening roadmap

| Planned PR | Scope | Why it matters |
|---:|---|---|
| #159 | Session/rate-limit hardening | Prevent obvious auth/session abuse |
| #160 | Retention, anonymization, and account deletion executor | Convert policy docs into lifecycle behavior |
| #161 | Agent/MCP/connector policy guardrail hardening | Reduce mutation risk |
| #162 | Database migration rollback and restore drill | Prove recoverability |
| #163 | Observability and error telemetry without PII leakage | Debug production safely |
| #164 | Android offline/session-expiry/retry polish | Improve mobile reliability |
| #165 | Release candidate evidence bundle | Centralize proof |
| #166 | v1.0 release candidate tag and launch decision | Freeze a defensible release state |

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

Signed Android artifact validation when relevant:

```text
Run manual signed Android artifact workflow with real release configuration.
Download artifact.
Install/test through intended review or closed-test path.
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
Start only when deployment environment contract is ready.

Hosted database:
Start only when DB connection, migration, backup, and restore expectations are clear.

Email provider:
Start only when verification provider abstraction/config path is ready.

Play Console:
Use only when signed artifact, public pages, privacy/data safety, and tester plan are close enough for closed testing.
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
The hosted product has evidence-backed hardening for sessions, retention, audit, DB recovery, observability, mobile reliability, and release candidate decision-making.
```

## 13. Hard truth checkpoint

The repo has moved fast. That is good, but speed creates a dangerous illusion: lots of merged PRs can look like production readiness even when paid infrastructure, real hosted DB, real email, real public URLs, and real-device evidence are missing.

This bible exists to prevent that mistake.

The next correct move after PR #149 is not paid hosting. The next correct move is to execute and record Phase 0 LAN evidence. Only after that should Phase 1 begin with:

```text
PR #150 — Deploy hosted backend review environment
```
