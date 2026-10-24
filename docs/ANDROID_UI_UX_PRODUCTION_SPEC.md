# Android UI/UX Production Specification

**Status:** Design contract for the production-readiness program
**Scope:** Existing Aixion Control Tower Android app
**Implementation status (2026-10-22):** Partially implemented. The compact-phone shell has four locked destinations (Home, Approvals, Connect, Account) and keeps later-release technical/admin routes hidden; release one has no More destination. The app has a warm-paper/olive visual system, an approval inbox/history split, a persistent approve/reject footer with high-risk confirmation, Home-to-Connect navigation, and source-level light and dark semantic palettes. Local UI polish now uses a custom Link glyph for Connect, route-specific detail titles, visible invocation identity in decision context, a full-diff acknowledgement gate for high/critical file approvals, 14sp section subtitles, and 16dp cards. The theme uses Android system sans for UI and monospace for payloads. These source changes are not device or visual certification. Unless a paragraph says otherwise, screen contracts below describe the target design; they are not claims that every detail is implemented.

Current gaps include adaptive navigation, provider liveness/last-seen health on Home, delivery-outcome history, password reset, and TalkBack/large-font/device visual validation. Source-level dark palette selection and high-risk confirmation checks exist, but dark rendering, composed contrast, and those interactions still need device validation. Home sync timestamp and password show/hide are implemented in source but still need device and accessibility validation. The current Connect surface provides setup guidance; it does not establish a live provider session. Native Codex Desktop approval interception and same-invocation resume are not verified.

**Code-only UI audit checkpoint (2026-10-22; pre-polish snapshot):** The Android UI/UX audit scored the then-current implementation **16/24** across copywriting, visuals, color, typography, spacing, and experience design. This is a pre-polish code-only score, not a visual score or release certification. Subsequent local polish includes the Connect Link icon, route-specific detail titles, 14sp core Home/history text, and 16dp card corners. Local Android JVM UI tests passed and Android instrumentation sources compiled; instrumentation was not run on hardware. Screenshots, physical-device rendering, TalkBack, 200% font scaling, final composited contrast, and adaptive-window behavior remain unverified; ADB reported no connected device during the audit. The audit did not authorize broader navigation: the four-destination release-one contract remains locked and secondary technical/admin screens remain hidden.

**Screen-by-screen source audit (2026-10-23):** Inspected the actual release-one route table and shell, all four primary destination composables, authentication/account states, approval inbox/history/card/detail/decision and diff flows, provider setup and project-scoped Agent Access, shared panels/badges/diff components, theme tokens, spacing, and relevant JVM/instrumentation tests. The source correction in this audit caps approval-card previews at three lines while preserving the full summary in accessibility semantics, changes auth/connect/account-notification/provider-credential/diff-review state labels to sentence case, raises Home technical diagnostics to the 14sp shared body style, and corrects the stale token/contrast values in this document. The code audit does not certify rendered behavior. `adb devices -l` returned no attached device in this pass.

**Scope lock:** this is a design contract for release one only. User-facing destinations are Home, Approvals (including History and request detail), Connect, and Account (including sign-in/register/logout). Agent Work, projects, command chat, MCP, technical audit, diagnostics, and administration remain outside release-one navigation. Billing and cloud deployment are outside this document. The UI must not imply that a local computer is online or that an agent resumed unless the backend/connector provides fresh evidence.

## 0.1 Contract at a glance: preserve the working baseline, close the release gaps

This is a source-backed implementation contract, not a device-certification claim. Keep the compact, quiet four-destination design. Prioritize the approval workflow and evidence gaps before decorative work or new destinations.

| Area | Already in source or locked | Remaining release evidence or work |
|---|---|---|
| Navigation | Home, Approvals, Connect, Account; technical/admin routes stay hidden. | Verify labels, focus order, insets, notification routing, and adaptive navigation on device. |
| Visual identity | Warm paper/charcoal, mineral olive, fine boundaries, system sans, monospace payloads; light/dark semantic palettes exist. | Verify both rendered palettes and composited contrast. Avoid a broad rebrand. |
| Sign-in | Email/password, one Log in action, show/hide password, same-screen Create account. OAuth is not presented. | Verify keyboard/IME, autofill, TalkBack, scaling, and errors. Show password recovery only when it works end to end. |
| Home and Connect | Home handles backend error/empty states and links to setup; Connect explains setup and labels provider phone flow unverified. | Add only evidence-backed liveness/last-seen; do not imply a live connection from setup state. |
| Request review | Inbox/history split, exact-action context, acknowledgement and high-risk confirmation, fail-closed stale/unknown states. | Verify hierarchy, wrapping, sticky controls, accessibility traversal, and expiry on device. |
| Decision history | Decision and reviewer/time are separate from delivery/execution claims. | Complete the user-facing delivery timeline; never label a request resumed without same-invocation evidence. |
| Accessibility and windows | Scrollable Compose surfaces and semantic palettes provide a base. | Verify 200% text, TalkBack, keyboard/D-pad, landscape/foldable states, and final contrast on one exact APK. |

