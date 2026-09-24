# Rollback plan

Rollback is initiated when migrations, health checks, workers, authentication,
provider validation, or read-only smoke checks fail.

1. Stop routing new traffic to the release candidate.
2. Preserve logs with secret-aware redaction and record image digests and Git
   metadata.
3. Stop application services; do not delete volumes.
4. If no write-compatible migration was applied, restore the previous Compose
   file and immutable images and restart services.
5. If a database rollback is required, stop all writers and restore the verified
   pre-deploy encrypted backup into new volumes. Never restore over the only
   copy of a database.
6. Run Alembic, integrity, health, worker, log, and plaintext-sensitive checks
   before restoring traffic.
7. Keep the failed release and evidence isolated until the incident review is
   complete.

Application downgrades must not decrypt or reintroduce plaintext-sensitive
values. Existing payments and ledger records must never be edited manually.
