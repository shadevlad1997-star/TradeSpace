# Webhook integration

TradeSpace delivers merchant events as HTTPS `POST` requests. Webhook
credentials are separate from inbound Merchant API credentials: an API key or
API secret is never used to sign an outgoing webhook and is never sent in its
headers.

## Provisioning

A Superadmin provisions the first `MerchantWebhookSigningKey` after the
merchant webhook URL is approved. TradeSpace generates an unpredictable key ID
and secret, encrypts the secret at rest, audits the action using only a key
fingerprint, and returns the plaintext secret exactly once in a no-store
response.

If a merchant has a webhook URL but no active signing key, delivery is blocked
as `configuration_required`. TradeSpace does not generate a predictable secret
and does not fall back to a Merchant API secret. The Superadmin cabinet and the
redacted key-list endpoint expose this configuration state.

## Request format

Every delivery includes:

- `X-TradeSpace-Event-ID`: stable event UUID;
- `X-TradeSpace-Event-Type`;
- `X-TradeSpace-Timestamp`: Unix time in seconds;
- `X-TradeSpace-Key-ID`;
- `X-TradeSpace-Signature-Version: 1`;
- `X-TradeSpace-Signature`;
- `X-Correlation-ID`.

The body is deterministic UTF-8 JSON:

```json
{"event":"deposit.paid","payload":{"amount":"1250.00","id":"..."}}
```

The canonical message is the ASCII timestamp, one period byte, and the exact
raw body:

```text
{timestamp}.{raw_body}
```

`X-TradeSpace-Signature` is the lowercase hexadecimal
`HMAC-SHA256(webhook_secret, canonical_message)`. Verify against the raw bytes
before parsing JSON and compare signatures in constant time. The normative
fixture is `tests/fixtures/webhook_signature_vectors.json`.

Consumers should deduplicate by `X-TradeSpace-Event-ID`. A retry preserves the same
event and correlation IDs but has a fresh timestamp and signature.

## Delivery lifecycle

Active states are:

```text
pending -> processing -> delivered
                      -> retry_scheduled -> processing
                      -> dead_letter
```

`configuration_required` is a blocked configuration state. Legacy state names
are migrated and normalized; new delivery logic does not emit `queued`,
`delivering`, or terminal `failed`.

Each worker atomically claims one event with `lock_owner`, `locked_at`, and
`lease_until`, commits that short transaction, performs DNS and HTTP without a
database row lock, then stores the result in a separate transaction. An expired
processing lease is recoverable. Two workers cannot claim the same live lease.

One claimed HTTP attempt adds exactly one attempt record. Successful 2xx
responses become `delivered`. Timeouts, network errors, 429, 408, 425, and
selected 5xx responses use bounded exponential backoff with jitter. A safe,
bounded `Retry-After` value is honored. Non-retryable 4xx and the last failed
attempt become `dead_letter` immediately.

The scanner selects only pending events, due retry events, and expired
processing leases. It does not select delivered, dead-letter, configuration
blocked, or actively leased events. Consequently, a database commit followed
by enqueue failure is recovered by the next scanner run.

## Manual retry

Manual retry is restricted to Superadmin and is audited. It retains the same
event ID, payload, prior attempt count, and attempt history. The retry extends
the event's allowed attempt budget instead of resetting accounting.

Resolve `configuration_required` before requesting a manual retry.

## Rotation

Rotation creates a new active key and moves the previous active key to
`retiring` until `retire_at`. During that overlap, both key IDs remain valid for
consumer verification. New delivery claims prefer the active key; a previously
selected retiring key may finish during the overlap. Revoked or expired keys
are never selected.

Safe rotation procedure:

1. Superadmin creates the new key through the authenticated cabinet or admin
   endpoint.
2. Securely copy the one-time secret to the merchant.
3. The merchant installs the new key while retaining the retiring key.
4. Confirm deliveries using the new `X-TradeSpace-Key-ID`.
5. After the overlap, remove the retired key from the consumer.
6. Revoke immediately only for credential compromise; audit and investigate
   resulting dead letters.

Never put webhook secrets, API credentials, signatures, payloads, or target
URLs containing credentials into logs or audit details.


## Appeal rejection and operation state

A rejected Deposit appeal restores the operation state captured when the appeal was opened. The event is `deposit.<actual status>`: restoring pending emits `deposit.pending` with `payload.status=pending`, preserving its collateral reservation. It must not be treated as `deposit.failed` or a release of funds. Rejection on an already terminal operation retains that terminal state. Approval uses the existing `deposit.paid` contract and financial idempotency guards. External consumers must explicitly accept the nonterminal event and preserve their operation-level idempotency; local delivery tests do not establish partner compatibility.
