# Invite Onboarding

Invite onboarding closes the biggest remaining auth hole after owner-only role management: unrestricted self-registration is not a serious production posture.

## Current scope

The backend supports owner-controlled invite administration and invite-token registration acceptance.

Invite administration:

```text
POST /auth/invites
GET  /auth/invites
POST /auth/invites/{invite_id}/revoke
```

Registration acceptance:

```text
POST /auth/register
```

Android Account now supports:

```text
enter invite code during registration
owner create invite
owner list invites
owner revoke pending invite
```

Registration rule:

```text
first registered user -> OWNER without invite
all later registrations -> require a valid pending invite token
```

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

The backend returns the invite code only when an owner creates an invite. It stores only the token hash.

Hard truth: storing reusable plain invite codes would be lazy security. Even for an MVP, that is the wrong habit.

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

## Android owner invite management

The Android Account screen includes an owner invite-management panel after login.

Owner can:

```text
enter invited email
choose OWNER / MAINTAINER / REVIEWER
create invite
copy the one-time invite code shown after creation
refresh invite list
revoke pending invite
see accepted invite metadata
```

Non-owner behavior:

```text
backend returns 403
Android shows a clear owner-only error
```

The invite code is intentionally shown only immediately after creation. The list endpoint never returns it.

## Register with invite

The first user can bootstrap without an invite and becomes OWNER.

Every later user must register with the invite code issued by an owner.

```http
POST /auth/register
Content-Type: application/json
```

Body:

```json
{
  "email": "maintainer@example.com",
  "password": "valid-password-123",
  "display_name": "Maintainer",
  "invite_token": "INVITE_CODE_FROM_OWNER"
}
```

Behavior:

```text
missing invite after bootstrap returns 403
unknown invite returns 404
revoked, expired, or accepted invite returns 409
email must match the invite email
new user gets the role from the invite
accepted invite is marked ACCEPTED
accepted_by_user_id is set
auth.invite_accepted audit event is written
```

## Android registration

Android Account registration includes an invite code field.

```text
first owner: leave invite code blank
later users: paste owner-issued invite code
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
never returns raw invite code
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

Backend validation:

```bash
cd backend
python -m pytest tests/test_invite_onboarding.py
python -m pytest tests/test_role_management.py
python -m pytest
```

Android validation:

```bash
cd mobile/android
./gradlew testDebugUnitTest
./gradlew assembleDebug
./gradlew assembleRelease
```

Focused Android invite-admin test:

```bash
cd mobile/android
./gradlew testDebugUnitTest --tests '*InviteAdminRepositoryTest'
```

## Remaining gaps

Invite onboarding is now serious enough for a demo, but still not enterprise-grade.

Missing:

```text
email delivery for invites
invite resend/rotate
polished dedicated Android admin screen
role/invite audit UI polish
project-scoped invites or project-scoped roles
owner session revocation
```

Do not claim enterprise onboarding is complete until those are addressed.
