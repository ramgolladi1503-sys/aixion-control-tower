# Aixion Control Tower — Phase Roadmap & Product Scope Bible

Status: roadmap control document  
Repository: `ramgolladi1503-sys/aixion-control-tower`  
Applies to: Phase 0 local validation, Phase 1 paid production-shaped setup, Play Store readiness track, Phase 2 hardening

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
   - Play Store readiness must start only after Phase 1 evidence is accepted.
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
PR #149 — Add Phase 0 LAN completion gate
```

Decision after PR #149:

```text
Do not enter paid Phase 1 until Phase 0 is fully proven with real-device LAN evidence.
Do not enter Play Store readiness until Phase 1 evidence is accepted.
```

## 4. Phase model

```text
Phase 0 — Local testable product
Goal: prove the app works on a real Android phone connected to laptop backend over same Wi-Fi LAN.
Money: no required paid infrastructure.

Phase 1 — Paid production-shaped setup
Goal: move from LAN demo to public/review/production-shaped setup with domain, hosted backend, hosted database, email/review communication path, push path where required, signed build smoke, and public URLs.
Money: domain, hosting, database, maybe email/provider services.

Play Store readiness track — after Phase 1
Goal: prepare Play Console, store listing, policy recheck, signed AAB upload validation, internal testing, closed testing, and final store-readiness evidence.
Money: Play Console account and any testing/release support costs.

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
  -> review/prod auth and email path where required
  -> push setup where required
  -> public privacy/account deletion pages
  -> signed Android artifact smoke-tested against hosted backend
```

Play Console readiness is intentionally not part of Phase 1. It starts after Phase 1 evidence is accepted.

### Expected paid or possibly paid items

| Item | Why needed | Paid? |
|---|---|---|
| Domain | Stable public identity and URLs | Usually yes |
| DNS / hosting routing | Public backend and policy URL routing | Free or low-cost at start |
| Backend hosting | Public review/prod API | Usually yes |
| Hosted database | Durable state outside laptop | Usually yes |
| Email provider | Production verification/resend or support path | Free tier possible, paid later |
| Monitoring/logging | Production diagnostics | Free tier possible, paid later |
| Push service | Approval notifications if enabled | Usually free at start |

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
| #154 | Integrate real email verification/provider path | Phase 1 | Email provider may start |
| #155 | Configure and validate production push notification path if enabled | Phase 1 | Usually no direct spend |
| #156 | Android production backend config and signed AAB smoke evidence | Phase 1 | Depends on signing config and hosted backend |
| #157 | Phase 1 evidence gate and release decision record | Phase 1 | No new spend, validates all spend |

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
[ ] Data Safety draft is marked ready for post-Phase-1 Play alignment, not submitted yet.
[ ] Legal/privacy review is marked required before external launch.
```

Hard rule: do not invent legal/operator/privacy values. If values are unknown, keep the PR blocked.

### PR #154 — Real email verification/provider path

Purpose: replace dev-only verification behavior with production-capable email verification/resend or define a conscious review-safe alternative.

Acceptance gate:

```text
[ ] Production mode has an email/provider path or explicit approved alternative.
[ ] Dev/test mode does not require external provider.
[ ] Missing provider config fails safely in production mode.
[ ] Resend cannot be abused without limits if email verification is enabled.
[ ] Verification credential is not exposed in production logs.
[ ] Backend tests cover success and provider failure paths.
```

### PR #155 — Production push notification validation

Purpose: validate mobile push notification path for pending approval visibility if notifications are enabled for the release/review build.

Acceptance gate:

