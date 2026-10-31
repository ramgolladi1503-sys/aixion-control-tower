# Aixion Control Tower Production Readiness Plan

## Product scope

The first release is an Android approval console for a Mac-hosted Antigravity
session. A synchronous Mac hook pauses one exact tool invocation, creates an
approval request, and waits. The Android app displays the request and its risk
context. The authenticated API records the operator decision. The Mac hook
resumes the invocation only after it reads an approved decision for the same
request and verifies the frozen payload. Denial, expiry, invalid state, network
failure, or credential revocation must stop the invocation.

The first release does not claim that push delivery is guaranteed, that the
phone can approve from a lock-screen action, or that all agent providers are
supported. Add Codex and other providers only through separate adapters after
each provider passes this contract.

## V1 account and ownership model

V1 uses **one workspace per account**. The first account creator is the
workspace owner; invited maintainers and reviewers join that same workspace.
Users belong to exactly one workspace in v1. A future organization product may
allow multiple workspaces per person, but that is a separate schema/API change.

Every project belongs to exactly one workspace. Approvals, work orders, agent
tasks, MCP requests, connectors, devices, notifications, and audit records are
scoped to that workspace directly or through a required parent project/resource
relationship. External agents and connectors may act only on project IDs in
their explicit workspace scope. Role checks alone are not tenant authorization.
Missing workspace ownership or a missing parent relationship denies access.

## Production decision flow

```text
Antigravity hook on Mac
  -> HTTPS approval API
  -> durable approval state and audit event
  -> notification outbox -> push wake-up
  -> authenticated Android fetch and human review
  -> one-time server-side decision
  -> Mac hook fetches and verifies the exact decision
  -> resume only the matching invocation, otherwise deny
```

Push is a best-effort wake-up signal. It is never the decision authority. The
app must also show pending work when push is delayed or disabled. Notification
content must not expose tool arguments or diffs on the lock screen by default.

## Staged delivery and gates

### Stage 0 — Freeze scope and contracts

- Keep the first release to one Android app, one Mac integration, and
  Antigravity.
- Specify approval states, payload hashing, expiry, one-time consumption,
  duplicate requests, simultaneous decisions, user/project boundaries, and
  failure behavior.
- Record supported Antigravity versions and the exact hook decision vocabulary.

**Exit gate:** reviewed threat model and API/state contract; no ambiguous state
can be interpreted as approval.

### Stage 1 — Secure the Android release boundary

- Encrypt access tokens with an Android Keystore-backed key.
- Migrate tokens from the old plaintext preference format before returning
  them to API callers; discard corrupt or undecryptable sessions.
- Disable app data backup until an explicit backup design is approved.
- Allow cleartext HTTP only in debug builds. Require a configured HTTPS API URL
  for release builds.

**Exit gate:** instrumented token storage/migration/logout tests pass; release
configuration fails closed without HTTPS; debug build still supports local
emulator/USB development.

### Stage 2 — Durable multi-user backend

- Replace the current process-memory maps and single-writer SQLite persistence
  with transactional PostgreSQL before running multiple API instances or
  workers.
- Introduce explicit tenant ownership and enforce it for every project,
  approval, agent credential, device, and audit query.
- Make approval decision, one-time consumption, and audit insertion atomic.
- Add versioned migrations, point-in-time recovery, encrypted backups, restore
  drills, retention rules, and deletion/export flows.
- Keep local/test SQLite separate from the hosted production database.

**Exit gate:** multi-process concurrency tests, cross-tenant authorization tests,
migration rollback/recovery tests, and a successful documented restore drill.

#### Implementation order

1. **Map the current persistence contract.** Inventory every `store.<map>`
   read/write, every `persist()` call, and every caller that relies on mutable
   Pydantic objects. Group the current entities by ownership and lifecycle.
   Do not treat the existing generic `kv_store` JSON rows as a normalized schema
   or as a tenant boundary.
2. **Implement the v1 ownership model.** Add a workspace/account record;
   associate every user with exactly one workspace; associate projects with a
   workspace; and scope invitations, devices, agent credentials, and resource
   queries to that same workspace. Roles (OWNER, MAINTAINER, REVIEWER) are
   interpreted within the workspace. Add foreign keys and uniqueness
   constraints. Missing or ambiguous ownership must deny access.
3. **Add a repository boundary.** Introduce typed persistence operations for
   approvals, audit events, devices, credentials, and notifications. Routes and
   services must stop mutating shared dictionaries directly. Keep SQLite as a
   local/test adapter behind the same operations; do not silently switch local
   developers to a remote database.
4. **Implement PostgreSQL schema and transaction semantics.** Approval decision
   must conditionally update a still-pending row, write the immutable audit
   event, write any linked AgentTask state, and enqueue the push outbox record
   in one database transaction. A losing concurrent decision gets a conflict;
   transaction failure exposes no approval state to the Mac poller. Outbox
   delivery is acknowledged separately and cannot roll back a decision. The
   The SQLite repository now commits direct human and Antigravity approval
   creation, each scoped audit event, and its primary approval notification
   together; those API paths update their in-memory views only after that
   commit. AgentTask approval creation now commits the approval, task link,
   task timeline event, both scoped audit records, and task notification in one
   repository transaction for both adapters. PostgreSQL migration
   `pg_0002_agent_task_events` adds the scoped task timeline table and supports
   approval-linked and task-linked notification outbox records. These repository
   operations remain a slice, not the full application store. The SQLite
   decision adapter also commits the decision, audit event, decision-derived linked
   AgentTask status, task timeline event, task audit record, and approval/task
   notification outbox records in one writer transaction. PostgreSQL uses the
   same decision transaction boundary. Approved-run creation and run audit
   remain follow-up operations with startup reconciliation; they are not part
   of the decision transaction. Run creation now reads the durable approval,
   checks its status and task/project/workspace binding, and recomputes the
   frozen payload hash before writing run rows. Reviewer attestations and
   capability lease issuance use the durable approval and reject stale or
   mismatched frozen payloads before authorizing a lease. The Postgres
   repository has a matching tested approval-creation transaction.
   Authenticated approval inbox/detail reads and
   Antigravity host polling now read through the repository and refresh their
   compatibility cache from durable rows before applying tenant checks. Both
   adapters validate the durable approval-to-project/workspace relationship;
   missing or mismatched parent scope is excluded or rejected. The
   application-wide repository binding still selects SQLite. AgentTask, MCP,
   project, and other route paths still rely on the broader SQLite-backed store;
   manual, Antigravity, and AgentTask approval paths still expose compatibility
   views from that store after repository transactions. The app is not ready for
   a PostgreSQL runtime switch.
   Startup reconciliation repairs missing decision timeline events without
   re-enqueueing task notifications. It creates the first approved-task run
   only when no run record exists; a terminal failed/cancelled run is not
   automatically restarted, and retry requires an explicit operator action.
   Run rows, run-specific follow-up notifications, push delivery outcomes, and
   their audit records are not part of the approval decision transaction. This is not a production
   substitute for the PostgreSQL transaction above. The SQLite
   decision repository validates transitions against the canonical lifecycle
   table before opening its write transaction, so adapter callers cannot bypass
   route-level transition checks. It also requires the durable `approval.decision`
   audit event's previous/new status fields to match the expected and committed
   states, respectively. The caller's complete expected approval snapshot must
   match the durable row before the update; otherwise the repository returns a
   conflict instead of overwriting concurrently changed approval content.
5. **Migrate in a recoverable, read-only-first sequence.** Back up and hash the
   source SQLite database; validate and export records with counts and schema
   versions; import into an isolated PostgreSQL staging database; compare counts,
   ownership relations, hashes, and audit-chain validity; then exercise API
   reads and approval decisions against staging. Keep the source database
   untouched through sign-off. Production cutover needs a write freeze, final
   delta, explicit operator sign-off, and a tested rollback path.
6. **Prove process and tenant isolation.** Run at least two independent API
   processes against the same PostgreSQL instance. Race approve/deny/expire and
   duplicate requests; verify exactly one terminal decision and corresponding
   audit event. Attempt cross-tenant reads and decisions for every resource
   class, not only approvals.
7. **Remove the production startup block last.** Only after migration, restore,
   authorization, and multi-process gates pass should production startup route
   application reads, writes, and workers to PostgreSQL and remove the explicit
   store-integration block. Continue requiring `AIXION_POSTGRES_DSN` and reject
   SQLite paths in production. Never log DSNs or credentials. Compose,
   deployment secrets, health checks, backups, and restore drills must target
   the same PostgreSQL deployment.

#### Current persistence-debt baseline

The current read-only AST inventory (2026-10-20) observes 347 direct map reads,
152 direct map writes, 118 whole-store `persist()` calls, and 30 direct SQLite
boundary accesses across 56 application modules. The CI ceilings now match
these measured counts, so any added legacy access must be offset by a removal.
The inventory is static and does not prove all dynamic access has been found;
the ceiling is a guardrail, not evidence of PostgreSQL readiness. The largest
concentrated read surface is `workspace_access.py` with 70 map reads. The next
repository migration should move its workspace-scope resolution behind typed,
durable lookups for the relevant parent resources and fail closed when any
parent or ownership binding is missing or inconsistent. Do not enable the
PostgreSQL application runtime until this and the remaining route/worker paths
no longer rely on SQLite maps.

