# TradeSpace production release runbook

This runbook describes an explicit release sequence. It does not authorize a
production deployment. Run every command from the reviewed release checkout
and use a secrets-managed environment file outside Git.

```bash
export APP_ENV_FILE=/run/secrets/tradespace.env
```

## 1. Create and verify the backup

1. Stop release activity and record the image digest, Git revision, current
   Alembic revision, and maintenance owner.
2. Create an encrypted PostgreSQL backup using `BACKUP_RESTORE.md`.
3. Verify the encrypted artifact checksum.
4. Restore the backup into an isolated PostgreSQL instance and run
   `pg_restore --list` plus the documented reconciliation checks.
5. Keep the verified backup and previous application images until the release
   is accepted.

Do not continue if backup verification or financial reconciliation fails.

## 2. Validate configuration

```bash
docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  config --quiet

docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  --profile migrate run --rm migrate \
  python -m scripts.migrate --preflight
```

The preflight prints only Alembic `current` and `head`; it performs no upgrade
and never prints the database URL. Confirm the expected single head before
continuing.

## 3. Run the one-shot migration job

Stop `api`, `worker`, and `beat` before the schema change. PostgreSQL and Redis
remain running.

```bash
docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  stop api worker beat

docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  --profile migrate run --rm migrate
```

The `migrate` profile is not selected by a normal `docker compose up`. The job
runs one `alembic upgrade head`, verifies that `current == head`, and exits
nonzero on failure. If it fails, stop: do not retry blindly, edit data by hand,
or start the new application image.

Superadmin bootstrap is a separate, explicit operation and is not part of
normal releases:

```bash
docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  run --rm api python -m scripts.bootstrap_superadmin --confirm-production
```

The command creates only the first Superadmin. An existing account is unchanged
unless `--update-existing` is also supplied. Password and 2FA values come from
the environment and are never command-line arguments or output.

## 4. Start services and reload nginx

```bash
docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  up -d --build api worker beat

docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  up -d nginx

docker compose \
  --env-file "$APP_ENV_FILE" \
  -f docker-compose.production.yml \
  exec nginx nginx -s reload
```

The production commands for `api`, `worker`, and `beat` only start their
processes. They never run Alembic, seed data, bootstrap users, change 2FA, or
write administrative records.

## 5. Preflight and smoke

1. Run `python -m scripts.preflight_v2`.
2. Verify `/version`, `/health`, and `/ready`.
3. Run the GET-only checks in `SMOKE_TEST.md`.
4. Inspect `api`, `worker`, `beat`, nginx, PostgreSQL, and Redis logs without
   copying credentials into the release record.
5. Complete financial reconciliation and the manual release checklist.

## 6. Failure handling

Prefer a reviewed forward-fix after a schema migration. Restore the verified
backup only under the rollback procedure in `ROLLBACK_PLAN.md`, after stopping
all writers and confirming that post-backup data loss is accepted. Never assume
that an Alembic downgrade reverses PostgreSQL ENUM or data transformations.

Do not copy a development environment file, local log, plaintext dump, private
backup identity, workstation credential, password, TOTP seed, or API secret to
the server or a public issue.