**Implementation order:** first prove notification → exact request review → approve/deny → same invocation resumes or fails closed. Fix UI defects observed in that run. Then verify sign-in, history, notification routing, and accessibility/window behavior. Defer decorative motion, analytics, extra tabs, OAuth, and broad visual-system replacement.

**Distinctive approval-card direction:** use a calm “field note” treatment: warm neutral surface, narrow olive identity rule, clear provider/project metadata, a high-contrast exact-action block, and plain-language risk/state labels. Keep the rest typographic and quiet; the request and decision should dominate the card.

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

Target behavior: on expanded window widths, replace the bottom bar with a navigation rail and use list-detail layouts where they reduce navigation overhead (approval list beside approval detail). On compact and medium widths retain one content pane and bottom navigation. Choose based on current app window size class, including resized/foldable states, not device model. The compact four-destination bottom bar and expanded-width navigation rail are implemented; approval list-detail behavior is not implemented. Neither adaptive behavior has been visually/device-verified. These recommendations follow Android's adaptive navigation model.

## 3. Visual system

### Unique visual direction: “Quiet Instrument Panel”

The interface should feel like a calm, trustworthy instrument panel: warm, ink-like surfaces; finely drawn borders; ample space; clear typographic contrast; and small, deliberate olive accents. Avoid the current near-black/electric terminal aesthetic. The visual distinction comes from warm neutrals and an editorial hierarchy rather than ornamental branding.

### Color tokens

The current theme selects semantic light or dark palettes from the Android system appearance. Both palettes exist in source; rendered dark-mode consistency and contrast have not been device-verified. Do not use wallpaper dynamic color for approval, risk, or decision semantics, because those meanings must remain consistent. The following are design tokens; verify actual composited contrast ratios for both appearances before release.

| Token | Light | Dark | Use |
|---|---:|---:|---|
| `canvas` | `#F4F2EC` | `#171A16` | Main app background |
| `surface` | `#FBFAF6` | `#20241F` | Cards, sheets, fields |
| `surfaceRaised` | `#FFFFFF` | `#292E28` | Dialogs, selected panels |
| `surfaceSubtle` | `#EAE8E0` | `#343B33` | Secondary cards and chips |
| `ink` | `#20241F` | `#E9EEE5` | Primary text and icons |
| `inkMuted` | `#596057` | `#C2C9BE` | Supporting text |
| `inkDisabled` | `#656B61` | `#AAB2A5` | Disabled text; never the only state cue |
| `line` | `#D4D5CC` | `#515A4F` | Dividers and card boundaries |
| `brand` | `#53664D` | `#A9C19B` | Primary action, selected destination, links |
| `onBrand` | `#FFFFFF` | `#1C261A` | Text/icons on brand |
| `positive` | `#286348` | `#79D5A4` | Confirmed success only |
| `caution` | `#805600` | `#F3C96D` | Attention and approaching expiry |
| `danger` | `#A23F35` | `#FF9287` | Reject, revoke, blocked, destructive errors |
| `info` | `#3B6178` | `#91C4E7` | Neutral information |

Neutral surfaces should occupy roughly 85–90% of the screen. Brand olive is reserved for the primary action, selected navigation, and connection CTA. Positive, caution, and danger colors are reserved for explicit semantic states; do not use them as decoration. Pair every semantic color with text/icon/shape, never color alone.

**Contrast baseline (source tokens, calculated 2026-10-23):** solid light ratios are `ink/canvas 15.08:1`, `inkMuted/canvas 6.21:1`, `inkDisabled/canvas 4.90:1`, `onBrand/brand 6.22:1`, `positive/canvas 6.32:1`, `caution/canvas 5.77:1`, `danger/canvas 5.69:1`, and `info/canvas 5.92:1`. Solid dark ratios are `ink/canvas 13.37:1`, `inkMuted/canvas 9.29:1`, `inkDisabled/canvas 8.05:1`, `onBrand/brand 8.04:1`, `positive/canvas 9.92:1`, `caution/canvas 11.19:1`, `danger/canvas 8.12:1`, and `info/canvas 9.41:1`. The JVM contrast test checks primary/supporting text on canvas and surface, on-brand text, and semantic status text on its 11% tinted badge surface in both palettes. These checks do not establish every composited component, focus indicator, border, disabled control, or rendered screen; those still require device validation. Do not use `line` as the only boundary for a meaningful control unless its contrast is independently verified.

### Typography

Use Android system sans (Roboto) for regular UI and a system monospace face only for exact code/action payloads. Do not bundle a decorative display face: distinction comes from warm-paper surfaces, olive accents, restrained line work, and clear hierarchy. Support user font scaling without clipping or truncating request details. Use sentence case; never use all-caps for risk, state, or action labels.

