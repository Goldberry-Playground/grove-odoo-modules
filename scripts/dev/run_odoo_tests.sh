#!/usr/bin/env bash
# Local mirror of the CI install-smoke-test job, plus the OCA addons that
# grove_schedule_f depends on. Usage: scripts/dev/run_odoo_tests.sh <module> [--allow-empty]
set -uo pipefail

usage() {
  echo "Usage: $0 <module> [--allow-empty]" >&2
  echo "  --allow-empty  treat a run that executes 0 tests as success" >&2
  exit 2
}

MODULE=""
ALLOW_EMPTY=0
for arg in "$@"; do
  case "$arg" in
    --allow-empty) ALLOW_EMPTY=1 ;;
    -*) usage ;;
    *) [ -z "$MODULE" ] && MODULE="$arg" || usage ;;
  esac
done
[ -n "$MODULE" ] || usage

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1
[ -d "$MODULE" ] || { echo "No such module directory: $MODULE" >&2; exit 2; }

OCA_REPOS=(account-reconcile bank-statement-import account-financial-tools
  account-financial-reporting reporting-engine server-ux mis-builder)

mkdir -p .oca
for r in "${OCA_REPOS[@]}"; do
  if [ ! -d ".oca/$r" ]; then
    git clone --depth 1 --branch 19.0 "https://github.com/OCA/$r.git" ".oca/$r" || exit 1
  fi
done

SUFFIX="$$"
NET="odoo_test_net_$SUFFIX"
PG="odoo_test_pg_$SUFFIX"
OD="odoo_test_odoo_$SUFFIX"
DB="odoo_test_$SUFFIX"

cleanup() {
  docker rm -f "$OD" "$PG" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

# A user-defined network instead of --network host: host networking is not
# reliably available on Docker Desktop for Mac.
docker network create "$NET" >/dev/null || exit 1
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres \
  postgres:17 >/dev/null || exit 1
for _ in $(seq 1 30); do
  docker exec "$PG" pg_isready -U odoo >/dev/null 2>&1 && break
  sleep 2
done

ADDONS="/mnt/extra-addons"
MOUNTS=(-v "$REPO_ROOT:/mnt/extra-addons")
for r in "${OCA_REPOS[@]}"; do
  ADDONS="$ADDONS,/mnt/oca/$r"
  MOUNTS+=(-v "$REPO_ROOT/.oca/$r:/mnt/oca/$r")
done
ADDONS="$ADDONS,/usr/lib/python3/dist-packages/odoo/addons"

docker run -d --name "$OD" --network "$NET" "${MOUNTS[@]}" \
  -e HOST="$PG" -e USER=odoo -e PASSWORD=odoo \
  odoo:19 \
  -- --addons-path="$ADDONS" \
     --database="$DB" \
     --without-demo=all \
     --init="$MODULE" \
     --test-enable \
     --test-tags="/$MODULE" \
     --stop-after-init \
     --log-level=info >/dev/null || exit 1

# Wait up to 20 minutes for Odoo to exit (OCA dependency install is slow).
for _ in $(seq 1 240); do
  [ -n "$(docker ps -q --filter "name=$OD" --filter status=running)" ] || break
  sleep 5
done

fail=0
reason=""
# A still-running container reports ExitCode 0, so detect the timeout first.
if [ -n "$(docker ps -q --filter "name=$OD" --filter status=running)" ]; then
  fail=1; reason="timed out waiting for Odoo to finish"
fi
status=$(docker inspect "$OD" --format '{{.State.ExitCode}}')
logs=$(docker logs "$OD" 2>&1)

if [ "$status" != "0" ]; then
  fail=1; reason="Odoo exited with status $status"
fi
if grep -qiE "tests won't run|database not initialized" <<<"$logs"; then
  fail=1; reason="Odoo reported it would not run tests"
fi
if grep -qE "^[^ ]+ [^ ]+ [0-9]+ (ERROR|CRITICAL) |(^|[^A-Za-z])(FAIL|ERROR): test_" <<<"$logs"; then
  fail=1; reason="ERROR/FAIL lines in the Odoo log"
fi
if ! grep -qE "odoo\.addons\.${MODULE}\.tests\." <<<"$logs"; then
  if [ "$ALLOW_EMPTY" = "1" ]; then
    echo "NOTE: 0 tests ran for $MODULE (allowed by --allow-empty)"
  else
    fail=1; reason="0 tests ran for $MODULE"
  fi
fi

if [ "$fail" != "0" ]; then
  echo "FAILED: $reason" >&2
  printf '%s\n' "$logs" | tail -n 120
  exit 1
fi
printf '%s\n' "$logs" | tail -n 15
echo "OK: $MODULE"
