#!/usr/bin/env bash
# One command from nothing to a repaid table.
#
#   export OPENAI_API_KEY=sk-...
#   ./run-demo.sh langgraph        # or dbos | temporal | inngest
#
# Everything this needs that you do not have, it tells you about and stops.
# It never invents a result and it never runs the probe against a stack that is
# not up: a probe that cannot reach its services fails in a way that looks like
# a durability finding, which is the one wrong answer this repo must not give.
set -euo pipefail
IMPL="${1:-langgraph}"
KILL_AFTER="${2:-8}"
PG="postgresql://postgres:postgres@localhost:5432/durable"

die() { echo "  $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

case "$IMPL" in
  langgraph|dbos|temporal|inngest) ;;
  *) die "unknown implementation '$IMPL' (langgraph | dbos | temporal | inngest)";;
esac

echo "== preconditions"
[ -n "${OPENAI_API_KEY:-}" ] || die "OPENAI_API_KEY is not set. The summarize step is a real
  gpt-4o-mini call, which is the entire point: the bill is what a crash makes you pay twice.
  One full run is about 13 calls at a few hundred tokens each."
have docker || die "docker is not installed. Every implementation needs Postgres."
docker info >/dev/null 2>&1 || die "docker is installed but not running."
echo "  ok  docker, OPENAI_API_KEY"

echo "== services"
docker compose up -d postgres >/dev/null
until docker compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
echo "  ok  postgres"
if [ "$IMPL" = "temporal" ]; then
  docker compose up -d temporal >/dev/null
  echo "  .. waiting for temporal (auto-setup takes ~30s on a cold start)"
  for _ in $(seq 1 60); do
    docker compose exec -T temporal temporal operator namespace describe default \
      >/dev/null 2>&1 && break
    sleep 2
  done
  docker compose exec -T temporal temporal operator namespace describe default >/dev/null 2>&1 \
    || die "temporal never came up. docker compose logs temporal"
  echo "  ok  temporal"
fi
if [ "$IMPL" = "inngest" ]; then
  have npx || die "npx is not installed and inngest_impl needs the dev server."
  curl -sf http://localhost:8288/health >/dev/null 2>&1 \
    || die "the inngest dev server is not running. In another shell:
    npx inngest-cli@latest dev"
  echo "  ok  inngest dev server"
fi

echo "== the run"
export DATABASE_URL="$PG" TEMPORAL_ADDRESS="localhost:7233"
# Reset = drop LangGraph's checkpoint tables and every workflow row. The probe runs
# the work three times and the FIRST is a clean baseline; without this it leaves a
# finished checkpoint and the killed run resumes from it in milliseconds.
RESET="docker compose exec -T postgres psql -U postgres -d durable -q -c \
'drop table if exists checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations cascade'"

case "$IMPL" in
  langgraph) CMD="python -m langgraph_impl.main --query 'kv cache eviction'";;
  dbos)      CMD="python -m dbos_impl.main --query 'kv cache eviction'";;
  temporal)  CMD="python -m temporal_impl.main --query 'kv cache eviction'"
             echo "  .. start the worker in another shell: python -m temporal_impl.worker";;
  inngest)   CMD="npx tsx inngest_impl/index.ts";;
esac

set -x
python probe.py --run "$CMD" --kill-after "$KILL_AFTER" --reset "$RESET"