| Style | Size / line height | Weight | Use |
|---|---|---|---|
| Page heading (`headlineLarge`) | 28 / 36 sp | 600 | Primary screen heading; the Home and sign-in screens use this token |
| Section/card title (`titleLarge` / `titleMedium`) | 20 / 28 or 17 / 24 sp | 600 | App-bar/detail section and approval-card headings |
| Body (`bodyLarge` / `bodyMedium`) | 16 / 24 or 15 / 22 sp | 400 | Explanatory copy, fields, and request context |
| Label (`labelLarge` / `labelMedium` / `labelSmall`) | 14 / 20 or 14 / 18 sp | 500 | Metadata, controls, and navigation labels |
| Payload (`TowerCodeTextStyle`) | 14 / 21 sp | 400 | Commands, file paths, and immutable action payloads |

These values match the current `core/ui/theme/Theme.kt` Compose tokens rather than proposing a second typography system. The theme also defines 32/40 `displayLarge` and 24/32 `headlineMedium`; use those only where the screen hierarchy calls for them, and do not introduce them as another default page-title size. Android system sans is the UI face and system monospace is reserved for exact payloads. Do not hard-code a smaller substitute in a release-one screen. Verify the resulting hierarchy at 1.0×, 1.3×, and 2.0× system font scales on device; source token values do not prove rendered wrapping or readability.

Do not render core content below 14sp. Use sentence case and normal word spacing; reserve uppercase micro-labels for short, non-essential metadata only.

Body and labels wrap naturally. Never ellipsize, horizontally clip, or silently omit an approval's command, path, target, risk reason, expiry, or state. Long payloads preserve whitespace and line breaks in a selectable monospace region; wrap at safe boundaries and allow horizontal scrolling only for unbroken tokens where wrapping changes meaning. Metadata may wrap onto extra lines. At 200% font scale, vertical scrolling is expected; hiding decision safety context is not.

### Spacing, shape, and elevation

Use a 4dp base scale: `4, 8, 12, 16, 24, 32, 40, 48`. Screen content uses 20dp horizontal padding on compact widths, 24dp on medium widths, and a centered 720dp maximum readable column on expanded widths. Cards use 16dp inner padding and 16dp corner radius; dialogs/sheets use 24dp radius; fields and buttons use 12dp radius. The local card implementation now uses the 16dp corner radius, but its rendered geometry remains unverified on device. Prefer one-pixel tonal borders and spacing over shadows. Use elevation only to clarify modal hierarchy.

## 4. Screen contracts

### 4.1 Sign-in and registration

Target screen is a compact sign-in form: logo/wordmark, “Sign in”, email, password with show/hide control, primary **Log in** button, and a low-emphasis **Create account** link. Place password reset in the same form. Do not show technical API endpoints, agent setup, or an expanded registration form on initial load. The current screen has the simple email/password and account-creation flow with a show/hide control; password reset remains open work. Show/hide behavior still needs device and accessibility validation.

Tapping **Create account** switches the same surface to a short registration state with email, password, password requirements, and **Create account**. Verification follows as a separate, clearly labeled next step. Keep login/register accessible without making users choose among competing sign-in providers. Offer Google/GitHub sign-in only after provider auth, account linking, and recovery behavior are implemented and reviewed; never render placeholder OAuth buttons.

Errors remain adjacent to the affected input or submit action. Preserve entered email after a recoverable error; clear passwords after authentication failure. Do not disclose whether an email address has an account. Loading state disables duplicate submits and labels the action “Signing in…” or “Creating account…”.

**Composition and behavior:** use a single-column form in a readable centered width: heading, email, password, primary submit, then a quiet mode-switch link. Do not place provider logos, feature grids, connector setup, or backend URLs on this screen. Use email keyboard/autofill semantics, secure password input, a visibility toggle whose state is announced to TalkBack, submit from the IME, and scroll/resize for the keyboard. Registration validation explains requirements before submission and associates errors with fields. Logout returns to this sign-in surface and clears authenticated navigation state; it does not erase server-side approval history.

### 4.2 Home

Target Home is a short status summary, not an analytics dashboard:

1. Top app bar with page title and no More menu.
2. A single status header: “2 approvals waiting” or “All clear”, with a small, explicit sync state and last successful sync time.
3. Pending approvals preview: up to three compact cards, each with provider, project or target, concise action summary, risk label, and time/expiry; **Review** opens the exact request. **See all** opens Approvals.
4. Connection card: connected provider(s), connector last-seen state, and **Connect an agent** if none are active. Current Home offers a **Connect an agent** section and **Connect platform** action plus the computer-availability reminder; it does not claim a provider is connected or display heartbeat/last-seen health.
5. Short availability reminder only when useful: “Your computer and agent connector need to stay online for work to resume.”

If backend sync fails, show the last successful sync time and a persistent “Could not refresh” panel with Retry. Clearly mark data as stale; disable decisions for stale approvals according to repository decision policy. Never say “All clear” when freshness cannot be established. The current Home shows the last successful sync timestamp, a backend-sync error, and hides fallback/mock data; device validation is still pending.

