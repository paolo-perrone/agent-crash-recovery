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
#
# EVERY IMPLEMENTATION DECLARES ITS OWN RESET (2026-09-07). One shared reset string
# was wrong for three of the four: it dropped LangGraph's checkpoint tables, so DBOS
# kept its completed workflow and the killed run resumed instantly, and Temporal got
# no reset at all and re-ran everything. Four state models, four resets.
set -euo pipefail
IMPL="${1:-langgraph}"
KILL_AFTER="${2:-8}"
PG="postgresql://postgres:postgres@localhost:5432/durable"
PSQL="docker compose exec -T postgres psql -U postgres -d durable -q -c"

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
  curl -sf http://localhost:3000/api/inngest >/dev/null 2>&1 \
    || die "inngest_impl is not being served. In a third shell:
    npx tsx inngest_impl/serve.ts"
  echo "  ok  inngest dev server + served function"
fi

echo "== the run"
export DATABASE_URL="$PG" TEMPORAL_ADDRESS="localhost:7233"
KILL_CMD=""

case "$IMPL" in
  langgraph)
    # LangGraph's PostgresSaver owns four tables. Dropping them puts the next run
    # back at a cold start, which is what the probe's baseline has to be.
    RESET="$PSQL 'drop table if exists checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations cascade'"
    CMD="python -m langgraph_impl.main --query 'kv cache eviction'";;
  dbos)
    # DBOS keeps workflow and step state in its own schema inside the system
    # database, so LangGraph's table list clears nothing here. Drop the schema and
    # DBOS.launch() rebuilds it.
    RESET="$PSQL 'drop schema if exists dbos cascade'"
    CMD="python -m dbos_impl.main --query 'kv cache eviction'"
    echo "  .. dbos rebuilds its schema on launch, so the first run is a little slower";;
  temporal)
    # Nothing to drop: Temporal keeps history server-side, keyed by workflow id.
    # The reset terminates the previous run so the baseline starts clean, and the
    # id stays stable so the RESTART resumes instead of starting over.
    RESET="docker compose exec -T temporal temporal workflow terminate \
      --workflow-id research-demo --reason probe-reset >/dev/null 2>&1 || true"
    CMD="python -m temporal_impl.main --query 'kv cache eviction'"
    echo "  .. start the worker in another shell: python -m temporal_impl.worker";;
  inngest)
    # Inngest runs the function INSIDE its dev server. The client this probe starts
    # only fires an event, so killing the client kills nothing under test. --kill-cmd
    # kills the served app, which is where the steps actually execute. Read
    # "Measuring Inngest" in the README before trusting a number from this path.
    RESET="curl -sf -X DELETE http://localhost:8288/v1/runs >/dev/null 2>&1 || true"
    CMD="npx tsx inngest_impl/fire.ts 'kv cache eviction'"
    KILL_CMD="pkill -f 'tsx inngest_impl/serve.ts' || true";;
esac

set -x
if [ -n "$KILL_CMD" ]; then
  python probe.py --run "$CMD" --kill-after "$KILL_AFTER" --reset "$RESET" --kill-cmd "$KILL_CMD"
else
  python probe.py --run "$CMD" --kill-after "$KILL_AFTER" --reset "$RESET"
fi
