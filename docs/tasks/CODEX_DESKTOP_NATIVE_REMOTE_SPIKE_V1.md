# Task: Codex Desktop Native Remote Integration Spike v1

## Repository and branch

- Repository: `ramgolladi1503-sys/aixion-control-tower`
- Branch: `spike/codex-desktop-native-remote-v1`
- Base: `feature/local-codex-session-supervision-v1`
- Parent draft PRs: `#190`, `#191`
- Do not merge or deploy.

## Why this spike exists

OpenAI now provides a first-party Codex desktop-to-mobile remote workflow. Supported desktop Codex chats can appear in the ChatGPT mobile app Remote tab, including active state, approvals, terminal output, diffs, tests, and project context while files and credentials remain on the Mac.

Therefore, Aixion must not build an unsupported duplicate approval channel for Codex Desktop unless a documented extension point exists.

The primary question is no longer:

> How can Aixion hijack or mirror a Codex Desktop approval?

It is:

> How should Aixion integrate with the supported Codex Desktop + ChatGPT mobile Remote workflow while preserving Aixion's cross-agent governance, evidence, and universal control-console value?

## Product correction

### Codex-specific baseline

For normal Codex use, the preferred user experience is:

```text
Codex Desktop on Mac
→ user starts task in TradeBot or another project
→ user leaves the Mac
→ ChatGPT mobile Remote tab shows the same Codex thread
→ user reviews output and resolves native Codex approvals
→ same desktop thread continues
```

Aixion must not replace this with terminal scraping, AppleScript, UI automation, synthetic keyboard input, SSH exposure, or a second Codex app-server session.

### Aixion value after the correction

Aixion remains the universal control and evidence plane for:

- Antigravity;
- OpenClaw;
- Claude or other agents with supported APIs/hooks;
- custom local agents;
- normalized approval policy;
- cross-agent audit history;
- durable evidence chains;
- organizational approval rules;
- consolidated status across machines and providers;
- agents that do not have a first-party mobile remote experience.

For Codex, Aixion should prefer a supported integration/overlay rather than becoming the primary approval transport.

## Required spike lanes

### Lane A — Verify first-party Codex Remote end to end

Using the latest supported Codex/ChatGPT desktop app and ChatGPT mobile app:

1. Open a disposable local repository in Codex Desktop.
2. Set a safe approval configuration that requires at least one harmless command approval.
3. Start a task from Codex Desktop.
4. Open the ChatGPT mobile app Remote tab.
5. Confirm the same desktop thread appears.
6. Confirm native project/thread state is visible.
7. Trigger a harmless approval request.
8. Verify the command has not executed before approval.
9. Approve from mobile.
10. Verify the same desktop thread/turn continues and the command executes exactly once.
11. Repeat with Reject.
12. Record app versions, thread ID if exposed, timestamps, command, file-before/file-after evidence, and screenshots.

This lane determines whether the user's original Codex-specific pain is already solved by the official product.

### Lane B — Supported integration surface discovery

Inspect only documented/supported Codex interfaces:

- Codex app-server JSON-RPC;
- app-server Unix control socket;
- thread list/read/resume APIs;
- hooks;
- configuration and approval reviewer options;
- official remote/mobile behavior;
- any documented programmatic access or enterprise integration surface.

Answer these questions with evidence:

1. Can an external local client enumerate desktop-owned active threads without taking ownership?
2. Can it subscribe read-only to desktop-owned events?
3. Can it resolve an approval for a desktop-owned turn through a documented API?
4. Can multiple clients safely observe the same thread?
5. Is there a documented compare-and-set or first-response-wins approval contract?
6. Can an external client register as an approval reviewer for a desktop-owned session?
7. Can hooks provide enough information for Aixion auditing without controlling the approval?
8. Are programmatic access tokens restricted to Business/Enterprise or otherwise unavailable to the current user plan?

Do not infer support from the mere existence of a socket. Prove each capability with a documented schema or a disposable live test.

### Lane C — Safe Aixion integration options

Rank these options using evidence:

#### Option 1 — Native Remote delegation

Codex approvals stay entirely in OpenAI's first-party desktop/mobile remote path.

Aixion shows:

- machine online/offline;
- Codex project/thread presence if available through supported metadata;
- a status card saying approvals are handled in ChatGPT Remote;
- optional deep-link/navigation guidance;
- independent Aixion audit events only when obtainable through supported hooks or APIs.

