# Production-safe smoke test

This smoke is read-only unless the owner separately authorizes one controlled
payment.

1. Validate both Compose configurations without rendering them to shared logs.
2. Confirm TradeSpace and Casino health endpoints return HTTP 200.
3. Confirm Alembic current revisions match heads in both applications.
4. Confirm PostgreSQL and Redis health checks and both Celery worker pings.
5. Open the TradeSpace login page and verify the expected product name.
6. Confirm production guards accept the supplied configuration and mock or
   sandbox modes remain disabled.
7. Scan logs for fatal exceptions and exact runtime secret values without
   printing matches.
8. Count plaintext-sensitive database fields using aggregate queries only; the
   result must be zero.
9. Confirm no alternate distribution containers are running.

Do not create a deposit, accept or reject an existing request, adjust a ledger,
or invoke a callback during this smoke without explicit approval.