The deadline-expiry sweep now lists approvals from the approval repository
instead of iterating the process-local approval map. A regression test removes
an approval from the compatibility cache while retaining its durable row and
proves the sweep still expires it. This removes one authoritative cache read;
other approval creation, workspace-access, trust, recovery, and task paths
still use the SQLite-backed store and prevent a PostgreSQL approval cutover.
Recovery export now reads every included SQLite collection and migration
metadata from one read transaction. Workspace filtering resolves parent scope
from that same snapshot; AgentTask rows are read only to resolve legacy audit
scope and remain excluded from the exported payload. This closes the mixed
cache/repository snapshot for the SQLite runtime. PostgreSQL does not yet
provide this recovery snapshot contract, and this remains a single-database,
single-runtime backup rather than a multi-process cutover proof.
Trust workspace checks for capability leases and reviewer attestations now
resolve their referenced approval through the repository, so a missing cache
entry cannot make those checks ignore the durable approval. The inventory
count decreased by six approval-related direct map reads across those changes.
Run capability issuance and side-effect trust validation now also resolve the
approval repository-first; regression tests remove the compatibility cache
entry before these checks. The inventory has decreased by ten direct approval
map reads in total. The broader run/task/approval lifecycle still has SQLite
map dependencies and is not multi-process safe.

Test-run creation now verifies its parent approval through the same scoped
repository lookup used by approval detail, rather than treating cache presence
as proof of existence. The inventory is down by one additional direct read.

#### Storage implementation acceptance criteria

- All production reads and writes use PostgreSQL-backed repositories; no
  process-local dictionary is authoritative for authorization or approval state.
- All state transitions use conditional updates or row locks and explicit
  transactions. Audit insertion is in the same transaction as the action it
  attests.
- Tenant ownership is enforced at the repository boundary and checked by
  negative tests for each resource category.
- Connection pool limits, statement timeouts, transaction timeouts, migration
  locking, retryable serialization failures, and health probes are explicit
  configuration with bounded values.
- Backups are encrypted, retention is documented, and restore is tested into a
  clean environment. A successful backup job alone does not pass recovery.
- A migration report records source identity, snapshot hash, exported/imported
  counts, rejected records, verification results, and operator sign-off without
  copying secrets into logs.

#### Migration and rollout notes

- Production configuration requires a secret-managed PostgreSQL DSN in
  `AIXION_POSTGRES_DSN` with `sslmode=verify-full`; it must never be returned by
  readiness APIs or included in exception text.
- `AIXION_DB_PATH` remains a local/test SQLite setting. Do not reinterpret an
  existing SQLite path as a PostgreSQL URL.
- Push queue settings are opt-in and bounded:
  `AIXION_PUSH_DISPATCH_ENABLED` (default `false`),
  `AIXION_PUSH_DISPATCH_POLL_SECONDS` (1–60, default 5),
  `AIXION_PUSH_MAX_ATTEMPTS` (1–10, default 5),
  `AIXION_PUSH_RETRY_BASE_SECONDS` (1–300, default 15),
  `AIXION_PUSH_RETRY_MAX_SECONDS` (1–3600, default 300 and at least the base),
  and `AIXION_PUSH_LEASE_SECONDS` (30–600, default 60).
- Existing SQLite data is not auto-migrated at startup. The operator runs an
  explicit export/import tool against a verified backup, reviews its report,
  then schedules a write freeze and cutover.
- Before designing the importer, create a quiesced, independently backed-up
  source copy and run the read-only inventory against that copy:
  `cd backend && PYTHONPATH=. python scripts/audit_legacy_sqlite.py --database /absolute/path/to/source-copy.sqlite3 --output /absolute/path/to/legacy-audit.json`.
  The command requires an explicit existing database path, opens it with
  SQLite `mode=ro`, reports schema/integrity checks, entity counts, known-model
  validation failures, ownership records that need operator mapping, and
  mismatches between an entity's workspace and its linked parent. Its
  deterministic `record_findings` list contains record type/ID, explicit and
  inferred workspace IDs, and finding codes; it never copies record payloads or
  credentials. The report always sets `migration_authorized: false`; even
  `STRUCTURAL_REVIEW_PASSED` means only that this read-only check found no
  listed structural issues. `snapshot_certified` is always false; the report
  also states whether a `-wal` sidecar is present. It is not permission to
  import or cut over and does not prove the main database file and WAL were
  captured as one recoverable snapshot. Use
  this as an operator review ledger and construct any mapping
  separately with approval and provenance. A nonzero
  exit means review is required. It does not export, redact, repair,
  assign ownership, hash WAL sidecars, or certify a recoverable snapshot; the
  report is a planning input only. Never run it against a live writer as a
  substitute for a coordinated backup.
- Roll back by directing traffic to the preserved pre-cutover service/database
  only if no production writes have occurred on PostgreSQL, or through a
  separately designed reverse migration. Never dual-write silently.

#### Approval-slice staging importer (not a cutover tool)

`backend/scripts/import_approval_slice_to_postgres.py` currently supports
workspaces, one-workspace-per-user accounts, sessions, projects, ideas, work
orders, external agents, agent credential metadata, registered devices,
approvals, linked AgentTasks, approval/project/idea/work-order and supported
agent lifecycle audit events, and approval notifications. It rejects missing
or mismatched workspace owners, agent creators, project grants, devices, or
task/approval/work-order principals. It omits trust/MCP/connector records,
unrelated audit events, and other unsupported entities. The report lists
omissions; this is not a complete account migration and cannot be used to serve
production traffic. User password hashes, session token hashes, agent secret
hashes, and device push tokens are sensitive: snapshot and staging database
access must remain restricted, and the importer never prints their payloads.
For an Idea or WorkOrder whose legacy payload lacks `workspace_id`, the importer
derives scope only through its exact linked Project and reports those counts in
`derived_workspace_scope_counts`; missing or mismatched parents block import.

Prepare an independently coordinated, quiesced SQLite snapshot outside the
repository, owned by the operator with mode `0600`, with no `-wal` or `-shm`
sidecars. The tool re-runs the read-only structural audit and then prints a
dry-run plan without writing to either database:

```bash
cd backend
chmod 600 /secure/path/control-tower-snapshot.sqlite3
PYTHONPATH=. python scripts/import_approval_slice_to_postgres.py \
  --source /secure/path/control-tower-snapshot.sqlite3
```

For a disposable staging database whose name ends in `_test` or `_staging`,
set `AIXION_TEST_POSTGRES_DSN` through a secret-safe environment mechanism and
explicitly acknowledge the operator-created snapshot and omitted scope:

```bash
PYTHONPATH=. python scripts/import_approval_slice_to_postgres.py \
  --source /secure/path/control-tower-snapshot.sqlite3 \
  --apply --confirm-source-snapshot --allow-partial-slice
```

Remote staging also requires `--allow-remote-staging`. The target must have no
rows in the imported slice unless it contains the exact existing immutable
source-hash manifest, in which case the operation is idempotent. Inserts and
the manifest share one transaction. The CLI always reports
`migration_authorized: false` and `snapshot_certified: false`; operator review,
record-level comparison, full-entity import, production recovery tests, and a
separate cutover plan remain mandatory. Do not point this importer at a
production database.

New staging configuration: `AIXION_TEST_POSTGRES_DSN` (test/staging-only; never
put the DSN in command-line arguments or logs). Existing SQLite databases are
not altered. The importer requires PostgreSQL schema support for an immutable
`sqlite_import_runs` manifest; since this migration is not yet deployed, edits
to the unreleased `pg_0001_approval_transaction` schema are part of initial
staging setup. Any environment that already applied an earlier draft of that
migration needs a reviewed forward migration before using this importer.

#### Persistence inventory baseline

The read-only AST inventory command is:

```bash
cd backend
PYTHONPATH=. python scripts/inventory_persistence.py > /secure/path/persistence-inventory.json
```

On 2026-10-18, the inventory found persistence access in 55 application
modules: 366 map reads, 120 direct map/list writes, 133 `store.persist()` calls,
and 19 direct `_connect()`/`atomic()` boundary accesses (638 access events
total). On 2026-10-20, it found 56 modules: 360 reads, 157 writes, 121
`store.persist()` calls, and 28 direct boundary accesses. The report identifies
access locations by file and line, plus per-module counts. Keep generated
reports in the restricted migration evidence area because source paths and
persistence layout are operational metadata. The tool does not read database
contents or write any source files.

This is a static inventory, not a complete mutation proof. It can miss dynamic
attribute access, indirect aliases, raw SQL, callbacks, and mutation of model
objects retrieved from a map. Review those patterns in source before assigning
repository ownership. The direct SQLite connection findings mean the migration
must include worker claims, approval expiry/recovery, authentication, and trust
recorder code, not only HTTP routes.

Worker claims now use typed AgentTask repository operations. The SQLite adapter
re-reads the linked approval and project inside its writer transaction, refuses
ineligible claims, and writes both successful-claim and specific-task refusal
audits atomically with lease changes. A
PostgreSQL adapter operation also locks the task, validates its durable approval
and project ownership, and commits the lease with a compare-and-set update. The
API binding still selects SQLite. The claim concurrency test and full backend
suite passed against a disposable local PostgreSQL test cluster on 2026-10-20
(`850 passed`, 6 warnings). This does not establish production PostgreSQL
runtime or multi-process readiness. Legacy unowned tasks remain blocked until
an operator-reviewed account/workspace mapping is applied.

The initial model lacked workspace ownership and approval filtering. The
current branch now adds the first additive ownership slice, but isolation is
not complete: recovery export, device/session administration, operational
metrics, worker queues, and several background services still need a complete
route-by-route review. Multi-user deployment must remain blocked until every
resource and worker has a tested ownership path.

For existing SQLite records, migration must use an explicit account-to-workspace
mapping reviewed by an operator. Do not automatically merge users or assume
that all existing projects belong to the first owner. Records whose ownership
cannot be established are quarantined from production import until resolved.

This breadth is why the PostgreSQL migration is a staged repository refactor,
not a connection-string swap. Establish tenant, user, session, and project
ownership first; then migrate approvals with their audit and outbox transaction
as the first safety-critical vertical slice. Migrate agent credentials/tasks,
workers, connectors, MCP, and trust domains afterward. Keep each feature out of
production until its persistence, authorization, and recovery tests pass.

### Stage 3 — Production notifications

- Keep lock-screen content generic; fetch task and approval detail only through
  the authenticated app session. Do not persist push-provider response bodies.
