# Connected-Agent Interaction Roadmap

This document turns the Phase 0 product-architecture gaps from issues #159 through #164 into one controlled implementation roadmap.

It is the source of truth for how a user connects ChatGPT, Codex, Claude, Gemini, or a custom agent to Aixion Control Tower and then reviews approval-required work from Android.

## Product promise

Aixion is a mobile approval console for external AI work.

```text
Connected agents can submit work.
Aixion records the work.
Risky work waits for mobile approval.
The user approves, denies, or requests revision.
Only approved work can continue toward execution, tests, PRs, or evidence.
```

The product must never feel like a loose set of tabs. It must feel like one loop:

```text
Connect agent
-> receive task
-> review approval
-> approve / deny / request revision
-> track outcome
```

## Locked interaction model

Use these definitions everywhere in Android UI, backend docs, release notes, and demo scripts.

```text
Connectors
= setup external access for ChatGPT, Codex, Claude, Gemini, local bridges, and custom agents.

Agent Work
= work submitted by those connected agents. This is currently implemented as AgentTasks.

Approvals
= human decision queue where the operator approves, denies, or requests revision.

Work Orders
= structured planning packages: goal, scope, tasks, tests, rollback plan, source, and risk.

Command
= manual user-created work composer. It creates controlled Work Orders without external-agent input.

Home
= attention dashboard and shortcut surface.
```

Hard rule:

```text
Connectors configure access.
Agent Work tracks submitted work.
Approvals make decisions.
Work Orders explain scope and plan.
Command creates work manually.
Home tells the user what needs attention.
```

## End-to-end connected-agent user journey

### First-time setup

```text
1. User opens Connectors.
2. User selects a template: ChatGPT Actions Bridge, Codex Agent Bridge, Claude/Cursor, Gemini, Local Bridge, or Custom Agent.
3. User taps Create connector from selected template.
4. User issues a credential.
5. User copies the webhook URL and setup block.
6. User pastes the setup into the external agent.
7. User tests a sample payload.
8. Connector shows usable status, credential status, health, last activity, and next action.
```

### Normal ChatGPT/Codex/Claude flow

```text
1. External agent receives a user request inside ChatGPT/Codex/Claude.
2. External agent sends an AgentTask to Aixion through the configured connector or GPT Actions contract.
3. Aixion stores the AgentTask with provider, source, repository, branch, goal, risk, requested action, and metadata.
4. If approval is required, Aixion creates or links an ApprovalRequest.
5. Android Home shows an Action count.
6. Android Agent Work shows the submitted task and timeline.
7. Android Approvals shows the linked decision request.
8. User opens the request and chooses Approve, Deny, or Request Revision.
9. Approval decision propagates back to the AgentTask.
10. Approved work can continue toward worker execution, tests, PR, or evidence.
11. Denied/revision work stops or returns to planning.
```

### Manual user flow

```text
1. User opens Command.
2. User describes work manually.
3. Aixion creates a controlled Work Order.
4. Work Order appears in Work Orders.
5. Work may later require approval or agent execution.
```

Manual Command is not the same as connected-agent intake. Do not make the user guess this.

## Screen responsibilities

### Home

Purpose:

```text
Show what needs attention now.
```

Required behavior:

```text
Action card -> opens Approvals filtered to pending/requested decisions.
Execution card -> opens Agent Work / execution queue.
Blocked card -> opens Approvals filtered to blocked/stopped work.
Failed Tests card -> opens Tests filtered to failures.
```

Home should not teach every concept. It should route the user to the correct next action.

### Connectors

Purpose:

```text
Connect and manage external agent access.
```

Required visible concepts:

```text
Templates are presets.
Configured connectors are real usable connections.
Credentials are required before external agents can call the backend.
Webhook/setup block must be copied into the external agent.
Testing payload proves mapping before live use.
External agents submit work; they do not approve it.
```

Required connector states:

```text
Not created
Created, no credential
Credential issued
Enabled
Healthy
Last used
Auth failures
Mapper/test errors
Disabled
```

Required next-action copy examples:

```text
Create connector
Issue credential
Copy setup block
Test payload
Enable connector
Fix auth failure
Open submitted Agent Work
```

### Agent Work

Purpose:

```text
Show work submitted by connected agents.
```

This screen should not look like a connector registry. That belongs to Connectors.

