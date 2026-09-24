# Merchant API HMAC v2

HMAC v2 is the primary and default authentication protocol for the TradeSpace
Merchant API. HMAC v1 is disabled by default and must not be enabled for a new
integration.

## Required headers

Every request must include:

- `X-API-Key`
- `X-Signature-Version: 2`
- `X-Timestamp`: current Unix time in seconds
- `X-Nonce`: a new 16–128 character value containing only letters, digits,
  `.`, `_`, `~`, or `-`
- `X-Signature`: lowercase hexadecimal HMAC-SHA256

`POST`, `PUT`, `PATCH`, and `DELETE` requests must also include a non-empty
`Idempotency-Key` of at most 128 printable characters. Generate the key once
for a business operation and retain it until that operation has completed.

## Canonical request

Join these eight fields with a single LF byte (`\n`), with no trailing LF:

```text
v2
{timestamp}
{nonce}
{UPPERCASE_METHOD}
{normalized_path}
{canonical_query}
{normalized_content_type}
{sha256_hex_of_exact_raw_body}
```

Rules:

1. Normalize the Unicode path to NFC, ensure a leading slash, then
   percent-encode UTF-8 bytes. The only unescaped characters are
   `/`, `-`, `.`, `_`, and `~`.
2. Decode query names and values as UTF-8, retaining blank and duplicate
   values. Re-encode each name and value with RFC 3986 unreserved characters,
   sort by encoded name and then encoded value, and join with `&`.
3. Normalize `Content-Type` by trimming it, lowercasing it, and collapsing
   runs of whitespace. An absent header is an empty line.
4. Hash the exact transmitted body bytes. Do not parse and reserialize JSON
   between signing and sending.
5. Compute `HMAC-SHA256(secret, canonical_request_utf8)` and send its
   64-character hexadecimal digest.

Comparison on the server is constant-time. A timestamp outside the configured
UTC tolerance is rejected. A nonce is reserved atomically per merchant and API
key in Redis. Exact replay is rejected even when the original request failed
later in application processing. If replay protection is unavailable, mutating
requests fail closed with `replay_guard_unavailable`.

## Idempotent retries

A client that did not receive a response must retry with:

- the same `Idempotency-Key`;
- the exact same business payload;
- a fresh timestamp;
- a fresh nonce;
- a newly calculated signature.

For deposit and payout creation, TradeSpace stores an immutable SHA-256
fingerprint of the complete validated request payload, including `metadata`.
The same key and fingerprint return the existing operation without a second
Rapira quote, balance hold, or financial movement. Reusing the key or
`external_id` with a different fingerprint returns HTTP 409
`idempotency_conflict`.

Rows created before HMAC v2 do not have a trustworthy request fingerprint.
They therefore fail closed with `idempotency_conflict` when addressed as an
HMAC v2 retry.

## Stable authentication errors

Authentication failures use:

```json
{
  "error": {
    "code": "hmac_invalid_signature",
    "message": "Invalid HMAC signature.",
    "request_id": "server-generated-correlation-id"
  }
}
```

Possible codes include `hmac_missing_header`, `hmac_unsupported_version`,
`hmac_invalid_timestamp`, `hmac_expired`, `hmac_invalid_nonce`,
`hmac_invalid_signature`, `hmac_replay_detected`,
`replay_protection_unavailable`, `idempotency_key_invalid`, and
`idempotency_conflict`.

No error or deprecation log contains the API key, secret, signature, nonce, or
request payload.

## HMAC v1 compatibility gate

Repository inspection found no first-party HMAC v1 client or documented v1
consumer. That does not prove that no external production consumer exists.
Before disabling v1 on an existing deployment, an operator must check
production request metrics and integration ownership outside this repository.

The temporary compatibility gate is `MERCHANT_HMAC_V1_ENABLED`. It defaults to
`false`, including in the production environment example. When explicitly
enabled, v1 requests emit a deprecation metric and a redacted warning containing
only the internal API key identifier. Remove the gate only after external usage
has been ruled out.

The normative cross-language vector is
`tests/fixtures/hmac_v2_vectors.json`.
