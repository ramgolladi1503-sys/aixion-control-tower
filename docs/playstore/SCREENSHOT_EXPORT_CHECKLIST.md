# Play Store Screenshot Export Checklist

This checklist prepares **AIXION Control Tower** for Play Store visual assets after the forged UI rollout.

## Asset set

Prepare the following assets:

- app icon
- feature graphic
- phone screenshots
- optional tablet screenshots later

## App icon

Required direction:

- use Logo 3 / forged-network mark
- keep background simple and dark
- avoid stripes, grid clutter, glow, neon, robot, shield, or sparkle symbols
- ensure the mark remains readable at small size

Recommended export:

- 512 x 512 px PNG for Play Console
- Android adaptive icon foreground/background for the app build

## Feature graphic

Required direction:

- parent brand: AIXION LABS
- product: AIXION Control Tower
- dark premium engineering look
- one clear product promise
- no fake awards, rankings, pricing, or unsupported claims

Recommended headline:

```text
Approve AI actions before they execute.
```

Alternative headline:

```text
Human approval for AI/code execution.
```

## Phone screenshot set

Capture real implemented screens from the Android app.

Recommended order:

1. Home Dashboard
2. Approval Detail
3. Diff Review
4. Agent Tasks
5. Connectors
6. MCP Queue
7. Audit Trail
8. Account / Owner Controls

## Screenshot capture rules

Use demo-safe data only.

Do not show:

- real tokens
- real one-time connector secrets
- private email addresses unless demo-safe
- private repository secrets
- real production API URLs
- personal access tokens
- FCM keys
- GitHub tokens
- internal credentials

Use realistic but safe values:

```text
ram@example.com
connector_demo_1
feature/demo-approval-bridge
ramgolladi1503-sys/aixion-control-tower
PR #104
AgentTask #79
```

## Caption set

Recommended captions:

```text
Know what needs review
Review before AI acts
See every file change
Control connected agents
Configure external agents
Block risky MCP calls
Every decision is traceable
Manage roles and sessions
```

Keep captions short.

Do not let caption text dominate the actual app screenshot.

## Store copy draft

### App title

```text
AIXION Control Tower
```

### Short description

```text
Approve AI coding actions, track execution, and audit every decision.
```

### Full description draft

```text
AIXION Control Tower is a mobile approval console for AI/code execution.

Review pending AI and agent actions from your Android phone, inspect risk context, open file diffs, approve or reject safely, track execution progress, and audit every decision.

Built for developers and builders who use AI coding agents and need a human approval layer before powerful actions are executed.

Core features:
- Review approval requests
- Inspect risk, source, branch, test plan, and rollback context
- View mobile-readable diffs
- Track AgentTask, connector, and MCP queue activity
- Monitor runtime readiness
- Review audit history
- Manage roles, invites, and sessions

AIXION Control Tower is built around one principle: automation should be powerful, but never unaccountable.
```

## Final manual review before upload

Before uploading Play Store assets, verify:

- screenshots match the real app UI
- captions are accurate
- no unsupported integrations are claimed
- no secrets are visible
- app icon is clean at small sizes
- feature graphic is readable on desktop and mobile
- short description does not overclaim
- full description matches implemented functionality

## Required engineering follow-up

This docs PR does not generate final Play Store files from the built app.

The next engineering step should be a capture/export workflow:

1. add demo-safe backend seed data or fixture mode
2. run Android app against fixture/demo backend
3. capture screenshots from emulator or real device
4. export final 1080 x 1920 phone assets
5. verify every screenshot against Play Store policy and product reality
