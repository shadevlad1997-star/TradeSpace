# Encrypted PostgreSQL backup policy

This policy covers the primary TradeSpace PostgreSQL database and the integrated
Casino PostgreSQL database. Backups are not valid until encryption, checksum
verification, and an isolated restore test have all succeeded.

## Mandatory controls

- Use PostgreSQL custom-format dumps (`pg_dump --format=custom`).
- Encrypt every dump with `age` before it leaves the backup host.
- Record SHA-256 for the plaintext dump before encryption and for the encrypted
  artifact after encryption.
- Store backup sets outside every Git checkout and outside any public web root.
- Keep age private identities in a separate secrets vault or offline recovery
  location. Never copy a private identity into this repository, a container
  image, CI output, logs, command history, or the backup directory.
- Keep only the public recipient file on the backup host, also outside Git.
- Never run these scripts with shell tracing (`set -x`).
- Do not delete an old unencrypted backup without explicit owner approval and a
  verified encrypted replacement.

Recommended retention is 30 daily, 12 weekly, and 12 monthly verified backup
sets. The production owner may adopt stricter RPO/RTO and retention values.

## Key setup

Create the age identity on an offline administration host:

```sh
umask 077
age-keygen -o /secure/offline/tradespace-backup-identity.txt
age-keygen -y /secure/offline/tradespace-backup-identity.txt \
  > /secure/backup-host/tradespace-backup-recipients.txt
```

The first file is private and belongs in the recovery vault. The second file is
public and may be copied to the backup host, but it must remain outside Git.
Record the key owner, creation date, recovery custodians, and rotation date in
the secrets inventory without recording the private key itself.

## Create a backup set

Install `age` on the backup host and run from the TradeSpace repository:

```sh
./scripts/backup_encrypted.sh \
  --output-dir /srv/tradespace-backups \
  --recipient-file /etc/tradespace-backup/recipients.txt
```

Container names can be supplied with `--tradespace-db` and `--casino-db`. The script
uses the containers' existing `POSTGRES_USER` and `POSTGRES_DB` environment
variables without printing them. It performs `pg_restore --list`, encrypts each
dump, verifies encrypted SHA-256 files, and removes its temporary plaintext
files. It refuses a destination inside the repository and never overwrites an
existing backup set.

The script is fail-closed: missing Docker/age/checksum tools, unavailable DB
containers, an empty dump, a failed list check, encryption failure, or checksum
failure makes the command fail. A set with `restore_verification=pending` is not
eligible for retention or off-site promotion.

## Isolated restore test

Run the verifier on a restricted recovery host that has access to the private
identity:

```sh
./scripts/verify_encrypted_backup_restore.sh \
  --backup-set /srv/tradespace-backups/backup-set-YYYYMMDDTHHMMSSZ \
  --identity-file /secure/offline/tradespace-backup-identity.txt
```

The verifier checks the encrypted hashes, decrypts into mode-`0600` temporary
files, verifies the pre-encryption hashes, and restores each database into its
own disposable PostgreSQL container on an internal Docker network. No ports are
published and no application container is attached. The test fails if either
dump cannot be listed/restored or contains no application tables.

After a successful drill, record the operator, UTC time, artifact hashes,
PostgreSQL image version, and result in the external backup inventory. Do not
put the private identity or decrypted dump in that record. Run a drill at least
monthly, after backup-script changes, after PostgreSQL upgrades, and before a
production migration.

## Fail-closed operator checklist

1. Confirm both source DB containers are healthy and identify the correct
   compose projects without displaying their environment.
2. Confirm the destination is outside Git and has restrictive permissions.
3. Confirm `age`, Docker, SHA-256 tools, and free disk space are available.
4. Confirm the public recipient matches the separately stored recovery identity.
5. Run the backup script; do not promote a partial or pending set.
6. Run the isolated restore verifier on both dumps.
7. Copy only encrypted artifacts, checksums, and the non-secret manifest to the
   off-site store.
8. Verify the off-site hashes again.
9. Mark the set verified in the external inventory.
10. Preserve existing unencrypted checkpoints until the owner separately
    approves cleanup after encrypted replacements are verified.

## Key loss or compromise

Key loss makes every backup for that recipient unrecoverable. Key compromise
requires immediate recipient rotation, new backups under the new recipient,
isolated restore verification, access-log review, and owner-approved retirement
of artifacts encrypted only to the compromised key.

At the time this policy was introduced, `age` was not installed on the local
Windows workstation. The scripts therefore intentionally stop before reading a
database until `age` and a valid public recipient file are present.
