# Android UI/UX Production Specification

**Status:** Design contract for the production-readiness program
**Scope:** Existing Aixion Control Tower Android app
**Implementation status:** Not implemented; this document is a design brief only.

## 1. Product outcome and design principles

The app is a focused approval console for a user's coding-agent work that is already running on a computer. A user should be able to sign in, connect a supported local agent connector, receive an approval request, inspect the exact request and risk context, approve or reject it, and later inspect the recorded outcome. It must communicate that the computer, agent process, connector, network, and credentials need to remain available for work to resume.

Design principles:

1. **Review before decision.** Put the exact action, target, provider, project/workspace, invocation, risk, and expiry before decision controls.
2. **Fail closed in the interface.** Stale, expired, unsupported, mismatched, blocked, offline, uncertain, or reconciliation-required requests show a clear explanation and no enabled decision action.
3. **Acknowledge the actual delivery state.** “Decision recorded” and “Agent resumed” are separate states. Do not imply the agent resumed until the connector confirms delivery/continuation.
4. **One task, one obvious path.** Keep exactly four release-one destinations. Keep Agent Work, administration, operations, MCP, audit, and other secondary routes out of release-one navigation.
5. **Quiet, distinctive visual identity.** Use a restrained graphite, warm-paper, and mineral-olive palette with a precise editorial hierarchy; avoid neon, glowing effects, excessive gradients, and decorative dashboards.
6. **Native Android behavior.** Use Material 3 components, system insets, scalable text, adaptive navigation, TalkBack semantics, and predictable back behavior.

This brief reflects the Slice 5 release-one refinement in `docs/PRE_BILLING_WORK_LOCK.md`: Jetpack Compose, Material 3, four destinations (Home, Approvals, Connect, Account), no More menu, hidden Agent Work/admin/ops/MCP/audit routes, and real-data/fail-closed approval states.

## 2. Navigation and information architecture

### Phone navigation

Use exactly four compact-width bottom destinations, in this order:

| Destination | Label | User question answered |
|---|---|---|
| Home | Home | Is anything waiting, and are my connections healthy? |
| Approval inbox | Approvals | What needs my decision, and what did I decide? |
| Provider connections | Connect | How do I connect and verify a supported agent? |
| Account | Account | Who am I, and how do I manage this account? |

Use a real, consistent icon per destination with visible text labels; never use the first letter of the label as the icon. Show a small numeric badge only for pending approvals, capped visually at `99+`; do not use a badge for errors. A badge is supplemental and must not be the only alert channel.

`Connect` is a primary bottom destination. Home may include a prominent **Connect an agent** action and a connection status card that both navigate to Connect. Do not add a More or overflow destination menu in release one.

### Large windows

On expanded window widths, replace the bottom bar with a navigation rail and use list-detail layouts where they reduce navigation overhead (approval list beside approval detail). On compact and medium widths retain one content pane and bottom navigation. Choose based on current app window size class, including resized/foldable states, not device model. These recommendations follow Android's adaptive navigation model.

## 3. Visual system

### Unique visual direction: “Quiet Instrument Panel”

The interface should feel like a calm, trustworthy instrument panel: warm, ink-like surfaces; finely drawn borders; ample space; clear typographic contrast; and small, deliberate olive accents. Avoid the current near-black/electric terminal aesthetic. The visual distinction comes from warm neutrals and an editorial hierarchy rather than ornamental branding.

### Color tokens

Use a custom brand scheme in both light and dark modes. Do not use wallpaper dynamic color for approval, risk, or decision semantics, because those meanings must remain consistent. The following are initial design tokens; calculate and verify actual contrast ratios in implementation before release.

| Token | Light | Dark | Use |
|---|---:|---:|---|
| `canvas` | `#F4F2EC` | `#171916` | Main app background |
| `surface` | `#FBFAF6` | `#20231F` | Cards, sheets, fields |
| `surfaceRaised` | `#FFFFFF` | `#292D28` | Dialogs, selected panels |
| `surfaceSubtle` | `#EAE8E0` | `#30352F` | Secondary cards and chips |
| `ink` | `#20241F` | `#F1F0E9` | Primary text and icons |
| `inkMuted` | `#596057` | `#B7BDB2` | Supporting text |
| `inkDisabled` | `#777D73` | `#92998F` | Disabled text; never the only state cue |
| `line` | `#D4D5CC` | `#41473F` | Dividers and card boundaries |
| `brand` | `#53664D` | `#B0C1A4` | Primary action, selected destination, links |
| `onBrand` | `#FFFFFF` | `#172016` | Text/icons on brand |
| `positive` | `#286348` | `#9BD0AF` | Confirmed success only |
| `caution` | `#805600` | `#E8C66E` | Attention and approaching expiry |
| `danger` | `#A23F35` | `#F0A097` | Reject, revoke, blocked, destructive errors |
| `info` | `#3B6178` | `#9FC5D8` | Neutral information |

