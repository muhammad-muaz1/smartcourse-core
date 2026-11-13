#!/usr/bin/env bash
# Throwaway Postgres + Redis for the integration tests on a machine without Docker
# (`make test-local`). Fresh every run, in .tools/, on ports nothing else uses — never a
# development database: the suites drop all tables and FLUSHDB between tests.
#
#   scripts/test_infra.sh start   # prints the two export lines for the test env
#   scripts/test_infra.sh stop
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.tools/test-infra"
PG_PORT=55432
REDIS_PORT=56379
PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"

start() {
  stop >/dev/null 2>&1 || true
  mkdir -p "$STATE"
  # A brand-new cluster each run (a new mktemp dir): trust auth, its own socket dir and port.
  local data
  data="$(mktemp -d "$STATE/pg.XXXXXX")"
  "$PG_BIN/initdb" -D "$data" -U postgres --auth=trust >/dev/null
  "$PG_BIN/pg_ctl" -D "$data" -l "$STATE/postgres.log" -w \
    -o "-p $PG_PORT -k $STATE -c listen_addresses=localhost -c fsync=off" start >/dev/null
  echo "$data" > "$STATE/pgdata"
  redis-server --port "$REDIS_PORT" --save "" --appendonly no --daemonize yes \
    --pidfile "$STATE/redis.pid" --logfile "$STATE/redis.log"
  echo "export SMARTCOURSE_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@localhost:$PG_PORT/postgres"
  echo "export SMARTCOURSE_TEST_REDIS_URL=redis://localhost:$REDIS_PORT/0"
}

stop() {
  if [ -f "$STATE/pgdata" ]; then
    "$PG_BIN/pg_ctl" -D "$(cat "$STATE/pgdata")" -m fast stop >/dev/null || true
  fi
  if [ -f "$STATE/redis.pid" ]; then
    kill "$(cat "$STATE/redis.pid")" 2>/dev/null || true
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
