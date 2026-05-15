# Public Connector Callback Hardening

This document defines PR 112: hardened public callback authentication for connector webhooks.

## Purpose

Connector webhooks can be exposed to external agents. A loose HMAC over the raw body is not enough for public callback safety because it does not protect against replay.

PR 112 introduces an HMAC v1 contract with signature versioning, timestamp validation, nonce tracking, and stale request rejection.

## HMAC v1 request headers

HMAC connectors must send:

```http
X-Aixion-Signature-Version: v1
X-Aixion-Timestamp: <unix_epoch_seconds>
X-Aixion-Nonce: <unique_nonce>
X-Aixion-Connector-Signature: v1=<hex_signature>
```

## Signing payload

The client signs:

```text
v1.<timestamp>.<nonce>.<sha256_raw_body_hex>
```

The server validates the signature using the stored connector secret hash as the HMAC key.

## Replay protection

The server rejects:

```text
missing signature version
unsupported signature version
missing timestamp
invalid timestamp
stale timestamp
missing/invalid nonce
reused nonce
invalid signature
```

Timestamp tolerance:

```text
5 minutes
```

Nonce memory window:

```text
10 minutes
```

## Bearer/API key behavior

Bearer/API key connectors keep the existing bearer-token behavior.

## Security boundary

This is public-callback hardening, not a complete edge-security layer. Production deployments should still use:

```text
HTTPS only
rate limiting
narrow project/repository scopes
short secret rotation windows
centralized logs
firewall/WAF where possible
```

## Example signing pseudocode

```text
body_hash = sha256(raw_body)
signing_payload = "v1." + timestamp + "." + nonce + "." + body_hash
signature = hmac_sha256(connector_secret_hash, signing_payload)
header = "v1=" + signature
```

## Important note

For the current MVP, the HMAC key is the stored connector secret hash. This keeps raw secrets out of normal storage paths but is not ideal long-term. A future production vault-backed secret model should sign with a secret retrieved from a secure secret store.

## Next PR

PR 113 should implement actual containerized validation runner execution.