Neutral surfaces should occupy roughly 85–90% of the screen. Brand olive is reserved for the primary action, selected navigation, and connection CTA. Positive, caution, and danger colors are reserved for explicit semantic states; do not use them as decoration. Pair every semantic color with text/icon/shape, never color alone.

### Typography

Use Android system sans (Roboto) for regular UI and a system monospace face only for code/action payloads. Support the user's font scaling without clipping or truncating request details.

| Style | Size / line height | Weight | Use |
|---|---|---|---|
| Display | 28 / 34 sp | 600 | One primary page heading or short decision outcome |
| Title | 20 / 26 sp | 600 | Card and detail section titles |
| Body | 16 / 24 sp | 400 | All main explanatory copy and fields |
| Label | 14 / 20 sp | 500 | Metadata, buttons, navigation labels |
| Code | 14 / 21 sp | 400 | Commands, file paths, immutable action payloads |

Do not render core content below 14sp. Use sentence case and normal word spacing; reserve uppercase micro-labels for short, non-essential metadata only.

### Spacing, shape, and elevation

Use a 4dp base scale: `4, 8, 12, 16, 24, 32, 40, 48`. Screen content uses 20dp horizontal padding on compact widths, 24dp on medium widths, and a centered 720dp maximum readable column on expanded widths. Cards use 16dp inner padding and 16dp corner radius; dialogs/sheets use 24dp radius; fields and buttons use 12dp radius. Prefer one-pixel tonal borders and spacing over shadows. Use elevation only to clarify modal hierarchy.

## 4. Screen contracts

### 4.1 Sign-in and registration

Default screen is a compact sign-in form: logo/wordmark, “Sign in”, email, password with show/hide control, primary **Log in** button, and a low-emphasis **Create account** link. Place password reset in the same form. Do not show technical API endpoints, agent setup, or an expanded registration form on initial load.

Tapping **Create account** switches the same surface to a short registration state with email, password, password requirements, and **Create account**. Verification follows as a separate, clearly labeled next step. Keep login/register accessible without making users choose among competing sign-in providers. Offer Google/GitHub sign-in only after provider auth, account linking, and recovery behavior are implemented and reviewed; never render placeholder OAuth buttons.

Errors remain adjacent to the affected input or submit action. Preserve entered email after a recoverable error; clear passwords after authentication failure. Do not disclose whether an email address has an account. Loading state disables duplicate submits and labels the action “Signing in…” or “Creating account…”.

### 4.2 Home

Home is a short status summary, not an analytics dashboard:

1. Top app bar with page title and no More menu.
2. A single status header: “2 approvals waiting” or “All clear”, with a small, explicit sync state and last successful sync time.
3. Pending approvals preview: up to three compact cards, each with provider, project or target, concise action summary, risk label, and time/expiry; **Review** opens the exact request. **See all** opens Approvals.
4. Connection card: connected provider(s), connector last-seen state, and **Connect an agent** if none are active.
5. Short availability reminder only when useful: “Your computer and agent connector need to stay online for work to resume.”

If backend sync fails, show the last successful sync time and a persistent “Could not refresh” panel with Retry. Clearly mark data as stale; disable decisions for stale approvals according to repository decision policy. Never say “All clear” when freshness cannot be established.

### 4.3 Approval inbox and decision history

Top area has two labeled filters: **Needs review** and **History**. History filters chips: All, Approved, Rejected, Expired, Delivery issue. Keep `Pending` count visible without making it a separate tab. Sort pending oldest-expiry first, then creation time; label ordering in accessibility semantics.

Every card shows provider badge (Antigravity/Codex/other explicitly supported), project/workspace, readable action summary, risk and state labels, expiry/countdown where applicable, and a text affordance “Review request”. Show the status through a labeled icon and text; never color-only dots. Do not show approve/reject buttons inside list cards; decisions require opening the full details.

