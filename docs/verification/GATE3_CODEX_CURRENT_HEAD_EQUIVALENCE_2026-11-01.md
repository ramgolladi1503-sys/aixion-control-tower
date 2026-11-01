# Gate 3 Codex current-head equivalence — 2026-11-01

## Result

The October 31 Codex CLI phone approval and denial evidence remains source-equivalent to PR #204 head `853b0fbecba1d56db1bf36daa81cf4a713992411`: all three files implementing the adapter and its installation/security behavior have identical Git blob IDs at the recorded E2E source revision and this head.

This is source-equivalence evidence only. The native invocation and phone decisions were performed on October 31. The November 1 attempts below did not create a phone approval request and are not native approval evidence.

## Compared revisions and files

- Recorded native E2E source: `f254658cfca66d3d9475edca98345db13aaaa8c8`
- PR #204 head verified on November 1: `853b0fbecba1d56db1bf36daa81cf4a713992411`

| File | Blob ID at both revisions |
|---|---|
| `integrations/codex/aixion_permission_request_hook.py` | `97119a50b8d98a9bb2caee1c2a62cfcd0bebdfcc` |
| `integrations/codex/setup_workspace.py` | `63bccd1d2d1a5c9294bbbd0ab3e9c9646d35d326` |
| `integrations/codex/host_security.py` | `9b3839d1ca255dfb4c364c1945ef63afbe326d35` |

The comparison used `git rev-parse <revision>:<path>` for each file at both revisions. GitHub confirms PR #204 is open and draft at the same head.

## Native evidence carried forward

`docs/verification/CODEX_NATIVE_PHONE_E2E_20261031.md` records:

- Phone-approved request `approval_5a56c6758b8b`; the exact Codex CLI invocation resumed and created its requested marker.
- Phone-denied request `approval_18a18f965d5d`; the exact Codex CLI invocation stopped and its requested marker remained absent.

The adapter implementation blobs for those runs match PR #204 head. This carries forward the October 31 Codex CLI evidence; it does not establish a new November 1 phone round trip.

## November 1 attempts — not approval evidence

The system-installed Codex CLI `0.146.0` rejected both `gpt-6-luna` and `gpt-5.5` as unsupported for the signed-in ChatGPT account (`model_not_found`). Those sessions did not produce approval requests.

The ChatGPT-app-bundled CLI `0.163.0-alpha.11.2` accepted `gpt-6-luna`. Four isolated `mkdir` attempts were then run from the trusted Codex workspace. They completed successfully without a phone request, including attempts launched with `--ask-for-approval on-request`, `--sandbox read-only`, and `--no-daemon`:

- `/private/tmp/aixion-ct-native-codex-20261101/approved-marker`
- `/Volumes/TradeBot/workspaces/aixion-ct-native-codex-20261101/approved-marker`
- `/private/tmp/aixion-ct-native-codex-20261101-readonly/approved-marker`
- `/private/tmp/aixion-ct-native-codex-20261101-nodaemon/approved-marker`

These marker directories are intentionally left in place. They are not approval artifacts. The emulator was checked afterward and showed “You’re all caught up”; no phone decision was made for these commands. The attempts show that the runtime policy/hook path used in these invocations did not produce the required permission request. They do not prove a hook defect by themselves.

## Limits and remaining work

Gate 3 remains open until current Codex and Antigravity hook paths each produce a real phone request and the same native invocation is shown to resume after approval and stop after denial.

This evidence does not certify:

- Codex Desktop-owned sessions or Windows native execution;
- Antigravity’s current live phone round trip;
- hosted HTTPS, off-LAN reachability, push credentials, or locked-phone delivery;
- PostgreSQL production authority, full-source cutover, or backup/recovery;
- a signed Android release or Play Store acceptance.

No source database, credentials, cloud resource, billing, DNS, deployment, or PR merge was changed. The four isolated marker directories above were created by the unsuccessful November 1 attempts.
