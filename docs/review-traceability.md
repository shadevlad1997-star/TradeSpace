# Rolling and Deposit lifecycle review traceability

This document maps the reviewed requirements to their implementation and direct
tests. PostgreSQL tests use independent sessions and the real PostgreSQL 16
locking/constraint behavior. Route tests use the application ASGI middleware,
real database dependencies, realm sessions, password verification, TOTP, CSRF,
and a test-only Redis service.

| Requirement | Implementation | Direct test |
|---|---|---|
| Trader/operator decline is forbidden | `app.web.routes.cabinet_decline_deposit` and admin-only template guard | `test_postgres_trader_session_cannot_decline_deposit`; `test_direct_decline_request_is_forbidden_for_trader` |
| Immutable Deposit TTL | `app.services.deposit_ttl.new_deposit_expires_at`; Merchant and Aggregator create paths | `test_new_deposit_ttl_uses_nondefault_application_setting`; `test_ttl_uses_immutable_expires_at` |
| Confirm after deadline commits before error response | `app.services.deposit_confirmation.confirm_deposit_payment`; admin API route | `test_postgres_http_confirm_after_deadline_commits_before_409` |
| Unified unsuccessful lifecycle | `app.services.deposit_lifecycle.finalize_unsuccessful_deposit` | unsuccessful finalization, expiry worker, and repeated timeout PostgreSQL tests |
| Settle routing without confirmed Rolling and no Rapira call | `app.services.rolling.apply_merchant_financing` settle branch | `test_deposit_created_before_confirmation_goes_to_settle_even_if_paid_later` |
| Rolling create and immutable snapshot | `rolling_quote_for_deposit_create`; `create_pending_allocation` | original-rate idempotency and strict stale-cache tests |
| Explicit Rolling eligibility | allocation `eligibility_status` DB CHECK; legacy classification in `0020` | backup-derived pending/released/settle-only/crossing fixtures and future-top-up exclusion |
| Strict Rapira contract | `app.services.rapira` | direct/inverse, numeric, missing/null/zero/negative ask, symbol, stale/future/naive timestamp, multiple entries, and malformed JSON tests |
| Pending exposure | pending allocation create/release services | pending-over-outstanding, failed release, and mode-switch PostgreSQL tests |
| Pending created before top-up | paid-time account split in `apply_merchant_financing` | `test_postgres_pending_created_before_topup_recovers_the_new_rolling` |
| Paid recovery and crossing/overflow | `apply_merchant_financing` | baseline partial split, full recovery, zero-outstanding settle, and decimal boundary matrix |
| Top-up | `register_funding` | idempotent/reactivation and paid/top-up concurrency tests |
| Reversal atomicity | `reverse_paid_allocation` | exact restore, withdrawn-settle rollback, and confirm/reversal race tests |
| Reconciliation | `reconcile_rolling_account` | baseline funding/split/reversal and paid-allocation concurrency tests |
| Merchant API compatibility | Merchant balance/create/operation serializers | stable balance fields and decimal-as-string Rolling state tests |
| Superadmin 2FA, session, CSRF, validation, idempotency | `_require_superadmin_web`; funding route; `register_funding` | `test_postgres_superadmin_rolling_funding_requires_real_2fa_session_and_csrf` |
| Outbox commit/enqueue crash recovery | transactional outbox plus due scanners | `test_postgres_outbox_scanner_recovers_commit_enqueue_crash` |
| No HTTP under outbox DB locks and newer-claim protection | two-phase delivery claim | `test_postgres_outbox_releases_db_lock_during_http_and_preserves_newer_claim` |
| Lock order and deadlock timeout | Deposit → Appeal, account → allocation, explicit timeout | confirm/expire, confirm/reversal, two paid, paid/top-up, cancel/confirm, and approve/reject concurrency tests |
| Migration upgrade/downgrade safety | migrations `0014` and `0015` | `test_postgres_clean_downgrade_and_funded_rolling_downgrade_guard` |

## Historical 38 versus 40 discrepancy

The earlier statement combined two different units and therefore was not
traceable: `38 passed` was a pytest item count, while “40 minimal scenarios”
was a manual checklist count in which several assertions or parameter cases
were counted separately. They were not forty independently collected tests, so
the claim was not justified.

The current review uses only pytest collection as the executable count and the
table above for requirement coverage. Parameter cases are visible as collected
pytest items; non-executable checklist bullets are not called tests. The exact
final collected/passed count is reported by the validation run rather than
hard-coded here.
