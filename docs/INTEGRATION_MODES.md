# Sandbox and Production integration

Sandbox is a real processing simulation in an independent database, not a frontend label or a mock response. Routing, holds, commissions and lifecycle services run normally against test actors and test funds. Only synthetic recipients and controlled callback receivers belong there; Sandbox does not prohibit outbound Webhook HTTP by itself.

Production uses a **separate deployment, PostgreSQL database, Redis/worker queues, secrets, merchant accounts and credentials**. Never import the local database, accounts, orders, balances, QA credentials or signing keys. No operation or balance is copied by activation. A merchant can keep using Sandbox on its Sandbox URL while using different credentials on its Production URL.

## Environment binding

Revision `0026_integration_modes` creates an immutable singleton database binding and per-merchant authorization records. A local/test/staging deployment binds its existing DB to Sandbox. A deployment configured with `ENV=production` can install this revision only into a clean database before bootstrap. A populated database is rejected transactionally. Changing ENV afterwards cannot promote a Sandbox database: HTTP database dependencies, merchant HMAC and workers fail closed on a mismatch; readiness fails too. SQL update/delete/truncate of the binding are forbidden by a trigger. Downgrade is blocked after any approval or for a Production binding.

Migration/configuration alone **does not activate any merchant**. Existing `sandbox_mode` is compatibility data, not permission to process live traffic. The previous integration checkbox cannot change it. Historical financial tables and calculations are unchanged.

## Owner-controlled activation

`PRODUCTION_ACTIVATION_ENABLED=false` by default. This prevents new activation/reactivation until the owner separately authorizes enabling the setting on the target server. It is not a substitute for merchant suspension and does not revoke already approved traffic. Keep it false during preparation.

After an authorized clean Production installation:

1. Bootstrap a new Superadmin and set up 2FA. Create merchant accounts independently; configure actual tariffs and a public HTTPS Webhook.
2. Admin/Superadmin issues the **Production** API key and Superadmin issues a fresh Webhook signing key. API and Webhook secrets are encrypted and displayed once with `no-store`; lists expose no secret. Sandbox keys use `pk_test_`, Production keys `pk_live_`. Possessing a Production key alone never activates traffic.
3. Open **Интеграции → credentials → карточка мерчанта → Режим интеграции**. The page lists blocking conditions. A merchant sees its environment and reasons in **API и Webhooks**, but cannot grant itself access.
4. Only authenticated Superadmin with existing mandatory 2FA may choose **Подключить Production**, type `PRODUCTION` and provide a reason of 10–1000 characters. Browser requests require existing CSRF/session checks. The server rechecks environment, owner flag, active/unlocked account, HTTPS/public callback, active API/signing credentials and current merchant fee rule. Callback reachability/delivery and matching executor tariffs still require the normal integration/routing checks; this is not certification of bank execution.
5. Activation creates exactly one auditable state change under the merchant row lock. Repeating an already applied command does not create a new approval or audit. No money, operation, fee snapshot or history is changed. Merchant Production HMAC requires both a key of this environment and active approval.
6. **Приостановить Production** requires `SUSPEND`, a reason and the same rights. It blocks authorizations that read approval after suspension commits. Already admitted requests may finish; existing operations, settlement commands and delivery jobs retain their normal lifecycle. Approval uses READ COMMITTED reads and introduces no merchant-row lock into the processing engine. It does not switch live balances into Sandbox. Reactivation checks all prerequisites again.

## Endpoints

- `GET /api/v1/integration/merchants/{id}`: owner Merchant or Admin/Superadmin JWT; staff 2FA required. Unowned records return 404. Response: environment, status, label, conditions, activation-role. No credentials.
- `POST /api/v1/integration/merchants/{id}/production/activate|suspend`: Superadmin JWT, JSON `{confirmation, reason}`. Authentication uses existing mandatory staff 2FA login. 403 permission failure, 422 malformed confirmation, 409 unmet conditions, 503 environment mismatch.
- Browser equivalents: `POST /staff/cabinet/merchants/{id}/production/activate|suspend`; same service, realm/session/CSRF and verified staff 2FA.
- Existing per-mode issue/rotate/revoke endpoints remain in use; HMAC v2 canonicalization/signature headers are unchanged. Cross-mode requests return `api_key_environment_mismatch`; unapproved live access returns `production_not_active` before financial writes.

## Testing and deployment boundary

`tests/test_integration_modes_postgres.py` creates disposable PostgreSQL schemas/databases and synthetic accounts. Production settings are simulated only inside the test process; no local merchant is promoted and no real payments/callbacks are sent. Tests cover migration isolation, immutability, roles, server conditions, CSRF, confirmation, concurrent semantic retry, audit, HMAC key segregation, real order creation and unchanged collateral mathematics.

Current local TradeSpace remains Sandbox. This change neither deploys nor authorizes live traffic. Enabling Production on a target server requires a separate owner instruction.


## Aggregator API: independent credentials and admission

The owner explicitly approved this extension of the protected Aggregator API contract on 2026-09-24. Aggregators use `AggregatorProductionAccess`, keyed by aggregator account, and `AggregatorApiKey`. They do **not** inherit authorization from `MerchantProductionAccess`; their internal settlement merchant remains an accounting relationship, not their access identity.