**Visual order:** page title → freshness/status line → pending count and up to three review cards → connection summary/setup action → short availability note. Keep one obvious primary action per state. Say “1 approval waiting”, “3 approvals waiting”, or “No approvals waiting”; avoid celebratory success styling for zero. “Connector online” requires a fresh heartbeat and should include “Last seen …”; registration alone is “Setup started” or “Not verified”. Cards stack on narrow screens; metadata wraps instead of collapsing into tiny columns.

### 4.3 Approval inbox and decision history

Top area has two labeled filters: **Needs review** and **History**. Current implementation offers All, Approved, Rejected, and Revision requested history filters. History events include reviewer and decision time and may include a verified request snapshot; delivery/resume outcome is not part of the displayed record. Keep the UI explicit that a decision record does not prove delivery or execution. Keep `Pending` count visible without making it a separate tab. Sort pending by earliest known expiry first, with missing expiry values last; creation-time tie breaking is future work because the list model does not currently expose creation time.

Every card shows provider badge (Antigravity/Codex/other explicitly supported), project/workspace, readable action summary, risk and state labels, expiry/countdown where applicable, and a text affordance “Review request”. Show the status through a labeled icon and text; never color-only dots. Do not show approve/reject buttons inside list cards; decisions require opening the full details.

The target design for history entries is decision, reviewer, date/time with timezone, exact request title, and delivery outcome as separate fields (e.g., “Approved · Delivery not confirmed”). The current history UI renders decision, delivery status/explanation, decision time, reviewer, reason, and a verified request snapshot when available. It still must not imply that a server-side connector claim proves agent execution or resumption.

**List interaction:** retain the selected tab and history filter when opening a detail and returning. Each row is one clear tap target with a concise accessible name; do not put nested decision buttons in a row. Needs review is sorted by earliest known expiry, with missing expiry last. History offers All/Approved/Rejected/Revision requested filters and labels each entry as a decision record without delivery or execution confirmation. An empty state links to Connect only when no connector has been configured; otherwise explain that new approvals appear here.

### 4.4 Approval detail and decision interaction

Use a clear vertical review sequence that exposes the exact invocation before a user must scroll:

1. Begin with the exact tool/action header and immutable action payload. Render tool name and arguments in selectable, wrapping monospace text with line breaks preserved. For file changes, show the exact file/path context and a safe preview. Do not truncate, normalize, or hide meaningful arguments. The request header must identify the provider and relevant project/workspace in ordinary text so the payload is never visually detached from its source.
2. For exact native tool requests, put the explicit **I reviewed the exact tool request** acknowledgment immediately after the payload and before the overview panel. Keep approval disabled until acknowledged. Bind the acknowledgment to approval identity, payload, risk, and pending status; reset it if any of those change. For high/critical file approvals without an exact native tool payload, require opening the dedicated full-diff view and explicitly acknowledging it before enabling approval.
3. Show the decision-context panel next: provider and request state, concise action summary, project/target, invocation/session identity, risk label and rationale, and expiry. Keep missing values visible as “Not provided” or “No risk rationale was supplied”; show multiple rationale items as separate lines. This order makes exact arguments visible at first entry while retaining a compact summary beside the decision controls.
4. Follow with full request provenance (connector installation, agent/session/invocation identifiers), target and scope (repository/project/workspace), creation time, expiry, required actions/policy warnings, risk rationale, test plan, rollback plan, and optional requester/comment context when attributed. The compact decision context supports quick review; it does not replace full provenance or exact payload inspection.
5. Keep the decision footer associated with this approval identity. Show secondary outlined **Deny** and primary **Approve request** only when the current server-backed request is actionable. On narrow screens, stack full-width controls with Deny first and Approve last; both are at least 48dp high, with 12dp separation. Use **Deny** as the action label and expose the resulting server status separately in decision history and accessibility announcements.

For high/critical risk, require an explicit confirmation sheet that repeats the exact action summary, target and risk. User confirms **Approve this request**; the confirm action stays disabled while in flight. **Deny** may use a short optional reason, but cannot become a destructive swipe action. Avoid one-tap gestures and notification quick actions for approving/denying. In history, display the server's recorded terminal status (for example, “Rejected”) distinctly from the action label.

For high/critical file-change approvals, require the reviewer to open the dedicated full-diff view and explicitly acknowledge reviewing it before enabling **Approve request**. Keep **Deny** available. Bind the acknowledgement to approval identity, exact file payload, risk, and pending status; any change to those values or a transition away from pending resets it. This is a client-side review safeguard and does not replace server-side request hash, expiry, connector, or authorization checks.

After submit, keep the detail bound to the same approval ID and show that request's authoritative server status; never leave a prior selection's “Denied”/“Approved” message visible while presenting a different request. Do not optimistically switch status or clear request identity while the response is pending. Show an outcome panel tied to that ID with “Decision recorded”, server timestamp, a short request identity/hash reference, and one of “Connector delivered decision”, “Delivery pending”, or “Delivery failed/unknown”. Decision and delivery are separate facts: “Approved” does not mean the connector received the decision or that the agent resumed. If delivery is unknown, say “We can’t confirm that the agent received this decision. Do not retry this invocation unless the connector reports it is safe.” Keep the same request visible while reconciling; offer only the API-supported reconciliation action and never present a second approval button. The current screen surfaces its last action and decision reconciliation state; it does not yet render the complete delivery timeline described here. The product API's reconciliation guidance remains authoritative.

