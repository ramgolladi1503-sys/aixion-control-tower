# Android Release-One Design Contract

**Status:** Implementation contract for the release-one approval console  
**Scope:** Existing Android app; inbox, approval detail, connection setup, account, and shared visual system  
**Product job:** Let a user recognize an exact pending request, understand its risk, approve or deny it confidently, and later inspect the recorded outcome.

## Design direction

Keep the existing warm, restrained palette and four-destination bottom navigation. Create a distinctive approval-console identity through clear typography, calm surfaces, strong action hierarchy, and precise status language. Do not use neon accents, decorative dashboards, or extra navigation destinations.

The primary visual sequence is:

1. What needs attention?
2. Which provider, project, session, and invocation sent it?
3. What exact action is requested?
4. Why is it risky, and what evidence supports that assessment?
5. What will approve or deny do?

Approval is a consequential action. The screen must make the request and its scope easier to inspect than the action buttons.

## Information architecture

### Bottom navigation

Keep exactly four destinations: **Home**, **Approvals**, **Connect**, and **Account**. Keep secondary and infrequent functions in the existing account/menu surface. Do not add a destination for audit history; expose history from Approvals.

### Approvals inbox

- Use one primary switch between **Needs review** and **History**.
- Show one prominent pending count, only when the count is greater than zero.
- Avoid repeating the same queue count, sync state, or “needs review” message in the header, filter, and each card.
- Each request card should prioritize provider, concise action summary, risk level, and age/expiry. Keep technical identifiers secondary.
- Preserve clear loading, empty, offline/error, and stale-data states. Never present a cached queue as current when refresh failed.

### Approval detail

- Put provider, project, and session/invocation context near the top.
- Show the exact requested action in a selectable, wrapped, non-truncated evidence block. Preserve command arguments and file paths; do not summarize away the payload that the user is authorizing.
- Explain the risk label in plain language and show the evidence that produced it.
- Place supporting metadata after the action and risk explanation, with technical identifiers available but visually secondary.
- Keep **Deny** and **Approve** available in a persistent bottom action area while content scrolls. The area must respect system insets and remain usable at enlarged font sizes.
- For high-risk or otherwise confirmation-gated requests, the confirmation must repeat the exact provider and invocation identity and the action summary. Do not turn confirmation into a generic “Are you sure?” prompt.
- If the request expires, is already decided, is stale, or cannot be reconciled with the server, disable both decision actions and explain the current state.

## Visual system

- Continue using the existing semantic light and dark palettes. Do not introduce one-off colors in screen files.
- Use semantic status color together with text and icon/shape cues; never communicate risk or outcome by color alone.
- Use the shared Material typography roles for page title, section title, body, label, and technical evidence. Avoid scattered screen-specific font sizes when an existing role fits.
- Keep body text scalable and long content legible. The exact action, file path, provider name, and explanation must wrap or scroll horizontally only when that preserves exact content; never silently ellipsize authorization evidence.
- Use the existing spacing scale. Reserve strong bordered surfaces for independently meaningful groups, such as the exact action and risk evidence. Use lighter grouping for secondary metadata to avoid a stack of visually identical panels.
- Use motion only to clarify refresh, submission, and state transitions. Keep it brief, avoid celebratory effects for security decisions, and respect the system reduced-motion setting.

## Interaction and accessibility requirements

- Every interactive control must have an accessible name and state. Icons that are decorative must not be announced as controls.
- Provide at least **48 dp × 48 dp** touch targets for interactive elements.
- Support Android font scaling without clipping request content, decision controls, or navigation labels.
- Maintain readable text contrast and distinguish controls/surfaces from the background.
- Announce queue refresh, decision submission, and decision result changes through appropriate Compose semantics without repeatedly announcing unchanged content.
- Do not require swipe, color perception, or animation to complete or understand any approval action.
- Keep copy short, specific, and calm. State what happened and what the user can do next.

Android platform references:
- [Android mobile accessibility design guidance](https://developer.android.com/design/ui/mobile/guides/foundations/accessibility)
- [Material 3 typography roles](https://m3.material.io/styles/typography/applying-type)

## Verification gates

Source-level tests and compilation are necessary but do not establish visual or device readiness. Before release-one UI is accepted:

1. Render Home, Approvals, Connect, Account, and Approval Detail on the supported compact phone.
2. Render the same primary screens at the supported expanded width.
3. Repeat Approval Detail with large system font size and a long command/file path.
4. Verify light and dark themes, loading/empty/error states, expired/stale requests, and high-risk confirmation.
5. Use TalkBack to review order, labels, live announcements, and decision controls.
6. Confirm no approval button remains actionable after stale, expired, or ambiguous server state.
7. Record device model, app build/commit, screen dimensions, font scale, theme, and observed results.

Until these checks run on a connected device, report visual and accessibility readiness as **UNVERIFIED**.
