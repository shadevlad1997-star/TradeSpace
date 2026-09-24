# Backup and Restore

This guide describes the minimum production backup procedure for PostgreSQL,
Redis, uploads, and configuration secrets.

For the mandatory TradeSpace + Casino encrypted PostgreSQL workflow, checksums,
key separation, and isolated restore drill, follow
[`ENCRYPTED_BACKUP_POLICY.md`](ENCRYPTED_BACKUP_POLICY.md). An unencrypted dump
is a temporary checkpoint only and is not eligible for off-site promotion.

## What to back up

- PostgreSQL database.
- Redis append-only data if Redis persistence is used.
- Uploads volume.
- `.env.production` and TLS certificate metadata, stored in a secure vault.
- Deployment version or release ZIP checksum.

Never store backups in the public web root. Never commit backups to Git.

## PostgreSQL backup

Run from the server directory that contains `docker-compose.production.yml`:

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
New-Item -ItemType Directory -Force -Path ".\backups" | Out-Null
docker compose -f docker-compose.production.yml exec -T db pg_dump -U processor -d processor_db -Fc > ".\backups\postgres_$stamp.dump"
```

Encrypt or move the dump to a private backup store immediately.

## Uploads backup

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
docker run --rm -v tradespace_uploads_production:/uploads:ro -v "${PWD}\backups:/backups" alpine tar -czf "/backups/uploads_$stamp.tar.gz" -C /uploads .
```

Adjust the Docker volume name if the compose project name differs.

## Redis backup

Redis is used for cache, rate limits, queues, and short-lived state. If Redis
persistence is enabled and operational recovery requires it, back up the Redis
volume:

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
docker run --rm -v tradespace_redisdata_production:/redis:ro -v "${PWD}\backups:/backups" alpine tar -czf "/backups/redis_$stamp.tar.gz" -C /redis .
```

## Restore PostgreSQL

Stop API and workers before restoring:

```powershell
docker compose -f docker-compose.production.yml stop api worker beat
docker compose -f docker-compose.production.yml exec -T db dropdb -U processor processor_db
docker compose -f docker-compose.production.yml exec -T db createdb -U processor processor_db
Get-Content ".\backups\postgres_YYYYMMDD_HHMMSS.dump" -Encoding Byte | docker compose -f docker-compose.production.yml exec -T db pg_restore -U processor -d processor_db --clean --if-exists
docker compose -f docker-compose.production.yml up -d api worker beat
```

Run this first on staging. Production restore is a maintenance-window action.

## Restore uploads

```powershell
docker compose -f docker-compose.production.yml stop api worker beat
docker run --rm -v tradespace_uploads_production:/uploads -v "${PWD}\backups:/backups:ro" alpine sh -c "rm -rf /uploads/* && tar -xzf /backups/uploads_YYYYMMDD_HHMMSS.tar.gz -C /uploads"
docker compose -f docker-compose.production.yml up -d api worker beat
```

## Restore Redis

Only restore Redis if queue/cache state is required. In many incidents it is
safer to start Redis clean and let the application rebuild transient state.

```powershell
docker compose -f docker-compose.production.yml stop redis api worker beat
docker run --rm -v tradespace_redisdata_production:/redis -v "${PWD}\backups:/backups:ro" alpine sh -c "rm -rf /redis/* && tar -xzf /backups/redis_YYYYMMDD_HHMMSS.tar.gz -C /redis"
docker compose -f docker-compose.production.yml up -d redis api worker beat
```

## Restore validation

- Run `alembic upgrade head`.
- Check `/ready`.
- Log in as superadmin.
- Verify merchant API authentication.
- Verify webhook delivery on a staging merchant.
- Verify a small end-to-end deposit on staging before enabling production traffic.


## Native Windows local drill (fresh QA data)

Stop nothing for this read-only source snapshot; the command exports a PostgreSQL
REPEATABLE READ snapshot and uses it for both row/sum comparison and pg_dump.
The output directory must be new and outside the repository. It uses guarded
local endpoints and creates a uniquely named `tradespace_audit_restore_*` database;
it never restores over dev/test and never disables constraints or triggers.

```powershell
.\.venv\Scripts\python.exe -m scripts.local_restore_drill --confirm-local --output-dir R:\TradeSpace-Archive\new-restore-drill
```

The encrypted dump is immediately decrypted and checked byte-for-byte, then
restored with `--single-transaction --exit-on-error`. The report compares all
tables and numeric financial aggregates. Keep external local configuration
separately for decryption. This Fernet drill is local QA tooling, excluded from
the clean release, and is separate from the production encryption/custody policy
above. Old damaged local archives are preserved for evidence, never imported.
