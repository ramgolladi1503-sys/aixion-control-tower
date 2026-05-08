# Product Scope — Aixion Control Tower

## Definition

Aixion Control Tower is a mobile-first AI project execution cockpit. It turns ideas into structured project plans, creates agent work orders, reviews proposed changes, scores risk, enforces approval rules, triggers tests, and safely applies approved patches through GitHub workflows.

## Core Pillars

### 1. Idea Capture

The mobile app must let the user quickly capture raw ideas using text or voice and attach those ideas to either a new project or an existing project.

Examples:

- Build a Job Application Agent that creates tailored resumes and recruiter messages from a job description.
- Improve Tradebot feed stability so stale market data never becomes executable.

### 2. Project Brain

Every project must maintain structured memory:

- project goals
- risk rules
- important files
- previous decisions
- test requirements
- forbidden actions
- active branches and pending work

### 3. Agent Work Orders

Ideas are not sent directly to file editing. They are converted into work orders containing:

- goal
- context
- files likely affected
- implementation steps
- risk level
- required tests
- rollback plan
- approval checkpoints

### 4. Risk-Based Approval

Every proposed action must be classified as LOW, MEDIUM, HIGH, CRITICAL, or BLOCKED before the user approves it.

### 5. Mobile Diff Review

Approvals must show clear diffs, affected files, test impact, risk explanation, and rollback guidance.

### 6. Test Gate

Approval is not the same as merge. The correct flow is approval, branch application, tests, audit, and then PR readiness.

### 7. Audit Trail

Every request, decision, test result, and patch application must be recorded.

## MVP Outcome

The MVP proves the complete human-in-the-loop workflow using a working backend API and mocked or local agent requests.

## Elite Outcome

The elite version adds multi-agent review, approval intelligence score, project health scoring, daily briefings, rollback intelligence, and safe autonomy modes.