- **Implemented:** replace the legacy FCM sender with FCM HTTP v1, OAuth access
  tokens from Google Application Default Credentials, and an explicit
  `AIXION_FCM_PROJECT_ID`. The old `FCM_SERVER_KEY` is no longer read.
- **Implemented:** approval/task state and its notification record are
  persisted before FCM is contacted, so a delivered push cannot reference a
  record that failed its initial SQLite commit.
- **Implemented for the single-process SQLite profile:** the API lifecycle
  starts an opt-in outbox dispatcher. It persists claims and attempt counts,
  retries transient transport/provider failures with capped exponential
  backoff, recovers expired send leases, and stops retrying permanent FCM 4xx
  failures. If at least one device accepts a send, the notification is recorded
  as `SENT` and the full fan-out is not retried; partial failures are recorded
  as `PARTIAL_DELIVERY` to avoid duplicating pushes on devices already reached.
  An FCM `UNREGISTERED` response removes only the still-matching device token
  and emits an audit event. Duplicate delivery remains possible after a crash
  between provider acceptance and outcome persistence; notification IDs are
  the deduplication key. Delivery-status persistence failures do not reverse
  the committed approval and leave a recoverable `SENDING` lease.
- SQLite database and existing journal sidecar files are created or corrected
  to owner-only mode (`0600`) on connection. Non-regular files, files owned by
  another user, and non-sticky directories writable by other users are refused.
  This local data-at-rest hardening does not make SQLite suitable for multiple
  API processes or production deployment.
- **Not production-ready:** this queue still uses the process-local SQLite
  store and is not safe across API processes. Move enqueue/claim/ack into the
  PostgreSQL transaction/outbox before enabling production dispatch.
- For a single-process local run, explicitly set
  `AIXION_PUSH_DISPATCH_ENABLED=true`, configure `AIXION_FCM_PROJECT_ID`, and
  provide Google ADC. The dispatcher starts with the API and stops on shutdown.
  Do not run multiple API workers with this SQLite queue.
- Send a minimal notification containing an opaque entity reference; fetch
  sensitive details only after app authentication.
- **Implemented as a foreground fallback:** Home and Approvals refresh their
  backend-backed approval data while their screen is started, then stop polling
  when the screen leaves the foreground. `AIXION_APPROVAL_REFRESH_INTERVAL_SECONDS`
  defaults to 15 seconds and accepts values from 5 through 120. This keeps the
  visible console current when push is unavailable; transient failures use
  bounded exponential backoff up to five minutes and a successful refresh resets
  the interval. It does not wake a locked or backgrounded phone and is not a
  substitute for FCM. No data migration is required.
- **Implemented, not operationally verified:** Android Account requests
  notification permission only after an explicit tap. Once permission is
  available, authenticated session restore/login and opening/resuming Account
  fetch the current FCM token and post it to the authenticated,
  workspace-scoped `POST /notifications/devices` endpoint. The UI distinguishes
  successful registration, missing Firebase configuration, permission denial,
  and backend failure; it does not log or show the token. Logout attempts the
  owner-scoped device DELETE before clearing the session. Failed cleanup IDs are
  retained and retried only under the same account. A successful registration
  rebinds an installation token to the currently authenticated account, removes
  duplicate token rows, and records an audit event. The registration API accepts
  only the currently supported Android platform and bounds token/version input;
  the opaque FCM token is not parsed or logged.
- **Still missing:** this Android project has no Firebase app configuration,
  and the phone's notification permission is currently denied. Token
  acquisition, registration, logout cleanup on the device, real push delivery,
  and notification deep-link behavior therefore remain unverified. Failed
  server cleanup while offline can leave a generic push registered until the
  same account reconnects or that installation token is registered by another
  account. Supply Firebase Android configuration through the protected
  build/release process and pass the real-device matrix before enabling
  production push.
- Handle permission denial, stale device tokens, duplicate delivery, delayed
  push, and notification deep links.
- Never let push delivery or notification actions decide whether an agent call
  runs.

**Exit gate:** real-device validation passes for foreground, background, locked,
Doze, notification permission denied, offline/reconnect, and stale-token cases.

### Stage 4 — Mac integration and recovery

- Provide guided, workspace-scoped setup for the Antigravity hook.
- Keep agent tokens in macOS Keychain; scope each token to one owner/project and
  only the required create/read-decision/expire-own-approval permissions.
- Set `AIXION_AGENT_APPROVAL_TTL_SECONDS` (default 600 seconds, allowed range
  30–900) as the backend deadline. The hook expires its own pending request at
  its wait timeout; agent polling and authenticated inbox reads backfill old
  agent approvals and mark due requests expired. Mobile decisions cannot revive
  expired requests.
- **Implemented and focused-tested:** the Mac hook accepts an approval only if
  its response is received before the host wait deadline. A late `APPROVED`
  response, or an `APPROVED` result racing the expiry call, denies the exact
  tool invocation. This is separate from the backend request TTL and protects
  against a slow or delayed poll response extending the host's wait window.
- Add health state, reconnect/backoff, safe update, disable/uninstall, and
  credential revocation instructions.
- Do not expose an inbound Mac port; use outbound HTTPS polling or another
  authenticated outbound channel.
- Test Mac sleep/wake, Antigravity restart, backend restart, duplicate hook
  events, user logout, and revoked tokens.

**Exit gate:** a fresh Mac can set up a disposable workspace, and every failure
case denies without executing the tool call.

### Stage 5 — Closed beta and public release

- Deploy an isolated staging environment with production auth, HTTPS,
  PostgreSQL, trust enforcement, secrets management, monitoring, alerting, and
  tested backups.
- Run an invite-only beta with an incident owner, support channel, privacy
  policy, data retention policy, vulnerability reporting, and rollback path.
- Complete Android signing, release versioning, Play listing, data disclosures,
  and account deletion/export behavior before public distribution.
- Add Codex and other provider adapters one at a time after the same safety
  contract passes.

**Exit gate:** a 30-day soak with no lost approval/audit records, a successful
restore and incident drill, no open critical/high security findings, and the
full provider/device failure matrix passing.

## Release-blocking verification matrix

For every supported provider/version and disposable workspace, verify:

- Approve resumes only the exact invocation once and records the actor,
  approval ID, payload hash, timestamp, risk, and decision.
- Deny, timeout, malformed response, changed arguments, revoked credential,
  backend outage, and expired request never run the invocation.
- Duplicate delivery/taps and simultaneous approve/deny attempts cannot create
  multiple terminal decisions or executions.
- Cross-user and cross-project reads/decisions fail closed.
- API restart during a pending approval preserves its state and audit history.
- The phone can recover the request from the authenticated inbox without push.
- Each notification deep link opens the matching request and requires the same
  in-app review as the inbox.

## Operational measures

Instrument request creation, pending age, decision persistence, hook resume or
deny reason, push attempt/result, API error rate, database health, migration
status, audit write failures, and recovery status. Establish numeric service
targets from staging measurements before beta. Do not describe an FCM send
acceptance as proof that a person saw the notification.

## Current state

- **Verified against the current checkout on 2026-10-18:** a temporary API
  server on loopback with an isolated SQLite database completed two actual
  Antigravity hook subprocess runs. For the approve case, the hook created a
  `PENDING_REVIEW` request containing the exact serialized `run_command`
  arguments; after the API recorded `APPROVED`, the hook returned
  `auto_approve` only after the approved-payload hash matched. For the deny
  case, the API recorded `DENIED` and the hook returned `deny`. The temporary
  server and database were stopped/removed, and the pre-existing API on port
  8000 was left untouched. This used the test profile with authentication
  disabled and did not involve Android; it proves local hook/API behavior,
  not authenticated phone approval or production operation.
- **Contract correction verified on 2026-10-20:** the installed Antigravity
  2.24.0 bundled PreToolUse documentation lists `allow`, `deny`, `ask`, and
  `force_ask`. The earlier `auto_approve` response was unsupported by this
  installed contract. The hook now returns `allow` only after the phone-backed
  approval identity, frozen snapshot, and payload hash verify. The earlier
  direct hook/API and phone tests did not prove native resume; repeat native
  approve and deny scenarios before claiming that behavior is certified.
- **Verified end to end on 2026-10-18:** Antigravity desktop session
  `3f9000aa-3160-42b9-bce1-05b44a04b455` invoked the workspace-scoped
  `run_command` hook. Control Tower created approval
  `approval_3cb96c29dc52` with `CRITICAL` risk and the same source session ID.
  The operator approved it from the authenticated Android app on the physical
  Pixel. The record is `APPROVED`, names the approving user, and contains an
  approved payload hash. Antigravity then reported that the command executed
  successfully. The expected `phone-approved-roundtrip.txt` appeared in the
  disposable workspace with the exact expected content; the prior
  `denied-marker.txt` was absent. This verifies one live phone-to-backend-to-hook
  approval and same-invocation resume. It is a controlled local test, not a
  production certification.
- **Still unverified end to end:** a fresh physical-device denial on this exact
  live workspace hook, plus timeout, malformed/changed payload, credential
  revocation, backend outage/restart, sleep/wake, and reconnect through the real
  phone-to-hook flow.
- **Verified in focused tests (2026-10-19):** the hook denies an approval that
  arrives after its configured deadline and denies when expiry races with an
  approval. The Antigravity hook and backend wait test files pass together; this
  does not replace real-device failure-case certification.
