# Gate 1 source database quiescence check

Date: 2026-10-31
Purpose: determine whether a new staging source snapshot can be safely captured.

## Owner-confirmed staging source

The owner identified this SQLite source as the source for the staging rehearsal:

`/Users/madhuram/Library/Application Support/AixionControlTower/antigravity-native-m1-20261029/backend.sqlite3`

The existing candidate copy is at:

`/Volumes/TradeBot/AixionControlTower/slice1-source-snapshot-candidate-not-quiesced-20261029T095010Z/backend.sqlite3`

Its manifest records SHA-256
`294003ad65846e10508f5ece13911dad65ad50e5f90bf0e1964508f05211aff2`,
but also records `writer_quiesced: false`, 29 writable database handles at capture, and `owner_source_identity_confirmed: false` at that time. It is not suitable as a coordinated source snapshot.

## Current read-only source check

At the time of this check:

- The owner-confirmed source file existed, was mode `0600`, and was 110,592 bytes.
- No `-wal` or `-shm` sidecars were present.
- `lsof` showed a Python process, PID `38201`, with nine open writable handles (`3u` through `11u`) on the source database.

The open writable handles establish that the source is not quiesced. The absence of WAL/SHM sidecars does not override that finding.

## Action and gate status

No process was stopped, no source file was changed, and no new snapshot or import was attempted. The previously imported staging rehearsal must not be treated as a fresh capture of this currently active source.

Gate 1 remains open. A new source snapshot requires a coordinated maintenance window in which the owner-approved application/API writer is stopped through its normal shutdown procedure, all writer handles are confirmed closed, and a new owner-only SQLite backup is captured and hashed. The production startup guard remains enabled.

## Follow-up read-only check (2026-10-31 04:22 UTC)

The same source remains open and unmodified:

- `ps` identifies PID `38201` as `uvicorn app.main:app --host 127.0.0.1 --port 8003 --log-level info`.
- `lsof` reports 112 descriptors in read/write (`u`) mode for the source path. These are file descriptors, not a count of distinct SQLite connections; any read/write descriptor means quiescence has not been established.
- The source remains 110,592 bytes with no reported modification since Oct 30 03:41:47 local time. Current SHA-256: `17f49ae6989393e12fc9d8d88789f492a1bda20cb5ab90bc7552056d3f072491`.
- The current hash differs from the older non-quiesced candidate hash above. That candidate remains unsuitable and is not a substitute for a fresh coordinated snapshot.

No service was stopped, and no source, staging, or production database was written. Gate 1 remains open.
