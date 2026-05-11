# Role Permissions

This document defines the first backend role-based permission model for Mobile Approval Console / Aixion Control Tower.

This is not enterprise RBAC yet. It is the minimum serious permission boundary needed after adding authenticated Android sessions.

## Roles

| Role | Purpose |
| --- | --- |
| `OWNER` | Owns the control tower, manages agents, manages user roles, manages invites, and controls all operational surfaces. |
| `MAINTAINER` | Can create and execute controlled work, register MCP child servers, recover queues, and run GitHub execution paths. |
| `REVIEWER` | Can inspect work and make approval decisions from the mobile approval flow. |

## Current registration rule

Self-registration still follows this rule:

```text
first registered user -> OWNER
later registered users -> REVIEWER
```

This prevents every new self-registered user from becoming an owner, but it is not good enough for production onboarding.

Hard truth: open registration after bootstrap is still a weak posture. PR #60 adds invite administration, but invite acceptance must be completed next before onboarding can be called serious.

## Owner role management

The backend exposes a minimal owner-only role management surface.

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

## Owner invite management

The backend now exposes the first invite administration layer.

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `POST /auth/invites` | `OWNER` only | Create an invite for an email and role. |
| `GET /auth/invites` | `OWNER` only | List invite metadata without exposing raw invite tokens. |
| `POST /auth/invites/{invite_id}/revoke` | `OWNER` only | Revoke a pending invite. |

Invite statuses:

```text
PENDING
ACCEPTED
EXPIRED
REVOKED
```

Invite rules:

```text
role defaults to REVIEWER
role must be OWNER, MAINTAINER, or REVIEWER
raw invite token is returned only at creation time
the store persists only the invite token hash
accepted invites cannot be revoked
successful create/revoke actions write audit events
```

Audit behavior:

```text
auth.invite_created
auth.invite_revoked
```

See:

```text
docs/INVITE_ONBOARDING.md
```

## Permission matrix

| Area | OWNER | MAINTAINER | REVIEWER |
| --- | --- | --- | --- |
| Register/login/self session | Yes | Yes | Yes |
| Read supported role choices | Yes | Yes | Yes |
| List users | Yes | No | No |
| Promote/demote users | Yes | No | No |
| Create/list/revoke invites | Yes | No | No |
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
manage invites
```

## Why maintainers can execute

Maintainers are trusted operators. They can create work and run execution paths, but they do not manage external agents, users, or invites.

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
manage invites
act as owner
```

## Why agent, user, and invite management are owner-only

External agents can create approvals and initiate powerful workflows. User role changes decide who can approve, execute, and administer those workflows. Invites decide who can enter the system next.

Only owner can:

```text
register external agents
list external agents
inspect agent configuration
list users
promote or demote users
create/list/revoke invites
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

Role and invite administration are improved, but this is still not enterprise RBAC.

Missing:

```text
invite acceptance registration flow
blocking open registration after bootstrap
project-scoped roles
role and invite audit UI polish
session revocation by owner
production deployment/secrets/monitoring
```

## Safe claim

After this layer passes CI, it is safe to claim:

```text
The backend now enforces first-pass role boundaries and includes owner-only role management plus owner-only invite creation/list/revoke APIs.
```

## Unsafe claim

Do not claim:

```text
Enterprise RBAC is complete.
Invite onboarding is production-grade.
Open registration is solved.
```

Those would be false.

## Brutal truth

Auth without roles means every logged-in user is too powerful. Roles without owner administration are clumsy. Owner administration without invites still leaves onboarding weak. This invite layer fixes administration, but PR #61 must wire invite acceptance before onboarding becomes serious.
