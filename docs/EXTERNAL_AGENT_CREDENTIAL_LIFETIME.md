# External agent credential lifetime

External-agent credentials used by Antigravity and Codex connectors have a
bounded lifetime. The default is 2,592,000 seconds (30 days). Configure a
shorter lifetime with `AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS`; accepted values
are 300 through 2,592,000 seconds. The server rejects explicit expiries that are
in the past, omit a timezone, or exceed the configured lifetime. Omitting the
expiry during registration or rotation uses the configured lifetime.

Authentication checks the effective expiry in both SQLite and PostgreSQL before
accepting a connector request. Every effective expiry is capped at the configured
lifetime from `token_rotated_at`, or from `created_at` for never-rotated
credentials. Older credentials with no expiry derive that fixed deadline; older
credentials with a later explicit expiry are capped to it. Reading or restarting
the service does not extend the deadline. Once expired, the owner must rotate the
token and update the host connector's secure token store. Rotation invalidates
the previous token.

## Validation

On 2026-10-22, the token-lifetime, existing token-governance, and settings tests
passed **99/99**. The PostgreSQL external-agent auth suite passed **20/20**
against the isolated PostgreSQL test service. The complete backend suite passed
**1,368 tests, 0 skipped, 6 warnings**. Ruff, Python compilation, and
`git diff --check` passed.

From `backend/`, run:

```bash
python -m pytest -q tests/test_agent_token_lifetime.py
python -m pytest -q tests/test_postgres_external_agent_auth_repository.py
ruff check app/agent_token_lifetime.py app/agent_auth.py app/agent_auth_repository.py app/postgres_external_agent_auth_repository.py app/settings.py tests/test_agent_token_lifetime.py tests/test_postgres_external_agent_auth_repository.py
```

The PostgreSQL integration test requires `AIXION_TEST_POSTGRES_DSN` and uses a
unique disposable schema. No database schema migration is required: the expiry
and issue-time fields already exist in the external-agent credential record.

## Rollout

1. Set `AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS` to the reviewed value on every
   API process. Keep it at or below 30 days.
2. Deploy the API. Existing credentials without explicit expiry remain usable
   only until their derived issue-time deadline; connectors older than the
   configured lifetime must be rotated.
3. Verify each host has stored the newly returned token in its secure credential
   store. The API returns the raw token only during registration or rotation.
4. Confirm the owner's credential status shows an expiry. Revoke any lost or
   unconfirmed token; do not extend a credential by editing its database row.

This is a local implementation contract. It does not prove that either native
provider session completes an approval round trip through Android.
