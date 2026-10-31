# Antigravity local denial evidence — 2026-10-28

- Runtime source: `e3abf61d4b7115ef288bc4d4cc76b85d91c674bc`
- Provider: Antigravity 2.26.0; Android: debug 0.1.0 on API 35 emulator
- Request: `approval_8adc7798f1c1`
- Invocation: `antigravity:v1:446fdc166ba70ec52939246c44b5db39f797b522b956648b083aeda848ebe43b`
- Command: `mkdir /tmp/aixion-ct-deny-20261028-b92d`

The human denied the request in the emulator. The backend recorded the denial
at 12:23:35 IST, and the same Antigravity conversation reported that its
pre-tool policy hook denied the command. The directory was not created.

This is local human-denial evidence; the remaining failure and production
release gates are not established by this result.