- **Verified against SQLite in backend tests:** a repository transaction
  updates approval state only when its durable status matches the expected
  status and inserts the audit event in the same transaction. An audit insert
  failure rolls back the approval update, and two independent SQLite
  connections racing approve/deny produce one commit and one conflict. Route
  tests additionally cover linked-task status commit/rollback, notification
  enqueue rollback, ambiguous task links, missing denied-event recovery without
  duplicate notification, terminal-run non-restart, phone decisions racing each
  other, agent timeout,
  and the inbox deadline sweeper, plus agent polling around commit failure.
  A route-level test confirms a losing request reloads the competing durable
  decision before returning HTTP 409, avoiding a stale in-memory pending state.
  This does not establish PostgreSQL behavior or full multi-process API safety:
  most application reads still use process-local maps, and task events, run
  scheduling, task-specific notification/outbox writes, and push delivery
  outcomes remain outside the decision transaction. Production remains blocked
  on PostgreSQL repositories and end-to-end multi-process integration tests.
- **Test-only setup used for that verification:** the Android app reached the
  loopback API over USB reverse (`127.0.0.1:8000`); the hook ran from this
  checkout with a short-lived project-scoped agent credential. The hook
  configuration was placed only in the disposable workspace. This does not
  establish remote connectivity when the phone leaves the USB/local network;
  production setup must use HTTPS and macOS Keychain.
- **Implemented and tested:** agent approvals receive an expiry deadline; the
  owning agent can expire only its own pending request; overdue requests are
  marked expired on agent polling or authenticated inbox reads; late decisions
  are rejected. Expiration now uses a durable compare-and-set and commits its
  audit event with the EXPIRED state. Timeout handlers reload durable approval
  state, so a stale local pending record cannot overwrite a committed phone
  decision. Legacy pending agent approvals without an `expires_at` field derive
  their deadline from `created_at` and the configured TTL; the deadline is
  persisted with the expiration transition. PostgreSQL will need an explicit
  versioned migration and equivalent transaction before any production cutover.
- **Implemented locally:** external-agent authentication telemetry writes only
  credential telemetry fields and append-only auth audit records. It no longer
  rewrites the full process-local entity store on every hook request, which
  could overwrite an approval committed concurrently. Agent rate-limit state
  and other application maps are still not safe shared multi-process state; this
  does not remove the PostgreSQL production gate.
- **Verified on the physical Pixel:** the approval inbox and detail screen
  instrumentation package passes all six tests, including refresh and closed
  decision controls. The full Android instrumentation suite is not green yet;
  two connector tests still assert stale UI copy.
- **Verified:** four primary Android navigation destinations.
- **Implemented but not production-certified:** approval, agent, audit,
  mission-control worker, deployment scaffolding, and FCM foundations.
- **Verified on the physical Pixel (2026-10-19):** five approval inbox
  instrumentation tests pass, including polling while the screen lifecycle is
  `STARTED` and stopping polling after it returns to `CREATED`. The same shared
  foreground refresh effect is attached to Home and Approvals. This validates
  foreground refresh only, not authenticated inbox data, a real phone decision,
  background wake-up, or same-invocation agent resume.
- **Implemented, partially validated:** Android token-at-rest and release
  transport hardening in Stage 1. Unit tests and release URL guard checks pass;
  the approval UI instrumentation package passes on device, but the broader
  Android instrumentation suite still needs a clean run.
- **Implemented and focused-tested:** push lock-screen text is generic and FCM
  error bodies and credential exception details are excluded from persisted
  push status. FCM HTTP v1 uses ADC OAuth credentials. An opt-in,
  single-process SQLite outbox now retries transient failures and reports
  workspace-scoped queue metrics. The queue is not safe for multi-process
  production, stale-token cleanup is missing, and live FCM delivery is not
  certified. Runtime readiness only checks the configured project ID; it does
  not verify that ADC can send.
- **Implemented, not operationally verified (2026-10-18):** Android requests
  notification permission explicitly, registers its current FCM token with the
  authenticated backend, and attempts owner-scoped device deregistration on
  logout. Failed cleanup is retained for same-account retry; token reassignment
  is audited and scoped to the current account. Clean-copy debug build and
  Android unit tests pass. Firebase app configuration is absent, the physical
  device permission is denied, and push delivery has not passed; do not claim
  push readiness.
- **Implemented, incomplete workspace-isolation foundation:** first-owner
  registration creates one workspace; invited users and projects inherit it;
  the current API/auth layer scopes project, approval, work-order, idea,
  AgentTask, AgentRun, external-agent credentials, connectors, MCP, trust,
  audit/recovery, device/session administration, notifications, and the
  operational metrics surfaces. Cross-workspace denial tests cover principal
  paths and the backend suite passes. This is still not a complete tenant
  guarantee: in-process maps remain authoritative, worker claims now reject
  missing or inconsistent task/approval/project workspace bindings, and some
  direct service paths need a full review, parent-child consistency must be
  enforced in storage, and legacy records still need explicit operator mapping.
  Unassigned or ambiguous records remain inaccessible to workspace users.
- **Production startup is intentionally blocked:** this build's only store is
  single-process SQLite with in-process entity maps. The production profile
  refuses to start until a PostgreSQL store is implemented. The existing
  Compose file still describes SQLite and is not a production deployment
  topology. CI validates its Compose syntax and asserts that the production API
  exits with the explicit PostgreSQL-not-implemented block; that check is not a
  successful production startup test. Local inspection found no Docker or
  PostgreSQL client available in this workspace, so a database-backed
  multi-process implementation cannot yet be exercised here; no PostgreSQL
  container or unused configuration was added.
- **Not ready for public production:** hosted multi-tenant persistence,
  transactional outbox, FCM real-device delivery certification,
  deployment/restore certification,
  security review, provider compatibility matrix, and closed-beta soak.

The current single-writer SQLite store and synchronous FCM transport are
documented starting points, not proof of production scale or notification
reliability.

## Verification update — 2026-10-19

- Android approval parsing now preserves unrecognized backend status and risk
  values as explicit `UNKNOWN` values. An unknown status is routed to history;
  an unknown risk is excluded from actionable counts. Approval detail disables
  Approve, Reject, and Revise when either value is unknown, and the inbox/home
  surface displays an unsupported-data warning. This is a fail-closed client
  compatibility behavior; it does not replace server-side lifecycle or policy
  enforcement.
- Validation on 2026-10-19: `:app:testDebugUnitTest` and `:app:assembleDebug`
  passed. On the connected Pixel 10 Pro Fold, `ApprovalDetailScreenTest` passed
  8/8 and `ApprovalInboxScreenTest` passed 6/6. `git diff --check` passed.
  Instrumentation removed the debug package; the built debug APK was reinstalled
  and opened afterward. Both debug and release packages are present. Backend
  `/health` returned `{"status":"ok","service":"aixion-control-tower"}`.
- The physical approval workflow is **not verified in this run**: the app is
  still at the signed-out/login state, and no authenticated user action or
  approval tap occurred. Do not treat UI instrumentation as evidence of a
  phone-to-host Antigravity decision/resume. That gate requires an authenticated
  phone session and a live, exact-request host hook verification.
- **Production account onboarding remains blocked on verified email delivery:**
  the SMTP sender and encrypted SQLite outbox are implemented below, but no
  real provider has been configured or contacted. Production startup must stay
  blocked until PostgreSQL outbox support, resend/abuse limits, provider failure
  handling, sender-domain authentication, and a real inbox verification test
  pass. The full backend suite passed 642 tests on 2026-10-19 using a dedicated
  temporary SQLite database; this does not cover a live email provider or
  production database.
- **Implemented locally, not production-certified:** verification delivery now
  has an opt-in SMTP STARTTLS worker backed by a dedicated SQLite outbox. The
  verification hash and encrypted email payload commit together in the local
  SQLite adapter; retries use bounded backoff and leases; sent ciphertext is
  cleared; persisted errors contain only a failure category. The dispatcher
  requires `AIXION_EMAIL_OUTBOX_KEY`, SMTP host/from, and optional paired SMTP
  credentials. It is disabled by default and remains a single-process SQLite
  implementation. Production validation requires email dispatch configuration,
  but startup remains blocked on PostgreSQL. No SMTP provider was contacted in
  validation; provider delivery, inbox placement, sender-domain authentication
  (SPF/DKIM/DMARC), abuse controls, multi-process claiming, and the PostgreSQL
  outbox migration remain open.
- Runtime readiness now reports only booleans for email dispatch, SMTP
  configuration, and outbox-key presence; it never returns SMTP credentials or
  the encryption key. SQLite migration `0002_auth_email_outbox` is additive and
  runs through the existing versioned migration runner. It does not define or
  apply a PostgreSQL schema migration.
- **Implemented locally, not production-certified:** public registration,
  login, email verification, and resend routes use atomic fixed-window limits.
  Bucket identifiers are HMAC’d with a dedicated 32-byte key; the SQLite table
  stores only the digest, window start, and count. Responses return HTTP 429 with
  `Retry-After`; logs record the route bucket without IP/email values. SQLite
  migrations `0003_auth_rate_limit_buckets` and
  `0004_auth_rate_limit_expiry_index` are additive; the latter indexes the
  expiry cleanup path. This does not establish
  cross-process/PostgreSQL enforcement, proxy identity correctness, bot defense,
  or abuse operations. Production startup also requires
  `AIXION_AUTH_RATE_LIMIT_HMAC_KEY`.

### Email verification local validation and rollout

- New settings: `AIXION_EMAIL_DISPATCH_ENABLED` (default `false`),
  `AIXION_EMAIL_DISPATCH_POLL_SECONDS` (1–60, default 5),
  `AIXION_EMAIL_MAX_ATTEMPTS` (1–10, default 5),
  `AIXION_EMAIL_RETRY_BASE_SECONDS` (1–300, default 15),
  `AIXION_EMAIL_RETRY_MAX_SECONDS` (1–3600, default 300 and at least the base),
  `AIXION_EMAIL_LEASE_SECONDS` (30–600, default 60),
  `AIXION_EMAIL_SMTP_HOST`, `AIXION_EMAIL_SMTP_PORT` (1–65535, default 587),
  `AIXION_EMAIL_SMTP_TIMEOUT_SECONDS` (1–30, default 10),
  `AIXION_EMAIL_SMTP_USERNAME`, `AIXION_EMAIL_SMTP_PASSWORD`,
  `AIXION_EMAIL_FROM`, and `AIXION_EMAIL_OUTBOX_KEY` (64 hex characters).