History entries show decision, reviewer, date/time with timezone, exact request title, and delivery outcome as separate fields (e.g., “Approved · Delivery not confirmed”). Tapping opens read-only detail. An approved decision is not proof of local agent continuation.

### 4.4 Approval detail and decision interaction

Use a clear vertical review sequence:

1. Provider and request state header, project, connector installation identity, agent/session/invocation identifiers in a “Request provenance” disclosure.
2. Exact action payload rendered in selectable, wrapping monospace text with line breaks preserved. For file changes show a safe summary and a dedicated diff view. Do not truncate, normalize, or hide meaningful arguments.
3. Target and scope (repository/project/workspace), risk level with textual rationale, creation time, expiry, and required actions/policy warnings.
4. Optional requester/comment context, if available and attributed.
5. Sticky action footer only when decision is currently valid: secondary outlined **Reject** and primary **Approve request**. On narrow screens, stack full-width buttons with reject first and approve last. Both controls at least 48dp high; 12dp separation.

For high/critical risk, require an explicit confirmation sheet that repeats the exact action summary, target and risk. User confirms **Approve this request**; the confirm action stays disabled while in flight. Reject may use a short optional reason, but cannot become a destructive swipe action. Avoid one-tap gestures and notification quick actions for approving/rejecting.

After a decision, show a terminal state and transition timeline: “Decision recorded”, server timestamp, request identity/hash short reference, and one of “Connector delivered decision”, “Delivery pending”, or “Delivery failed/unknown”. If delivery is unknown, say “We can’t confirm that the agent received this decision. Do not retry this invocation unless the connector reports it is safe.” The product API's reconciliation guidance remains authoritative.

Disable both controls for expired, consumed, blocked, unsupported/unknown, stale, mismatched, revoked-connector, or reconciliation-required requests. Pair disabled actions with the exact reason and a refresh/reconcile action if available. Do not present “request revision” as equivalent to rejection unless the backend contract retains that distinct decision state and wording.

### 4.5 Agent Work (hidden in release one)

Do not expose Agent Work as a release-one screen or navigation destination. Its route may remain in source for a later release, but the release-one app focuses on approvals and connection health. Agent task push notifications that do not identify an approval must not navigate to a hidden screen; approval notifications open the approval directly after session validation and a fresh authoritative fetch.

### 4.6 Connect an agent

First screen presents provider choices as clear cards:

- **Antigravity** — “Connect the local Antigravity approval hook.” Show support status: **Available**, **Setup required**, or **Not verified** based on shipped capability and evidence.
- **Codex** — “Connect Codex approval requests.” State the precise supported surface. If native Codex Desktop approval interception/resume is not proven, show **Not verified for native sessions** and do not provide a misleading “Connected” state for a generic MCP connector.

For each provider, use a short numbered setup (computer prerequisite → install/configure local connector/hook → authenticate this installation → verify test request). Explain that the user does not install a Mac app; the user's machine runs the agent and its local connector. Let users copy one-time credentials with explicit “Shown once” notice, visible expiry, and Rotate/Revoke action. Mask secrets after leaving the screen; never put them in screenshots, notifications, logs, or clipboard history hints.

Connection health uses explicit last heartbeat, host online/offline, connector version, and last successful request. Do not equate API registration with active local connectivity. Setup steps have exact copyable commands but commands must be labeled by OS and shell, be read-only or least-privilege where possible, and never ask users to paste secrets into support chat. Revocation requires confirmation and explains which running approvals/requests will stop or fail closed.

### 4.7 Account

Show account email, workspace (one per account), session state, notification permission status, privacy controls, and **Log out**. Keep destructive account removal behind a separate privacy section, with explicit retention consequences. Logout confirmation states that the local sign-in session ends on this device and pending approvals remain on the server under the account's retention policy. Do not use ambiguous “Exit” or back-button logout.

### 4.8 Hidden routes and later-release screens

Do not expose Agent Work, Projects, Command, Work Orders, Agent Runs, Agent Access, Audit, Diagnostics/Ops, Test Runs, MCP, webhooks, or other admin/operations routes through any release-one navigation surface. Retain source routes only where needed for later work and existing internal validation. Do not bring dormant feature routes into navigation simply because their composables exist. Approval decision history remains in the Approvals destination and is distinct from the hidden technical audit route.

