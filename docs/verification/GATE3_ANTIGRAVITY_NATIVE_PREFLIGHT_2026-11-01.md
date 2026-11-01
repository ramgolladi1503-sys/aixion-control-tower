# Antigravity native-workspace preflight — 2026-11-01

## Result

**The isolated native workspace is configured and its current hook preflight passes. The native phone-approval round trip remains unverified.** The earlier default-path preflight failure did not describe this workspace's explicit hook configuration.

## Verified configuration

- Current PR source head before this note: `ef5ee579c4282a093e290e0b55c1dec108ca176c`.
- Antigravity process `48258` was launched with the isolated workspace `/private/tmp/aixion-live-demo-20261031/native-antigravity-workspace` and the dedicated user-data directory under `~/Library/Application Support/AixionControlTower/antigravity-native-m1-20261029/user-data`.
- The workspace `.agents/hooks.json` installs `integrations/antigravity/aixion_pre_tool_hook.py` for `PreToolUse`, with an explicit protected config and token-file path and a 960-second hook timeout.
- The protected config selects agent `agent_db8305500733`, project `project_37b52b635a3b`, and local API `http://127.0.0.1:8012`.
- The config and token file are owned by the current user and have mode `0600`. Their contents, including the token, were not printed or changed.
- The exact hook `--check`, run with those explicit paths, passed authenticated provider/project identity validation against the local API.
- The Android debug app is signed in and freshly synced through the emulator route `10.0.2.2:8000 -> host proxy :8006 -> API :8012`; its Home screen showed no pending approval.

## Limits

The Mac was at the lock screen during this check. No prompt was sent to the native Antigravity session, no request was created, and no phone decision or same-invocation resume/stop was observed. The preflight is not an approval test. An active token shown by the app is not evidence that a native process currently holds or uses it.

The app's visible existing connector for `agent_566b961e82fd` / `project_3aaa8ecd2d6c` is a separate credential from the native workspace's `agent_db8305500733` / `project_37b52b635a3b`. No token was rotated or revoked, and no connector was created. Keep the provider/project scopes distinct during the E2E.

## Gate status and next step

Gate 3 remains **OPEN**. After the Mac is unlocked, trigger a fresh harmless tool call from this exact isolated Antigravity workspace. Confirm that the same request appears in Control Tower, decide it on the emulator, and verify the exact native invocation resumes on approval and halts on denial. Record both outcomes with request/invocation IDs and timestamps. Do not substitute a backend-created request or the hook preflight for this proof.

## Change and validation record

- Runtime code, Android state, credentials, databases, cloud, DNS, and billing: unchanged.
- Validation: explicit Antigravity hook `--check` passed; `git diff --check` passed.
- Rollout: none.
