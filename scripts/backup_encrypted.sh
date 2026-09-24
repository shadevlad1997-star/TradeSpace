#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  cat <<'EOF'
Usage: backup_encrypted.sh --output-dir DIR --recipient-file FILE [options]

Required:
  --output-dir DIR       Directory outside the Git checkout.
  --recipient-file FILE  age recipients file containing public recipients only.

Options:
  --tradespace-db NAME        TradeSpace PostgreSQL container name.
  --casino-db NAME       Casino PostgreSQL container name.
  --help                 Show this help.

The script never accepts or reads an age private identity.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

output_dir=""
recipient_file=""
tradespace_db="${TRADESPACE_DB_CONTAINER:-processing_platform_production_ready-db-1}"
casino_db="${CASINO_DB_CONTAINER:-casino_platform_clean_integrated_fixed_with_start_file-postgres-1}"

while (($#)); do
  case "$1" in
    --output-dir)
      (($# >= 2)) || die "--output-dir requires a value"
      output_dir=$2
      shift 2
      ;;
    --recipient-file)
      (($# >= 2)) || die "--recipient-file requires a value"
      recipient_file=$2
      shift 2
      ;;
    --tradespace-db)
      (($# >= 2)) || die "--tradespace-db requires a value"
      tradespace_db=$2
      shift 2
      ;;
    --casino-db)
      (($# >= 2)) || die "--casino-db requires a value"
      casino_db=$2
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$output_dir" ]] || die "--output-dir is required"
[[ -n "$recipient_file" ]] || die "--recipient-file is required"
[[ -s "$recipient_file" ]] || die "recipient file is missing or empty"

for command_name in docker age sha256sum mktemp date awk; do
  require_command "$command_name"
done

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
mkdir -p "$output_dir"
output_root=$(cd "$output_dir" && pwd -P)
case "$output_root/" in
  "$repo_root/"*) die "backup destination must be outside the Git checkout" ;;
esac

docker inspect "$tradespace_db" >/dev/null 2>&1 || die "TradeSpace DB container is unavailable"
docker inspect "$casino_db" >/dev/null 2>&1 || die "Casino DB container is unavailable"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
set_dir="$output_root/backup-set-$stamp"
[[ ! -e "$set_dir" ]] || die "backup set already exists: $set_dir"
mkdir -m 700 "$set_dir"

temporary_files=()
cleanup() {
  local path
  for path in "${temporary_files[@]:-}"; do
    [[ -n "$path" ]] && rm -f -- "$path"
  done
  rmdir -- "$set_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

backup_database() {
  local logical_name=$1
  local container=$2
  local plain encrypted encrypted_tmp plain_hash encrypted_hash

  plain=$(mktemp "$output_root/.${logical_name}.XXXXXX.dump")
  encrypted="$set_dir/${logical_name}.dump.age"
  encrypted_tmp="$set_dir/.${logical_name}.dump.age.partial"
  temporary_files+=(
    "$plain"
    "$encrypted_tmp"
    "$encrypted"
    "$set_dir/${logical_name}.dump.plaintext.sha256"
    "$set_dir/${logical_name}.dump.age.sha256"
  )

  docker exec "$container" sh -ceu \
    'pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
    >"$plain"
  [[ -s "$plain" ]] || die "$logical_name dump is empty"

  docker exec -i "$container" pg_restore --list - <"$plain" >/dev/null
  plain_hash=$(sha256sum "$plain" | awk '{print $1}')

  age --encrypt --recipients-file "$recipient_file" --output "$encrypted_tmp" "$plain"
  [[ -s "$encrypted_tmp" ]] || die "$logical_name encrypted backup is empty"
  encrypted_hash=$(sha256sum "$encrypted_tmp" | awk '{print $1}')

  mv -- "$encrypted_tmp" "$encrypted"
  printf '%s\n' "$plain_hash" >"$set_dir/${logical_name}.dump.plaintext.sha256"
  printf '%s  %s\n' "$encrypted_hash" "${logical_name}.dump.age" \
    >"$set_dir/${logical_name}.dump.age.sha256"

  rm -f -- "$plain"
}

backup_database "tradespace" "$tradespace_db"
backup_database "casino" "$casino_db"

(
  cd "$set_dir"
  sha256sum --check tradespace.dump.age.sha256 >/dev/null
  sha256sum --check casino.dump.age.sha256 >/dev/null
)

cat >"$set_dir/MANIFEST.txt" <<EOF
format=age
created_utc=$stamp
tradespace_container=$tradespace_db
casino_container=$casino_db
plaintext_retained=no
restore_verification=pending
EOF

temporary_files=()

printf 'Encrypted backup set created: %s\n' "$set_dir"
printf 'Restore verification is mandatory before this set can be promoted.\n'