- Authentication rate-limit settings: `AIXION_AUTH_RATE_LIMIT_HMAC_KEY` (64
  hex characters, required in production), `AIXION_AUTH_RATE_LIMIT_WINDOW_SECONDS`
  (10–3600, default 60), `AIXION_AUTH_REGISTER_LIMIT_PER_IP` (1–1000, default
  10), `AIXION_AUTH_RESEND_LIMIT_PER_IP` (default 10),
  `AIXION_AUTH_RESEND_LIMIT_PER_EMAIL` (default 1),
  `AIXION_AUTH_LOGIN_LIMIT_PER_IP` (default 30),
  `AIXION_AUTH_VERIFY_LIMIT_PER_IP` (default 30), and
  `AIXION_AUTH_VERIFY_LIMIT_PER_EMAIL` (default 5).
- For isolated local validation only, set a disposable SMTP sink, a fresh
  32-byte key, and `AIXION_EMAIL_DISPATCH_ENABLED=true`. Run
  `cd backend && python -m pytest -q tests/test_email_delivery.py tests/test_email_verification_auth.py tests/test_database_migrations.py tests/test_settings.py tests/test_ops_readiness.py`.
  The test suite injects a fake SMTP sender and does not contact a provider.
- Before staging, verify the sender domain and SPF/DKIM/DMARC, place SMTP
  credentials and the independent outbox key in a secret manager, configure
  retention for recipient metadata, and add per-address/IP registration and
  resend rate limits. Then test accepted, rejected, timeout, retry, process
  restart, and duplicate delivery behavior using the hosted PostgreSQL outbox.
- Do not enable this SQLite dispatcher in hosted or multi-process environments.
  The PostgreSQL migration, transaction, dispatcher leasing, retention, and
  restore/recovery tests are required before production startup can be allowed.

## Verification update — 2026-10-19 (Antigravity invocation binding)

- The Antigravity PreToolUse hook now compares the approval returned by
  `POST /agents/approvals` with the exact tool payload submitted by that hook.
  Every decision poll must then match that original approval snapshot before
  `allow` is returned. The existing server payload hash remains an
  additional integrity check. A changed approval record is denied even if its
  changed content has a self-consistent hash.
- Validation: `backend/tests/test_antigravity_pre_tool_hook.py` and
  `backend/tests/test_antigravity_approval_wait.py` passed together (41 tests);
  Ruff and `git diff --check` passed. The installed Antigravity 2.24.0 bundled
  PreToolUse contract lists `allow`, `deny`, `ask`, and `force_ask` as supported
  decisions.
  These checks validate hook/API behavior, not a fresh phone-to-host run.
- Live observations during this update: loopback `/health` returned
  `{"status":"ok","service":"aixion-control-tower"}` from the existing
  Uvicorn process on port 8000; it was not restarted. ADB reported no connected
  devices, and no scrcpy window was available. No phone login or approval tap
  was performed in this update.
- The read-only persistence inventory found 31 store maps accessed by 58
  backend modules, with 364 static map reads, 132 writes, and 129 full-store
  `persist()` call sites. This AST report is a refactoring aid and may miss
  indirect mutations; it confirms PostgreSQL is a broad migration. No Docker
  or `psql` executable was available for a live PostgreSQL integration run.
- Production remains blocked on PostgreSQL-backed authority and multi-process
  transaction tests, real push delivery/device validation, deployment and
  recovery certification, and authenticated physical-device Antigravity
  verification. The hook hardening does not clear those gates.

## Verification update — 2026-10-20 (device path and recovery integrity)

- The notification dispatcher now marks a fan-out `SENT` when at least one
  device accepted it, avoiding a whole-fan-out retry that would duplicate pushes
  to devices already reached. FCM `UNREGISTERED` results remove only the still-
  matching token and create an audit event. Registration input is bounded and
  currently accepts Android only. Focused notification tests passed (30); Ruff
  and `git diff --check` passed.
- Recovery export now captures entity rows and derives counts under the SQLite
  writer lock. Validation rejects missing collections, count mismatches, and
  duplicate IDs. Snapshots enumerate every collection excluded from this
  partial export; older snapshots without the inventory receive a completeness
  warning. Focused recovery tests passed (9); Ruff and `git diff --check`
  passed. This remains a partial manual-recovery artifact, not a backup or
  restore drill.
- The Pixel `61191FDCG000KZ` is connected. The installed debug app had no active
  session and was compiled for the emulator-only `10.0.2.2` endpoint. A debug
  build targeting `127.0.0.1:8000` was installed with app data preserved, and
  reversible ADB USB forwarding was enabled. The existing API health endpoint
  returned `ok`. No credentials were entered and no approval was triggered.
- The phone-to-API login and approval workflow remains unverified until an
  account is authenticated on the device. Production remains blocked on the
  PostgreSQL migration, real-device FCM delivery, backup/restore drills, and
  the same-invocation Antigravity approval certification.

## Verification update — 2026-10-20 (subsequent authenticated device run)

This update supersedes the preceding same-day note that said the Pixel had no
active session and that the phone-to-API approval flow remained unverified.
After the operator authorized use of the local demo account, the app showed
`SESSION ACTIVE` and `SIGNED IN` as `Smoke Owner` on the physical Pixel. The
separate device validation record
[`DEVICE_APPROVAL_LOOP_VALIDATION_2026-10-20.md`](DEVICE_APPROVAL_LOOP_VALIDATION_2026-10-20.md)
documents a fresh native Antigravity 2.24.0 deny and approve run through the
phone, including the absent denied marker and exact approved marker. The demo
API used loopback port 8001 and isolated SQLite; this is local end-to-end
evidence, not hosted production evidence.

Push remains unverified and disabled: the demo account screen reports alerts
off; this checkout has no Android Firebase configuration, FCM project ID, or
Google Application Default Credentials. No permission or cloud-account
settings were changed during this update.

The current read-only AST inventory was rerun from `backend/`. It found 31
store maps, 58 modules with persistence access, 365 map reads, 134 direct
map/list writes, 130 `store.persist()` call sites, and 27 direct `_connect()`
or `atomic()` boundary accesses. These counts supersede the earlier 364/132/129
figures above for the current checkout. This remains a static inventory and
does not account for every indirect mutation. The environment also has no
Docker, PostgreSQL client/server executable, or installed psycopg driver, so
there is no local PostgreSQL integration target. Production remains blocked on
PostgreSQL-backed authority and multi-process tests, real push delivery,
backup/restore certification, and the other release gates listed above.

## Verification update — 2026-10-20 (PostgreSQL approval transaction slice)

A disposable PostgreSQL 17.11 instance was installed and started for local
integration testing. The test cluster listens only on a private Unix socket and
is separate from the running SQLite demo APIs and databases. Homebrew also
initialized its standard cluster directory during package installation; that
server is stopped and is not used by this project.

Implemented a versioned PostgreSQL schema for workspace, project, approval,
AgentTask, audit, and notification records, plus a `PostgresApprovalDecisionRepository`.
The adapter locks the approval row, verifies the expected full request and
workspace/project ownership, propagates a linked task decision, writes the
immutable audit record, and inserts the notification record in one transaction.
Concurrent decisions are serialized at the row; stale decisions conflict. A
tested audit insert failure rolls back the approval transition. The notification
insert shares that transaction, so its failure also rolls back all prior writes.

The audit table now stores generic scoped events using `entity_type`,
`entity_id`, and `event_type`; only approval events carry the approval foreign
key, while every event remains bound to a workspace and immutable. This permits
agent and project lifecycle events to be represented without weakening the
approval relationship constraint. It does not yet create foreign keys for all
non-approval entity types or import the legacy audit stream.

The adapter is intentionally not wired into `approval_decisions.py` or the API
store. The rest of the application still uses process-local maps and SQLite;
production startup remains blocked. This migration creates the first normalized
transaction slice only. It neither imports nor changes existing SQLite records.
Only disposable per-test schemas have applied `pg_0001`. This predeployment
change extends that initial migration; if any shared database has already
applied the earlier definition, use a new forward migration rather than replaying
an edited migration.

Validation against the live PostgreSQL 17.11 test instance:

- PostgreSQL adapter integration tests: 8 passed (commit/task/audit/outbox,
  project and agent audit events, audit rollback, notification rollback,
  concurrent approve/deny, and expiry/late-decision).
- Full backend suite with the PostgreSQL DSN set: 701 passed, 6 warnings.
- Ruff, Python compilation, and `git diff --check` passed.
- CI now provisions PostgreSQL 17.11 for the backend test job and sets the
  test-only `AIXION_TEST_POSTGRES_DSN`; tests create and drop a unique schema
  per case.

Run the integration tests against a disposable local database with:

```sh
docker run --rm --name aixion-postgres-test \
  -e POSTGRES_DB=aixion_test \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 postgres:17.11-alpine3.24

# In another terminal, from backend/:
AIXION_TEST_POSTGRES_DSN='postgresql://postgres:postgres@127.0.0.1:5432/aixion_test' \
  python -m pytest -q tests/test_postgres_approval_repository.py
```

`AIXION_TEST_POSTGRES_DSN` is test-only. No production DSN or production
configuration key was added. Do not deploy this adapter or remove the startup
block until repository coverage includes creation, reads, expiry, AgentTask,
credentials, devices, notifications, and audit across all processes; the
SQLite-to-PostgreSQL importer, migration/recovery drills, and multi-process API
authorization tests also remain required.

## Verification update — 2026-10-20 (read-only ownership preflight)

The existing SQLite audit tool was extended to resolve `agent.registered` and
`agent.token_revoked` events through the exact `ExternalAgent` ID carried by
those event types. It does not infer ownership when that parent is missing; a
regression test keeps that case in `REVIEW_REQUIRED`.

