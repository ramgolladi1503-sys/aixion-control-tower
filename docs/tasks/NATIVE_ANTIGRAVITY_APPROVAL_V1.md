# Native Antigravity Mobile Approval V1

## Objective

Prove that a customer can continue working in the native Google Antigravity application
while Aixion Android receives, approves or rejects a side-effecting tool call and the
same Antigravity conversation reacts to that decision.

No replacement agent, SDK conversation, task launcher or second Antigravity process is
allowed.

## Supported provider surface

Use only Google's documented `PreToolUse` hook contract:

```text
https://antigravity.google/docs/hooks
```

Expected input fields:

```text
toolCall.name
toolCall.args
conversationId
workspacePaths
stepIdx
transcriptPath
artifactDirectoryPath
```

Expected result:

```json
{"decision":"allow|deny","reason":"..."}
```

## Architecture under test

```text
native Antigravity
-> global PreToolUse hook
-> integrations/antigravity/aixion_pre_tool_hook.py
-> aixion-relay hook antigravity
-> backend trust/action queue
-> physical Aixion Android
-> exact decision
-> same blocked hook returns allow or deny
-> same native Antigravity conversation continues or stops
```

## Non-negotiable boundaries

- Do not launch Antigravity through Aixion.
- Do not use the Antigravity SDK to create a replacement conversation.
- Do not use Accessibility or UI automation.
- Do not copy Google credentials into Aixion.
- Do not place relay tokens in hooks.json.
- Do not expose a local agent port publicly.
- Do not approve through a backend script or Mac UI during physical certification.
- Do not modify TradeBot.
- Do not merge or deploy before certification.

## Implementation gates

### Contract correctness

- `hooks.json` uses the documented named-hook schema.
- `PreToolUse` matcher targets side-effecting tools.
- payload parser reads `toolCall.name` and `toolCall.args`.
- exact command and target paths are normalized into first-class action fields.
- canonical SHA-256 binds the complete provider payload.
- source-code bodies are not copied into generic cloud metadata.

### Fail-closed behavior

The hook returns a valid Antigravity `deny` decision for:

```text
malformed JSON
oversized input
unpaired workspace
missing relay executable
relay API failure
invalid relay response
approval timeout
inconsistent allow response with nonzero relay exit
```

A valid deny is emitted with process exit zero so Antigravity consumes the decision.

### Customer setup

The one-time configurator:

- preserves unrelated global hooks;
- writes the documented Antigravity schema;
- stores only non-secret workspace-to-session mappings;
- writes the Aixion mapping atomically with mode `0600`;
- never launches an agent.

## Physical disposable-repository certification

### Approve

1. Create a fresh disposable workspace.
2. Confirm `antigravity_phone_approve_once.txt` is absent.
3. Open that workspace in native Antigravity.
4. Request exactly this side effect:

```bash
python3 -c "from pathlib import Path; p=Path('antigravity_phone_approve_once.txt'); assert not p.exists(); p.write_text('executed-once\n', encoding='utf-8')"
```

5. Verify native Antigravity is blocked before execution.
6. Verify exactly one action appears on physical Aixion Android.
7. Compare:
   - provider `ANTIGRAVITY`;
   - tool `run_command`;
   - exact command;
   - cwd;
   - conversation ID;
   - step index;
   - canonical payload hash.
8. Tap Approve on the phone.
9. Verify the same Antigravity conversation continues.
10. Verify the file exists with exactly one line and was written once.

### Reject

1. Confirm `antigravity_phone_reject_once.txt` is absent.
2. Trigger a fresh command targeting that file.
3. Verify exactly one fresh action appears on the phone.
4. Tap Reject.
5. Verify Antigravity reports the denial in the same conversation.
6. Verify the file remains absent.

### Recovery

Certify:

- backend loss denies;
- approval timeout denies;
- reconnect does not duplicate the action;
- repeated mobile resolution is rejected;
- Android queue and backend state reconcile;
- no approval can be applied to a different payload hash.

## Evidence

Persist:

```text
Antigravity version
Aixion branch and commit
hook configuration hash
workspace mapping hash with session ID redacted
relay ID
relay session ID
conversation ID
step index
action ID
canonical payload hash
exact command
cwd
phone decision actor/device
created/decided timestamps
proof-before
proof-after
backend event/audit IDs
```

Do not persist credentials, relay tokens, device tokens, source-code bodies or full
transcripts.

## Verdicts

Before physical certification:

```text
NATIVE_ANTIGRAVITY_HOOK_IMPLEMENTED_NOT_CERTIFIED
```

On complete success:

```text
NATIVE_ANTIGRAVITY_MOBILE_APPROVAL_CERTIFIED_DISPOSABLE_REPOSITORY
```

Any failure must name the exact boundary:

```text
HOOK_NOT_LOADED
HOOK_PAYLOAD_SCHEMA_MISMATCH
WORKSPACE_SESSION_BINDING_FAILED
DESKTOP_TO_BACKEND_PUBLISH_FAILED
ANDROID_APPROVAL_DELIVERY_FAILED
MOBILE_DECISION_RESOLUTION_FAILED
NATIVE_CONVERSATION_DID_NOT_CONTINUE
EXACT_ONCE_FAILED
FAIL_CLOSED_GATE_FAILED
```