Disable both controls for expired, consumed, blocked, unsupported/unknown, stale, mismatched, revoked-connector, or reconciliation-required requests. Pair disabled actions with the exact reason and a refresh/reconcile action if available. Do not present “request revision” as equivalent to rejection unless the backend contract retains that distinct decision state and wording.

**Detail layout:** show the exact tool/action and review acknowledgment at first entry, then the decision-context panel, full provenance, and optional context. Long content scrolls behind the safe-area-aware decision footer. The footer must remain associated with this request and must not obscure the final request text; at compact width it stacks Deny above Approve. The high-risk confirmation is a distinct sheet/dialog, names the provider and target, repeats the summarized exact action, and requires a deliberate confirmation tap. Do not add swipe-to-decide, double-tap shortcuts, or notification action buttons. For any conflict between displayed data and authoritative server state, refresh and fail closed before submitting.

### 4.5 Agent Work (hidden in release one)

Do not expose Agent Work as a release-one screen or navigation destination. Its route may remain in source for a later release, but the release-one app focuses on approvals and connection health. Agent task push notifications that do not identify an approval must not navigate to a hidden screen; approval notifications open the approval directly after session validation and a fresh authoritative fetch.

### 4.6 Connect an agent

First screen presents provider choices as clear cards:

- **Antigravity** — “Connect the local Antigravity approval hook.” Show support status: **Available**, **Setup required**, or **Not verified** based on shipped capability and evidence.
- **Codex** — “Connect Codex approval requests.” State the precise supported surface. If native Codex Desktop approval interception/resume is not proven, show **Not verified for native sessions** and do not provide a misleading “Connected” state for a generic MCP connector.

For each provider, target a short numbered setup (computer prerequisite → install/configure local connector/hook → authenticate this installation → verify test request). Explain that the user does not install a Mac app; the user's machine runs the agent and its local connector. Let users copy one-time credentials with explicit “Shown once” notice, visible expiry, and Rotate/Revoke action. Mask secrets after leaving the screen; never put them in screenshots, notifications, logs, or clipboard history hints. Current setup guidance is not proof of a connected or live provider.

Connection health uses explicit last heartbeat, host online/offline, connector version, and last successful request. Do not equate API registration with active local connectivity. Setup steps have exact copyable commands but commands must be labeled by OS and shell, be read-only or least-privilege where possible, and never ask users to paste secrets into support chat. Revocation requires confirmation and explains which running approvals/requests will stop or fail closed.

**Connect hierarchy:** provider selection → provider capability/support label → prerequisite and concise numbered setup → installation authentication → explicit verification result. State labels mean: **Connected** only with a fresh authenticated heartbeat; **Setup required** when user action remains; **Offline** when previously verified but heartbeat is stale/missing; **Not verified** when this provider surface has no proven approval/resume flow. Never present a generic webhook as native approval interception. Keep secret issuance/revocation behind explicit actions and confirmation, separate from ordinary provider instructions.

### 4.7 Account

Show account email, workspace (one per account), session state, notification permission status, privacy controls, and **Log out**. Keep destructive account removal behind a separate privacy section, with explicit retention consequences. Logout confirmation states that the local sign-in session ends on this device and pending approvals remain on the server under the account's retention policy. Do not use ambiguous “Exit” or back-button logout.

Keep the primary Account surface short: identity and workspace, notification permission/status with a system-settings path when denied, then Log out. Do not imply notification delivery is guaranteed by permission alone. Account deletion is not a release-one shortcut or a synonym for logout.

### 4.8 Hidden routes and later-release screens

Do not expose Agent Work, Projects, Command, Work Orders, Agent Runs, Agent Access, Audit, Diagnostics/Ops, Test Runs, MCP, webhooks, or other admin/operations routes through any release-one navigation surface. Retain source routes only where needed for later work and existing internal validation. Do not bring dormant feature routes into navigation simply because their composables exist. Approval decision history remains in the Approvals destination and is distinct from the hidden technical audit route.

### 4.9 Inspected release-one screen matrix and source evidence

This matrix records source-level behavior and existing automated coverage. Test files are evidence of the named assertions only; they do not prove a physical-device or native-provider round trip.

