# Physical Device Approval Loop Validation — 2026-10-20

## Result

**Native Antigravity approve and deny paths verified on the physical Pixel.**
Antigravity 2.24.0 issued fresh `run_command` calls from a disposable
workspace. The Android Control Tower app displayed each request; the operator
reviewed the exact command arguments and made the decision on the phone. The
deny call remained blocked and did not create its marker file. The approve
call resumed the same Antigravity conversation, exited successfully, and
created its requested marker file.

## Evidence

- Device: Pixel 10 Pro Fold, serial `61191FDCG000KZ`, app package
  `com.aixion.controltower.debug`.
- Authenticated account: local demo owner `smoke-owner@example.com`.
- API: loopback port `8001`, isolated SQLite database
  `/tmp/aixion-control-tower-phone-demo-20261020.sqlite3`; ADB reverse mapped
  device port `8001` to Mac port `8001`.
- Native provider: Antigravity `2.24.0`.
- Disposable workspace: `/private/tmp/aixion-control-tower-deny-20261020`.
- Agent: `agent_792a891bfb12`, named `DenyTest`, scoped to project
  `project_705dd1c12443` and only `CREATE_APPROVAL`,
  `READ_APPROVAL_DECISION`, and `EXPIRE_OWN_APPROVAL`.
- Approval: `approval_6a4ef8886ed5`; the durable local record is `DENIED` and
  names the same agent, project, and Antigravity conversation ID
  `68981939-84f7-4089-8457-ca913160f2c9`.
- Displayed command: `printf "DENIED" > aixion-approval-denied.txt`.
- Antigravity result: “The execution of the shell command was denied by the
  security hook (Control Tower decision: DENIED). Consequently, the file
  aixion-approval-denied.txt could not be created via shell command.”
- Filesystem check: `aixion-approval-denied.txt` was absent.
- Fresh approval: `approval_b0193a8ee142` was approved on the phone. It names
  source agent `agent_13212cc059d3`, project `project_705dd1c12443`, and
  Antigravity conversation `c0051558-4abe-4e4c-864c-ebcec44585e3`.
- The phone's full-diff review displayed the exact `run_command` arguments:
  `printf "APPROVED" > approval-approved.txt`. Antigravity reported command
  exit code 0 and then replied that the file was created.
- Filesystem check: `approval-approved.txt` contained exactly `APPROVED`.
- Durable local record: approval `approval_b0193a8ee142` is `APPROVED` and
  retains the same source agent and conversation ID as the native call.

The request appeared below several older resolved cards in the inbox during
this run. The inbox now sorts pending approvals first, followed by blocked and
unsupported entries, then resolved items. A focused ordering regression test
covers this behavior.

## Cleanup

- The denial-test agent `agent_792a891bfb12` and the two temporary agents
  `agent_9a6449afdfd0` and `agent_13212cc059d3` were revoked from the Android
  Agent Access screen. Their API records have no secret hashes and their
  credentials have revocation timestamps.
- The hook test
  read its one-time token from an owner-only file; that file was deleted after
  the run. No test token remains in macOS Keychain.
- During local setup, temporary demo token text appeared in diagnostic output.
  The affected agents were revoked immediately; their durable secret hashes
  are absent and their credentials have revocation timestamps. No usable test
  credential remains.
- The workspace hook and non-secret config were removed with
  `setup_workspace.py --remove`. The disposable workspace and successful
  `approval-approved.txt` marker remain available because Antigravity has the
  project open. The denial marker is absent, as expected.
- The isolated API on port `8001`, database, signed-in demo session, and ADB
  reverse mapping remain available for inspection. The separate API on port
  `8000` was not changed.
- The updated debug APK was installed on the Pixel with
  `./gradlew clean installDebug
  -PAIXION_API_BASE_URL=http://127.0.0.1:8001/`. The clean build removed a
  stale generated resource with a space in its filename. The package update
  cleared the prior mobile session; the operator signed back in with the
  local demo owner account. The app now shows four bottom tabs: Home,
  Approvals, Agent Work, and Account.

## Limits and remaining production gates

- This used loopback HTTP and a local SQLite database, not hosted HTTPS or
  PostgreSQL. It does not establish multi-process decision safety, backup and
  restore readiness, or production tenant isolation.
- Push notifications were not tested. The app used authenticated foreground
  API refresh; phone notification alerts were off.
- This was a local end-to-end demonstration, not production certification.
  Verify timeout, network loss, changed payload, revoked credentials during a
  wait, backend restart, and Mac sleep/wake all fail closed before beta.
- Public release still requires PostgreSQL repositories and transactional
  outbox, production HTTPS deployment, real-device FCM validation, recovery
  drills, security review, observability, and a closed-beta soak.

## Validation and run notes

- Both live decisions were verified from the Pixel UI and isolated approval
  records. The denial was also corroborated by Antigravity's denial response
  and absent marker; approval was corroborated by Antigravity's successful
  tool result and the exact marker content.
- The inbox-ordering regression test was added in
  `ApprovalSelectionTest.kt`; `ANDROID_HOME=/Users/madhuram/Library/Android/sdk
  ANDROID_SDK_ROOT=/Users/madhuram/Library/Android/sdk ./gradlew
  testDebugUnitTest --tests
  com.aixion.controltower.feature.approvals.ApprovalSelectionTest` completed
  successfully (4 tests, 0 failures). Environment overrides were command-local;
  no SDK path was written into project configuration.
- Local-only connectivity used the Gradle property
  `-PAIXION_API_BASE_URL=http://127.0.0.1:8001/` and
  `adb reverse tcp:8001 tcp:8001`. Do not use these settings for a remote or
  production deployment.
- No production config keys or database migrations were added.
