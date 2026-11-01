# Antigravity local expiry evidence — 2026-10-28

- Runtime source: `e3abf61d4b7115ef288bc4d4cc76b85d91c674bc`
- Provider: Antigravity 2.26.0; Android: debug 0.1.0 on API 35 emulator
- Request: `approval_c283034f0551`
- Command: `mkdir /tmp/aixion-ct-e2e-20261028-deny-4e92`

The request expired without a human decision. The backend returned `EXPIRED`,
the Antigravity conversation reported that the approval timed out, and the
directory was not created.

This is local timeout evidence. It does not establish a human rejection or the
other required failure cases.
