# Smoke Test Matrix

Use this matrix before demos, release candidates, and deployment rehearsals.

## Backend smoke

```text
health endpoint responds
runtime readiness endpoint responds
auth register/login/me works in configured profile
project list/create works
approval list/detail/decision works
audit list works
```

## Android smoke

```text
app launches
login/register works
bottom navigation works
approval inbox loads
approval detail opens
diff view opens
approve/reject/revise actions call backend
AgentTask screen loads
MCP queue screen loads
connector screen loads
runtime readiness screen loads
```

## GitHub worker smoke

```text
approved AgentTask can create branch
approved AgentTask can apply file changes
validation executes in containerized path
validation failure stops PR creation
validation pass allows PR creation
ready-for-PR status is visible
summary event is written
```

## MCP smoke

```text
mutating MCP request creates/links approval
pending request appears in MCP queue
approval resolves pending request
retry path works for retryable request
dead-letter/health counters are understandable
```

## Connector smoke

```text
connector templates list
connector can be created from template
secret can be issued, rotated, revoked
schema mapper can be applied
simulator returns accepted result for valid payload
simulator returns errors for invalid scope
HMAC v1 webhook succeeds with valid signature
HMAC v1 rejects stale timestamp
HMAC v1 rejects reused nonce
```

## Owner admin smoke

```text
owner can list users
owner can change roles
owner can create/revoke invites
owner can list sessions
owner can revoke target user sessions
```

## Failure smoke

```text
missing production config fails startup in production profile
unknown newer DB migration fails safely
container runtime unavailable fails validation closed
connector missing credential refuses live webhook
out-of-scope connector payload is refused
```

## Demo pass criteria

A demo is passable only if:

```text
core mobile screens load
approval flow is explainable
connector flow is explainable
worker path is explainable
failure behavior is explainable
no claim depends on unimplemented automation
```
