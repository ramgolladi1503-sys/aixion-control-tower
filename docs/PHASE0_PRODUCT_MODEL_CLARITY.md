# Phase 0 Product Model Clarity

This document defines the user-facing meaning of the main Phase 0 product concepts. It exists because the app had working screens, but the mental model was unclear to a first-time user.

## Core rule

Aixion is not a chat app and not a generic task tracker.

Aixion is a mobile approval console for agent-generated or operator-generated work. External agents can prepare work, but the phone remains the decision point.

## Main objects

### Connector

A connector is the configured doorway from an external tool into Aixion.

Examples:
- ChatGPT Actions Bridge
- Codex Agent Bridge
- Claude or Cursor Agent
- Gemini Custom Agent
- Local Agent Bridge

Connector answers:
- who is allowed to call Aixion
- how the external tool authenticates
- what payload shape it sends
- what projects or repositories it can touch
- whether credentials are configured

A template is not a live connection. A configured connector is the live connection.

### Agent Task

An Agent Task is a request submitted by a connected agent or external workflow.

It is the inbox item before work becomes an approval, execution run, PR, or failure record.

Agent Task answers:
- which agent submitted the work
- what goal was requested
- which repo/project is involved
- whether approval is required
- what timeline events happened
- whether the work is waiting, approved, executing, failed, ready for PR, or done

### Work Order

A Work Order is the structured execution package.

It converts a raw command or agent request into a clearer package containing:
- goal
- tasks
- risk
- required tests
- source/provenance
- project context

A Work Order is not automatically approval. A Work Order can lead to approval, but the UI must show that relationship explicitly.

Work Order answers:
- what work is being prepared
- why it exists
- who or what created it
- what tests are expected
- what risk level it carries

### Approval

An Approval is the decision gate.

It is where the phone user approves, rejects, or requests revision before sensitive work continues.

Approval answers:
- what exact action needs permission
- what risk/context is attached
- what files/commands/test plans are proposed
- what happens after approve/reject/revise

### GitHub Execution

GitHub Execution is the worker path after approval.

It should be explained as:

> Approved work is being prepared for GitHub review: branch, file changes, validation, and pull request evidence.

It should not be shown as unexplained internal jargon.

GitHub Execution states should tell the user:
- approved but not started
- branch/file work running
- validation running
- ready for PR
- failed and needs review

### Command

Command is the operator input surface.

It lets the user turn a rough instruction into controlled work. In Phase 0, Command creates controlled Work Orders. Later, it may route through selected agents or templates.

Command should not pretend that typed input magically executes work. It prepares work for review.

### Review / Action Queue

Review is the decision queue.

If Home metric cards become the main entry point, Review should either:
- become a filtered destination opened from Home, or
- be renamed to Action Queue / Approvals

The app should avoid duplicate paths that show the same pending work without explaining why.

## First-time user flow

Recommended mental model:

1. Connect an agent or use Command.
2. The agent/user creates an Agent Task or Work Order.
3. The work becomes an Approval when it needs permission.
4. The phone user approves, rejects, or asks for revision.
5. Approved work enters GitHub Execution if configured.
6. Worker output becomes PR/evidence/failure status.

## UI wording rules

Use direct words:
- Action
- Review queue
- Work package
- Connected agent
- Approval required
- Ready for PR
- Failed validation

Avoid unexplained words:
- human action
- execution, without explanation
- work order, without lifecycle context
- agent, without source/status
- template, without explaining that it is not connected yet

## Phase 0 implementation policy

Phase 0 should prioritize clarity over more features.

Before Phase 1 starts, a first-time user should understand:
- what to connect
- where connected-agent work appears
- what a Work Order does
- why approval is required
- what happens after approval
- where failed or blocked work goes
