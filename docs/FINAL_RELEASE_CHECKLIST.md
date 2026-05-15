# Final Release Checklist

This checklist is the final gate before treating Aixion Control Tower as demo-ready or release-candidate software.

## Release rule

Do not add more core features during release validation. Fix only blockers, correctness bugs, security gaps, broken docs, compile failures, or demo-breaking UX issues.

## Required validation

### Backend

```bash
cd backend
python -m pytest
```

Required result:

```text
all backend tests pass
no skipped critical tests without explicit reason
no startup failure in demo/local profile
```

### Android

```bash
cd mobile/android
./gradlew testDebugUnitTest
./gradlew assembleDebug
```

Required result:

```text
unit tests pass
APK builds
app launches against configured backend
```

### End-to-end demo smoke

Follow `docs/DEMO_SCRIPT.md` from start to finish.

Required result:

```text
idea/work order/approval flow works
mobile approval/rejection flow works
GitHub branch/PR worker path is demonstrable
connector template/simulator flow works
container validation failure/pass behavior is explainable
```

## Security gates

Verify these are true before public exposure:

```text
production auth enabled
production env validation enabled
no API key MVP auth in production
no secrets committed
connector secrets issued once and rotated when exposed
HMAC v1 callback headers documented for HMAC connectors
public connector callback URL uses HTTPS
rate limits configured
owner-only connector management
owner-only role/session/invite management
no direct main branch execution
no auto-merge
```

## Data/persistence gates

```text
SQLite persistence configured intentionally for demo/local
migration baseline exists
startup migrations run
recovery snapshot export exists
unknown newer DB versions fail safely
production database limitations are documented
```

## Agent execution gates

```text
AgentTask intake works
AgentTask approval bridge works
approved-task orchestrator creates branch/files/validation/PR
retry and cancellation controls work
containerized validation is enabled by default
container runtime unavailable path fails closed
validation logs are captured in summaries
```

## Connector platform gates

```text
connector registry works
connector credential issue/rotate/revoke works
scoped external AgentTask access works
generic inbound webhook works
schema mapper works
templates are visible
Android connector console works
simulator returns accepted/errors/warnings
HMAC v1 replay/stale timestamp protections exist
```

## Android UX gates

```text
login/register works
approval inbox works
approval detail works
diff view works
AgentTask screen works
MCP queue screen works
connector screen works
owner admin screens work
runtime readiness screen works
```

## Stop-build criteria

After this checklist passes, stop core feature development and move to:

```text
real device testing
deployment rehearsal
Play Store preparation
UX polish
bug fixes
```

Do not keep adding features to avoid the harder release work.
