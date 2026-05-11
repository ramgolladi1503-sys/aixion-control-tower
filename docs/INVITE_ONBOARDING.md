# Invite Onboarding

Invite onboarding closes the biggest remaining auth hole after owner-only role management: unrestricted self-registration is not a serious production posture.

This document covers the first backend invite layer only. Invite acceptance registration is intentionally split into a later PR.

## Current scope

PR #60 adds owner-controlled invite administration:

```text
POST /auth/invites
GET  /auth/invites
POST /auth/invites/{invite_id}/revoke
```

It does not yet change `/auth/register` to require an invite token. That belongs in the next PR so the API and tests stay reviewable.

## Invite model

Each invite stores:

```text
id
email
role
token_hash
status
expires_at
created_by_user_id
accepted_by_user_id
created_at
updated_at
```

Invite status values:

```text
PENDING
ACCEPTED
EXPIRED
REVOKED
```

The backend returns the raw invite token only when an owner creates an invite. It stores only the token hash.

Hard truth: storing raw invite tokens would be lazy security. Even for an MVP, that is the wrong habit.

## Create invite

```http
POST /auth/invites
Authorization: Bearer OWNER_TOKEN
Content-Type: application/json
```

Default body:

```json
{
  "email": "reviewer@example.com"
}
```

Explicit role body:

```json
{
  "email": "maintainer@example.com",
  "role": "MAINTAINER",
  "expires_in_days": 7
}
```

Behavior:

```text
OWNER only
role defaults to REVIEWER
role must be OWNER, MAINTAINER, or REVIEWER
pending duplicate invite for the same email returns 409
successful creation writes auth.invite_created audit event
```

## List invites

```http
GET /auth/invites
Authorization: Bearer OWNER_TOKEN
```

Behavior:

```text
OWNER only
returns invite metadata
never returns raw invite token
expired pending invites are surfaced as EXPIRED
```

## Revoke invite

```http
POST /auth/invites/INVITE_ID/revoke
Authorization: Bearer OWNER_TOKEN
```

Behavior:

```text
OWNER only
pending invite becomes REVOKED
accepted invite cannot be revoked
expired invite cannot be revoked
successful revocation writes auth.invite_revoked audit event
```

## Validation commands

```bash
cd backend
python -m pytest tests/test_invite_onboarding.py
python -m pytest tests/test_role_management.py
python -m pytest
```

## Next PR

PR #61 should wire invite acceptance into registration:

```text
register with invite token
validate token hash
reject revoked/expired/accepted invites
create user with invite role
mark invite ACCEPTED
set accepted_by_user_id
write auth.invite_accepted audit event
optionally disable open self-registration after bootstrap owner exists
```

Do not claim onboarding is production-grade until PR #61 removes open registration for non-bootstrap users.
