# Clean server start

This is a procedure for a separately authorized staging step, not a deployment performed by local remediation.

1. Verify the release ZIP SHA-256 and every entry in `RELEASE_MANIFEST.json`. No local data, QA seed, credentials, run directory or dumps belong in the artifact.
2. Provision an empty PostgreSQL database and dedicated Redis identities/queues. Generate new secrets through the target secret store. Use the safe example solely as a list of settings; replace placeholders. Do not import any archived development database.
3. Install CPython 3.12 and the PostgreSQL client library (`libpq5` on the planned Linux image; PostgreSQL `bin` on PATH for native Windows), the runtime dependency constraints and the package in a new environment. The lock was verified on native Windows; Linux-only wheels/dependencies and container layers must be validated on the selected target before starting an image.
4. Inject configuration through the process environment, including `ENV=production`, database/Redis URLs, independent encryption/session/signing/metrics credentials, permitted hosts/origins and secure cookie/TLS settings. Keep `SEED_DEMO_DATA=false`.
5. Run `python -m alembic upgrade head` against the explicitly selected EMPTY database. Expected head: `0027_aggregator_credentials`.
6. Set new initial administrator credentials and 2FA secret; run `python -m scripts.bootstrap_superadmin --confirm-production` once. Re-running without `--update-existing` must be unchanged. There should be one initial Superadmin, required migration reference rows, its audit record, zero operations and zero financial ledger entries. No seed or QA script is part of startup.
7. Start API, worker and beat as separate managed services with the configured working directory. Financial commits do not depend on successful callback HTTP delivery.
8. Run preflight and smoke with the correct base URL. Verify role/session/CSRF/2FA and target network/TLS/egress. A green local test is not proof of production configuration.
9. Test merchant webhook consumers with independent new signing keys, including `deposit.pending` after a rejected appeal restores pending. Terminal consumers must inspect operation status and deduplicate business processing by operation; delivery retry is at least once. Confirm actual consumer compatibility before real traffic.
10. Prove ordinary encrypted backup/restore and offsite recovery with the server's actual encryption/key custody policy, scheduled jobs, storage, retention and alert recipients. The local Fernet drill is not certification of production backup infrastructure.

No bank/blockchain execution, real partner callback, real-money transfer or server deployment was performed as part of the local fix. Actual payment execution policies, external consumers, server secrets, operational monitoring and physical mobile-device checks remain target-environment acceptance items.

Integration environments and explicit owner-controlled activation: see [INTEGRATION_MODES.md](INTEGRATION_MODES.md). Migration never enables merchant traffic.


Aggregator admission is separate from Merchant approval. On the actual Production
server, migration/bootstrap never issues live aggregator keys. Keep the owner flag
disabled until authorized. Superadmin later issues a fresh environment-bound key,
configures tariffs and explicitly activates the aggregator, using the commands in
[INTEGRATION_MODES.md](INTEGRATION_MODES.md). Never import local test credentials or
balances. Existing local Sandbox keys stay Sandbox after the additive migration.
