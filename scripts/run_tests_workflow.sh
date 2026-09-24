#!/usr/bin/env bash
set -euo pipefail

compose_file="docker-compose.test.yml"
project_name="tradespace-test"
extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--compose-file)
      compose_file=$2
      shift 2
      ;;
    -p|--project)
      project_name=$2
      shift 2
      ;;
    *)
      extra_args+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "$compose_file" ]]; then
  echo "FAILED: compose file not found: $compose_file" >&2
  exit 1
fi

case "$project_name" in
  tradespace_test_run|tradespace-test|tradespace-test-*)
    ;;
  *)
    echo "REFUSED: test project is outside the cleanup allowlist: $project_name" >&2
    exit 2
    ;;
esac

cleanup() {
  docker compose -p "$project_name" -f "$compose_file" down --volumes --remove-orphans >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

docker compose -p "$project_name" -f "$compose_file" up \
  --build --abort-on-container-exit --exit-code-from tests --remove-orphans \
  "${extra_args[@]}"
cleanup_exit_code=$?

trap - EXIT
cleanup
exit "$cleanup_exit_code"
