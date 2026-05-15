# Android Connector UX Polish

This document describes PR 115: optional Android connector setup polish.

## Purpose

The connector management screen already works, but the first version was operator-heavy. PR 115 makes connector setup easier to run from the phone without changing backend behavior.

## Improvements

```text
copy webhook URL
copy setup block
show selected template setup notes
show selected setup preset on connector card
improve one-time connector credential panel
add copy credential button
add hide credential button
replace secret wording with credential wording in Android UI
show user-facing notices after copy/setup actions
rename preview action to test selected template payload
```

## Scope boundary

This PR intentionally does not add backend features, new connector endpoints, QR sharing, deep-link sharing, or a full setup wizard.

## Why this matters

Users should not manually reconstruct webhook URLs, mapper notes, or setup data. The mobile console should generate a setup block that can be copied into the external agent configuration.

## Remaining future polish

```text
QR/share config
multi-step connector wizard
masked credential reveal flow
per-template setup checklist
connector setup success screen
```
