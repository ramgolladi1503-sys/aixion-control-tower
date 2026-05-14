# Connected-Agent Scope

This document locks the original Aixion Control Tower vision into scope.

Aixion is not only an approval dashboard. The target product is a mobile-first control tower for external AI agents such as ChatGPT, Codex, Claude, Cursor, GitHub Actions workers, and manual operators.

## Product promise

External agents submit work into Aixion. Aixion tracks the task, classifies risk, creates approvals when needed, records audit events, and shows progress on Android. After approval, a worker continues execution and reports tests, pull requests, failures, and final evidence back to Aixion.

## Target loop

1. ChatGPT, Codex, Claude, Cursor, or a manual operator creates a task.
2. Aixion receives the task through an agent task API.
3. Aixion creates a task record and risk decision.
4. Risky work creates or links an approval request.
5. Android shows the task, approval, timeline, and latest event.
6. The operator approves, denies, cancels, or reviews the task.
7. Approved workers execute safely, run tests, open PRs where needed, and report status back.
8. Audit, recovery, release summary, and demo evidence capture the outcome.

## Required future scope

The elite MVP must add:

- AgentTask and AgentTaskEvent models
- Agent task ingestion API
- task-to-approval bridge
- provider model for CHATGPT, CODEX, CLAUDE, CURSOR, GITHUB_ACTIONS, MANUAL, OTHER
- GPT Actions/OpenAPI contract
- Codex/GitHub worker contract
- Android Agent Tasks screen
- agent task timeline/events
- task notification lifecycle
- secure public callback or deployment guide
- human-control policy rules

## Required API shape

Planned endpoints:

- POST /agent/tasks
- GET /agent/tasks
- GET /agent/tasks/{task_id}
- POST /agent/tasks/{task_id}/events
- POST /agent/tasks/{task_id}/cancel

## Required lifecycle

Initial task statuses:

- RECEIVED
- PLANNING
- WAITING_FOR_APPROVAL
- APPROVED
- DENIED
- EXECUTING
- TESTING
- READY_FOR_PR
- FAILED
- CANCELLED
- DONE

## Android requirement

Android needs an Agent Tasks screen showing provider, project, task title, status, risk, linked approval, latest event, PR/result link, error summary, and task timeline.

## Worker requirement

A Codex/GitHub worker should claim approved tasks, create a safe branch, apply changes, run tests, commit, open a PR, write task events, write audit events, and report failures clearly.

See:

- `docs/AGENT_WORKER_CONTRACT.md`
- `docs/AGENT_WORKER_DRY_RUN_PLAN.md`

## GPT Actions requirement

Aixion needs a GPT Actions/OpenAPI contract so ChatGPT or a Custom GPT can create tasks and check status safely. The contract must expose only safe agent operations, not owner-only controls or unsafe reset operations.

## Public access requirement

External agents cannot reliably call a laptop-only localhost backend. Demo mode may use a secure temporary tunnel. Production mode needs a deployed HTTPS backend with token authentication, rate limiting, and audit logging.

See:

- `docs/PUBLIC_HTTPS_CALLBACK_GUIDE.md`

## Current honest status

Already strong:

- backend control plane
- approval lifecycle
- audit, recovery, readiness foundation
- Android operator screens
- MCP approval/wait-mode foundation
- demo smoke validation
- seeded demo data
- release/demo evidence chain
- AgentTask and AgentTaskEvent models
- agent task ingestion API
- task-to-approval bridge
- provider model for CHATGPT, CODEX, CLAUDE, CURSOR, GITHUB_ACTIONS, MANUAL, OTHER
- GPT Actions/OpenAPI contract
- Android Agent Tasks screen
- agent task timeline/events
- AgentTask notification lifecycle
- Codex/GitHub worker contract documentation
- dry-run worker lifecycle
- transactional worker claim foundation
- public HTTPS callback/deployment guide

Not fully built yet:

- production ChatGPT direct task ingestion validation
- Codex/GitHub worker implementation
- Claude/external-agent adapter
- task cancellation endpoint
- Android deep-link routing into exact task/approval screens
- real device FCM proof
- notification retry queue
- adaptive Android layout polish for tablets/folds/flips

Hard truth: Aixion is now past approval-console foundation and into connected-agent control-tower territory. It still needs the actual worker loop before it can claim end-to-end connected-agent execution.
