# TradeSpace

Processing platform: merchant deposits and payouts, trader assignments, immutable fee snapshots, Rolling, TeamLead accounting, settlements, disputes and durable webhooks.

The financial formulas remain covered by Golden scenarios. Addressed remediation changes and their verification are recorded in the current remediation report/handoff. A clean staging installation is not permission to process real money.

## Native Windows local development

Supported verified runtime: CPython 3.12, PostgreSQL 16, Redis 7, without Docker. Keep the source, runtime tools/data and local credentials in separate directories. The existing local layout is `R:\TradeSpace`, `R:\TradeSpace-runtime`, `R:\TradeSpaceData`, and `R:\TradeSpace-local`.

Prerequisite: the PostgreSQL client library `libpq` must be available on PATH (the native local helper adds its configured PostgreSQL `bin` directory). A plain venv contains psycopg but not a bundled database client library. For direct migration/bootstrap commands, add the installed PostgreSQL `bin` directory to PATH first.

Fresh environment installation:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps .
.\.venv\Scripts\python.exe -m pip check
```

Create local configuration from safe templates with newly generated keys. `scripts.recovery_env` writes to the external `TradeSpace-local` directory and refuses to overwrite existing keys. Provision the dedicated PostgreSQL cluster/user from that configuration before using the helper. Native binaries are local tooling, not part of the release artifact. The launcher expects the existing `tmp/runtime` tools paths.

```powershell
.\.venv\Scripts\python.exe -m scripts.recovery_env
.\.venv\Scripts\python.exe -m scripts.recovery_run databases
.\.venv\Scripts\python.exe -m scripts.recovery_run migrate
.\.venv\Scripts\python.exe -m scripts.recovery_run bootstrap
.\scripts\recovery_windows.ps1 -Action Start
```

The explicit bootstrap creates only the initial Superadmin and its audit record. No demo users, initial money or QA scenarios are created by application startup, migrations or production bootstrap. Secrets remain outside the source tree. `TRADESPACE_LOCAL_DIR` may select another external configuration directory.

## Optional local QA scenarios

Only for the guarded `127.0.0.1:55432/tradespace_dev` database with `ENV=local`:

```powershell
.\.venv\Scripts\python.exe -m scripts.qa_seed --confirm-local
```

Seven roles and named scenarios are initialized reproducibly. API/HMAC, ledger and existing domain commands create the financial outcomes. The expiration case advances a domain test clock. Rates use the actual read-only Rapira provider. Evidence/transaction hashes are synthetic; no bank or blockchain transfer is performed. Re-running the initializer does not repeat credits or operations. Access details are written only to external `qa-access.json`; scenario IDs and counts are in external `qa-scenarios.json`.

To replace local data, first stop application services and Redis while retaining PostgreSQL, then use a new external backup directory:

```powershell
.\scripts\recovery_windows.ps1 -Action StopApplications
.\.venv\Scripts\python.exe -m scripts.qa_reset --confirm-local-reset --backup-dir R:\TradeSpace-Archive\new-local-reset
.\.venv\Scripts\python.exe -m scripts.recovery_run migrate
.\.venv\Scripts\python.exe -m scripts.recovery_run bootstrap
.\scripts\recovery_windows.ps1 -Action Start
.\.venv\Scripts\python.exe -m scripts.qa_seed --confirm-local
```

Reset refuses foreign endpoints/owners or active database connections. It encrypts and verifies both dumps before archiving the old databases, disables their connections and creates empty replacements. Redis uses a new data directory. It never drops databases or runs `FLUSHALL`. Preserve the external archived configuration for backup decryption. No old development dump is imported into staging.

## Verification

Run PostgreSQL tests and Golden sequentially; both use the disposable test database:

```powershell
.\.venv\Scripts\python.exe -m scripts.recovery_run test
.\.venv\Scripts\python.exe -m scripts.platform1_golden --verify
.\.venv\Scripts\python.exe -m scripts.platform1_freeze_guard
.\.venv\Scripts\python.exe -m scripts.recovery_run preflight
.\.venv\Scripts\python.exe -m scripts.recovery_run smoke
.\.venv\Scripts\python.exe -m scripts.recovery_smoke
node --test tests/tradespace/shell_interactions.test.cjs
git diff --check
```

The recovery suite creates its own labelled synthetic participants. It exercises a local HTTP receiver and real worker retry; rates are a separate external dependency. A deterministic rate fixture must be reported as a fixture, not an external integration result. Freeze changes require an expressly authorized core task, reviewed exact diff and tests; Golden must not be blindly re-recorded.

## Clean release and server preparation

```powershell
.\.venv\Scripts\python.exe -m scripts.build_release --output R:\TradeSpace-Release\tradespace-clean.zip
```

The allowlist excludes QA initialization, local runtime, credentials, dumps, logs, uploads and tests. The ZIP includes file hashes and is read back after creation. Install it with a new empty database, new runtime secrets and the minimal bootstrap described in [Clean server start](docs/CLEAN_SERVER_START.md). Do not copy local `.env`, keys, QA accounts or database contents.

`requirements.lock` fixes the installed native Windows dependency closure without upgrading packages. The Dockerfile pins an official Python manifest and uses explicit COPY sources; its image/layers and Linux-only dependencies require verification in the future server environment. Docker is not required for this local remediation.

## Documentation

- [Business and API integration](docs/merchant-integration.md)
- [Merchant HMAC v2](docs/merchant-hmac-v2.md)
- [Webhook delivery](docs/webhook-integration.md)
- [Rolling](docs/rolling.md)
- [TeamLead](docs/teamlead.md)
- [Backup and ordinary restore](docs/BACKUP_RESTORE.md)
- [Environment variables](docs/ENVIRONMENT_VARIABLES.md)
- [Security](SECURITY.md)

The migration head is `0027_aggregator_credentials`. Server migration/deployment is a separate authorized step.


## Independent private source repository

The source handoff is distinct from the deployment ZIP. It retains tests, Golden,
Freeze and synthetic fixture generator code, but no generated accounts, financial
rows, attachments, local credentials, archives, caches or prior Git history.

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_repository --output R:\TradeSpace-Publish\new-private-source
```

The destination must be new. Its sibling manifest lists the SHA-256 of every
copied source file. Scan this directory for secrets before initializing a new Git
history. The command does not initialize Git, commit, push or deploy. The original
working directory and history remain intact. CI targets the guarded disposable
`tradespace_test` database and supports the first commit without a parent.

Current approved dispute behavior and acceptance evidence:
[Appeal confirmation report](docs/APPEAL_CONFIRMATION_REPORT.md).
