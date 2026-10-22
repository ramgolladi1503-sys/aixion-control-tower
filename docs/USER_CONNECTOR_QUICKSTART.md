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

This setup is per project. It does not install a separate Mac app or take over
existing conversations that do not load the project's hook.

## Before you start

- Sign in to the Android app and choose the project you intend to protect.
- Have the Control Tower API URL that the Mac can reach. Use HTTPS for remote
  access; loopback HTTP is supported only for local development.
- Have the Control Tower repository checked out on the Mac and Python 3
  available. The connector scripts are installed from that checkout.
- Keep the Mac awake, the agent app open, and the project available until the
  approval finishes.

The app creates a provider-specific connector scoped to one project and grants
only approval-related actions. It displays the connector ID, project ID, and a
one-time token. Save the token immediately; do not paste it into a command,
chat, source file, or shell history. The current app's suggested macOS command
stores it in Keychain without putting the token in command arguments:

```sh
security add-generic-password -U -s com.aixion.controltower.agent -a "AGENT_ID" -w
```

Replace `AGENT_ID` with the displayed connector ID. Enter the one-time token at
the secure prompt. Do not enable Keychain access for all applications. If you
lose the token, rotate it in the app and update the host credential.

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
will invoke the hook or that the phone can receive a push notification.

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
  the hook in Codex before expecting it to run.

## Remove access

Run the matching installer with `--remove` for the same project folder. This
removes only the managed hook and its non-secret config. Revoke the connector
credential in the app when the Mac or project is no longer trusted.

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
