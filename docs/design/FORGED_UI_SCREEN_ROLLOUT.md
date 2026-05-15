# Forged UI Screen Rollout

This document records the completed Android screen redesign rollout for **AIXION Control Tower**.

The direction is based on the selected **Logo 3 / forged-network identity**:

- dark-first
- premium engineering surface
- calm control tower feeling
- human approval before AI/code execution
- evidence, auditability, and traceable decisions

## Design principle

AIXION Control Tower should not feel like a generic DevOps dashboard.

It should feel like a serious mobile command surface where AI/code actions remain paused until the right human decision, validation path, and audit record exist.

## Completed PR sequence

### PR #118 — Design foundation

Added shared forged UI foundation:

- dark color tokens
- spacing/radius tokens
- `TowerPanel`
- `TowerHeroPanel`
- `TowerSectionHeader`
- `TowerDivider`
- `ForgedLogoMark`
- restyled shared cards and badges

### PR #119 — Home Dashboard

Redesigned Home as the main command surface:

- AIXION branded header
- state-aware hero panel
- command-state summary
- action-required queue
- calm empty state

### PR #120 — Approval Detail + Diff Review

Redesigned decision-critical review surfaces:

- forged approval detail hero
- safety context panels
- decision gate panel
- mobile diff review hero
- restyled shared diff block

Approval/reject/revise behavior and safety gating were preserved.

### PR #121 — Agent Tasks + Connectors + MCP Queue

Redesigned connected-agent control surfaces:

- Agent Tasks queue and timeline
- Connectors templates, credentials, mapper actions
- MCP health, pending queue, recovery actions

Existing callbacks, tested labels, and backend behavior were preserved.

### PR #122 — Audit + Runtime Ops + Account/Admin

Redesigned proof and admin surfaces:

- Audit Trail evidence cards
- Runtime Readiness operational gates
- Account authentication
- owner role management
- owner invite management
- owner session/access management

Auth/admin behavior remained unchanged.

## Current visual language

### Background

Near-black command surface.

### Panels

Rounded dark panels with subtle borders.

### Typography

Clear hierarchy, compact metadata, minimal noise.

### Risk colors

- low / ready: green
- medium / pending: amber
- high / warning: orange
- critical / blocked: red or purple

### Logo usage

The forged mark appears in major hero surfaces only. Do not overuse it on every card.

## Screen readiness checklist

Before Play Store capture, verify each target screen has:

- real app UI, not mocked marketing art
- stable backend/dev data
- readable status text
- no secret tokens or private credentials visible
- no fake claims
- no broken cards on small devices
- no overflowing long branch/repository strings
- no one-time secrets visible unless intentionally using demo-safe values

## Screens to capture for Play Store

Recommended phone screenshot set:

1. Home Dashboard
2. Approval Detail
3. Diff Review
4. Agent Tasks
5. Connectors
6. MCP Queue
7. Audit Trail
8. Account / Owner Controls

## Caption recommendations

Use short captions above or outside the screenshot frame.

Recommended captions:

- Know what needs review
- Review before AI acts
- See every file change
- Control connected agents
- Configure external agents
- Block risky MCP calls
- Every decision is traceable
- Manage roles and sessions

Avoid weak or risky captions:

- World's best AI security app
- Fully autonomous coding without risk
- Works with every model
- Guaranteed safe execution
- Free forever

## Hard rule

Play Store screenshots must reflect the implemented app experience.

Do not submit generated mockups as final store screenshots unless they exactly match the actual app UI and real behavior.

## Next work after UI rollout

Recommended next PRs after the screenshot/docs handoff:

1. Android screenshot capture/demo data fixture support
2. Play Store metadata draft
3. real app icon/adaptive icon integration
4. feature graphic finalization
5. real-device screenshot pass
6. release readiness checklist update
