# Gate 1 staging database comparison

Date: 2026-10-31
PostgreSQL server: 17.11 (Homebrew)
Connection: local Unix socket `/tmp/aixion-codex-native-e2e-20261029/socket`, port `59174`
Databases compared:

- `aixion_gate1_rehearsal_20261030_staging`
- `aixion_gate1_restorecheck_20261030_7d3f5a46_test`

## Read-only comparison

For each public base table, rows were selected as `to_jsonb(row)::text`, sorted by that canonical JSON representation, and hashed with SHA-256. Only row counts and hashes were retained; row contents were not printed or copied.

Both databases had 45 public base tables and 40 rows in `schema_migrations`. The sum across all tables was 77 rows in each database. All table names, row counts, and content hashes matched.

Nonempty tables:

| Table | Rows | SHA-256 of sorted canonical rows |
| --- | ---: | --- |
| `audit_events` | 2 | `da529921183626a2c7b6b5c4c077ae033e28e607b98a4d625fc9401c60a59900` |
| `auth_rate_limit_buckets` | 17 | `67699537514939c8c18a85ec815e8b40948651d553c323cad9fe01ad96414f7e` |
| `external_agent_credentials` | 1 | `73ce79875b709bc421d5896d59fe95c22e701a561dbd5743fae4c3a6f0313ff8` |
| `external_agents` | 1 | `44db7b93df30fe0a3e625b4724c6d92daed7000fc75b65920457dd4d1657c92b` |
| `legacy_audit_events` | 2 | `d1dc93e476ab2e59152692126af9db46dc10f60902b30e71c93bb906b58edcb1` |
| `projects` | 1 | `02212f26c38a6ceb922a1bedb60cc03ed2588c61024ba9d66eda2fa12b4a3f49` |
| `schema_migrations` | 40 | `fcd28caa0b8349c965af13d1f44176d51882a42360eba894d0ae2e7018e69bd8` |
| `sessions` | 5 | `707d9307269937420edfa66e53be50e00c2c2463599efbff42c830f0c18c95fc` |
| `sqlite_import_runs` | 1 | `af964a7d7c97c1cf7d8dbc8933e5ec795b0e5daa712b45dd2a170491310c732f` |
| `trust_event_heads` | 1 | `f9a8d14481a3c990b5252ca7447c1cebac656f68b47319f74e5e33c3d429050b` |
| `users` | 3 | `51880bdd50f44b3869407b562c479e4c5534985c7d74080434ca1c1c799ed1e5` |
| `workspaces` | 3 | `487d881f794f2728c8ca930c31fe1e7135a9f5d109b9668a85d8921ae2faf328` |

The other 33 tables were empty in both databases. For each, the sorted-row hash was the SHA-256 of the empty byte string:
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

Result: **the two live local databases are logically identical at comparison time.**

## Limits and remaining Gate 1 work

This comparison establishes equality between these two databases only. It does not independently establish that `restorecheck` was restored from the recorded custom-format dump, because the dump and restore-command artifacts were on `/Volumes/TradeBotFast/AixionControlTower/gate1-staging-rehearsal-20261030/`, and `/Volumes/TradeBotFast` is not mounted in this session.

It also does not prove application-wide PostgreSQL authority, production-source authority, complete production data import, production cutover authorization, or operational backup/recovery readiness. The production startup guard must remain enabled. No database writes, migration, cloud action, billing change, DNS change, or deployment occurred during this comparison.