Revision `0027_aggregator_credentials` adds the two tables. Existing account credentials are imported without changing key, encrypted secret or financial data, and are classified **Sandbox only**. No Production key or active access record is created by migration. Existing Sandbox clients keep their `X-API-Key`, `X-Timestamp`, `X-Signature` and timestamp + dot + body HMAC contract. Payment paths, request/response fields, idempotency and callback signatures/headers remain unchanged. Authentication now exclusively resolves the key registry; account compatibility fields cannot revive a revoked key.

New accounts created in Sandbox retain the existing initial Sandbox credential issuance. Creating an account in Production issues **no API credential**: the account has a non-authenticating internal marker until explicit issuance. New keys use `ak_test_` / `ak_live_`. The server issues or rotates only keys matching both its ENV and immutable database binding. Production issue/rotation additionally requires the owner flag. Production and Sandbox deployments have different databases, accounts, secrets and queues. Never copy credentials across them.

### Management

- `GET /api/v1/integration/aggregators/{id}`: Admin/Superadmin JWT with existing staff 2FA. Returns environment, access status, blockers and key IDs/modes/statuses/last-use times; neither public credential value nor secret is returned.
- `POST /api/v1/integration/aggregators/{id}/credentials/issue|rotate|suspend|revoke`: Superadmin JWT. JSON `{mode, confirmation, reason, key_id?}`. Issue/rotate confirmation is `SANDBOX` or `PRODUCTION`; suspend/revoke confirmation is `SUSPEND` or `REVOKE`. Reason: 10–1000 characters. Rotation/suspension/revocation require the exact owned key ID. A fresh issuance conflicts if an unrevoked key exists for that account/mode. Suspended keys can be rotated or revoked; revoked keys are never reactivated. Concurrent issue/rotate is serialized by the account row lock and a unique unrevoked-key-per-mode index.
- Issue/rotate returns the new key and secret **only in that response**, with `Cache-Control: no-store`, `Pragma: no-cache`, and no-referrer. A lost response requires deliberate rotation; there is no secret retrieval endpoint. Repeating issue or rotating the already revoked previous key never redisplays a secret.
- `POST /api/v1/integration/aggregators/{id}/production/activate|suspend`: Superadmin JWT; same confirmation/reason contract as merchant management. Activation requires Production ENV/DB, owner flag, active/unarchived aggregator and internal settlement merchant, an active Production key, and currently effective merchant-fee and aggregator-executor-fee rules. Any configured aggregate callback must be public HTTPS. Per-payment callback validation and exact fee coverage/margin remain the existing payment-service checks. An internal aggregator user is intentionally disabled; merchant-owner-login checks are not applied mechanically.
- Browser counterparts: `/staff/cabinet/aggregators/{id}/credentials/{action}` and `/production/{action}`. Real staff session, verified 2FA and CSRF are required. **Участники → Агрегаторы → карточка** displays the actual environment, blockers and eligible confirmed commands. Browser issuance uses the existing user-bound Redis flash with TTL and atomic GETDEL; cookies contain only a random flash ID. The reveal page is masked and no-store. The retained `/secret` compatibility URL delegates to the same confirmed command and cannot bypass mode/role checks.

Key issuance itself does not activate Production. Both an active key and active aggregator access are required. Wrong-environment, suspended, revoked and unapproved credentials are rejected after signature verification but **before replay reservation, last-use updates, payment creation or any financial write**. Unknown keys remain HTTP 401; new admission denials are HTTP 403 with a structured code. Unmet management conditions are 409; malformed commands 422; environment/config mismatch 503. Existing bad signature/timestamp/replay errors are unchanged.

Admission rereads key revocation and access state at READ COMMITTED without adding financial locks. Requests admitted before suspension/revocation commits may complete. Suspension does not cancel admitted operations, change balances or suppress their outstanding callbacks. The owner flag controls new Production issuance/activation, not an emergency stop of already approved accounts; use explicit access suspension for that.

### Callback compatibility and rotation

As before, the aggregator API secret also signs platform-to-aggregator callbacks. Account credential columns are retained as the current callback-signing projection, while API authentication exclusively uses the registry. Rotation revokes the previous API key and updates this callback signing secret atomically. Consumers must update their request signer and callback verifier together; callbacks signed after rotation (including retries) use the new secret. Downstream aggregator-to-merchant secrets remain independent. Revocation blocks new inbound requests but retains the signing projection for callbacks of already accepted payments. A new issue after revocation changes it. UI confirmation explicitly describes this behavior. This task does not introduce callback payload versions or change retry/idempotency semantics.

Audits record actor, action, aggregator ID, credential UUID, prior UUID/status, new status, environment, reason and IP. They contain neither API credential strings nor plaintext/encrypted secrets. There is no financial journal entry for a mode/key-management action. Rotation changes credentials; it never transfers operations or funds.

### Verification and release handoff

`tests/test_aggregator_credentials_postgres.py` exercises independent disposable schemas for both modes, real migration/backfill, legacy HMAC compatibility, issuer/activator permissions, owner gates, key/access suspension and revocation, one-time display, CSRF/verified 2FA, concurrency, audit redaction, request replay/idempotency and real synthetic create/confirm/double-confirm outcomes. Production settings exist only within those isolated test processes. Existing aggregator HTTP 500 → retry → 204 signature regression remains required.

Keep `PRODUCTION_ACTIVATION_ENABLED=false` locally and during server preparation. Actual Production credential issuance and activation require a **separate owner deployment instruction** on the target Production server. No live credential is issued or real Production access enabled by this work. Rebuild and verify the clean release artifact before a later server move; the older release ZIP does not include these changes.
