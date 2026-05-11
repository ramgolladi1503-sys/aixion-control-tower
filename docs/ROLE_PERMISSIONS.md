# Role Permissions

This document defines the first backend role-based permission model for Mobile Approval Console / Aixion Control Tower.

This is not enterprise RBAC yet. It is the minimum serious permission boundary needed after adding authenticated Android sessions.

## Roles

| Role | Purpose |
| --- | --- |
| `OWNER` | Owns the control tower, manages agents, manages user roles, and controls all operational surfaces. |
| `MAINTAINER` | Can create and execute controlled work, register MCP child servers, recover queues, and run GitHub execution paths. |
| `REVIEWER` | Can inspect work and make approval decisions from the mobile approval flow. |

## Self-registration rule

Self-registration follows this rule:

```text
first registered user -> OWNER
later registered users -> REVIEWER
```

This prevents every new self-registered user from becoming an owner.

## Owner role management

The backend now exposes a minimal owner-only role management surface.

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /auth/roles` | Authenticated users | Return supported role choices. |
| `GET /auth/users` | `OWNER` only | List users with public identity and role fields. |
| `PATCH /auth/users/{user_id}/role` | `OWNER` only | Promote or demote a user between `OWNER`, `MAINTAINER`, and `REVIEWER`. |

Role update body:

```json
{
  "role": "MAINTAINER"
}
```

Safety guard:

```text
The backend blocks demoting the last active OWNER.
```

Audit behavior:

```text
successful role changes append auth.role_updated audit events
```

This closes the clumsy-administration gap from the first role-permission layer. It still does not replace invite-based onboarding or project-scoped roles.

## Permission matrix

| Area | OWNER | MAINTAINER | REVIEWER |
| --- | --- | --- | --- |
| Register/login/self session | Yes | Yes | Yes |
| Read supported role choices | Yes | Yes | Yes |
| List users | Yes | No | No |
| Promote/demote users | Yes | No | No |
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
manage user roles
```

## Why maintainers can execute

Maintainers are trusted operators. They can create work and run execution paths, but they do not manage external agents or users.

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
manage user roles
act as owner
```

## Why agent and user management are owner-only

External agents can create approvals and initiate powerful workflows. User role changes decide who can approve, execute, and administer those workflows.

Only owner can:

```text
register external agents
list external agents
inspect agent configuration
list users
promote or demote users
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

## Remaining gaps

Role management is improved, but still not enterprise RBAC.

Missing:

```text
Android owner role-management UI
invite-based onboarding
project-scoped roles
role audit UI
session revocation by owner
```

The current layer is valuable because owners now have a clean backend promotion/demotion path. But it is not complete enterprise-grade RBAC.

## Safe claim

After this role layer passes CI, it is safe to claim:

```text
The backend now enforces first-pass role boundaries and includes owner-only role management with last-owner protection.
```

## Unsafe claim

Do not claim:

```text
Enterprise RBAC is complete.
```

That would be false.

## Brutal truth

Auth without roles means every logged-in user is too powerful. Roles without owner administration means access control is clumsy. This layer fixes the worst administration gap, but project-scoped roles and invite-based onboarding are still missing.
