# Codex phone approval hook

This integration connects the existing Aixion Android approval console to a
Codex **local** `PermissionRequest` hook. When Codex is already about to show a
permission prompt, the synchronous hook creates a workspace-scoped approval,
waits for the phone decision, and returns Codex's documented `allow` or
`deny` response for that same paused request. It does not intercept tool calls
that Codex considers safe enough to run without a permission prompt.

The event schema and decision response follow the [official Codex hooks
documentation](https://learn.chatgpt.com/docs/hooks), including its
`PermissionRequest` event and `hookSpecificOutput.decision.behavior` values.

The hook uses only `PermissionRequest`. Do not change it to `PreToolUse`: the
Codex hook documentation says a failed or timed-out `PreToolUse` callback can
fail without blocking the tool. This adapter emits an explicit `deny` on
configuration errors, malformed input, API errors, payload changes, revoked
credentials, and timeout.

## Supported scope and limits

- Initial host support is macOS with local Codex and a token in macOS Keychain.
- The API URL must be HTTPS except for loopback HTTP during local development.
- The Codex project must be trusted. Codex requires review and trust of this
  non-managed hook before it runs; setup does not bypass that prompt.
- The Codex process and this hook must remain running and connected while the
  phone decision is pending. Cloud delivery cannot resume a sleeping,
  disconnected, or terminated host process.
- Codex's documented `PermissionRequest` payload includes `session_id` and
  `turn_id`, but does not list `tool_use_id`. If Codex supplies a valid
  `tool_use_id`, the hook uses it for restart-stable identity. Otherwise it
  creates a cryptographically random ID for that synchronous hook callback and
  keeps it through the create, wait, and single-use consume sequence. Repeated
  identical calls in one turn therefore get different IDs. If the hook process
  or Codex restarts, the new callback gets a new ID and cannot inherit an old
  approval; the old request can only expire or remain unconsumed. This fallback
  preserves fail-closed binding but does not provide restart recovery for an
  event whose callback process was lost. The installed app's actual input shape
  and callback behavior still require device/app verification.
- This is a per-project integration. It does not alter global Codex settings
  or take over existing conversations that do not load this project's hook.
- This hook handles Codex's local permission prompts. Codex-hosted tools that
  do not pass through local hooks are outside its coverage.
- The token needs only `CREATE_APPROVAL`, `READ_APPROVAL_DECISION`,
  `CONSUME_APPROVAL_DECISION`, `REPORT_APPROVAL_DELIVERY_FAILURE`, and
  `EXPIRE_OWN_APPROVAL`, and must be a
  `CODEX` external agent limited to one project. Server-side limits and expiry
  remain authoritative.
- Before transmitting tool arguments, the hook rejects known credential-bearing
  field names, recognizable secret values, and common CLI options such as
  `--password`, `--with-token`, and `-p`. This is a conservative heuristic, not
  general secret detection: arbitrary credentials embedded in free-form command
  text may not be recognized. Do not put credentials in approval-gated command
  arguments; use the tool's supported secret store or interactive prompt.

## Install

1. In the Control Tower app, register a `CODEX` external agent scoped to the
   intended project with only the five actions listed above. Save its token
   in macOS Keychain as a generic password using service
   `com.aixion.controltower.agent` and account equal to the agent ID. Do not
   enable Keychain access for all applications.
2. From the repository root, install the workspace hook. The command writes a
   protected non-secret config under `~/.config/aixion/workspaces/` and merges
   only the `PermissionRequest` entry in `<project>/.codex/hooks.json`:

   ```sh
   python3 integrations/codex/setup_workspace.py \
     --workspace "/absolute/path/to/project" \
     --api-base-url "https://control.example.com" \
     --agent-id "agent_..." \
     --project-id "project_..."
   ```

   If the project already has `.codex/hooks.json`, the installer requires the
   file to be owned by the current user and not writable by group or other
   users. It refuses installation without changing either the hook file or
   managed config when those write bits are present. Review the file's sharing
   requirements, then remove only the shared write bits before retrying:

   ```sh
   chmod go-w "/absolute/path/to/project/.codex/hooks.json"
   ```

3. Check connectivity and Keychain access. Preflight calls unauthenticated
   `/health` without the token, then authenticates `GET /agents/identity` to
   verify the registered CODEX installation, project scope, and required
   approval capabilities. The endpoint returns no credential. Production also
   requires a workspace-bound registration:

   ```sh
   python3 integrations/codex/aixion_permission_request_hook.py \
     --config "/Users/you/.config/aixion/workspaces/codex-<workspace-id>.json" \
     --check
   ```

4. Open the project in Codex, review and trust the hook through Codex's hook
   review UI/`/hooks`, and verify Codex lists the `PermissionRequest` hook.
   Use an action that normally produces a Codex permission prompt. Confirm the
   exact tool and arguments appear on the phone, then test approve and reject.
   Also test a network interruption and timeout; neither may execute the
   original tool.

To remove this integration, run `setup_workspace.py --workspace
"/absolute/path/to/project" --remove`. It removes only its own managed entry
and config, preserving other Codex hooks. Revoke the external-agent token in
Control Tower if it is no longer needed.

## Configuration and validation

The protected JSON config contains `api_base_url`, `agent_id`, and
`project_id`; it contains no secret. The optional timeout environment variable
is `AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS` (default 600, range 1–900 seconds).
During the wait, the hook honors API `Retry-After` responses and retries
ambiguous decision `GET` failures with exponential delays of 2, 4, then at most
10 seconds, bounded by the configured deadline. Each decision read and consume
request uses the remaining approval time and the 10-second per-request maximum
as one monotonic HTTP deadline. The shared standard-library transport applies it
to DNS resolution, connection, TLS, response headers, and the complete bounded
response body; it caps responses at 256 KiB and does not follow redirects. The
hook refreshes the invocation-scoped liveness lease every 15 seconds during a
long pending wait. The response must confirm the same approval, connector
installation, and invocation with `live: true`; an expired, mismatched,
malformed, unauthorized, or timed-out heartbeat denies before consume. The
lease never extends the approval expiry, and the server checks it again when
the connector claims an approved decision. Credential `last_used_at` is not
treated as liveness. The heartbeat uses the same remaining-time and per-request
HTTP deadline bounds as the decision wait. The
hook never returns `allow` if the consume response arrives at or after that
deadline. After timeout, it makes one best-effort expiry request within a
1-second cleanup grace; a consume-failure report is skipped when no approval
time remains. This cleanup can extend the hook by up to the bounded expiry
request. DNS uses at most two daemon workers. Python cannot stop an OS
`getaddrinfo` call that never returns, so a stuck call retains its worker; if
both workers become stuck, new hostname requests fail closed until capacity
recovers. IP-literal endpoints do not use DNS. Loopback tests cover slow headers,
slow bodies, bounded stalled DNS, response limits, and no redirect following.

For an ambiguous approval creation outcome, the hook retries the exact same
callback identity and body once after 250 ms; each create request has a
10-second timeout. If both outcomes are ambiguous, it denies and emits a generic
diagnostic to stderr. Invocation idempotency prevents a duplicate approval when
the first request committed. It never retries consume `POST`s after ambiguous
outcomes. The hook's Codex command timeout is 960 seconds so the API wait can
expire and return an explicit denial. Tokens are read from macOS Keychain or a protected owner-only file
(`AIXION_AGENT_TOKEN_FILE`). Process-environment tokens are disabled by
default; local tests must set both `AIXION_AGENT_TOKEN` and
`AIXION_ALLOW_AGENT_TOKEN_ENV=true`. Do not enable process-environment tokens
for a live host integration.

Unit tests mock the Control Tower API and verify protocol/decision behavior,
including callback-scoped identity when `tool_use_id` is absent. They do not
certify the installed Codex app. Certification requires a real Codex local
permission request and the same task resuming after the phone tap.
