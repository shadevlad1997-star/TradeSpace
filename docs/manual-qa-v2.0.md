# TradeSpace 2.0.0-rc2 manual QA

Run this checklist on an approved local or staging environment. Do not use
production funds, production wallet credentials, or real AI Office secrets.

## Preconditions

- `/version` returns `2.0.0-rc2`.
- `/health` and `/ready` return HTTP 200.
- Alembic current is `0024_required_schema_indexes`.
- A Superadmin has working 2FA.
- Separate Admin, Trader/Operator, Support, Merchant and TeamLead test
  accounts are available.
- Use a valid QA-only TRON mainnet-format Base58Check address. Its format is
  validated, but no blockchain transfer is performed.

## A. Static USDT TRC20 wallet

1. With no active wallet, open Trader **Баланс** and confirm:
   `Кошелёк временно не указан`; no QR image is rendered.
2. As Superadmin without a fresh 2FA session, attempt to save a wallet.
   Confirm the action is rejected.
3. Submit without CSRF. Confirm HTTP 403.
4. Submit an address with an invalid checksum. Confirm it is rejected.
5. Submit a valid Base58Check address with a non-TRON version byte. Confirm
   wrong network is rejected.
6. Save a valid TRON `0x41` address, label and reason as Superadmin.
7. Confirm exactly one active wallet and history version 1.
8. Submit the same address and label again. Confirm no new history version is
   created.
9. Save a different valid address. Confirm:
   - version 1 remains read-only in history;
   - version 1 is inactive with deactivation time;
   - version 2 is the only active wallet.
10. Log in as Admin. Confirm full read-only wallet/history is visible and no
    update form/action is available.
11. Attempt the update endpoint as Admin, Support, Trader and TeamLead.
    Confirm all are rejected.
12. Log in as Trader and Operator. In **Баланс**, confirm:
    - `USDT`;
    - `TRC20`;
    - full address;
    - label and update time;
    - warning about transfers in another network;
    - working copy button.
13. Scan or decode the QR and confirm its payload is exactly the displayed
    address with no URI prefix or additional text.
14. Change the active wallet again and confirm the QR URL/version changes.
15. Confirm Merchant, Support and TeamLead do not see this wallet block.

## B. AI Office settings

1. Open **Интеграция с AI-офисом** as Superadmin. Confirm initial state:
   `enabled=false`, inbound commands disabled, credentials not configured.
2. Confirm Admin, Support, Merchant, Trader and TeamLead cannot open update,
   clear-secret or Test connection actions.
3. Confirm missing CSRF is rejected and Superadmin requires a fresh 2FA
   session.
4. Save a disabled local configuration and a QA-only credential.
5. Reload the page. Confirm the secret value is absent from HTML and only
   `configured` is shown.
6. Save again with the secret input empty. Confirm the existing secret remains
   configured.
7. Use the separate clear action while config is disabled. Confirm the
   credential becomes `not configured` and an audit event records only its
   type/reason.
8. Confirm enabling `production` is rejected in this release candidate and
   the M2M warning remains visible.
9. For `staging`, configure a private/loopback/link-local URL and run Test
   connection. Confirm it is blocked before HTTP.
10. For `local`, use an approved local test health endpoint returning JSON
    `{"status":"ok"}`. Confirm Test connection succeeds.
11. Confirm the receiver observes only GET; no POST, event or payment payload
    is sent.
12. Test timeout/unreachable/TLS/error status cases. Confirm only a redacted
    error code/message and latency are stored.
13. Search application and audit logs for the QA bearer/API/HMAC value.
    Confirm it never appears.
14. Leave config disabled. Observe worker/beat logs and confirm no background
    AI Office requests occur.

## C. Release hardening

1. Run `python -m scripts.preflight_v2` and confirm no secret or database URL
   value appears in output.
2. Confirm preflight does not change row counts, Alembic revision, wallet
   version, balances, deposits, settlements, or audit records.
3. Run `python -m scripts.smoke_v2`; confirm request logs contain only GET.
4. Confirm `/health` includes version `2.0.0-rc2`.
5. Confirm `/version` returns exactly `{"version":"2.0.0-rc2"}`.
6. Confirm the Superadmin footer displays `TradeSpace 2.0.0-rc2`.
7. Confirm OpenAPI is skipped when disabled and remains protected when enabled.

## D. Financial regression

1. Create/confirm a Deposit without eligible confirmed Rolling.
2. Create/confirm a Rolling Deposit with a fresh Rapira live ask.
3. Confirm a TeamLead-attributed Deposit and verify accrual from gross.
4. Confirm trader income, merchant payable, Rolling allocation and platform
   expense match the pre-release formulas.
5. Request/reject/retry/complete a TeamLead settlement and verify the existing
   168-hour cooldown and fee accounting.
6. Confirm wallet/AI settings create no Deposit, balance, Rolling, TeamLead,
   settlement, webhook or Celery business objects.

Record timestamps, account roles, wallet versions, sanitized screenshots and
the final pass/fail decision. Never attach passwords, TOTP seeds, private
tokens, HMAC secrets, database URLs or full production logs.
