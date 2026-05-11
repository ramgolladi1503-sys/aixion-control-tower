# Role Permissions

This document defines the first backend role-based permission model for Mobile Approval Console / Aixion Control Tower.

This is not enterprise RBAC yet. It is the minimum serious permission boundary needed after adding authenticated Android sessions.

## Roles

| Role | Purpose |
| --- | --- |
| `OWNER` | Owns the control tower, manages agents and all operational surfaces. |
| `MAINTAINER` | Can create and execute controlled work, register MCP child servers, recover queues, and run GitHub execution paths. |
| `REVIEWER` | Can inspect work and make approval decisions from the mobile approval flow. |

## Self-registration rule

Self-registration follows this rule:

```text
first registered user -> OWNER
later registered users -> REVIEWER
```

This prevents every new self-registered user from becoming an owner.

Hard truth: role-promotion management is not fully solved yet. The backend now enforces roles, but a polished owner-only role-management UI/API is still future work.

## Permission matrix

| Area | OWNER | MAINTAINER | REVIEWER |
| --- | --- | --- | --- |
| Register/login/self session | Yes | Yes | Yes |
| Read projects, ideas, work orders | Yes | Yes | Yes |
| Read approvals/grouped approvals | Yes | Yes | Yes |
| Read test runs | Yes | Yes | Yes |
| Read audit | Yes | Yes | Yes |
| Make approval decisions | Yes | Yes | Yes |
| Create projects | Yes | Yes | No |
| Create ideas | Yes | Yes | No |
| Create work orders | Yes | Yes | No |
| Create manual approvals | Yes | Yes | No |
| Record test runs | Yes | Yes | No |
| Submit MCP gateway requests | Yes | Yes | No |
| Retry/recover MCP pending requests | Yes | Yes | No |
| Resolve MCP approvals into forwarding | Yes | Yes | No |
| Register/enable/disable MCP child servers | Yes | Yes | No |
| Use MCP JSON-RPC transport endpoint | Yes | Yes | No |
| Run GitHub patch plan/execution endpoints | Yes | Yes | No |
| Register/list/get external agents | Yes | No | No |

## Why reviewers can approve

The product is a mobile approval console. A reviewer role that cannot review or decide approvals would be useless.

Reviewer can:

```text
inspect approvals
approve/deny/request revision
inspect audit evidence
inspect MCP queue status
```

Reviewer cannot:

```text
create projects
create work orders
create manual approvals
register agents
register child MCP servers
trigger GitHub execution
recover/retry MCP queue items
```

## Why maintainers can execute

Maintainers are trusted operators. They can create work and run execution paths, but they do not manage external agents.

Maintainer can:

```text
create projects/work/approvals
register MCP child servers
submit/resolve MCP calls
run GitHub execution endpoints
record test evidence
```

Maintainer cannot:

```text
register external agents
list external agent credentials/surfaces
act as owner
```

## Why agent management is owner-only

External agents can create approvals and initiate powerful workflows. Agent registration is a high-control surface.

Only owner can:

```text
register external agents
list external agents
inspect agent configuration
```

## Auth-disabled profile behavior

When auth is disabled, the backend uses a local dev identity:

```text
id: dev_user
email: dev@local
role: OWNER
```

This keeps local/demo scripts working without requiring login.

Do not confuse auth-disabled demo behavior with production security.

## Known gap

Role management is still incomplete.

Missing:

```text
owner-only user role management endpoint
Android role visibility/promotion UX
invite-based onboarding
project-scoped roles
role audit UI
```

The current PR is still valuable because backend enforcement now exists. But it is not complete enterprise-grade RBAC.

## Safe claim

After this role layer passes CI, it is safe to claim:

```text
The backend now enforces first-pass role boundaries for owner, maintainer, and reviewer operations.
```

## Unsafe claim

Do not claim:

```text
Enterprise RBAC is complete.
```

That would be false.

## Brutal truth

Auth without roles means every logged-in user is too powerful. This role layer fixes that broad trust problem, but it is still only the first serious RBAC step.