| Route/surface | Source inspected | Current source behavior and state handling | Automated evidence inspected | Remaining design/release gap |
|---|---|---|---|---|
| Sign in / register (`auth`) | `feature/auth/AuthScreen.kt`, `AuthUxCopy.kt`, `AuthViewModel.kt` | Single-column email/password form; registration is opt-in on the same surface; show/hide password, IME submit, verification step, loading/error copy, scrollable keyboard-safe content. Auth status copy now uses sentence case. | `AuthFormContentTest`, `AuthUxCopyTest`, `AuthSessionEstablishmentTest` | Password reset is absent. Verify autofill, IME, validation/error announcement, and 200% font scale on device. |
| Home (`home`) | `feature/home/HomeScreen.kt`, `HomeViewModel.kt` | Fresh backend state gates “No approvals waiting”/pending previews; sync time and retry/error are explicit; no synthetic requests; setup action goes to Connect. Technical error details are opt-in and now use 14sp body typography. | `HomeScreenTest`, `HomeApprovalSafetyTest`, `HomeScreenCopyTest` | Provider heartbeat/last-seen health is not available here. Device layout, loading motion, and high font scaling remain unverified. |
| Approval inbox (`inbox`, Needs review) | `feature/approvals/ApprovalInboxScreen.kt`, `ApprovalsViewModel.kt`, `core/ui/components/ApprovalCard.kt` | Refresh/loading/error/empty and unsupported/blocked cases are represented; unsafe/stale data hides selection or decision paths; approval rows open detail and contain no decision buttons. Summary preview is capped at three lines with an ellipsis; full text remains in accessibility semantics and detail review. | `ApprovalInboxScreenTest`, `ApprovalInboxFilterTest`, `ApprovalSelectionTest`, `ApprovalCardPreviewTest` | Physical scrolling, 200% wrapping, TalkBack traversal, and touch-target audit remain. |
| Decision history (`inbox`, History) | `feature/approvals/ApprovalInboxScreen.kt` | Separate history query and filters; decision, reviewer, timestamp, reason, verified request snapshot, and connector delivery state are distinct; delivery text does not claim execution. | `ApprovalInboxScreenTest`, `ApprovalRepositoryTest` | Validate older/unverified snapshots and delivery labels visually and with TalkBack. |
| Approval detail / decision | `feature/approvals/ApprovalDetailScreen.kt`, `ApprovalDecisionPolicy.kt`, `core/ui/components/DiffBlock.kt` | Decision context precedes technical details; exact tool args and file diffs are selectable; expired/blocked/unknown/stale/reconciliation states disable decisions; high/critical approvals confirm; high/critical file changes require opening and acknowledging full diff; decision panel stays below the scrollable request. | `ApprovalDetailScreenTest`, `ApprovalRiskConfirmationUiTest`, `ApprovalDecisionPolicyTest`, `ApprovalDecisionReconciliationTest`, `ApprovalStaleDecisionViewModelTest` | Device proof is still required for footer association, dialog focus, long payloads, TalkBack, and risky review flow. |
| Full diff (`diff`) | `feature/diff/DiffViewerScreen.kt`, `core/ui/components/DiffBlock.kt` | Dedicated route has explicit title/up navigation; displays affected paths, risk, selectable line-by-line diff; “Full diff review” label is sentence case; detail acknowledgement is bound to opening the route and payload. | `ApprovalDetailScreenTest`, `ApprovalRiskConfirmationUiTest` | Long unbroken lines and screen-reader interpretation need device review. |
| Provider selection (`connectors`) | `feature/connectors/ConnectorsScreen.kt`, `ConnectorsViewModel.kt` | Antigravity and Codex options link to their specific setup route; Codex Desktop support and same-session phone return are explicitly marked unverified; setup copy says the agent/computer must remain available. The Antigravity `Prior local E2E: passed` badge describes dated test evidence, not current connector liveness. | `ConnectorsScreenTest` | Content is setup guidance, not live provider status; verify exact onboarding steps with current adapters. |
| Provider setup (`agent_access/{provider}`) | `feature/agentaccess/AgentAccessScreen.kt`, `AgentAccessViewModel.kt` | Project-scoped credential setup, provider/project selection, sentence-case token state, one-time secret masking/clipboard clearing on background/disposal, secure-window flag while visible, and confirmation for rotate/revoke. | `AgentAccessPermissionsTest`, `OneTimeTokenPanelTest`, `AgentAccessProviderNavigationTest` | Requires device verification for secret masking, clipboard, secure flag, dialogs, and screen-reader labels. Do not equate registration with heartbeat. |
| Account / logout (`account`) | `ControlTowerApp.kt`, `feature/auth/AuthScreen.kt`, `LogoutConfirmation.kt`, `NotificationPermissionPanel.kt` | Authenticated account shows identity/session, sentence-case notification permission state, and logout; logout is confirmed in shell and returns to sign-in. It does not display the unused owner admin panels. | `LogoutConfirmationCopyTest`, `NotificationPermissionAnnouncementsTest`, `NotificationPermissionPanelTest`, `AuthRepositoryLogoutTest`, `ControlTowerAdaptiveNavigationTest` | Password reset and user-facing privacy/account-removal entry point are absent; notification permission does not guarantee push delivery. |
| Navigation shell | `ControlTowerApp.kt`, `navigation/Routes.kt`, `core/ui/theme/Spacing.kt` | Four compact destinations are Home, Approvals, Connect, Account; an 840dp width threshold selects rail. Secondary routes are not navigation destinations. Approval badge is suppressed while data is loading, refreshing, or errored. | `ControlTowerAdaptiveNavigationTest`, `NavigationDestinationsTest`, `NotificationNavigationTest` | Width-based rail is source-tested but not verified in resized/foldable windows; no list-detail pane is implemented. |

