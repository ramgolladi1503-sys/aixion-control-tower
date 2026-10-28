# Antigravity local approval evidence — 2026-10-28

- Source: `e3abf61d4b7115ef288bc4d4cc76b85d91c674bc`
- Provider: Antigravity 2.26.0; Android: debug 0.1.0 on API 35 emulator
- Request: `approval_ab829793b589`; project: `project_d1a3bd714a2c`
- Invocation: `antigravity:v1:e90a2046b7b807208bff33e3bdf7bea431e06528c7023b99dad4154f07772204`
- Command: `mkdir /tmp/aixion-ct-e2e-20261028-1208-7b3f`

The command was absent before the human approved it in the Android emulator.
The backend recorded the approval and its history reported `CONSUMED`.
At 12:06:07 IST the directory existed, and the same Antigravity conversation
reported the command completed.

This is local approve-path evidence. Rejection, failure cases, notifications,
and away-from-home operation are not established by this result. M1 remains
unverified until its full acceptance criteria pass.