A consistent, temporary backup of the isolated local phone-demo database was
audited read-only. SQLite integrity and all model payloads passed. The snapshot
contained one workspace, one project, five approvals, nine external agents and
credentials, nine notifications, twelve sessions, one user, and 26 audit
events. The auditor now reports no ownership, identity, or parent conflicts.
This resolves the 15 prior `workspace_unresolved` findings for the agent
registration/revocation events through their exact parent record.

The result is `STRUCTURAL_REVIEW_PASSED`, with `migration_authorized=false` and
`snapshot_certified=false`. This is only a local demo-database preflight; it is
not a production-source audit, backup certification, import, or cutover. The
temporary copy was removed; the live SQLite database and API were not modified.

The PostgreSQL migration ledger now stores a deterministic SHA-256 checksum
over each migration ID, name, and ordered SQL statement list. Startup migration
execution rejects changed definitions and unknown future migration IDs. A
preexisting ledger row without a checksum fails closed and requires operator
verification; do not backfill a checksum automatically, since that would bless
an unknown schema state. Existing deployed schemas, if any, need an explicit
forward migration and reviewed checksum reconciliation before this runner is
used against them.

## Verification update — 2026-10-20 (notification persistence failures)

Device registration, token reassignment, device removal, invalid-token cleanup,
and marking a notification read now restore their process-local cache changes if
the SQLite persistence call fails. This prevents a failed write from appearing
accepted to later requests in the same process and then reversing after restart.
Invalid-token cleanup remains best effort after push delivery; a failed cleanup
leaves the token available for a later retry and removes the uncommitted audit
event from the cache.

Failure-injection tests cover durable-row and cache consistency for these paths.
The notification test module passed 35 tests; the full backend suite passed 746
tests with 6 warnings. Ruff and Python compilation passed. No migration or
configuration key was added. This does not make the process-local SQLite store
safe across API processes and does not change the PostgreSQL production gate.

## Verification update — 2026-10-20 (approval expiry integrity)

SQLite and PostgreSQL approval repositories now share expiration validation.
An expiration must preserve every approval field except status, update time,
and the effective expiry deadline. The immutable `approval.expired` audit event
must match the approval workspace, transition, and deadline before either
adapter opens its write transaction. This prevents repository callers from
using expiration to alter approval content or misattribute its audit event.

The focused SQLite and PostgreSQL repository suites passed 49 tests, and the
full backend suite passed 752 tests with 6 warnings. Ruff and Python
compilation passed. No database migration or configuration key was added.
PostgreSQL remains an unbound approval slice; the app still uses the SQLite
store and production startup remains blocked pending full persistence coverage
and multi-process authorization and recovery validation.

## Verification update — 2026-10-20 (scoped approval expiry audit)

The expiry service now copies the approval's workspace ID into the
`approval.expired` audit record. A workspace-scoped SQLite deadline-sweep test
and a PostgreSQL service-level expiration test verify the audit persists with
the committed state. The focused approval expiry/concurrency suites passed 28
tests; the full backend suite passed 851 tests with 6 warnings. No schema
migration or configuration key was added. PostgreSQL remains unbound at the API
runtime, and this does not change the production persistence gate.

## Verification update — 2026-10-20 (durable approvals in recovery export)

Recovery export now reads the included collections and SQLite migration ledger
from one transactionally consistent database snapshot. Workspace filtering
uses parent records from that snapshot, not process-local maps. A regression
removes durable project and approval rows from the cache, adds a cache-only
project, and verifies only durable records are exported. Recovery-route and
workspace-isolation tests passed 46 cases with 1 dependency warning. This
contract is implemented only for SQLite; PostgreSQL recovery export and
multi-process recovery validation remain open.

## Verification update — 2026-10-20 (PostgreSQL notification outbox slice)

Added `PostgresNotificationOutboxRepository` for transactional push queue
operations. Claims use `FOR UPDATE SKIP LOCKED`, bounded leases, and monotonic
attempt numbers. Outcome writes are fenced by the claimed attempt and an
unexpired lease, so a stale worker cannot overwrite a newer claim. Expired and
missing leases are reclaimable; exhausted rows become terminal failures.
Device target reads join the account row and require exact workspace scope,
honor a notification's optional user target, and exclude disabled accounts.
FCM `UNREGISTERED` cleanup deletes only the matching device ID/token and writes
its audit event in the same transaction; the token is not copied into audit.
An injected dispatcher service now composes these operations with the existing
FCM HTTP v1 sender, scopes sends to the outbox row's workspace/user, preserves
partial-delivery behavior, and persists bounded retry categories with capped
backoff. Provider calls remain outside database transactions.

PostgreSQL migration `pg_0003_notification_outbox_claim_indexes` adds separate
partial indexes for due/retry rows and expired sending leases. It is
forward-only under the checksum migration runner; no production database was
modified. No production configuration key was added. The dispatcher is not
wired to this repository, and Android device registration writes are still on
the SQLite store, so this slice does not enable PostgreSQL push delivery or
clear the production startup block.

The PostgreSQL outbox integration suite passed 12 tests, covering claim
concurrency, retries, lease recovery, stale-result fencing, workspace/user
isolation, dispatcher success/retry/partial delivery, and atomic token
invalidation/audit rollback. The full backend suite passed 764 tests with 6
warnings; Ruff and Python compilation passed. For
isolated validation, use the test-only `AIXION_TEST_POSTGRES_DSN` and run
`python -m pytest -q tests/test_postgres_notification_outbox.py` from `backend/`.

## Verification update — 2026-10-20 (import content verification)

The partial SQLite-to-PostgreSQL staging importer now compares normalized
payloads for every imported entity after writing and on an idempotent retry.
The retry path still checks the source digest, scope manifest, and complete
table counts, then verifies record content as well. A same-count mutation of a
target project row now blocks the retry rather than reporting the original
import as intact. The content comparison runs inside the import transaction;
the stored manifest remains immutable.

The importer integration suite passed 13 tests and the full backend suite
passed 765 tests with 6 warnings. Ruff and Python compilation passed. No schema
or configuration change was required. The importer remains a deliberately
partial staging tool: it does not authorize cutover, certify the source
snapshot, or prove a restore. Run it only against an isolated test/staging
target after the documented coordinated snapshot and explicit flags.

## Verification update — 2026-10-20 (PostgreSQL account/session slice)

Added an isolated PostgreSQL auth repository for normalized user lookup,
session creation, bearer-session authentication, workspace-owner session
listing, and session revocation. Authentication reads current session and user
rows rather than process-local maps; revoked, expired, disabled, or unverified
accounts fail closed. Revocation locks actor and target in deterministic user
ID order, changes only active sessions in the same workspace, and appends the
workspace-scoped audit event in the same transaction. An audit insert failure
rolls back all session changes. The repository validates normalized columns
against JSONB payloads and does not store raw bearer tokens.

The PostgreSQL auth integration suite passed 9 tests; the full backend suite
passed 774 tests with 6 warnings. Ruff and Python compilation passed. No
migration or configuration key was added. The repository is not wired into the
API: account registration, email verification delivery, resend controls,
rate-limit state, and the remaining application maps still use SQLite. Do not
enable PostgreSQL auth alone as a production mode; complete the account
onboarding transaction and full store boundary before switching runtime
authority.

## Verification update — 2026-10-20 (PostgreSQL onboarding transaction slice)

Added PostgreSQL migration `pg_0004_auth_onboarding` for workspace owner
identity, workspace-scoped invitations, encrypted verification-email delivery,
and hashed authentication rate-limit buckets. `PostgresAuthRepository`
creates an independent owner account workspace and its verification email in
one transaction; this supports the selected one-workspace-per-account v1
model rather than a single global bootstrap owner. Invite registration locks
and validates a pending invite, checks exact email, role, and workspace
binding, then creates the unverified user, queues encrypted delivery material,
consumes the invite, and writes the workspace audit event atomically. Plain
verification codes are not stored by these methods.

The repository also supports verification-code rotation and verification
under a user-row lock. Rotation commits the salted hash and encrypted email
outbox together and invalidates older pending or leased codes. Verification
uses constant-time digest comparison, rejects invalid/expired/missing codes,
clears code material when successful, and invalidates any still-queued email.
These methods have not been selected by the HTTP routes.

PostgreSQL invite administration now covers owner-authorized create/list/revoke,
workspace scoping, durable expiry transitions, pending-invite uniqueness, and
transactional audit events. Audit failure rolls back invite creation. HTTP
invite routes remain SQLite-backed.

PostgreSQL owner controls now include workspace user listing, role updates, and
account-deletion requests. Role updates lock the workspace before user rows so
concurrent changes cannot remove the final active owner; role and account
deletion audit writes share the transaction with state changes. Deletion also
revokes active sessions atomically. The corresponding HTTP routes still use
SQLite maps.

`PostgresAuthRateLimitRepository` atomically consumes fixed-window slots from
keyed identifier digests, preserving the existing no-raw-email/IP storage
contract. `PostgresEmailOutboxRepository` claims rows with `SKIP LOCKED`,
reclaims expired leases, fences stale workers by attempt and lease, preserves
ciphertext for retry, and clears it when delivery reaches a terminal state.
The new `dispatch_one_due_email_postgres` service uses the existing AES-GCM
decryptor and SMTP sender, performs network I/O outside the database
transaction, and records bounded error categories. It is not called by the
application background loop; the API auth rate-limit calls also remain on
SQLite.

The PostgreSQL onboarding methods and migration are not wired to auth routes.
Signup, email verification/resend, SMTP outbox dispatch, invite administration,
and auth rate-limit calls still use SQLite. No production database was modified
and no configuration key was added. Do not route production traffic to this
partial repository slice.