```text
[ ] Device registration path works from Android if push is enabled.
[ ] Backend can trigger a test approval notification if push is enabled.
[ ] Notification opens or routes user to relevant approval path where possible.
[ ] Missing/invalid push config fails safely.
[ ] Notification identifier privacy/retention is documented.
[ ] If push is deferred, the deferral is explicit and Play disclosure docs are updated.
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

### PR #157 — Phase 1 evidence gate and release decision record

Purpose: create a clear go/no-go record for entering the Play Store readiness track.

Acceptance gate:

```text
[ ] Phase 0 LAN gate passed.
[ ] Hosted backend gate passed.
[ ] Hosted DB gate passed.
[ ] Domain/public URL gate passed.
[ ] Public page real values gate passed.
[ ] Email/provider gate passed or consciously deferred.
[ ] Push gate passed or consciously deferred.
[ ] Signed build smoke gate passed.
[ ] Remaining blockers are documented with owner/action.
[ ] Decision recorded: ready or not ready to start Play Store readiness track.
```

## 7. Play Store readiness track — after Phase 1

Dedicated plan:

```text
docs/PLAY_STORE_READINESS_AFTER_PHASE1.md
```

This track starts only after PR #157 accepts Phase 1 evidence.

| Planned PR | Scope | Gate |
|---:|---|---|
| #158 | Play Store policy recheck and readiness lock | Official requirements rechecked; repo checklist updated |
| #159 | Final store listing metadata and review notes | App name, descriptions, support contact, review instructions finalized |
| #160 | Final screenshots, icon, and feature graphic evidence | Required visual assets exported and reviewed |
| #161 | Final privacy policy, account deletion, and Data Safety alignment | Public URLs and Play form answers match actual app/backend behavior |
| #162 | Play App Signing and signed AAB upload validation | Signed AAB/upload path verified without committing signing material |
| #163 | Internal testing track package | Internal test instructions, build notes, known limitations, tester guidance |
| #164 | Closed testing package and tester evidence | Closed test setup, tester instructions, feedback collection plan |
| #165 | Play Store readiness evidence gate | Go/no-go record for Play closed testing or production access request |

## 8. Phase 2 — Production hardening roadmap

Phase 2 starts after the Play Store readiness track is either completed or consciously deferred.

| Planned PR | Scope | Why it matters |
|---:|---|---|
| #166 | Session/rate-limit hardening | Prevent obvious auth/session abuse |
| #167 | Retention, anonymization, and account deletion executor | Convert policy docs into lifecycle behavior |
| #168 | Agent/MCP/connector policy guardrail hardening | Reduce mutation risk |
| #169 | Database migration rollback and restore drill | Prove recoverability |
| #170 | Observability and error telemetry without PII leakage | Debug production safely |
| #171 | Android offline/session-expiry/retry polish | Improve mobile reliability |
| #172 | Release candidate evidence bundle | Centralize proof |
| #173 | v1.0 release candidate tag and launch decision | Freeze a defensible release state |

## 9. Execution discipline for every future PR

Every PR after this bible must include:

```text
## Summary
What changed in plain English.

## Phase mapping
Phase 0 / Phase 1 / Play Store readiness / Phase 2.

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

## 10. Required validation commands by area

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

## 11. Release claim policy

Allowed claims:

```text
Local demo-ready
LAN-testable
Review-environment ready
Phase 1 evidence accepted
Play Store prep in progress
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

## 12. Spending gate

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
Use only after Phase 1 evidence is accepted and Play Store readiness track begins.
```

## 13. Definition of done by phase

### Phase 0 done

```text
A real Android phone on same Wi-Fi as laptop can login, load real backend data, approve/reject pending work, and show audit/provenance without mock fallback.
```

### Phase 1 done

```text
A signed Android build can connect to a hosted HTTPS backend backed by durable DB, use real auth/email behavior or documented review-safe alternative, expose public privacy/account deletion URLs, pass URL/readiness checks, and pass hosted-backend physical-device smoke testing.
```

### Play Store readiness done

```text
The Play Store policy recheck, store listing, visual assets, privacy/account deletion/Data Safety alignment, signed AAB upload validation, internal testing package, closed testing package, and final readiness evidence gate are complete.
```

### Phase 2 done

```text
The hosted product has evidence-backed hardening for sessions, retention, audit, DB recovery, observability, mobile reliability, and release candidate decision-making.
```

## 14. Hard truth checkpoint

The repo has moved fast. That is good, but speed creates a dangerous illusion: lots of merged PRs can look like production readiness even when paid infrastructure, real hosted DB, real email, real public URLs, and real-device evidence are missing.

This bible exists to prevent that mistake.

The correct order is:

```text
1. Finish Phase 0 evidence.
2. Complete Phase 1 hosted infrastructure evidence.
3. Start Play Store readiness track.
4. Then continue deeper hardening/release-candidate work.
```
