# Codex phone approval hook

This integration connects the existing Aixion Android approval console to a
Codex **local** `PermissionRequest` hook. When Codex is already about to show a
permission prompt, the synchronous hook creates a workspace-scoped approval,
waits for the phone decision, and returns Codex's documented `allow` or
`deny` response for that same paused request. It does not intercept tool calls
that Codex considers safe enough to run without a permission prompt.

The event schema and decision response follow the [official Codex hooks
documentation](https://developers.openai.com/codex/hooks/), including its
`PermissionRequest` event and `hookSpecificOutput.decision.behavior` values.
The connector rejects duplicate JSON object keys in both the hook event and
Control Tower responses, so request and decision fields cannot depend on a
parser's duplicate-key handling.

The hook uses only `PermissionRequest`. Do not change it to `PreToolUse`: the
Codex hook documentation says a failed or timed-out `PreToolUse` callback can
fail without blocking the tool. This adapter emits an explicit `deny` on
configuration errors, malformed input, API errors, payload changes, revoked
credentials, and timeout.

## Install target versus verified runtime

The installer writes a project-local Codex hook definition to
`<project>/.codex/hooks.json`; it does not connect to Codex App Server, attach
to an existing conversation, or take over a running session. Automated tests
launch the hook command with synthetic `PermissionRequest` JSON and exercise
the Control Tower API. They do not launch Codex CLI or the native Codex
desktop app. A separate manual local runtime checkpoint is recorded in
[`docs/CODEX_CLI_ANDROID_E2E_20261029.md`](../../docs/CODEX_CLI_ANDROID_E2E_20261029.md):
Codex CLI `0.163.0-alpha.11.2` sent a real `PermissionRequest` through the
hook to Android emulator `5556`; the approved command executed and a phone
denial prevented its command marker from being created. The exact final hook
denial message was not captured. This is narrow CLI runtime evidence, not
production certification; live timeout, API failure, stale-payload,
credential-revocation, and restart cases remain open. Codex Desktop phone
approval is unverified until an installed desktop session produces the real
event and the same native task is observed continuing after approval (and
stopping after denial). Reviewing or trusting the hook in Codex only confirms
that the configuration is accepted; it is not runtime-flow evidence.

## Supported scope and limits

- The adapter implementation targets macOS and Windows Codex CLI. macOS uses
  Keychain; Windows uses the current user's Windows Credential Manager and
  protected NTFS ACLs. Windows implementation support is conditional on the
  `Codex Windows validation` CI job passing for the exact revision; this is
  not native Codex runtime certification.
- A real Windows Codex same-invocation phone approval and denial remains an
  explicit certification gate. Do not infer Windows runtime behavior from the
  Windows CI tests or the macOS Codex CLI round trip.
- The Windows installer rejects UNC paths, reparse points, null or unknown
  DACLs, and access granted to broad or unrecognized principals. It accepts
  only the current user, LocalSystem, and privileged Builtin Administrators
  on protected files/directories. Shared project `.codex` directories may need
  an owner-controlled directory; unsafe ACLs are never silently rewritten.
- The API URL must be HTTPS except for loopback HTTP during local development.
- The Codex project must be trusted. Codex requires review and trust of this
  non-managed hook before it runs; setup does not bypass that prompt.
- The Codex process and this hook must remain running and connected while the
  phone decision is pending. Cloud delivery cannot resume a sleeping,
  disconnected, or terminated host process.
- The current public hook documentation lists `session_id`, `cwd`,
  `permission_mode`, `turn_id`, `tool_name`, and `tool_input` for
  `PermissionRequest`; it does **not** list `tool_use_id` for this event. The
  same documentation lists `tool_use_id` for `PreToolUse` and `PostToolUse`,
  which are different events and do not substitute for this adapter's
  permission decision. For the documented `PermissionRequest` shape, the hook
  creates a cryptographically random invocation ID for that synchronous
  callback and keeps it through approval creation, decision wait, and the
  single-use consume sequence. Repeated identical requests in one turn get
  different IDs. The adapter tolerates a non-empty, bounded `tool_use_id` if a
  Codex build supplies it, but this is an undocumented compatibility extension
  and is not relied on by the integration. If the hook process or Codex
  restarts, the new callback gets a new ID and cannot inherit an old approval;
  the old request can only expire or remain unconsumed. This preserves
  fail-closed binding but does not provide restart recovery for a callback
  whose process was lost. The installed app's actual input shape and callback
  behavior still require native runtime verification.
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
  field names, recognizable secret values, common CLI options such as
  `--password`, `--with-token`, MySQL `-p`, and `sshpass -p`, and curl Basic Auth
  forms such as `-u user:password`, `--user user:password`, URL user-info, and
  an `Authorization: Basic` header. This is a finite, conservative heuristic,
  not general secret detection: arbitrary credentials embedded in free-form
  command text may not be recognized. Do not put credentials in approval-gated
  command arguments; use the tool's supported secret store or interactive
  prompt.

## Install

1. In the Control Tower app, register a `CODEX` external agent scoped to the
   intended project with only the five actions listed above. Save its token in
   the host credential store under service `com.aixion.controltower.agent` and
   account equal to the agent ID. On macOS, use a generic password in Keychain
   and do not enable access for all applications. On Windows, use the hidden
   prompt below to store the token in Windows Credential Manager; the token is
   not placed in command-line arguments, Codex config, or a plaintext file:

   ```powershell
   py -3 integrations/codex/setup_workspace.py --store-token --agent-id "agent_..."
   ```

   To remove the Windows credential, run
   `py -3 integrations/codex/setup_workspace.py --remove-token --agent-id "agent_..."`.
2. From the repository root, install the workspace hook. The command writes a
   protected non-secret config under the host's Aixion config directory and
   merges only the `PermissionRequest` entry in `<project>/.codex/hooks.json`.
   macOS uses `~/.config/aixion/workspaces/`; Windows uses
   `%LOCALAPPDATA%\Aixion\ControlTower\Codex\workspaces\`:

   ```sh
   python3 integrations/codex/setup_workspace.py \
     --workspace "/absolute/path/to/project" \
     --api-base-url "https://control.example.com" \
     --agent-id "agent_..." \
     --project-id "project_..."
   ```

   On Windows, run the equivalent command from PowerShell:

   ```powershell
   py -3 integrations/codex/setup_workspace.py `
     --workspace "C:\Users\you\source\Aixion Project" `
     --api-base-url "https://control.example.com" `
     --agent-id "agent_..." `
     --project-id "project_..."
   ```

   Windows stores non-secret workspace config under
   `%LOCALAPPDATA%\Aixion\ControlTower\Codex\workspaces\`. The generated
   hook entry includes Codex's Windows-only `commandWindows` override with
   Windows argument quoting for interpreter, script, and config paths that
   contain spaces. New app-owned directories/files get explicit protected
   DACLs; installation fails closed when existing paths cannot be validated.

   Existing installations must rerun the same command after upgrading. The
   installer adds the canonical workspace path to the protected config and
   preserves the existing hook and host credential (macOS Keychain or Windows
   Credential Manager). No credential reset is needed. Until this metadata is
   present, the hook fails closed.

   If the project already has `.codex/hooks.json`, the installer requires the
   file to be owned by the current user and not writable by group or other
   users. It refuses installation without changing either the hook file or
   managed config when those write bits are present. Review the file's sharing
   requirements, then remove only the shared write bits before retrying:

   ```sh
   chmod go-w "/absolute/path/to/project/.codex/hooks.json"
   ```

3. Check connectivity and host credential-store access. Preflight calls unauthenticated
   `/health` without the token, then authenticates `GET /agents/identity` to
   verify the registered CODEX installation, project scope, and required
   approval capabilities. The endpoint returns no credential. Production also
   requires a workspace-bound registration:

   ```sh
   python3 integrations/codex/aixion_permission_request_hook.py \
     --config "/Users/you/.config/aixion/workspaces/codex-<workspace-id>.json" \
     --check
   ```

4. In the Codex runtime you intend to use, review and trust the hook through
   Codex's hook review UI or `/hooks` in the CLI, and confirm it lists the
   `PermissionRequest` hook. This verifies configuration only. The dated
   checkpoint above demonstrates one local Codex CLI approval and denial on
   an emulator; repeat the exact-action check for your installation and verify
   network interruption and timeout fail closed before relying on it. Codex
   Desktop has no verified runtime flow here and must not be treated as
   supported based on the CLI result.

To remove this integration, run `setup_workspace.py --workspace
"/absolute/path/to/project" --remove` (or the equivalent quoted Windows
PowerShell command). It removes only its own managed entry and config,
preserving other Codex hooks. Remove the Windows Credential Manager token with
`--remove-token`, and revoke the external-agent token in Control Tower if it is
no longer needed.

## Configuration and validation

The protected JSON config contains `api_base_url`, `agent_id`, `project_id`,
and the canonical `workspace_path`; it contains no secret. The hook requires
Codex's documented `cwd` field and permits the workspace root or a real
subdirectory after symlink resolution. The optional timeout environment variable
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
expire and return an explicit denial. Tokens are read from macOS Keychain,
Windows Credential Manager, or an explicitly configured protected owner-only file
(`AIXION_AGENT_TOKEN_FILE`). Process-environment tokens are disabled by
default; local tests must set both `AIXION_AGENT_TOKEN` and
`AIXION_ALLOW_AGENT_TOKEN_ENV=true`. Do not enable process-environment tokens
for a live host integration.

Unit tests mock the Control Tower API and verify protocol/decision behavior,
including callback-scoped identity when `tool_use_id` is absent. They do not
certify the installed Codex app. Certification requires a real Codex local
permission request and the same task resuming after the phone tap.
