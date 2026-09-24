#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  cat <<'EOF'
Usage: verify_encrypted_backup_restore.sh --backup-set DIR --identity-file FILE

The identity file is read by age and must be stored outside the repository.
Two temporary PostgreSQL containers are created on an internal Docker network.
No ports are published and no application container is connected.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

backup_set=""
identity_file=""
postgres_image="${POSTGRES_RESTORE_IMAGE:-postgres:16-alpine}"

while (($#)); do
  case "$1" in
    --backup-set)
      (($# >= 2)) || die "--backup-set requires a value"
      backup_set=$2
      shift 2
      ;;
    --identity-file)
      (($# >= 2)) || die "--identity-file requires a value"
      identity_file=$2
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

[[ -d "$backup_set" ]] || die "backup set directory is missing"
[[ -s "$identity_file" ]] || die "age identity file is missing or empty"

for command_name in docker age sha256sum mktemp awk seq; do
  require_command "$command_name"
done

backup_set=$(cd "$backup_set" && pwd -P)
run_id="stage2-restore-$$"
network_name="${run_id}-net"
containers=("${run_id}-tradespace" "${run_id}-casino")
plain_files=()

cleanup() {
  local path container
  for path in "${plain_files[@]:-}"; do
    [[ -n "$path" ]] && rm -f -- "$path"
  done
  for container in "${containers[@]}"; do
    docker rm -f "$container" >/dev/null 2>&1 || true
  done
  docker network rm "$network_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker network create --internal "$network_name" >/dev/null

verify_one() {
  local logical_name=$1
  local container=$2
  local encrypted encrypted_checksum plain_checksum expected_plain plain table_count

  encrypted="$backup_set/${logical_name}.dump.age"
  encrypted_checksum="$backup_set/${logical_name}.dump.age.sha256"
  plain_checksum="$backup_set/${logical_name}.dump.plaintext.sha256"
  [[ -s "$encrypted" ]] || die "$logical_name encrypted backup is missing"
  [[ -s "$encrypted_checksum" ]] || die "$logical_name encrypted checksum is missing"
  [[ -s "$plain_checksum" ]] || die "$logical_name plaintext checksum is missing"

  (cd "$backup_set" && sha256sum --check "${logical_name}.dump.age.sha256" >/dev/null)

  plain=$(mktemp "/tmp/${logical_name}.restore.XXXXXX.dump")
  plain_files+=("$plain")
  age --decrypt --identity "$identity_file" --output "$plain" "$encrypted"
  [[ -s "$plain" ]] || die "$logical_name decrypted dump is empty"

  expected_plain=$(awk 'NR == 1 {print $1}' "$plain_checksum")
  [[ -n "$expected_plain" ]] || die "$logical_name plaintext checksum is invalid"
  [[ "$(sha256sum "$plain" | awk '{print $1}')" == "$expected_plain" ]] \
    || die "$logical_name plaintext checksum mismatch"

  docker run -d --name "$container" --network "$network_name" \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=restore_test \
    "$postgres_image" >/dev/null

  for _ in $(seq 1 60); do
    if docker exec "$container" pg_isready -U postgres -d restore_test >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  docker exec "$container" pg_isready -U postgres -d restore_test >/dev/null 2>&1 \
    || die "$logical_name isolated PostgreSQL did not become ready"

  docker exec -i "$container" pg_restore --list - <"$plain" >/dev/null
  docker exec -i "$container" pg_restore --exit-on-error --no-owner --no-acl \
    --username=postgres --dbname=restore_test <"$plain" >/dev/null
  table_count=$(docker exec "$container" psql -U postgres -d restore_test -tAc \
    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');")
  [[ "$table_count" =~ ^[1-9][0-9]*$ ]] || die "$logical_name restore contains no application tables"

  rm -f -- "$plain"
  docker rm -f "$container" >/dev/null
}

verify_one "tradespace" "${containers[0]}"
verify_one "casino" "${containers[1]}"

printf 'Isolated restore verification passed for TradeSpace and Casino.\n'
