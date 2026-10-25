# Connect Antigravity or Codex to Control Tower

This guide describes the current, workspace-scoped connector setup. It is a
developer preview: local tests and setup checks do not certify a production
service or a live phone-to-agent approval loop.

## What connecting means

The Android app is the approval console. A small hook is installed in the
supported agent's project on your Mac. The intended behavior, once the hook
runs and its live provider round trip has been verified, is to send the pending
approval to the Control Tower API, wait for the phone decision, and return that
decision to the same paused local invocation. Setup and `--check` alone do not
prove this behavior. The Mac, agent app, project, hook process, and network
connection must stay available while it waits. Cloud hosting can relay a
decision to an online Mac; it cannot wake a sleeping Mac or resume a terminated
agent task.

This setup is per project. It does not install a separate Mac app, sign in to
Antigravity or Codex for you, or take over existing conversations that do not
load the project's hook. You keep using the provider's normal signed-in desktop
app; the hook uses a separate Control Tower connector credential, not your
provider password or account token.

## Before you start

- Create or sign in to your Control Tower account in the Android app, then
  choose the project you intend to protect on Connect.
- Know the Control Tower API URL and ensure both the Android app and Mac
  connector use that same backend and account/project. Use HTTPS for remote
  access. Loopback HTTP is for local development; a phone cannot reach the
  Mac's `127.0.0.1` unless you configure the documented USB reverse tunnel.
- Have the Control Tower repository checked out on the Mac and Python 3
  available. The provider hook and its scripts run locally from that checkout
  whenever the provider invokes the configured workspace hook; there is no
  separate always-on Aixion desktop daemon.
- Keep the Mac powered, awake, connected to the API, and the provider app,
  project, and waiting hook process available until the decision returns. If
  the Mac sleeps, loses connectivity, the agent exits, or the hook is stopped,
  a phone decision cannot wake or resume that local task. The request instead
  times out or remains unresolved according to the server state; inspect it
  before starting a replacement action.

In Connect, select the provider you will use and create its connector for the
chosen project. The registration must grant only the approval actions required
by that provider's guide. The app displays the connector ID, project ID, and a
one-time token. Save the token immediately; do not paste it into a command,
chat, source file, or shell history. Store the token in macOS Keychain under
service `com.aixion.controltower.agent` and account equal to the connector ID.
The command below prompts for the secret rather than placing it in command
arguments:

```sh
security add-generic-password -U -s com.aixion.controltower.agent -a "AGENT_ID" -w
```

Replace `AGENT_ID` with the displayed connector ID. Enter the one-time token at
the secure prompt. Do not enable Keychain access for all applications. If you
lose the token, rotate it in the app and update the host credential. This
registration associates a local hook with your Control Tower account; it does
not link or import your Antigravity/Codex provider login.

## Install for one project

From the repository checkout on your Mac, run the command for your provider.
Replace the project folder, API URL, connector ID, and project ID with the
values for your setup:

```sh
python3 integrations/antigravity/setup_workspace.py \
  --workspace "/absolute/path/to/project" \
  --api-base-url "https://CONTROL-TOWER-API" \
  --agent-id "AGENT_ID" \
  --project-id "PROJECT_ID"
```

For Codex, use:

```sh
python3 integrations/codex/setup_workspace.py \
  --workspace "/absolute/path/to/project" \
  --api-base-url "https://CONTROL-TOWER-API" \
  --agent-id "AGENT_ID" \
  --project-id "PROJECT_ID"
```

Each installer adds a project-local hook and a protected non-secret config
file; it preserves other hooks and refuses conflicting managed settings. It
prints the config path. Use that path in the provider check below.

From the repository checkout, check the connector and API before using it:

```sh
python3 integrations/antigravity/aixion_pre_tool_hook.py \
  --config "/path/printed/by/installer.json" --check
```

For Codex, run:

```sh
python3 integrations/codex/aixion_permission_request_hook.py \
  --config "/path/printed/by/installer.json" --check
```

The check verifies API health and that the token belongs to the expected
provider, project, and connector. It does not prove that the native agent app
will invoke the hook, that the phone can reach the same API, or that the phone
will receive a push notification. Push wake-up and hosted HTTPS delivery are
not certified; keep the app available and confirm the request appears before
relying on an away-from-computer approval.

## Verify the same paused action

Start with a harmless action in the protected project that would cause the
provider's approval hook to run. Confirm the phone shows the expected provider,
project, action, and request details. Approve it, then verify that the exact
same waiting action continues. Repeat with a safe action that can be denied;
verify that action does not run. Do not use destructive commands as a test.

Also verify timeout and network-loss behavior. A timeout, malformed request,
changed payload, unavailable API, revoked credential, or ambiguous delivery
must leave the action blocked. If a request is stale or its outcome is
uncertain, do not retry the action as if it had been approved; inspect the
approval state and start a new invocation if needed.

Provider-specific coverage differs:

- **Antigravity:** the workspace hook checks all tools; only its explicit
  read-only allowlist passes without phone approval. Review that allowlist for
  the installed Antigravity version. Mutating and unknown tools require an
  approval.
- **Codex:** the hook handles local `PermissionRequest` prompts that Codex is
  already about to show. It does not intercept tools Codex runs without a
  permission prompt or Codex-hosted tools outside local hooks. Review and trust
  the hook in Codex before expecting it to run. This is a local Codex hook, not
  interception of every native Codex Desktop approval or hosted session.

## Remove access

To disconnect, first revoke or disable the connector credential in the Control
Tower app so the host can no longer authenticate. Then run the matching
installer with `--remove` for the same project folder. This removes only the
managed hook entry and its non-secret config, preserving unrelated hooks. If
the Mac or project is no longer trusted, remove the Keychain item as well:

```sh
security delete-generic-password -s com.aixion.controltower.agent -a "AGENT_ID"
```

Replace `AGENT_ID` with the connector ID. Revocation is the server-side access
cutoff; deleting the local hook and Keychain item cleans up the host.

## Support and current proof limits

Detailed steps and protocol limits are in the [Antigravity connector
README](../integrations/antigravity/README.md) and [Codex connector
README](../integrations/codex/README.md).

**Live support is not yet certified.** Current evidence does not establish a
fresh Antigravity phone-to-same-invocation round trip on the current revision,
or a native Codex same-invocation phone approval. The Codex hook adapter and
local tests are not proof of behavior in the installed Codex app. Push
notification wake-up, hosted HTTPS delivery, and PostgreSQL production runtime
also remain unverified. Treat a successful `--check` as connector
configuration evidence only, not production readiness.
