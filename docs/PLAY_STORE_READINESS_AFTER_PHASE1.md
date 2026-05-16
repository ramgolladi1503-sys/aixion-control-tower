# Play Store Readiness After Phase 1

Status: planning gate  
Applies after: Phase 0 LAN evidence and Phase 1 infrastructure evidence  
Purpose: prevent confusing Play Store preparation with real Play Store readiness

## 1. Hard rule

Play Store readiness starts after Phase 1 is complete.

Do not claim Play Store readiness during Phase 0 or while Phase 1 infrastructure is still incomplete.

Correct order:

```text
Phase 0: prove local laptop + physical Android phone LAN flow.
Phase 1: prove hosted backend, hosted database, domain/public URLs, email, push path, and signed build smoke.
Play Store readiness track: prepare and validate Play Console submission after Phase 1 evidence is accepted.
Phase 2: hardening and release-candidate confidence.
```

## 2. Why this exists

The repo already has Play Store drafts, public page scaffolds, signing scaffolds, and asset specs. Those are useful, but they are not the same as upload readiness.

Play Store readiness depends on real infrastructure and real evidence:

```text
[ ] real backend URL
[ ] real database persistence
[ ] real public privacy/account deletion URLs
[ ] real email/support values
[ ] real signed Android artifact
[ ] real physical-device smoke test
[ ] real Data Safety answers matching the artifact and backend behavior
[ ] real tester/reviewer instructions
```

If those are missing, the product is not Play Store ready.

## 3. Required prerequisites before Play Store readiness starts

### Phase 0 prerequisite

```text
[ ] Phase 0 LAN completion evidence is recorded.
[ ] Physical Android phone reaches laptop backend over same Wi-Fi.
[ ] Login/session flow works.
[ ] Approval and rejection work from phone.
[ ] Audit/provenance is verified.
[ ] App restart/retry behavior is verified.
```

### Phase 1 prerequisite

```text
[ ] Hosted backend review environment exists.
[ ] Hosted database path and recovery plan are documented.
[ ] Domain/public URL plan is active.
[ ] Privacy policy URL is live and placeholder-free.
[ ] Account deletion URL is live and placeholder-free.
[ ] Real support/privacy contact is set.
[ ] Real email verification provider path is ready or consciously deferred.
[ ] Push notification path is validated or consciously deferred.
[ ] Signed Android artifact is generated.
[ ] Signed artifact smoke test passes against hosted backend.
[ ] Phase 1 evidence gate is accepted.
```

## 4. Official-policy recheck rule

Before Play submission or closed testing setup, re-check current Google Play Console guidance.

At minimum, re-check:

```text
[ ] Data Safety form requirements
[ ] privacy policy requirement
[ ] account deletion requirement
[ ] app access/reviewer login instructions
[ ] Play App Signing / upload key flow
[ ] closed testing requirements for the current developer account type
[ ] target SDK / permissions / SDK policy status
```

Do not rely only on old repo docs for final policy decisions.

## 5. Play Store readiness track

This track begins only after Phase 1 evidence is accepted.

| Planned PR | Scope | Gate |
|---:|---|---|
| #159 | Play Store policy recheck and readiness lock | Official requirements rechecked; repo checklist updated |
| #160 | Final store listing metadata and review notes | App name, descriptions, support contact, review instructions finalized |
| #161 | Final screenshots, icon, and feature graphic evidence | Required visual assets exported and reviewed |
| #162 | Final privacy policy, account deletion, and Data Safety alignment | Public URLs and Play form answers match actual app/backend behavior |
| #163 | Play App Signing and signed AAB upload validation | Signed AAB/upload path verified without committing signing material |
| #164 | Internal testing track package | Internal test instructions, build notes, known limitations, tester guidance |
| #165 | Closed testing package and tester evidence | Closed test setup, tester instructions, feedback collection plan |
| #166 | Play Store readiness evidence gate | Go/no-go record for Play closed testing or production access request |

## 6. PR #159 — Play Store policy recheck and readiness lock

Purpose:

Refresh the repo against current Play Console requirements before any submission work.

Acceptance gate:

