# Antigravity PostgreSQL Phone Approval E2E Evidence

**Result:** PASS for the local Antigravity approval round-trip
**Date:** 2026-10-24 (IST)
**Repository revision:** `2ad22d417b0bdc0afd0f2d53fd61075577bb4f2f`
**Pull request:** [#204](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204), open and unmerged

## Environment

- Android device: Pixel 10 Pro Fold; app version `0.1.0`
- Antigravity version: `2.26.0`
- API: local PostgreSQL-backed API at `127.0.0.1:8005`
- Database: isolated local database `aixion_phone_pg_e2e`
- Connector: project-scoped Antigravity connector `agent_171dbeb5bbca`
- Project: `project_2283220a0874`

## Exact round-trip

1. Antigravity in the disposable project `aixion-pg-e2e-20261024` invoked exactly:
   `mkdir /tmp/aixion-ct-phone-pg-e2e-20261024-approved`
2. The Android app displayed the resulting approval request. The user approved it in the app.
3. PostgreSQL recorded approval `approval_c7be7d692100` as `APPROVED`, version `1`.
4. The decision audit recorded matching reviewed and approved payload hashes.
5. PostgreSQL recorded one `approval.delivery_claimed` event for the same `source_agent_id` and `source_invocation_id`.
6. Antigravity reported that the same command exited with code `0`; the exact target directory existed afterward.

The observed invocation identity was `antigravity:v1:a47214061821708cb4b42219ba675625e11f240786bb76f46a40849a0d0e3087`. Audit events were `approval.created_by_agent`, `approval.decision`, and `approval.delivery_claimed`.

## Repeatable certification on a future exact revision

The dated result above is historical evidence for the listed revision and
environment. Use the verifier below when repeating the physical flow on a new
revision; do not carry this PASS forward to code that has not been exercised.

1. Use a disposable local PostgreSQL database whose name includes `e2e`,
   `test`, or `disposable`. Configure the Android app and Antigravity hook to
   reach the same API/database. For USB, confirm the exact ADB reverse mapping
   and keep the backend bound to loopback.
2. Run the hook's authenticated preflight with the workspace config. Confirm
   `adb devices -l` shows the intended unlocked device as `device`.
3. Choose a unique marker path and verify it does not exist. Record a UTC start
   timestamp immediately before the call. In Antigravity, invoke exactly
   `run_command` with `{"command":"mkdir /tmp/aixion-ct-phone-e2e-<unique-id>"}`.
   Approve that exact request from the Control Tower Android app, then observe
   the same Antigravity call finish successfully and the marker appear.
4. Run the verifier against that disposable database. It opens a read-only
   PostgreSQL transaction, rejects remote/non-test databases, and requires an
   exact tool payload, one matching approval decision, a matching at-most-once
   claim and audit event, and a marker newer than the recorded start time:

   ```sh
   export AIXION_E2E_POSTGRES_DSN='<disposable-local-postgres-dsn>'
   python3 integrations/antigravity/certify_phone_e2e.py \
     --approval-id 'approval_<id-shown-in-phone-or-hook-log>' \
     --agent-id 'agent_<registered-agent>' \
     --project-id 'project_<registered-project>' \
     --tool-name run_command \
     --expected-arguments '{"command":"mkdir /tmp/aixion-ct-phone-e2e-<unique-id>"}' \
     --marker '/tmp/aixion-ct-phone-e2e-<unique-id>' \
     --started-at '2026-10-25T00:00:00Z'
   ```

The verifier is evidence support, not the physical E2E itself. A PASS confirms
durable approval/claim identity and a fresh marker; the operator must also
record that the phone displayed and approved this exact request and that the
same pending Antigravity call resumed. No phone or Antigravity UI is automated
by the verifier. Any missing row, duplicate event, mismatched invocation/hash,
stale marker, remote database, or unreadable state fails nonzero.

## Limits

This certifies one local PostgreSQL-backed approval round-trip on the tested device and Antigravity version. It does not certify hosted/off-LAN reachability, push wake-up, production deployment, restart/revocation/timeout failure cases, or Codex native-session approval. Those gates remain open.