#### Option 2 — Read-only Codex observability overlay

Aixion ingests supported Codex lifecycle events but does not resolve approvals.

Required properties:

- no second provider session;
- no duplicate responses;
- no credentials copied to the phone;
- no private socket exposed over the network;
- clear separation between OpenAI-native approvals and Aixion evidence.

#### Option 3 — Enterprise programmatic integration

Use an official enterprise/programmatic access surface if available and eligible.

Do not assume the current Plus plan provides it.

#### Option 4 — Aixion-owned Codex client

Keep `aixion-relay codex` only as a fallback for users who explicitly choose an Aixion-owned session rather than Codex Desktop.

This is not the default desktop UX.

#### Rejected option — desktop UI hijacking

Reject any design based on:

- AppleScript approval clicks;
- Accessibility API click injection;
- screenshot/OCR approval extraction;
- terminal scraping;
- process memory inspection;
- undocumented socket mutation;
- monkey-patching the signed Codex application;
- intercepting or replaying credentials.

## Required code outcomes

Do not start broad implementation before the spike verdict.

Permitted code changes during the spike:

1. A read-only diagnostic command, for example:

   ```bash
   aixion-relay codex-desktop doctor
   ```

   It may report:

   - supported desktop app detected;
   - Codex CLI/app-server version;
   - control socket present/absent;
   - official Remote prerequisites;
   - whether documented read-only integration is available;
   - whether Aixion Codex approval control is unsupported.

2. Android copy/state changes that clearly distinguish:

   - `OPENAI_NATIVE_REMOTE`;
   - `AIXION_OWNED_SESSION`;
   - `OBSERVABILITY_ONLY`;
   - `UNSUPPORTED`.

3. Documentation and tests for the capability matrix.

Do not add a misleading Android Approve button for a desktop-owned Codex approval unless a documented supported resolver is proven.

## Capability matrix

Create `docs/research/codex_desktop_native_remote_spike_v1_results.md` containing:

| Capability | Officially documented | Live proven | Available on current plan | Aixion action |
|---|---:|---:|---:|---|
| Same desktop thread visible on mobile | | | | |
| Native mobile command approval | | | | |
| Native mobile reject | | | | |
| Desktop active-thread enumeration | | | | |
| Read-only external event subscription | | | | |
| External approval resolution | | | | |
| Multi-client first-response-wins | | | | |
| Hooks for audit/evidence | | | | |
| Programmatic token access | | | | |

Every `yes` must cite either official documentation or recorded disposable live evidence.

## Decision rules

### Verdict: USE_OPENAI_NATIVE_REMOTE

Use when the first-party Remote tab solves the original Codex-specific use case and no supported third-party approval delegation exists.

Then:

- stop building Codex Desktop approval interception;
- keep PR #191 as a non-default Aixion-owned-session foundation;
- update Aixion Android to direct Codex users to the native Remote workflow;
- focus Aixion approval innovation on other providers and cross-agent governance.

### Verdict: SUPPORTED_OBSERVABILITY_ONLY

Use when Aixion can safely observe desktop sessions but cannot resolve approvals.

### Verdict: SUPPORTED_NATIVE_AIXION_REVIEWER

Use only when a documented supported external reviewer/resolver works on a desktop-owned thread with no second session and exact-once resolution.

### Verdict: BLOCKED_UNSUPPORTED

Use when the required integration depends on undocumented or unsafe mechanisms.

## Required validation

At minimum:

- official documentation references captured with access date;
- disposable desktop-to-mobile approve smoke;
- disposable reject smoke;
- no second session/thread created;
- no file change before approval;
- exactly one file change after approval;
- diagnostic command tests if code is added;
- backend/relay/Android focused tests for touched files;
- full regression only if implementation moves beyond documentation/diagnostics.

## Safety boundary

- Do not use TradeBot for the spike.
- Do not merge or deploy.
- Do not expose local sockets or ports publicly.
- Do not copy OpenAI credentials into Aixion.
- Do not claim Aixion controls Codex Desktop approvals unless supported and live-proven.
- Do not market a feature already provided natively by OpenAI as an Aixion invention.

## Final deliverable

Return:

- verdict;
- exact official capabilities found;
- disposable live evidence;
- limitations by subscription plan;
- recommended Aixion product positioning;
- changed files;
- test totals;
- branch SHA;
- draft PR number;
- whether PR #191 should continue, narrow, or be closed.
