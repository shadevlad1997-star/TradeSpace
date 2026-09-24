# TradeSpace 2.0.0-rc2 release notes

`2.0.0-rc2` is a release candidate prepared for independent audit. It is not a
production release. Manual QA, security review and financial reconciliation
must be approved before promotion to `2.0.0`.

## Scope

This release candidate includes:

- an immutable Deposit lifecycle with explicit expiry and unsuccessful
  finalization;
- strict live Rapira `askPrice` handling for Rolling, including nullable
  provider timestamps and `fetched_at` freshness evidence;
- settle as the base merchant accounting model, without a persistent
  settlement-mode switch;
- merchant-confirmed Rolling transfers, immutable funding evidence and FIFO
  recovery;
- TeamLead historical attribution, accrual, reversal, debt and settlement
  accounting;
- a versioned, display-only platform USDT TRC20 wallet with Base58Check
  validation and QR rendering;
- encrypted AI Office settings, disabled by default;
- production startup separated from migration, bootstrap, and demo seed;
- canonical Merchant API HMAC v2, Redis-backed nonce replay protection, and
  immutable idempotency fingerprints;
- leased webhook delivery with terminal dead letters and independently rotated
  webhook signing keys;
- aligned ORM metadata and required PostgreSQL lookup indexes;
- pinned GitHub Actions release gates for PostgreSQL, migrations, concurrency,
  Docker, repository hygiene, secret scanning, and dependency audit;
- release preflight, GET-only smoke checks and PostgreSQL migration tests.

The platform wallet does not watch TRON, detect deposits, credit balances or
assign personal addresses. AI Office does not receive payment events, issue
commands, access the database or mutate payments in this release candidate.

## Migration chain

The release contains one Alembic head:

```text
0024_required_schema_indexes
```

Relevant release migrations are:

- `0014_deposit_expiry_finance_profile`;
- `0015_merchant_rolling`;
- `0016_rolling_rapira_freshness`;
- `0017_teamlead`;
- `0018_platform_crypto_wallet`;
- `0019_ai_office_config`;
- `0020_rolling_confirmation_flow`;
- `0021_settlement_hardening`;
- `0022_hmac_v2_idempotency`;
- `0023_webhook_hardening`;
- `0024_required_schema_indexes`.

Migration `0020` preserves existing merchant, trader and TeamLead balances. It
creates synthetic confirmed transfers only for legacy accounts that contain
actual Rolling finance and preserves existing allocations and ledger history.
Legacy allocations without real Rolling finance are marked ineligible.

Migration execution against staging or production is not authorized by this
publication. Reviewers should use an isolated PostgreSQL instance and verify the
single expected head before any local upgrade.

## Rolling confirmation model

Every merchant uses settle as the base model. A Rolling cycle becomes active
only after the merchant confirms a specific transfer registered by
Superadmin.

Pending, disputed and cancelled transfers do not change principal or
outstanding. Confirmation is transactional and idempotent. Deposit eligibility
is based on immutable `deposit.created_at`, so a Deposit cannot consume a
transfer confirmed after the Deposit was created.

Confirmed transfers are recovered FIFO. If one operation crosses the remaining
Rolling balance, the eligible portion recovers Rolling and the exact RUB
remainder is credited to settle.

See [Rolling confirmation and FIFO accounting](rolling.md) for the complete
invariants and lock order.

## TeamLead accounting

TeamLead attribution is historical: the assignment active at
`deposit.created_at` determines eligibility. Accrual is a separate platform
expense and does not change trader income, merchant payable or Rolling.

Settlement accounting separates requested payout, platform fee and total debit.
TRC20 addresses use Base58Check validation, completed transaction hashes are
network-unique, and cooldown is based only on `completed_at`.

See [TeamLead accounting](teamlead.md) for the full model.

## AI Office and wallet boundaries

AI Office settings:

- are disabled by default;
- store credentials encrypted;
- require Superadmin, CSRF and verified 2FA for changes;
- support a manual redacted GET health check only;
- do not run background payment integration.

The platform TRC20 wallet is display-only and versioned. It does not include a
private key and does not perform blockchain transactions.

## Validation

The release gate includes:

- the full isolated PostgreSQL pytest suite;
- Python bytecode compilation for `app` and `scripts`;
- Jinja template compilation;
- OpenAPI import;
- Alembic head/current verification;
- migration upgrade and invariant tests;
- Rolling reconciliation;
- secret and prohibited-artifact scans;
- the [manual QA checklist](manual-qa-v2.0.md).

## Alembic schema alignment

The baseline schema/model drift has been classified and reconciled by explicit
ORM metadata plus the narrow `0024_required_schema_indexes` migration.
`alembic check` reports no new upgrade operations on a clean PostgreSQL 16
database.

The comparison, unsafe raw-autogenerate operations, and the still-open
production schema-clone gate are documented in
[the Alembic drift review](review-alembic-drift.md). A production migration is
not authorized until that gate is independently completed.

## Audit and release boundary

Before promotion:

- complete the manual QA checklist;
- independently review migrations `0014` through `0024`;
- verify financial reconciliation on an approved non-production copy;
- confirm no environment files, credentials, backups, dumps, archives or
  private operational discovery are present in the published branch;
- obtain explicit approval for any production deployment or migration.

This release publication does not authorize merge, deployment, production data
changes or creation of a final `2.0.0` tag.