```text
[ ] Official Play Console Data Safety guidance rechecked.
[ ] Official account deletion guidance rechecked.
[ ] Official privacy policy guidance rechecked.
[ ] Official app access/reviewer login guidance rechecked.
[ ] Official Play App Signing guidance rechecked.
[ ] Official testing-track requirements rechecked.
[ ] Any changed requirements are added to repo docs.
```

## 7. PR #160 — Final store listing metadata and review notes

Purpose:

Finalize what reviewers and testers will see.

Acceptance gate:

```text
[ ] App name is final.
[ ] Short description is final.
[ ] Full description is final.
[ ] Support email is real.
[ ] Review/demo instructions exist.
[ ] If login is required, reviewer credentials or access instructions are prepared.
[ ] Known limitations are clear and honest.
```

## 8. PR #161 — Final screenshots, icon, and feature graphic evidence

Purpose:

Prepare visual assets without overclaiming product maturity.

Acceptance gate:

```text
[ ] App icon export exists.
[ ] Feature graphic export exists.
[ ] Phone screenshots exist.
[ ] Screenshot captions are honest.
[ ] Screenshots use safe demo/review data.
[ ] Screenshots do not expose secrets, private repos, tokens, emails, or real user data.
```

## 9. PR #162 — Final privacy/account deletion/Data Safety alignment

Purpose:

Make privacy docs and Play Console answers match actual behavior.

Acceptance gate:

```text
[ ] Privacy policy URL is public and loads without login.
[ ] Account deletion URL is public and loads without login.
[ ] In-app account deletion/request path exists if account creation exists.
[ ] Deletion/retention behavior is described honestly.
[ ] Data Safety answers match the final APK/AAB, backend, providers, SDKs, and permissions.
[ ] No TODO placeholders remain.
```

## 10. PR #163 — Play App Signing and signed AAB upload validation

Purpose:

Validate the artifact path for Play submission.

Acceptance gate:

```text
[ ] Upload/app signing approach is decided.
[ ] Signed AAB exists.
[ ] Version code is incremented.
[ ] Version name is set.
[ ] AAB upload validation passes or errors are documented.
[ ] Signing material is not committed.
```

## 11. PR #164 — Internal testing track package

Purpose:

Prepare a safe first Play Console testing path.

Acceptance gate:

```text
[ ] Internal test build is selected.
[ ] Internal tester list is prepared.
[ ] Tester install instructions exist.
[ ] Feedback channel exists.
[ ] Known limitations are documented.
[ ] Backend/test account setup is documented.
```

## 12. PR #165 — Closed testing package and tester evidence

Purpose:

Prepare wider controlled testing after internal smoke is stable.

Acceptance gate:

```text
[ ] Closed testing track setup is documented.
[ ] Tester requirement for the current developer account type is checked.
[ ] Tester list/process is prepared.
[ ] Testing duration requirements are recorded.
[ ] Feedback plan exists.
[ ] Production access request notes are drafted if applicable.
```

## 13. PR #166 — Play Store readiness evidence gate

Purpose:

Make an honest go/no-go decision before claiming Play Store readiness.

Acceptance gate:

```text
[ ] Phase 0 evidence accepted.
[ ] Phase 1 evidence accepted.
[ ] Policy recheck complete.
[ ] Store metadata complete.
[ ] Visual assets complete.
[ ] Privacy/account deletion/Data Safety complete.
[ ] Signed AAB/upload validation complete.
[ ] Internal testing package complete.
[ ] Closed testing package complete or consciously deferred.
[ ] Open blockers are recorded.
```

## 14. Forbidden claims before this gate

Do not claim:

```text
Play Store ready
ready for public launch
compliant
approved for Play
production release ready
```

Allowed claims before the final gate:

```text
Play Store prep in progress
closed-test prep in progress
signed build scaffold ready
public-page scaffold ready
Data Safety draft available
```

## 15. Hard truth

A signed AAB is not Play Store readiness.

A privacy policy draft is not Play Store readiness.

A checklist is not Play Store readiness.

Play Store readiness means the final artifact, backend, public URLs, account deletion flow, Data Safety answers, store listing, tester path, and evidence all line up.