The PostgreSQL auth, email outbox, and rate-limit integration suites passed 45
tests against the disposable PostgreSQL 17 instance, including invite
administration, role and deletion transactions, verification rotation, SMTP
dispatch success, retry, and terminal cleanup. The full backend suite passed
810 tests with 6 deprecation warnings. Ruff, Python compilation, and
`git diff --check` passed. Production cutover still requires completing the
PostgreSQL auth route boundary, exercising multi-process account isolation,
and completing the deployment, push, backup/restore, security, and soak gates
above.

## Verification update — 2026-10-20 (PostgreSQL runtime boundary)

Added `AIXION_POSTGRES_DSN` as a required production configuration value and
exposed only a boolean `postgres_dsn_configured` status in `Settings`, keeping
credentials out of settings representations and API responses. The production
example uses `sslmode=verify-full`; the PostgreSQL runtime rejects production
DSNs that do not request full certificate and hostname verification.

Added an explicit PostgreSQL runtime factory with a bounded psycopg connection
pool and a stable application name. It exposes the existing PostgreSQL repositories and provides a
read-only migration ledger check for missing, unknown, or checksum-mismatched
migrations. A fresh database with no migration ledger reports all expected
migrations as missing instead of raising an opaque relation error. It neither
applies migrations nor becomes the application store.
SQLite remains the active persistence authority; production startup remains
blocked. The DSN must be injected through the deployment secret manager. No
database migration or production database was touched in this slice.

PostgreSQL connection policy is now explicit and bounded: connect timeout,
statement timeout, lock timeout, and idle-in-transaction timeout are validated
from environment configuration and applied to every connection. Defaults are
5 seconds, 15 seconds, 3 seconds, and 15 seconds respectively. These settings
limit stalled connections and transactions; they do not replace connection
pool sizing, API transaction design, or runtime cutover validation. Pool size,
acquisition wait, and queued waiters are bounded with defaults of 2 minimum,
12 maximum, 5 seconds, and 20 waiters. The runtime exposes explicit `open`,
`close`, and non-secret pool counters.
An opt-in `AIXION_POSTGRES_RUNTIME_PROBE_ENABLED` lifecycle path now opens the
pool, waits for its configured minimum connections, and verifies the migration
ledger before the API begins serving. Invalid schemas fail startup and the
pool is closed on startup failure and shutdown. This path is a staging probe:
it does not route requests or background workers to PostgreSQL, and SQLite
remains the reported active backend.

The authenticated runtime readiness response reports the PostgreSQL probe as
`disabled`, `ready`, or `not_ready`, includes schema readiness and bounded pool
counters, and fails overall readiness if the enabled probe is unavailable or
fails. Probe errors are generic so connection details and credentials are not
returned to clients. It continues to identify SQLite as the active database.

## Verification update — 2026-10-20 (approval post-commit rewrite removal)

Removed the redundant full-store persistence call after approval decisions.
The approval repository already commits the approval state, decision audit,
notification, and linked AgentTask records atomically. Rewriting every SQLite
key-value row from compatibility maps afterward could overwrite unrelated
records written by another process and could return an error after the decision
had already committed. The route now publishes the committed records to its
in-process compatibility maps without issuing that whole-store rewrite.

A focused regression inserted a durable record outside the compatibility maps,
forced the legacy full-store persistence method to fail if called, and verified
that a phone-style deny decision still committed while preserving the unrelated
record. Approval concurrency/repository/lifecycle tests passed (42). The full
backend suite passed 773 tests with 92 PostgreSQL-integration skips because
`AIXION_TEST_POSTGRES_DSN` was not set for that run. Ruff, Python compilation,
and `git diff --check` passed. PostgreSQL remains a repository-test/staging
adapter only; SQLite remains the active application persistence authority.

## Verification update — 2026-10-20 (Android session failure-path tests)

Added device instrumentation cases proving corrupt encrypted token data and a
missing Android Keystore key both clear the local session and return an
unauthenticated result. The focused `AuthSessionSecurityTest` suite passed all
5 tests on the connected Pixel 10 Pro Fold. The connector instrumentation
assertions were aligned with the current subtitle, button labels, and uppercase
status badge. The callback test now scrolls each lazy-list control into view and
waits for Compose to become idle after content setup. The final complete
connected instrumentation suite passed 26 tests with zero failures or skips,
including the auth/session security and connector callback cases.

The test runner uninstalled its target app after execution. The debug app was
reinstalled against the existing isolated local demo API on loopback port 8001;
the existing API process was not restarted. No production configuration or
database was changed. The full connected instrumentation suite is green, but
the broader Stage 1 release gate still requires release-build HTTPS validation
and the remaining Android release-boundary checks.

## Verification update — 2026-10-20 (transactional device registration)

The authenticated SQLite device-registration routes now write only the affected
device rows and audit record in one `BEGIN IMMEDIATE` transaction. Token reuse
keeps one canonical registration, transfers ownership to the authenticated
account, and removes legacy duplicate-token rows in the same transaction.
Unregistration is an exact-owner no-op for unknown or foreign IDs. The API
updates its compatibility cache only after the durable transaction commits, so
these routes no longer rewrite unrelated application records or expose
uncommitted cache changes.

The PostgreSQL notification repository now has matching transactional
register/reassign/unregister operations. Registration validates the enabled
user and their one-workspace membership, serializes token ownership changes,
and stores audit records in the same transaction. These operations are
repository-only: the application still routes to SQLite, and the PostgreSQL
runtime remains a staging/test boundary. No schema migration or configuration
key was added.

The full backend suite passed 875 tests with 6 warnings. PostgreSQL cases used
the isolated local test database and per-test schemas; no production database
was used. The persistence inventory passed without increasing its direct
SQLite boundary ceiling. Ruff and Python compilation passed. The Pixel demo
account login and authenticated session check succeeded, and the app opened to
an empty approval queue. This does not verify a real Mac agent request, phone
decision delivery back to that agent, FCM wake-up, production PostgreSQL
routing, or multi-process API safety.

### Rollout for this slice

No configuration change or data migration is required. The current API
processes were left running and do not contain this source change yet. Deploy
the API update to an isolated staging instance first, then verify registration,
same-account refresh, token reassignment, owner-scoped removal, audit records,
and persistence after an API restart. Keep PostgreSQL device operations behind
the staging/test repository boundary until the complete application store,
authorization, backup/restore, and multi-process gates pass. Production startup
continues to fail closed.

## Verification update — 2026-10-20 (Android release HTTPS boundary)

The release APK assembled with the local command-line SDK and a deliberately
non-routable HTTPS placeholder. The merged release manifest sets
`usesCleartextTraffic=false`, and the release pre-build task rejects an HTTP
API URL with the expected validation error. No app was installed and no
production host was contacted. The resulting artifact is unsigned and must not
be distributed. The build emitted SDK tooling/Kotlin analysis warnings and an
existing unused-variable warning in `ForgedLogoMark.kt`; assembly completed.

Evidence and exact commands are recorded in
[`ANDROID_RELEASE_BOUNDARY_VALIDATION_2026-10-20.md`](ANDROID_RELEASE_BOUNDARY_VALIDATION_2026-10-20.md).
This closes only the local HTTPS configuration check. A real hosted API URL,
protected release signing, external-network TLS/auth validation, and the
remaining phone notification/account lifecycle matrix are still required.

## Verification update — 2026-10-20 (production SQLite configuration guard)

Production configuration no longer treats `AIXION_DB_PATH` as a required
setting and rejects it whenever it is supplied. The PostgreSQL setting name is
`AIXION_POSTGRES_DSN`, matching the runtime factory and production template.
`SQLiteBackedStore` also refuses the production profile before creating a
database path, independently of the startup validation guard. This prevents a
future settings-validation regression from silently initializing SQLite in
production.

The focused settings suite passed 58 tests; Ruff, Python compilation, and
`git diff --check` passed. No new configuration key, database migration, or
production deployment was added. The full PostgreSQL application-store boundary
is still incomplete, so production startup remains blocked. The environment,
secrets, HTTPS callback, and trust readiness guides now state that a DSN does
not enable PostgreSQL routing and that `AIXION_DB_PATH` is local/demo/test only.

## Verification update — 2026-10-20 (AgentRun persistence boundary)

Added PostgreSQL migration `pg_0005_agent_run_state` for workspace-scoped
`agent_runs`, `agent_run_steps`, and `agent_run_events`, with composite foreign
keys binding run, task, project, approval, and workspace identities. Added
SQLite/PostgreSQL AgentRun read adapters and routed workspace authorization's
run lookup through the repository. AgentTask scope lookups in the same module
also use durable repository reads, including an approval-to-task query that
does not truncate at the UI list limit.

The SQLite/PostgreSQL run repository now commits initial run creation, steps,
events, and its audit record as one unit after validating durable approved-task,
approval, project, and workspace scope. SQLite rejects a second active run for
the same task under its write lock. The supervisor publishes compatibility-map
entries only after this transaction succeeds. Startup recovery now commits
task-status repair, timeline event, and audit together before it can schedule
a run. Lease acquisition now locks the durable run row, updates its lease,
event, and audit in one transaction, and rejects every duplicate unexpired
claim, including a retry using the same worker ID. The SQLite contention test
confirms one winner. The full backend suite passed
794 tests with 98 skips; PostgreSQL integration tests skip because
`AIXION_TEST_POSTGRES_DSN` is not configured here. Ruff, Python compilation,
and `git diff --check` passed. No configuration keys were added.
Subsequent lease, step, event, and recovery transitions still write through
compatibility maps, and the active application binding remains SQLite.
Production PostgreSQL startup must remain blocked until those lifecycle writes,
reconciliation, migration application, and exact-database integration tests
are complete.

### Rollout for this slice

Do not apply `pg_0005_agent_run_state` to production yet. In an isolated
PostgreSQL test database, apply all migrations with
`run_postgres_migrations`, verify successful run/step/event inserts and foreign
key rejection for cross-workspace links, then test competing PostgreSQL lease
claims and migrate remaining supervisor transitions and restart recovery before
staging rollout. No deployment or database migration
was run in this workspace.

