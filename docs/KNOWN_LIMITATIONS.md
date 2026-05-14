# Known Limitations Lock

This document locks the current honest limitations for the Aixion Control Tower elite MVP/demo track.

The point is not to weaken the product. The point is to prevent fake claims.

## Current product level

```text
Strong demo-grade MVP
Operator-conscious backend
Mobile-first approval console foundation
Not yet production SaaS
Not yet fully connected to ChatGPT/Codex/Claude as live agent sources/workers
```

## Connected-agent limitations

```text
ChatGPT direct task ingestion is not implemented yet.
Codex worker loop is not fully implemented yet.
Claude/external-agent adapter is not implemented yet.
GPT Actions/OpenAPI contract is not implemented yet.
Android Agent Tasks timeline is not implemented yet.
Agent-task notification lifecycle is not implemented yet.
Public callback/deployment path for external agents is not complete yet.
```

## Backend limitations

```text
Deployment is not automated as a production release pipeline.
Monitoring and alerting are not production-grade.
Structured logging exists only at a basic foundation level.
Rate limiting and abuse protection are not yet complete.
Audit archival/deletion automation is not enabled.
Recovery snapshot validation exists, but full restore automation is not implemented.
Production backup/restore drills are not automated.
```

## Android limitations

```text
Android exposes readiness and approval/control surfaces, but full incident workflow UX is not complete.
Android does not yet have a dedicated Agent Tasks timeline screen.
Offline-first behavior is not complete.
Push notification delivery depends on real FCM configuration.
UI polish is demo-usable but not final consumer-grade product polish.
Manual Android QA still matters beyond unit/build checks.
```

## GitHub execution limitations

```text
GitHub execution requires real credentials and safe branch/repository configuration.
Protected branch behavior is guarded, but production policy enforcement still needs environment-specific validation.
The demo smoke script does not prove real GitHub PR creation end-to-end.
GitHub worker observability is not yet production-grade.
Codex-style approved-task-to-PR execution loop is not fully connected yet.
```

## MCP limitations

```text
MCP wait-mode approval foundation exists.
Registered child routing and queue lifecycle foundations exist.
External child MCP behavior still needs broader real-world compatibility testing.
Production MCP gateway security hardening is not complete.
```

## Security/compliance limitations

```text
This is not a certified compliance product.
No formal SOC2/ISO/HIPAA-style evidence pack exists.
Readiness surfaces expose booleans only for sensitive configuration.
Audit export redaction is a guardrail, not a guarantee against bad logging behavior.
Full threat modeling and penetration testing are not complete.
```

## Demo limitations

```text
Demo smoke validation proves core API flow, not every edge case.
Local/demo profile behavior is not the same as production behavior.
A PASS smoke report does not replace CI or manual review.
The demo should not claim automated disaster recovery.
The demo should not claim live ChatGPT/Codex/Claude task ingestion until the connected-agent scope is implemented.
```

## Claims allowed

Safe claims:

```text
serious demo-grade mobile approval console
mobile operator visibility into backend runtime readiness
approval lifecycle with auditability
backend migrations foundation
safe recovery snapshot export and validation foundation
audit export and retention-policy foundation
release validation summary foundation
backend demo smoke validation foundation
MCP wait-mode approval foundation
connected-agent scope is defined for next implementation phase
```

## Claims not allowed

Do not claim:

```text
fully production-ready SaaS
complete observability
full compliance coverage
automated disaster recovery
zero-risk AI/agent execution
finished enterprise security product
fully connected ChatGPT/Codex/Claude control tower
```

## Why this lock exists

A product becomes credible when it can clearly say both:

```text
what works
what is not finished yet
```

Overclaiming would make this project look immature. Clear limitations make it look serious.
