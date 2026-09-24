# Merchant API integration

The Merchant API is mounted at `/api/v1/merchant`. Use HTTPS in every
non-local environment and treat API keys and secrets as independent
credentials.

## Authentication

Implement HMAC v2 exactly as specified in
[`merchant-hmac-v2.md`](merchant-hmac-v2.md). Build the JSON body once as
bytes, sign those bytes, and send those same bytes. Generate a cryptographically
random nonce for every attempt.

Example request flow:

1. Serialize the business payload to UTF-8 JSON.
2. Generate `timestamp`, `nonce`, and a durable `Idempotency-Key`.
3. Build and sign the canonical request.
4. Send the exact bytes and all required headers.
5. On a lost response, retry the same payload and idempotency key with a new
   timestamp, nonce, and signature.

Do not reuse a nonce. Do not change whitespace in a JSON body after signing.
Do not create a new idempotency key merely because a network timeout occurred.

## Deposit creation

`POST /api/v1/merchant/deposits` accepts:

```json
{
  "external_id": "order-10001",
  "amount": "1250.00",
  "currency": "RUB",
  "method": "sbp",
  "metadata": {
    "customer_id": "customer-42"
  }
}
```

`metadata.callback_url` is not accepted. Configure the merchant webhook URL in
integration settings. A successful idempotent retry contains
`"idempotent": true` and the original operation identifier.

## Payout creation

`POST /api/v1/merchant/payouts` uses the same common fields and adds
`destination`. The idempotency fingerprint includes the destination and the
complete metadata object.

## Error handling

- Retry `replay_protection_unavailable` and transient HTTP 5xx responses with the
  same business payload and idempotency key, but a new nonce and signature.
- Treat `idempotency_conflict` as a client integration error and reconcile the
  original operation instead of retrying with altered data.
- Treat `hmac_replay_detected` as an exact-attempt replay; generate a new nonce
  and signature only if this is a legitimate retry.
- Correct timestamp drift before retrying `stale_timestamp`.

Never log the API secret, signature, raw authorization headers, or full payment
payload. Correlate requests using a non-sensitive request identifier.

## Legacy compatibility

HMAC v1 is not an onboarding option. Existing operators considering the
temporary `MERCHANT_HMAC_V1_ENABLED` flag must first complete the external
production usage gate described in `merchant-hmac-v2.md`.
