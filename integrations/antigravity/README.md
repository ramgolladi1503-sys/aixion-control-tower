# Antigravity approval gate

This integration pauses matching Antigravity tool calls in the host process,
creates a Control Tower approval, and returns Antigravity's `auto_approve`
decision only after the same approval is approved on mobile, its frozen payload
hash verifies, and its at-most-once delivery claim commits. Read-only passthrough
continues to return `allow`. The installed Antigravity 2.25.0 hook documentation
defines `allow` as delegating to the default policy, while `auto_approve`
approves a default-policy prompt but still honors explicit user-configured
rules. `deny` hard-blocks execution. See the [official Antigravity hook reference](https://www.antigravity.google/docs/hooks). A
denial, timeout, malformed response, revoked credential, or network failure
returns `deny`.

## Important scope

This is an opt-in host hook. It does not change Antigravity's global settings,
the active app process, or its other hooks. Install the example as a
workspace-local `.agents/hooks.json` after reviewing the command path. The
hook matcher covers all tools. It passes through only the explicit read-only
allowlist in the script; every other tool, including unknown or newly
introduced tools, requires an approval. Review the allowlist against the
installed Antigravity release before production use. Native UI prompts and
Control Tower prompts may differ by tool and Antigravity configuration.

## Prerequisites

- A reachable Control Tower backend using HTTPS, or loopback HTTP for local
  development only.
- An enabled external agent credential with provider `ANTIGRAVITY`, project
  scope limited to the intended project, and only `CREATE_APPROVAL`,
  `READ_APPROVAL_DECISION`, `CONSUME_APPROVAL_DECISION`,
  `REPORT_APPROVAL_DELIVERY_FAILURE`, and `EXPIRE_OWN_APPROVAL` actions. The hook claims an approved invocation exactly once before returning `auto_approve`; a lost claim response fails closed, best-effort reports the ambiguous outcome, and still denies. The expiry
  action can close only approvals created by that same agent. Set its request
  limit to at least 60 per minute per active hook; the default two-second poll
  interval uses about 30 decision reads per minute, plus request creation and
  expiry calls. The hook honors the API `Retry-After` response while polling.
  During a long pending wait, the hook refreshes the invocation-scoped liveness
  lease every 15 seconds through the approval heartbeat endpoint. The response
  must confirm the same approval, connector installation, and invocation with
  `live: true`; an expired, mismatched, malformed, unauthorized, or timed-out
  heartbeat denies the tool call before consume. The lease never extends the
  approval expiry, and the server checks it again when the connector claims an
  approved decision. Credential `last_used_at` is not treated as liveness.
  After an ambiguous network or server failure on the idempotent decision
  `GET`, it retries with exponential delays of 2, 4, then at most 10 seconds,
  bounded by the configured wait deadline. It does not retry approval creation
  or consumption after an ambiguous `POST`; both remain fail-closed. A decision
  that does not arrive before the deadline is denied.
- Credentials issued by older Android app versions may lack the consume and
  delivery-failure capabilities. Replace or explicitly reconfigure those
  registrations before installing this hook; setup verification rejects an
  incomplete capability set. Existing credentials are never widened silently.
- On macOS, the hook reads non-secret connection settings from the explicit
  workspace config path and retrieves the token from Keychain. The config
  contains `api_base_url`, `agent_id`, and `project_id`. The fallback config is
  `~/.config/aixion/antigravity.json`. For controlled local testing, equivalent
  non-secret connection settings are accepted through environment variables.
  The process token is disabled by default; tests must set both
  `AIXION_AGENT_TOKEN` and `AIXION_ALLOW_AGENT_TOKEN_ENV=true`. Normal host
  use should store the token in Keychain. A protected owner-only token file is
  also supported when Keychain is unavailable.

The token is sent only to the configured Control Tower API over HTTPS (or
loopback HTTP for development). The hook rejects secret-like argument keys,
common inline secret patterns, and argument payloads larger than 48 KiB. No
tool arguments are written to logs.

## Physical Android device over USB (local development only)

The Android debug app and Antigravity hook on the Mac can use the same local
backend without exposing it to the LAN. Run the isolated backend on the Mac
bound to `127.0.0.1:8003`, using a dedicated test database. Configure the host
hook with `http://127.0.0.1:8003` and build the Android debug app with that same
API URL. Both clients must reach the same backend instance and database for a
phone decision to unblock the pending hook call.

With the phone connected over USB and USB debugging authorized, find its serial
and create a reverse port mapping from the phone to the Mac:

```sh
adb devices -l
export ANDROID_SERIAL="<serial shown above>"
adb -s "$ANDROID_SERIAL" reverse tcp:8003 tcp:8003
adb -s "$ANDROID_SERIAL" reverse --list
```

Build and install a debug APK configured for the forwarded phone loopback URL:

```sh
cd /ABSOLUTE/PATH/mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://127.0.0.1:8003/
adb -s "$ANDROID_SERIAL" install -r app/build/outputs/apk/debug/app-debug.apk
```

On the phone, `127.0.0.1:8003` reaches the Mac's `127.0.0.1:8003` through the
USB reverse mapping. The host hook also calls that Mac loopback URL directly.
Keep the backend bound to loopback; this setup does not expose port 8003 to the
LAN. Remove the mapping when finished:

```sh
adb -s "$ANDROID_SERIAL" reverse --remove tcp:8003
```

This path works only while the Mac backend and USB debugging connection are
available. It is for local device verification, not away-from-Mac operation or
production reachability. Those require a deployed HTTPS API reachable by both
the phone and host, plus production notification and host-availability setup.

## Workspace installation

1. Register an Antigravity external agent in Control Tower with one project and
   only the five actions above. Store its one-time token in macOS Keychain using
   the secure prompt helper below. The token is entered at the macOS Keychain
   prompt; it is not put in shell history or process arguments. The helper
   refuses to replace an existing item. Do not enable Keychain's “allow all
   applications”.

   ```sh
   python3 /ABSOLUTE/PATH/integrations/antigravity/store_agent_token.py \
     --agent-id "agent_..."
   ```

   If the one-time token has already been dismissed or lost, it cannot be
   recovered from the server. An owner must issue a new credential in the app,
   then store that newly displayed token with this helper. Do not rotate a
   credential solely to test this command unless the owner intends to replace
   the active credential.
2. Install the hook only in the intended workspace. This writes protected
   non-secret metadata under `~/.config/aixion/workspaces/`, merges its hook
   entry into `.agents/hooks.json`, preserves other entries, and refuses to
   replace a conflicting hook:

   ```sh
   python3 /ABSOLUTE/PATH/integrations/antigravity/setup_workspace.py \\
     --workspace "/absolute/path/to/project" \\
     --api-base-url "https://control.example.com" \\
     --agent-id "agent_..." \\
     --project-id "project_..."
   ```

   For an isolated host-side hook simulation, create a disposable workspace
   and a dedicated metadata home. This avoids writing any Aixion config under
   the user's real home; the hook itself remains inside the disposable
   workspace. Use the current checkout path and IDs returned by the app's
   connector registration:

   ```sh
   E2E_ROOT="${TMPDIR:-/tmp}/aixion-antigravity-e2e"
   : "${AIXION_E2E_API_URL:?Set this to one verified disposable local API URL}"
   mkdir -m 700 -p "$E2E_ROOT/workspace" "$E2E_ROOT/config-home"
   python3 /ABSOLUTE/PATH/integrations/antigravity/setup_workspace.py \
     --workspace "$E2E_ROOT/workspace" \
     --config-home "$E2E_ROOT/config-home" \
     --api-base-url "$AIXION_E2E_API_URL" \
     --agent-id "agent_..." \
     --project-id "project_..."
   ```

   A loopback URL such as `http://127.0.0.1:<verified-port>` supports phone
   approval only when the Android device has an `adb reverse` mapping for that
   port, as shown in the USB instructions above; without that mapping,
   `127.0.0.1` on the phone refers to the phone itself and the URL supports
   only host-side simulation. Do not use port 8002 on the current
   workstation until its competing listeners have been resolved and the single
   intended disposable database has been verified. Do not use `--config-home`
   with the user's normal home, reuse the blocked disk-cleanup workspace, or
   install a global Antigravity hook. Open the exact `$E2E_ROOT/workspace`
   folder as a separate Antigravity project. If Antigravity reports a missing
   folder, stop and select this exact directory; do not repair or repoint
   another project.

   A phone-reachable approval run requires one separately verified disposable
   API instance reachable from both the Mac and phone. For an away-from-Mac
   run, use an HTTPS URL accepted by the hook and configure the Android build
   to use that same API and database. For local USB testing, the explicit
   `adb reverse` procedure below may use a loopback URL because the phone's
   loopback port is forwarded to the Mac. Do not mix API instances or start a
   second service on port 8002. A passing host-side simulation is not evidence
   of a phone approval or same-invocation Antigravity resume.

   Remote URLs must use HTTPS. Loopback HTTP is allowed only for local
   development. The generated matcher covers every tool; only the script's
   explicit read-only allowlist passes without phone approval.
3. Run preflight with the generated config path. It calls unauthenticated
   `/health` (without the token), then authenticates `GET /agents/identity`
   with the connector token and checks the registered agent, provider,
   project scope, and required approval capabilities. The endpoint returns no
   credential. Production also requires a workspace-bound registration:

   ```sh
   python3 /ABSOLUTE/PATH/integrations/antigravity/aixion_pre_tool_hook.py \\
     --config "/Users/you/.config/aixion/workspaces/<workspace-id>.json" --check
   ```

4. Confirm the hook is enabled in Antigravity Settings > Customizations > Hooks.
   First use a harmless mutating call. Verify that the phone shows the exact
   invocation and that this same call continues after Approve. Repeat with
   Deny, timeout, network loss, and revoked credentials; each must keep the tool
   blocked.

For the live Android/PostgreSQL certification flow, use the dated evidence and
repeatable read-only verifier in
[`docs/verification/ANTIGRAVITY_POSTGRESQL_PHONE_E2E_20261024.md`](../../docs/verification/ANTIGRAVITY_POSTGRESQL_PHONE_E2E_20261024.md).
The verifier checks durable approval, payload hash, invocation identity,
at-most-once delivery claim, and a fresh command marker. It cannot observe the
phone UI or Antigravity resuming; the operator must record those two live
observations separately.

To roll back, run `setup_workspace.py --workspace "/absolute/path/to/project"
--remove`. It removes only this installer's hook entry and associated non-secret
config, preserving unrelated hooks. Revoke the agent token from the phone if it
is no longer needed.

### Physical Android device over USB (local development only)

For local phone verification without exposing the backend to the LAN, run one
isolated backend on the Mac bound to `127.0.0.1` on a selected port with a
dedicated test database. Set `API_PORT` below to that exact port; configure the
Antigravity hook to call `http://127.0.0.1:${API_PORT}` and build the Android
debug app with that same API URL. Both clients must use the same backend
instance and database for the phone decision to unblock the pending hook call.
The prepared local E2E API in the current PR workspace uses port `8004`; use
`API_PORT=8004` when continuing that run. Do not start a second service for the
USB mapping.

With USB debugging authorized, run the following in one terminal session. Set
`API_PORT` to the verified isolated backend port (`8004` for the prepared local
E2E runtime) and `ANDROID_SERIAL` to the connected device serial:

```sh
adb devices -l
API_PORT=8004 # prepared isolated E2E API; change only if the backend uses another port
ANDROID_SERIAL="<serial shown above>"
export API_PORT ANDROID_SERIAL
adb -s "$ANDROID_SERIAL" reverse "tcp:${API_PORT}" "tcp:${API_PORT}"
adb -s "$ANDROID_SERIAL" reverse --list
cd /ABSOLUTE/PATH/mobile/android
./gradlew assembleDebug "-PAIXION_API_BASE_URL=http://127.0.0.1:${API_PORT}/"
adb -s "$ANDROID_SERIAL" install -r app/build/outputs/apk/debug/app-debug.apk
```

On the phone, `127.0.0.1:${API_PORT}` reaches the Mac's loopback service through
the USB reverse mapping. Use the matching `http://127.0.0.1:${API_PORT}` for
the Antigravity hook. Keep the backend bound to loopback; this setup does not
expose the selected port to the LAN. Remove the mapping when finished:

```sh
API_PORT=8004 # must match the backend port and USB reverse mapping above
ANDROID_SERIAL="<serial shown above>"
adb -s "$ANDROID_SERIAL" reverse --remove "tcp:${API_PORT}"
```

This path works only while the Mac backend and USB debugging connection are
available. It supports local device verification, not away-from-Mac use or
production reachability. Those require a deployed HTTPS API reachable by both
the phone and host, plus production notification and host-availability setup.

For the isolated E2E workspace, remove using the same explicit config home:

```sh
python3 /ABSOLUTE/PATH/integrations/antigravity/setup_workspace.py \
  --workspace "$E2E_ROOT/workspace" \
  --config-home "$E2E_ROOT/config-home" \
  --remove
```

The generated Antigravity hook timeout is 960 seconds (15 minute maximum wait
plus request margin).
The hook itself denies after `AIXION_APPROVAL_TIMEOUT_SECONDS` (default 600,
maximum 900). Keep the Antigravity hook timeout longer than that value.
If approval creation has an ambiguous network/server outcome, the hook retries
the exact same invocation once after 250 ms. Each create attempt has a
10-second timeout; if the retry is also ambiguous, the hook denies and emits a
generic diagnostic to stderr. The server's invocation idempotency prevents a
duplicate approval when the first request committed. During the decision wait,
each poll's HTTP request deadline and sleep are capped to the time remaining in
`AIXION_APPROVAL_TIMEOUT_SECONDS`; late responses are denied. The shared
standard-library HTTP transport enforces one monotonic deadline across hostname
resolution, connection, TLS, response headers, and the complete bounded response
body. It caps responses at 256 KiB and does not follow redirects. Its DNS
resolver uses at most two daemon workers. Python cannot stop an OS
`getaddrinfo` call that never returns, so such a call retains its worker; if both
workers become stuck, new hostname requests fail closed until capacity recovers.
IP-literal endpoints do not use DNS. At the approval deadline, the hook makes
one best-effort expiry request within the one-second cleanup grace, then denies
if expiry cannot be confirmed. Approval creation happens before the decision-
wait budget starts. Consume is capped to the remaining wait, and the hook denies
if the consume response arrives at or after the decision deadline (the claim
may then be consumed without the provider action running). An ambiguous consume
outcome is reported only within the remaining cleanup grace; the report is
skipped after that grace expires. Loopback tests cover slow headers, slow bodies,
bounded stalled DNS, response limits, and no redirect following. The backend
also assigns its own expiry when the request is created. Late mobile decisions
against expired approvals are rejected with HTTP 409.
The backend's `AIXION_AGENT_APPROVAL_TTL_SECONDS` (default 600, range 30–900)
is a server-side backstop for an agent process that disappears before it can
close its approval. When the hook reaches its own timeout, it attempts to expire
its still-pending approval within the one-second cleanup grace. Late mobile
decisions against expired approvals are rejected with HTTP 409.

## Rollback

Disable/remove only this workspace's `.agents/hooks.json` entry in Antigravity
settings. The application can be restarted without this hook. Revoke the
external agent credential from Control Tower if it is no longer needed.

## Current limitations

- Antigravity invokes synchronous hooks, so the host process remains blocked
  while a person responds. This is expected for the required same-session
  resume behavior.
- Tool argument structure and decision vocabulary are version-specific. Verify
  them against the installed Antigravity release before broad rollout.
- The workspace hook setup and connector registration can be validated
  locally, but a passing setup check does not prove a live phone-to-host
  approval. Certify the exact installed
  Antigravity version, workspace, phone tap, and resumed tool call before
  describing the integration as production-ready.
