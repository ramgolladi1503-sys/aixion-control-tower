# Native Antigravity Approval Connector

This integration keeps the customer inside the native Google Antigravity application.
It does **not** launch a replacement agent, task window, SDK conversation, or managed
Antigravity process.

```text
Native Antigravity conversation
-> documented PreToolUse hook
-> local Aixion hook executable
-> Aixion relay trust/action API
-> Aixion Android approval
-> allow or deny JSON returned to the same hook invocation
-> the original Antigravity conversation continues or is blocked
```

Google's current hook contract is documented at:

```text
https://antigravity.google/docs/hooks
```

The official `PreToolUse` payload uses camelCase fields:

```text
toolCall.name
toolCall.args
conversationId
workspacePaths
stepIdx
transcriptPath
artifactDirectoryPath
```

The hook must return one of Antigravity's documented decisions:

```text
allow
deny
ask
force_ask
```

Aixion returns only `allow` or `deny` for exact mobile decisions.

## Product boundary

The customer continues to type prompts and inspect work in native Antigravity.
Aixion owns only the approval relay and audit boundary.

The local hook and relay are background integration components comparable to a browser
extension or credential helper. They have no task launcher, prompt box, or agent UI.
The relay may run as a macOS LaunchAgent, but it never starts a second Antigravity
application or conversation.

## Current proof scope

The first certification uses an existing Aixion relay host and one existing relay
session mapped to one workspace. This proves native-session gating without creating a
second Antigravity instance.

Automatic creation/attachment of an Aixion session from each Antigravity
`conversationId` is the next product-hardening step after physical approve and reject
are proven.

## Prerequisites

1. The native Antigravity app is installed.
2. The Aixion backend and Android app are connected to the same customer account.
3. `aixion-relay` is installed and registered once on the Mac.
4. An ANTIGRAVITY relay session exists for the disposable certification workspace.
5. The relay token remains in the operating-system secret store. It is never copied
   into Antigravity configuration or a LaunchAgent plist.

## Invisible macOS connector service

After one-time relay registration, install the headless connector:

```bash
aixion-relay service install
```

Verify it without opening another agent window:

```bash
aixion-relay service status
```

The service definition:

- runs `aixion-relay run` as a background-only LaunchAgent;
- starts automatically at login;
- restarts after failure or network recovery;
- points only to the non-secret relay config path;
- retrieves the relay token from the operating-system secret store at runtime;
- writes operational logs under `~/Library/Logs/Aixion/`;
- does not launch or control Antigravity.

The final customer installer should perform these steps behind a one-time Connect
Antigravity flow. Terminal commands are certification scaffolding, not the intended
customer experience.

## One-time native hook configuration

Run the configurator from a normal macOS terminal:

```bash
python3 integrations/antigravity/configure_native_hook.py \
  --workspace /absolute/path/to/disposable-workspace \
  --session-id relay_session_xxx \
  --relay-executable /absolute/path/to/aixion-relay
```

It updates:

```text
~/.gemini/config/hooks.json
~/.config/aixion/antigravity-hook.json
```

The Antigravity file uses the documented named-hook schema. The Aixion file maps
workspace roots to non-secret relay session IDs and is written atomically with mode
`0600`.

The configurator preserves unrelated hooks already present in `hooks.json`.

Restart Antigravity after changing global hooks.

## Side-effect tools gated in v1

```text
run_command
write_to_file
replace_file_content
multi_replace_file_content
manage_task
schedule
ask_permission
invoke_subagent
define_subagent
send_message
manage_subagents
generate_image
```

Read-only file listing, searching and viewing are not routed to the phone by this
strict-side-effects matcher.

## Security behavior

The hook:

- reads the documented JSON payload from stdin;
- bounds input and output sizes;
- executes an argv array with `shell=False`;
- resolves the workspace using a local mapping;
- reads the relay credential only through `aixion-relay` and its OS secret store;
- sends the exact command or target paths plus a canonical payload hash;
- does not copy source-code bodies into generic cloud metadata;
- waits for a bounded mobile decision;
- emits a valid `deny` JSON decision for every integration failure;
- exits zero after a valid allow or deny so Antigravity consumes the decision;
- never clicks or automates Antigravity's local approval UI.

## Disposable physical certification

Use a disposable repository and one harmless command whose output file does not exist.

### Approve path

1. Confirm the target file is absent.
2. Prompt native Antigravity to run the exact harmless command.
3. Confirm the command does not execute before the phone decision.
4. Confirm exactly one approval appears in Aixion Android.
5. Compare command, cwd, provider payload hash, conversation ID and step index.
6. Tap Approve in Aixion Android.
7. Confirm the same native Antigravity conversation continues.
8. Confirm the target is created exactly once.

### Reject path

1. Use a fresh target file and fresh tool call.
2. Confirm the file is absent.
3. Tap Reject in Aixion Android.
4. Confirm Antigravity receives `deny`.
5. Confirm the file remains absent.

### Failure gates

Also test:

```text
backend unavailable -> deny
relay executable unavailable -> deny
unpaired workspace -> deny
approval timeout -> deny
malformed hook payload -> deny
oversized hook payload -> deny
replayed or stale mobile decision -> rejected by backend
```

## Required certification verdicts

Before the physical phone test completes:

```text
NATIVE_ANTIGRAVITY_HOOK_IMPLEMENTED_NOT_CERTIFIED
```

After approve, reject, timeout, backend-loss and exact-once evidence all pass:

```text
NATIVE_ANTIGRAVITY_MOBILE_APPROVAL_CERTIFIED_DISPOSABLE_REPOSITORY
```

Do not claim Codex Desktop or Claude integration from this result. Each provider needs
its own supported native lifecycle interface and independent certification.
