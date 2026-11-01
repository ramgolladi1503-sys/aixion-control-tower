# Current-Source Persistence Inventory — 2026-10-30

| Field | Value |
| --- | --- |
| Repository | `ramgolladi1503-sys/aixion-control-tower` |
| Branch | `ram/pre-billing-readiness-snapshot` |
| Source commit | `9a87ae396f26403f9970a78ff52b21b1f4583de6` |
| Generated | `2026-10-30 01:34:01 UTC` |
| Command | `python3 backend/scripts/inventory_persistence.py --backend-root backend` |
| Inventory format | `aixion-persistence-inventory-v7` |
| SHA-256 of generated JSON | `a31d35307ea26f2a39634bb714f3c41cb1bf4b29841a0ea85f9432e574126691` |

## Static inventory

| Measure | Count |
| --- | ---: |
| SQLite map reads | 297 |
| SQLite map writes | 167 |
| Explicit `store.persist()` calls | 94 |
| Direct SQLite persistence-boundary accesses | 33 |
| Application modules represented | 53 |
| Process-local store maps | 31 |
| Decorated routes | 148 |
| Routes with statically reachable SQLite accesses | 146 |
| Route-attributed access references | 1,478 |
| Unique route access sites | 453 |
| Lifespan/background entries | 9 |
| Background entries with statically reachable persistence accesses | 5 |
| Guarded script entrypoints | 28 |
| Script entrypoints with statically reachable persistence accesses | 12 |

Highest static route fan-out includes:

| Method and route | Reachable SQLite access sites |
| --- | ---: |
| `POST /agent/runs/{run_id}/execute-next` | 75 |
| `GET /agent/runs/metrics` | 59 |
| `GET /trust/metrics` | 55 |
| `GET /audit` | 46 |
| `POST /approvals/{approval_id}/decision` | 42 |

## Interpretation and limits

This is an AST-based refactoring inventory, not runtime tracing or proof that a route uses SQLite when a PostgreSQL branch is selected. The call graph can include guarded compatibility branches and shared helpers. It may miss dynamic dispatch, callbacks, runtime-loaded modules, indirect mutations, shell wrappers, and non-literal router mounting. Counts are access references, not unique data records. The two routes without a statically reachable persistence access are not thereby proven persistence-free.

The report is reproducible from the recorded source commit using the command above. The full generated JSON was kept outside the repository; its hash is recorded for identification.

## Gate status

**PostgreSQL authority and data cutover: OPEN.** This inventory does not establish application-wide PostgreSQL repository binding, complete migration/import, audit-event reconciliation, multi-process recovery, backup/restore, or production readiness. The owner-provided coordinated, quiesced SQLite snapshot and event inventory remain required. No production database was read, copied, stopped, or changed; the startup guard remains fail-closed.

## Next engineering focus

Use the current route inventory and source review to select a bounded, security-sensitive runtime vertical with complete request, worker, audit, and notification transaction boundaries. Preserve SQLite fallback traps and PostgreSQL integration coverage. Do not infer nullable legacy audit-event workspace ownership or apply the partial importer to a live database.
