# Antigravity approval gate

This integration pauses matching Antigravity tool calls in the host process,
creates a Control Tower approval, and returns Antigravity's `allow` decision
only after the same approval is approved on mobile and its frozen payload hash
verifies. The installed Antigravity 2.24.0 hook documentation lists `allow`,
`deny`, `ask`, and `force_ask` as valid PreToolUse decisions. A
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
  `REPORT_APPROVAL_DELIVERY_FAILURE`, and `EXPIRE_OWN_APPROVAL` actions. The hook claims an approved invocation exactly once before returning allow; a lost claim response fails closed, best-effort reports the ambiguous outcome, and still denies. The expiry
  action can close only approvals created by that same agent. Set its request
  limit to at least 60 per minute per active hook; the default two-second poll
  interval uses about 30 decision reads per minute, plus request creation and
  expiry calls. The hook honors the API `Retry-After` response while polling.
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

## Workspace installation

1. Register an Antigravity external agent in Control Tower with one project and
   only the five actions above. Store its one-time token in macOS Keychain as a
   generic password with service `com.aixion.controltower.agent` and account
   equal to the agent ID. Do not enable Keychain's “allow all applications”.
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

To roll back, run `setup_workspace.py --workspace "/absolute/path/to/project"
--remove`. It removes only this installer's hook entry and associated non-secret
config, preserving unrelated hooks. Revoke the agent token from the phone if it
is no longer needed.

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