**Shared-system inspection:** `Theme.kt` uses system sans typography, 14sp minimum Material labels/body-small, 16sp body-large, and 14sp monospace action text; `Color.kt` selects explicit light/dark semantic palettes without dynamic colors; `Spacing.kt` centralizes 4dp-grid spacing, 16dp card corners, content widths, and padding. `TowerPanel`, `TowerHeroPanel`, `RiskBadge`, `StatusBadge`, `ApprovalCard`, `DiffBlock`, and `StatusCard` were inspected. Existing contrast tests cover primary/supporting text, on-brand text, and semantic badge foregrounds in both palettes. Approval-card summaries use a three-line visual preview with ellipsis; Compose semantics retain the full text and opening the card remains the review path. An instrumentation assertion checks full accessible text and stable card height for long previews; remaining tiny typography on hidden later-release screens is outside the four-destination release-one surface. No custom motion or animation was added: state changes use Compose/Material behavior, so reduced-motion and transition behavior still need device assessment. Source inspection cannot verify final contrast after system bars, font scaling/line breaks, TalkBack focus order, actual target bounds, animation smoothness, or visual balance.

## 5. Shared states and copy

| State | Required treatment | Example copy |
|---|---|---|
| Initial load | Skeleton preserving final layout; announce loading once to accessibility services | “Loading approvals…” |
| Empty inbox | Quiet illustration/icon optional; primary path to connect | “No approvals waiting. New requests will appear here.” |
| Empty history | Explain empty scope and reset filters | “No requests match this filter.” |
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

State rendering rules apply consistently across screens:

- **Loading:** preserve the expected content shape with a small number of skeletons; do not show fabricated counts, sample requests, or a blank white frame. Announce one concise loading status, not every placeholder.
- **Empty:** explain what belongs here and provide the next relevant action. A zero count is only meaningful when the refresh is known fresh.
- **Offline/error:** retain cached content only if it is visibly marked with its last successful refresh time. Existing decisions are read-only; never queue an approval choice for later submission.
- **Stale:** show the age and refresh action beside the content. A stale approval can be inspected, but decision controls are absent/disabled until the request is re-fetched and revalidated.
- **Terminal:** preserve the request identity and distinguish approved/denied/expired from delivery pending/confirmed/unknown. Terminal requests never regain decision buttons from an optimistic local refresh.
- **Unsupported/unknown:** show the raw meaningful status in a human-readable fallback, keep provenance visible, and disable decisions. Unknown is not equivalent to pending.

## 6. Accessibility and inclusive interaction

- Minimum interactive target is 48dp in each dimension; aim for 56dp primary decision controls.
- Keep content order equal to TalkBack traversal order. Group static metadata into meaningful labeled blocks; avoid making decorative borders/icons focusable. Announce tab selection, filter selection, result count changes, refresh completion, and decision outcome without stealing focus.
- Meet at least WCAG 2.2 AA text contrast: 4.5:1 for normal text and 3:1 for large text/UI components as applicable. Test each token pair; do not rely on visual estimation.
- Support TalkBack, keyboard/D-pad focus, switch access, Android font scaling through 200%, and system display scaling. No clipped command text, overlapping actions, or hidden status labels.
- Give controls concise, action-oriented labels. A card's accessible name includes provider, action summary, risk, and status. Announce decision result and delivery result separately.
- Pair color with text and icon shape; do not use red/green alone. Respect “Remove animations” and reduced-motion settings.
- Ensure focus order follows visual reading order; confirmation sheet traps and restores focus; error text is announced and associated with the affected field/action.
- Code blocks support horizontal scrolling only when line wrapping changes meaning; provide select/copy controls with accessible descriptions and a non-color confirmation.

## 7. Motion and feedback

Use only short, functional transitions: 150–220ms fade/size change for list updates, 180ms sheet transitions, and subtle selection state. Do not animate risk or countdown with pulsing, flashing, or glow. Preserve scroll position and avoid full-screen transitions for refreshes. Use haptics only for explicit successful/failed user-triggered decisions, respecting device vibration settings. All essential state changes remain readable without motion or haptics; follow system animator scale and disable non-essential motion when animations are removed.

## 8. Responsive layouts