## 5. Shared states and copy

| State | Required treatment | Example copy |
|---|---|---|
| Initial load | Skeleton preserving final layout; announce loading once to accessibility services | “Loading approvals…” |
| Empty inbox | Quiet illustration/icon optional; primary path to connect | “No approvals waiting. New requests will appear here.” |
| Empty history | Explain empty scope and reset filters | “No decisions match these filters.” |
| Offline | Persistent banner, show cached timestamp, retry affordance, decision controls disabled | “You’re offline. Approval status may have changed; reconnect to review.” |
| Backend error | Explain and retry; do not inject demo data | “Couldn’t load live approvals. Check your connection and retry.” |
| Stale data | Timestamp and stale label; no decision action | “Last updated 8 minutes ago. Refresh before reviewing.” |
| Unsupported response | Keep request visible for diagnostics, disable actions | “This request uses a status this app doesn’t recognize. Update the app before deciding.” |
| Expired | Show expiry time and terminal state; no action | “This request expired at 10:42 AM.” |
| Decision in progress | Disable repeated submits and show progress | “Recording your decision…” |
| Decision accepted | Confirm server record only | “Decision recorded. Waiting for connector delivery.” |
| Delivery confirmed | Distinct connector acknowledgement | “Connector received the decision.” |
| Delivery unknown | Explain ambiguity; prevent duplicate action | “Delivery is unconfirmed. Check connector status before retrying.” |

Use dates/times localized to device locale and timezone; preserve an accessible absolute date for relative times. Countdown labels should not be the sole expiry signal.

## 6. Accessibility and inclusive interaction

- Minimum interactive target is 48dp in each dimension; aim for 56dp primary decision controls.
- Meet at least WCAG 2.2 AA text contrast: 4.5:1 for normal text and 3:1 for large text/UI components as applicable. Test each token pair; do not rely on visual estimation.
- Support TalkBack, keyboard/D-pad focus, switch access, Android font scaling through 200%, and system display scaling. No clipped command text, overlapping actions, or hidden status labels.
- Give controls concise, action-oriented labels. A card's accessible name includes provider, action summary, risk, and status. Announce decision result and delivery result separately.
- Pair color with text and icon shape; do not use red/green alone. Respect “Remove animations” and reduced-motion settings.
- Ensure focus order follows visual reading order; confirmation sheet traps and restores focus; error text is announced and associated with the affected field/action.
- Code blocks support horizontal scrolling only when line wrapping changes meaning; provide select/copy controls with accessible descriptions and a non-color confirmation.

## 7. Motion and feedback

Use only short, functional transitions: 150–220ms fade/size change for list updates, 180ms sheet transitions, and subtle selection state. Do not animate risk or countdown with pulsing, flashing, or glow. Use haptics only for explicit successful/failed user-triggered decisions, respecting device vibration settings. All essential state changes remain readable without motion or haptics.

## 8. Responsive layouts

- Compact: one scrollable content column, four-tab bottom navigation, sticky safe-area-aware decision footer.
- Medium: one content column with max width 640dp, navigation rail when window class supports it.
- Expanded: max 720dp body column; approval inbox/detail can become list-detail with independently scrollable panes, persistent navigation rail, and larger table/metadata layout.
- Landscape/foldable: keep decisions within the same content pane as the request, avoid split-pane layouts where the action is visually detached from its request, and re-evaluate window class dynamically.
- Handle IME insets for forms and reason sheets; keep primary submit visible or reachable without obscuring fields.

## 9. Privacy and security UX constraints

- Notification previews default to minimal content: provider plus “Approval needed”; offer an opt-in setting for showing project and action summary on lock screen. Never include credentials or full command payload in a lock-screen notification.
- Notification taps require session validation and fetch the current authoritative request. An old/deep-linked request must show expired/revoked/consumed status and cannot bypass detail review.
- Never store an approve/reject action locally for later automatic submission. Offline review is read-only.
- Avoid clipboard copy for approval payload unless user explicitly asks; warn that copied content may be sensitive and provide a timed “Copied” message without logging payload.
- Show connector token only once. Mask it immediately after navigation/backgrounding; logging and crash reporting must redact secret-bearing fields.
- Account/workspace ownership is one workspace per account for v1; do not imply team sharing or multi-owner controls.
- Confirmation UI never overrides backend constraints: server request version/hash, expiry, connector status, and authorization remain authoritative.