Required card fields:

```text
Provider/source: ChatGPT, Codex, Claude, Gemini, GitHub Actions, manual, other
Connector name when available
Status
Risk
Requires approval flag
Title
Goal
Repository
Branch preference
Requested action
Last activity
Linked approval
Timeline
Latest error
PR/result link when available
```

Required filters:

```text
All
Waiting for approval
Executing
Failed
Ready for PR
Done
Provider/source
Project/repository
```

### Approvals

Purpose:

```text
Let the user decide what can continue.
```

Required decisions:

```text
Approve
Deny
Request Revision
```

Required explanation:

```text
Approve = allow this work to continue inside configured execution boundaries.
Deny = stop this work.
Request Revision = send it back to planning with feedback.
```

Approvals should show linked context:

```text
AgentTask
Work Order
Connector/source
Risk reason
Affected repository/branch/files
Diff/evidence when available
Validation requirements
```

### Work Orders

Purpose:

```text
Show structured work plans before execution.
```

Required lifecycle copy:

```text
Created from Command or connected agent.
Defines goal, scope, tasks, tests, rollback plan, and risk.
Does not execute code by itself.
May link to approval and AgentTask execution.
```

Required fields:

```text
Created by
Source: manual / ChatGPT / Codex / Claude / backend / MCP
Status
Goal
Tasks
Required tests
Rollback plan
Risk
Linked approval
Linked AgentTask
Linked PR/result when available
```

### Command

Purpose:

```text
Manual way to create work.
```

Required rule:

```text
Use one clear action: Create Controlled Work Order.
```

Do not present fake separate actions like Generate Work Order unless they are real buttons with different behavior.

Recommended copy:

```text
Describe work you want prepared. Aixion will create a controlled Work Order. Nothing executes from this screen.
```

After creation:

```text
Work Order created.
View in Work Orders.
Next step: review scope before approval or execution.
```

## Backend/API alignment

Existing core pieces:

```text
POST /agent/tasks
GET /agent/tasks
GET /agent/tasks/{task_id}
GET /agent/tasks/{task_id}/events
POST /agent/tasks/{task_id}/events
POST /agent/tasks/{task_id}/approval
GET /approvals
POST /approvals/{approval_id}/decision
GET /connectors
POST /connectors
GET /connectors/templates
POST /connectors/{connector_id}/secret/issue
POST /connectors/{connector_id}/simulate
GET /work-orders
POST /work-orders
```

Design rule:

```text
External agents may create tasks and append progress events.
External agents must not decide approvals.
Mobile/operator decides approvals.
Worker execution must wait for approved state.
```

## Issue consolidation

### #159 — Define Work Orders purpose and lifecycle

Resolved by roadmap when:

```text
Work Orders screen displays lifecycle, source, status, next action, and links to approval/agent work.
```

Implementation PR:

```text
PR A2 — Clarify Work Orders lifecycle and source model
```

### #160 — Define Command tab behavior for real connected agents

Resolved by roadmap when:

```text
Command is locked as manual work composer.
Connected-agent work is routed through Connectors -> Agent Work -> Approvals.
```

Implementation PR:

```text
PR A1 — Lock connected-agent navigation language and route model
PR A3 — Clarify Command create-work-order behavior
```

### #161 — Clarify Generate Work Order vs Create Work Order

Resolved by roadmap when:

```text
Only Create Controlled Work Order remains unless Generate becomes a real draft-only action.
```

Implementation PR:

```text
PR A3 — Clarify Command create-work-order behavior
```

### #162 — Reevaluate Review tab after Home metric-card navigation

Resolved by roadmap when:

```text
Review is renamed to Approvals or Queue, and Home cards route to filtered destinations.
```

Implementation PR:

```text
PR A1 — Lock connected-agent navigation language and route model
PR A4 — Add filtered approval destinations from Home cards
```

### #163 — Define Agents screen scaling and connected-agent display model

Resolved by roadmap when:

```text
Agents is renamed or explained as Agent Work, with provider/status/risk/linked approval/timeline fields and filters.
```

Implementation PR:

```text
PR A5 — Add Agent Work filters and connected-agent context
```

### #164 — Clarify Connectors onboarding and agent connection flow

Resolved by roadmap when:

```text
Connectors shows setup stages, next action, credential/setup/test flow, and clear distinction between templates and configured connectors.
```

