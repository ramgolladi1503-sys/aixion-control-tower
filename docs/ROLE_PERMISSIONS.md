# Role Permissions

This document defines the first backend role-based permission model for Mobile Approval Console / Aixion Control Tower.

This is not enterprise RBAC yet. It is the minimum serious permission boundary needed after adding authenticated Android sessions.

## Roles

| Role | Purpose |
| --- | --- |
| `OWNER` | Owns the control tower, manages agents, manages user roles, manages invites, manages sessions, and controls all operational surfaces. |
| `MAINTAINER` | Can create and execute controlled work, register MCP child servers, recover queues, and run GitHub execution paths. |
| `REVIEWER` | Can inspect work and make approval decisions from the mobile approval flow. |

## Registration rule

Registration now follows this rule:

```text
first registered user -> OWNER without invite
later registered users -> require valid pending invite token
```

Later users receive the role attached to the invite.

This closes the open self-registration hole after bootstrap. It is still not enterprise onboarding because invite delivery, resend/rotate, project-scoped invites, and polished admin UX are not done.

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

The backend exposes invite administration and invite-token registration acceptance.

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `POST /auth/invites` | `OWNER` only | Create an invite for an email and role. |
| `GET /auth/invites` | `OWNER` only | List invite metadata without exposing invite codes. |
| `POST /auth/invites/{invite_id}/revoke` | `OWNER` only | Revoke a pending invite. |
| `POST /auth/register` | Public bootstrap or invite holder | Bootstrap first owner or accept a valid invite. |

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
invite code is returned only at creation time
the store persists only the invite token hash
accepted invites cannot be revoked
later registrations require a matching pending invite token
accepted invite is marked ACCEPTED and linked to accepted_by_user_id
successful create/revoke/accept actions write audit events
```

Audit behavior:

```text
auth.invite_created
auth.invite_revoked
auth.invite_accepted
```

See:

```text
docs/INVITE_ONBOARDING.md
```

## Owner session management

The backend exposes a first-pass owner-only session control layer.

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /auth/sessions` | `OWNER` only | List session metadata without exposing token hashes. |
| `POST /auth/users/{user_id}/sessions/revoke` | `OWNER` only | Revoke active sessions for another user. |

Session rules:

```text
session list never returns token_hash
owner can revoke another user's active sessions
owner cannot revoke their own sessions through the force-logout endpoint
revoked sessions immediately fail /auth/me with 401
successful revocation writes auth.sessions_revoked audit event
```

Audit behavior:

```text
auth.sessions_revoked
```

This is not full enterprise session governance yet. It does not include device fingerprinting, IP/User-Agent metadata, refresh-token rotation, or Android UI for session management.

## Permission matrix

| Area | OWNER | MAINTAINER | REVIEWER |
| --- | --- | --- | --- |
| Bootstrap first user | Yes | N/A | N/A |
| Register with valid invite | Yes | Yes | Yes |
| Login/self session | Yes | Yes | Yes |
| Read supported role choices | Yes | Yes | Yes |
| List users | Yes | No | No |
| Promote/demote users | Yes | No | No |
| Create/list/revoke invites | Yes | No | No |
| List/revoke sessions | Yes | No | No |
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
manage sessions
```

## Why maintainers can execute

Maintainers are trusted operators. They can create work and run execution paths, but they do not manage external agents, users, invites, or sessions.

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
manage sessions
act as owner
```

## Why agent, user, invite, and session management are owner-only

External agents can create approvals and initiate powerful workflows. User role changes decide who can approve, execute, and administer those workflows. Invites decide who can enter the system next. Sessions decide who remains logged in.

Only owner can:

```text
register external agents
list external agents
inspect agent configuration
list users
promote or demote users
create/list/revoke invites
list sessions
force logout another user
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

Role, invite onboarding, and session revocation are improved, but this is still not enterprise RBAC.

Missing:

```text
email delivery for invites
invite resend/rotate
dedicated Android invite-admin screen
project-scoped roles
role/invite/session audit UI polish
Android session-management UI
refresh-token rotation
production deployment/secrets/monitoring
```

## Safe claim

After this layer passes CI, it is safe to claim:

```text
The backend now enforces first-pass role boundaries, owner-only role management, owner-only invite administration, invite-token registration after bootstrap, and owner-only session revocation for force logout.
```

## Unsafe claim

Do not claim:

```text
Enterprise RBAC is complete.
Enterprise onboarding is complete.
Enterprise session governance is complete.
```

Those would be false.

## Brutal truth

Auth without roles means every logged-in user is too powerful. Roles without owner administration are clumsy. Owner administration without invite acceptance leaves onboarding weak. Invite onboarding without session revocation leaves stale access hanging around. This layer finally adds force logout, but enterprise governance still needs device metadata, rotation, project scoping, and admin UI polish.