- Compact: one scrollable content column, four-tab bottom navigation, sticky safe-area-aware decision footer.
- Medium: one content column with max width 640dp; use navigation rail only when the Android window-size class and available width support it without crowding content.
- Expanded: max 720dp body column; approval inbox/detail can become list-detail with independently scrollable panes, persistent navigation rail, and larger metadata layout. Keep the request and its decision controls in the same pane.
- Landscape/foldable: keep decisions within the same content pane as the request, avoid split-pane layouts where the action is visually detached from its request, and re-evaluate window class dynamically.
- Handle IME insets for forms and reason sheets; keep primary submit visible or reachable without obscuring fields.
- Recalculate layout on resize, hinge posture, split-screen, and rotation. Never infer layout only from phone/tablet model. Bottom navigation must not overlap system gesture insets; sheets and sticky controls must respect edge-to-edge system bars.

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
8. App supports light and dark appearance, 200% font scaling, TalkBack, 48dp minimum targets, and adaptive window layouts. Light/dark palettes and 48dp decision targets exist in source. Dark rendering, 200% scaling, TalkBack, full-screen target coverage, and adaptive layouts still require device/accessibility validation.
9. Color contrast is measured and documented for every foreground/background token pair; no semantic meaning depends only on color.
10. Product copy makes clear that a user’s computer and connector must remain running/connected for local work to continue.

## 10.1 Release gate evidence

Do not call the design or app release-ready from source inspection alone. Capture evidence for each gate on a physical device or representative emulator and retain screenshots/video plus test notes against the exact build:

1. **Visual baseline:** all four destinations and detail in compact portrait, compact landscape, expanded/resized, and at least one foldable/hinge posture; verify no clipped text, obscured buttons, or accidental extra destinations.
2. **Text/accessibility:** system font scales 1.0, 1.3, and 2.0; TalkBack full navigation including high-risk confirmation and terminal outcome; keyboard/D-pad focus; minimum target audit; accessibility scanner; contrast report for final composited states.
3. **State matrix:** loading, empty inbox, empty history, offline, backend error, stale request, unknown status/risk, expired, decision in flight, server accepted with delivery pending, confirmed delivery, and ambiguous delivery. Verify stale/unknown/terminal states cannot submit a decision.
4. **Auth/Connect:** login, registration, validation errors, keyboard/IME behavior, logout, notification permission denied/granted, unsupported provider copy, connector setup and heartbeat distinctions, and secret masking after leaving/backgrounding.
5. **Safety/usability:** screen recording of exact payload review → high-risk confirmation → one decision → server outcome; confirm no double-submit, swipe decision, stale approval, lock-screen payload leak, or false “agent resumed” message.

Any failed gate blocks that surface from release. Record unverified provider integration separately from app UI status; passing UI checks does not establish that Codex or Antigravity can intercept and resume the same native invocation.

## 11. Rollout sequence

1. **Token and component foundation:** maintain the light/dark semantic theme, typography, status/risk chips, loading/error/empty panels, buttons/fields, and accessibility labels; verify both appearances and retain existing approval policies.
2. **Navigation and shell:** preserve exactly Home, Approvals, Connect, and Account as the four compact destinations, no More menu, and stable approval deep links from notifications; implement and verify responsive navigation rail/list-detail behavior for expanded windows.
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
- [Android app accessibility guidance](https://developer.android.com/guide/topics/ui/accessibility/apps) — touch-target and contrast baselines for Android apps.
- [Test Compose accessibility](https://developer.android.com/develop/ui/compose/accessibility/testing) — automated contrast, target-size, and traversal checks.
- [W3C WCAG 2.2](https://www.w3.org/TR/wcag/) — contrast, target size, and accessibility requirements.
- [GitHub Mobile](https://docs.github.com/en/get-started/using-github/github-mobile) and [GitHub notification configuration](https://docs.github.com/en/subscriptions-and-notifications/get-started/configuring-notifications) — notification-to-review workflows and user-controlled notification settings.

## 13. Assumptions and unresolved product evidence

- Provider setup UX can be presented only for capabilities that have separate implementation evidence. Antigravity is first; Codex native desktop interception/resumption is explicitly unverified until proven.
- Four release-one destinations are locked to Home, Approvals, Connect, and Account. There is no More menu. Agent Work and admin/ops/MCP/audit routes remain hidden from release-one navigation.
- Google/GitHub OAuth, biometric step-up, configurable notification content, and account deletion are not assumed to be implemented; they require independent product/security decisions and backend support.
- The document specifies no backend behavior, authentication policy change, storage change, cloud configuration, or billing action.

**Authenticated Account copy follow-up (2026-10-23; JVM and Android-test-source validation):** The Account hero now says “Your account” and “You are signed in…” instead of the ambiguous “You are connected,” which could be mistaken for provider/agent liveness. Removed copy that described login and verification controls as hidden implementation details. Added JVM copy coverage and a Compose Account-panel regression. `ANDROID_HOME=/Users/madhuram/Library/Android/sdk ANDROID_SDK_ROOT=/Users/madhuram/Library/Android/sdk ./gradlew --offline :app:testDebugUnitTest --tests 'com.aixion.controltower.feature.auth.AuthUxCopyTest' :app:compileDebugAndroidTestKotlin --no-daemon` initially could not resolve uncached dependencies; the same command with offline mode removed completed successfully (**BUILD SUCCESSFUL**), including all six `AuthUxCopyTest` cases and Android instrumentation source compilation. ADB listed no connected device, so the Compose test was compiled but not run; rendered layout, TalkBack, font scaling, and device interaction remain unverified. No configuration, API, schema, migration, provider behavior, cloud, or billing change.