Implementation PR:

```text
PR A6 — Finish connector onboarding stage model and next-action guidance
```

Do not close #159 through #164 from documentation alone. Close each issue only when its implementation PR lands.

## Implementation PR sequence

### PR A0 — Add connected-agent interaction roadmap

Scope:

```text
Add this roadmap.
Link it from README.
Use it as the governing source for #159 through #164.
No runtime behavior changes.
```

Acceptance:

```text
Roadmap explains user interaction model.
Roadmap maps #159 through #164 to implementation PRs.
Roadmap prevents random isolated fixes.
```

### PR A1 — Lock connected-agent navigation language and route model

Scope:

```text
Rename Review label to Approvals.
Rename Agents label/screen copy to Agent Work or Tasks.
Expand Conn label/copy to Connectors where space allows.
Add short concept explainer cards for Connectors, Agent Work, Approvals, Work Orders, and Command.
No backend changes.
```

Acceptance:

```text
User can tell what each primary tab does.
Navigation labels match the locked interaction model.
No screen claims external-agent execution is complete if it is not.
```

### PR A2 — Clarify Work Orders lifecycle and source model

Scope:

```text
Add lifecycle panel to Work Orders.
Show source, status, next action, linked approval, linked AgentTask where data exists.
Improve empty/loading/error states.
```

Acceptance:

```text
User understands what a Work Order is.
User understands that a Work Order does not execute code by itself.
User can see where work came from and what happens next.
```

### PR A3 — Clarify Command create-work-order behavior

Scope:

```text
Remove fake Generate Work Order badge unless implemented as a real draft-only action.
Make Create Controlled Work Order the single action.
After creation, show View in Work Orders next action.
Remove or clearly mark offline fallback behavior.
```

Acceptance:

```text
Generate vs Create confusion is gone.
User knows created work appears under Work Orders.
User knows nothing executes from Command.
```

### PR A4 — Add filtered approval destinations from Home cards

Scope:

```text
Add approval filters for pending/requested, blocked, approved/execution-relevant states.
Home Action opens pending/requested filter.
Home Blocked opens blocked filter.
Bottom Approvals opens full queue.
```

Acceptance:

```text
Home card counts match destination content.
Review/Approvals no longer feels duplicated.
User lands exactly where the card promised.
```

### PR A5 — Add Agent Work filters and connected-agent context

Scope:

```text
Add filters by status/provider/project where current API supports it.
Show connector/source context where data exists.
Add stronger empty state: Connect an agent first.
Add linked approval and timeline affordances.
```

Acceptance:

```text
Screen scales beyond 5 agents/tasks.
User knows this screen is submitted work, not connector setup.
Waiting-for-approval tasks are obvious.
```

### PR A6 — Finish connector onboarding stage model and next-action guidance

Scope:

```text
Show setup stage per connector.
Show next action per connector.
Separate templates from configured connectors more strongly.
Improve ChatGPT and Codex setup copy.
Keep credential/setup/test actions full-width and readable.
```

Acceptance:

```text
First-time user can connect ChatGPT/Codex without guessing.
Connector card explains whether it is usable.
User knows where submitted work will appear.
```

## Release/demo script after implementation

Use this demo flow:

```text
1. Open Connectors.
2. Create ChatGPT Actions Bridge.
3. Issue credential.
4. Copy setup block.
5. Submit sample task.
6. Open Agent Work.
7. Show task waiting for approval.
8. Open linked approval.
9. Approve / deny / request revision.
10. Return to Agent Work and show updated status/timeline.
```

This is the product story. Anything that does not support this story is secondary.

## Non-goals for this roadmap

```text
No fake production ChatGPT callback claims.
No auto-approval by agents.
No direct raw approval-decision endpoint exposed to GPT Actions.
No pretending Codex/GitHub worker execution is complete until the worker loop is implemented and validated.
No Play Store claim based on docs only.
```

## Completion definition

This roadmap is complete when a first-time user can answer these questions without external explanation:

```text
How do I connect ChatGPT/Codex?
Where does submitted agent work appear?
Where do I approve or deny it?
What happens after I approve?
What is a Work Order?
What is Command for?
What is the difference between Connector and Agent Work?
```

If the app cannot answer those in the UI, the product architecture is still leaking complexity.