## Verification update — 2026-10-20 (AgentRun step completion transaction)

The supervisor now snapshots the durable `RUNNING` checkpoint before external
step execution. On completion, SQLite or PostgreSQL commits the updated run,
changed step rows (including a newly ready next step), completion events, lease
release, and `agent_run.step_completed` audit record in one repository
transaction with expected-state checks. A failed commit restores the in-memory
compatibility objects and removes uncommitted completion events and audit data;
the durable run remains at its leased `RUNNING` checkpoint for stale-lease
recovery. This avoids exposing an execution result whose completion transaction
did not commit.

Focused repository and supervisor tests pass (12 passed, 1 skipped). The skip
is PostgreSQL integration because `AIXION_TEST_POSTGRES_DSN` is not configured.
No configuration keys or schema changes were added. PostgreSQL code is not
runtime-verified here. Heartbeats, lease release, retries, pause/resume,
cancellation, and other run/task lifecycle transitions still include
compatibility-map writes; the application remains bound to SQLite.
This does not clear the production startup block.

### Rollout for this slice

No migration or deployment is required for this transaction boundary. Before
staging, run the repository integration tests against an isolated PostgreSQL
database using `AIXION_TEST_POSTGRES_DSN`, then run the full backend suite. Keep
PostgreSQL runtime disabled until the remaining run/task lifecycle operations,
multi-process recovery behavior, and exact-database integration gates pass.

## Verification update — 2026-10-20 (durable stale-lease recovery)

The watchdog now queries expired leases through the AgentRun repository with a
bounded candidate batch and optional workspace filter. Each run recovery
compares its durable run and current-step snapshots, clears the expired lease,
applies retry or human-review state, and inserts the stale-recovery event and
audit record in one SQLite or PostgreSQL transaction. Concurrent watchdogs
cannot both recover the same observed lease; the stale claimant receives a
conflict and is skipped. The compatibility cache is projected only after the
database commit. A missing or mismatched current step fails closed as an
integrity error.

The synchronized two-watchdog regression test and focused repository/supervisor
tests pass (13 passed, 1 skipped). The skip is PostgreSQL integration because
`AIXION_TEST_POSTGRES_DSN` is unavailable. No config keys or migrations were
added. The PostgreSQL implementation remains unverified against a live test
database. The application still selects SQLite, and retry, pause/resume,
cancellation, and other lifecycle operations still need durable repository
transactions. The full backend suite passes (796 passed, 98
skipped); Ruff, Python compilation, and `git diff --check` pass.

### Rollout for this slice

No deployment or schema migration is needed. Before staging, run the repository
and concurrency suite against an isolated PostgreSQL database with
`AIXION_TEST_POSTGRES_DSN`, then verify restart recovery with multiple workers.
Keep production PostgreSQL startup disabled until all remaining lifecycle paths
and cross-process integration gates pass.

## Verification update — 2026-10-20 (durable heartbeat and release)

Run heartbeat and explicit lease release now execute through the repository.
Both adapters validate the exact owner and token against the durable run and
reject expired renewals. Each operation commits the run, matching current-step
lease fields, run event, and audit record together. The compatibility cache is
updated only after commit. A release from an old owner/token is ignored so it
cannot clear a newer worker's lease. No configuration or schema change was
needed.

Focused repository/supervisor tests pass (15 passed, 1 skipped); the skipped
test requires `AIXION_TEST_POSTGRES_DSN`. PostgreSQL transaction behavior still
needs an isolated live-database run. Pause/resume, cancellation, manual control,
and other task/run transitions still have compatibility-map writes,
and the application remains bound to SQLite. The full backend suite passes
(798 passed, 98 skipped); Ruff, Python compilation, and `git diff --check` pass.

### Rollout for this slice

No migration or deployment is required. Validate PostgreSQL lease contention,
heartbeat-vs-expiry races, stale-token release, and audit/event atomicity in an
isolated test schema before staging. Keep the production database switch
disabled until remaining lifecycle transactions and multi-process integration
checks pass.

## Verification update — 2026-10-20 (durable bounded step retry)

Operator-approved step retry now builds updates from isolated run/step copies
and commits them through the AgentRun repository. The transaction compares the
durable snapshots, writes the run and step state with the operator event and
audit record, and publishes compatibility-cache changes only after commit. A
concurrent update or failed commit leaves the previous cache state visible and
returns a conflict. Retry is rejected for terminal runs, active leases, invalid
step states, and exhausted attempt budgets.

Focused repository/supervisor tests pass (16 passed, 1 skipped). The full
backend suite passes (799 passed, 98 skipped). Ruff, Python compilation, and
`git diff --check` pass. The PostgreSQL integration skip remains because
`AIXION_TEST_POSTGRES_DSN` is unavailable, so PostgreSQL control-transition SQL
still needs a live isolated-database run. No config keys or schema changes were
added. The app remains on SQLite; pause/resume, cancellation, and other
task/run transitions still need durable transaction boundaries.

### Rollout for this slice

No deployment or migration is required. In an isolated PostgreSQL test schema,
verify retry success, stale-snapshot conflict, concurrent retry contention,
and atomic event/audit insertion. Keep production PostgreSQL routing disabled
until that test and the remaining lifecycle gates pass.

## Verification update — 2026-10-20 (durable pause and resume)

Pause and resume now use the AgentRun control-transition transaction. The
transaction compares the durable run and current-step snapshots, commits the
run and any changed step with operator events and an audit record, then updates
the compatibility cache. Both service and repository boundaries reject stale
state; pause/resume also reject a run with an active lease. Lease acquisition
now refuses PAUSED or cancellation-requested runs, preventing a stale worker
request from starting after a committed pause.

Focused route/supervisor coverage passes (24 passed). The full backend suite
passes (802 passed, 98 skipped); Ruff, Python compilation, and
`git diff --check` pass. PostgreSQL integration remains skipped without
`AIXION_TEST_POSTGRES_DSN`, so these SQL transactions still need validation in
an isolated PostgreSQL test database. No config keys or schema changes were
added. Cancellation and other run/task control writes still need repository
transactions, and the app remains bound to SQLite.

### Rollout for this slice

No migration or deployment is required. Verify pause/resume against an isolated
PostgreSQL schema, including competing pause/resume requests and lease
acquisition races. Keep production PostgreSQL routing disabled until those
checks and the remaining lifecycle gates pass.

## Verification update — 2026-10-20 (durable run cancellation)

Run cancellation now reads the run and complete step set from the AgentRun
repository. The control-transition transaction verifies that the supplied
steps equal the complete durable set, then commits cancellation of every
unfinished step, the run's CANCELLED state, step/operator events, and audit
record together. Cache projection follows only after commit. Missing cache
steps therefore cannot cause a partial run cancellation. Active run leases or
RUNNING steps are rejected. The linked AgentTask remains unchanged, matching
the existing run-cancel endpoint behavior; task cancellation is a separate
operation.

Focused route/supervisor/repository coverage passes (28 passed, 1 skipped).
The full backend suite passes (804 passed, 98 skipped); Ruff, compilation, and
`git diff --check` pass. PostgreSQL integration remains skipped without
`AIXION_TEST_POSTGRES_DSN`, so the PostgreSQL transaction still requires an
isolated live-database test. No config keys or migrations were added. The
application remains bound to SQLite; task-level cancellation and other
remaining lifecycle transitions still need production transaction validation.

### Rollout for this slice

No migration or deployment is required. Test cancellation races, complete-step
set validation, event/audit atomicity, and parity with the SQLite adapter in
an isolated PostgreSQL schema. Keep production PostgreSQL routing disabled
until those checks and the remaining lifecycle gates pass.

## Verification update — 2026-10-20 (task cancellation lease safety)

AgentTask cancellation now fails closed when any worker lease field is
recorded. This includes active, expired, and incomplete lease records because
lease expiry does not prove an external worker has stopped. The API summary
reports the task as not cancellable until the worker stop is confirmed and the
lease is reconciled. Both SQLite and PostgreSQL repository adapters enforce
the same invariant within their cancellation transaction, preventing a
worker-claim race from turning a summary pre-check into a false cancellation.
Cancellation with no recorded lease still atomically writes task state, event,
and audit. AgentRun and AgentTask cancellation remain separate operations;
cross-repository partial success is not presented as an atomic combined
cancel.

Focused cancellation coverage passes (14 passed, 1 skipped); the full backend
suite passes (809 passed, 98 skipped). Ruff, Python compilation, and
`git diff --check` pass. The PostgreSQL adapter integration test remains
skipped because `AIXION_TEST_POSTGRES_DSN` is not configured. No schema
migration or config key was added. Production PostgreSQL routing remains
gated on isolated integration validation.

### Rollout for this slice

No migration or deployment is required. Before enabling production use, add a
worker-stop acknowledgement or operator lease-reconciliation workflow so a
recorded lease cannot indefinitely prevent cancellation; exercise cancellation
and worker claim races against PostgreSQL in an isolated schema.

## Verification update — 2026-10-21 (cancellation record binding)

The task cancellation repository now verifies that cancellation preserves the
durable task's project, workspace, and approval bindings. It also binds the
event and audit actor, operation type, previous/new status, approval reference,
lease-cleared statement, and explicit no-cleanup/no-approval-change claims.
Both storage adapters use this shared validator before opening a transaction.
Negative coverage tampers with each ownership or audit field and verifies no
state write occurs.

Validation: focused task cancellation suite passes (23 passed, 1 skipped); the
full backend suite passes (818 passed, 98 skipped); Ruff, Python compilation,
and `git diff --check` pass. PostgreSQL integration remains skipped without
`AIXION_TEST_POSTGRES_DSN`. No configuration key or migration was added.

Run from `backend/` with
`PYTHONPATH=. /tmp/aixion-ct-validation/bin/python -m pytest -q`.
