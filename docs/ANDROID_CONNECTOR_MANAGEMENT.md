# Android Connector Management Console

This document describes PR 110: first-pass Android management for configurable agent connectors.

## Purpose

The backend now supports connector registry records, credential lifecycle, inbound webhooks, schema mapping, and connector templates. PR 110 exposes the core owner controls from Android so the feature is usable from the phone.

## Android capabilities

The Android connector screen can:

```text
list connectors
list connector templates
create a connector from a selected template
enable or disable a connector
issue or rotate a connector secret
revoke a connector secret
apply the selected template mapper to a connector
preview selected template payload mapping
show connector status, health, secret state, failure count, last error, last used, actions, and repositories
```

## Navigation

A new bottom navigation item is added:

```text
Conn
```

It opens the connector console.

## Scope boundary

This PR intentionally keeps the Android UI compact. It does not add a full multi-step setup wizard, QR sharing, clipboard copy helpers, public callback setup, or sample webhook simulation. Those should be separate PRs after the first management screen compiles and lands.

## Hard truth

This is the first Android control surface, not the final polished connector setup experience. It makes the backend connector platform operable from mobile, but it still needs UX polish and a simulator/test button next.