## 10. Implementation acceptance criteria

1. A new user can identify sign-in and account creation without choosing a provider or reading connection setup first.
2. Authenticated users see exactly Home, Approvals, Connect, and Account as the four release-one destinations on compact phones; no More menu is present and Agent Work/admin/ops/MCP/audit routes remain hidden.
3. User can reach provider setup through the Connect destination and the Home connection card/action; provider support state is explicit and does not overclaim native-session support.
4. A valid approval requires opening detail and seeing exact action, target, risk, expiry, and provenance before a decision can be submitted.
5. Stale, offline, expired, blocked, unsupported, mismatched, or uncertain requests have no enabled approve/reject action.
6. After a decision, the app separately reports server decision recording and connector delivery/resume confirmation.
7. Every loading, empty, error, offline, stale, and terminal state above has an intentional UI and accessible announcement.
8. App supports light and dark appearance, 200% font scaling, TalkBack, 48dp minimum targets, and adaptive window layouts.
9. Color contrast is measured and documented for every foreground/background token pair; no semantic meaning depends only on color.
10. Product copy makes clear that a user’s computer and connector must remain running/connected for local work to continue.

## 11. Rollout sequence

1. **Token and component foundation:** add light/dark semantic theme, typography, status/risk chips, loading/error/empty panels, buttons/fields, and accessibility labels; retain existing approval policies.
2. **Navigation and shell:** implement exactly Home, Approvals, Connect, and Account as the four destinations, no More menu, responsive navigation rail, and stable approval deep links from notifications.
3. **Approval-first workflows:** Home summary, Inbox/History split, detail review hierarchy, high-risk confirmation, and decision/delivery timeline. Keep decision backend semantics unchanged.
4. **Connect and account:** simplify sign-in/register, provider-specific setup, honest connection health, account/logout.
5. **Scope cleanup:** keep Agent Work and all admin/ops/MCP/audit routes hidden from release-one navigation; retain only the four locked destinations.
6. **Verification:** screenshot review across small/large Android devices, font scaling, light/dark, TalkBack traversal, accessibility scanner, offline/stale/error cases, deep links, decision races, and provider-specific E2E gates. Device-based push and native-provider resume remain separately evidenced release gates.

No cloud, billing, or provider capability claim is granted by this UI specification.

## 12. References

Design choices are informed by Android's native Material and accessibility guidance, W3C contrast/target-size requirements, and established notification/review patterns. These references are design inputs, not evidence that Aixion has equivalent implementation or provider support.

- [Material 3 in Jetpack Compose](https://developer.android.com/develop/ui/compose/designsystems/material3) — theme roles, typography, contrast, adaptive theming.
- [Build adaptive navigation](https://developer.android.com/develop/adaptive-apps/guides/build-adaptive-navigation) — bottom navigation for compact windows, rail for expanded windows.
- [Use window size classes](https://developer.android.com/develop/adaptive-apps/guides/use-window-size-classes) — layout decisions from dynamic app window size.
- [Compose accessibility API defaults](https://developer.android.com/develop/ui/compose/accessibility/api-defaults) — semantics and minimum interactive target guidance.
- [Accessibility in Jetpack Compose](https://developer.android.com/develop/ui/compose/quick-guides/content/video/accessibility-in-compose) — content descriptions and semantics.
- [W3C WCAG 2.2](https://www.w3.org/TR/wcag/) — contrast, target size, and accessibility requirements.
- [GitHub Mobile](https://docs.github.com/en/get-started/using-github/github-mobile) and [GitHub notification configuration](https://docs.github.com/en/subscriptions-and-notifications/get-started/configuring-notifications) — notification-to-review workflows and user-controlled notification settings.

## 13. Assumptions and unresolved product evidence

- Provider setup UX can be presented only for capabilities that have separate implementation evidence. Antigravity is first; Codex native desktop interception/resumption is explicitly unverified until proven.
- Four release-one destinations are locked to Home, Approvals, Connect, and Account. There is no More menu. Agent Work and admin/ops/MCP/audit routes remain hidden from release-one navigation.
- Google/GitHub OAuth, biometric step-up, configurable notification content, and account deletion are not assumed to be implemented; they require independent product/security decisions and backend support.
- The document specifies no backend behavior, authentication policy change, storage change, cloud configuration, or billing action.
