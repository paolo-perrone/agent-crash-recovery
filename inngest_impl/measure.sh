#!/usr/bin/env bash
# The repaid table for Inngest, measured the way Inngest actually recovers.
#
# probe.py runs one command three times, which fits a system where the client and
# the work share a process. Inngest does not: the dev server owns the run, your app
# is a callback target, and recovery happens when the APP comes back with no second
# event. So the phases are:
#
#   baseline  app up, fire, run to completion
#   killed    app up, fire, SIGKILL the app mid-run
#   restart   app back up, NO new event, the dev server retries the same run
#
# Same arithmetic as probe.py: repaid = (killed + restart) - baseline.
#
# NOT YET PRODUCING A TRUSTWORTHY TABLE, 2026-09-08. Five failure modes were found
# and fixed here, and the sixth is unsolved:
#
#   1. A PID captured through $(...) is the subshell's, so the kill missed and every
#      phase wrote to the baseline ledger.
#   2. `lsof -ti :3000` matches CLIENTS as well as listeners, so clearing the port
#      killed the dev server, which polls the app there. Use -sTCP:LISTEN.
#   3. With --no-discovery the dev server syncs on its own schedule, so an app
#      restarted between phases is never registered and the event goes nowhere.
#      A PUT to the app's endpoint registers it immediately.
#   4. tsx spawns node as a CHILD. kill -9 on the tsx pid leaves the server running,
#      and it finishes the run into the ledger it started with.
#   5. Inngest fans the summaries out with Promise.all, so a run that takes 15s in
#      the sequential Python implementations takes about 5s here. KILL_AFTER has to
#      be a few seconds, not eight.
#   6. The three phases shared one dev server and one queue, so a retry scheduled
#      during the baseline executed during the restart. A 2-second kill produced an
#      empty baseline and 22 summarize in the restart, which was the baseline's own
#      work arriving late. The baseline now gets its own dev server, torn down
#      before the killed phase starts. Killed and restart still share one, because
#      the restart resuming the killed run IS the measurement.
#
# KILL_AFTER has a narrow useful range here. Under about 2 seconds the kill lands
# before search finishes and nothing is at risk; over about 5 the run is already
# done. 3 to 4.5 puts it inside the fan-out, which is where the interesting answer
# is.
#
# The app is started INLINE, never through command substitution. Capturing the PID
# with $(...) returns the subshell's pid, the kill misses, and the app keeps writing
# to whichever ledger it started with. That produced a baseline of 22 summarize and
# two empty phases on 2026-09-08.
set -uo pipefail
LEDGERS="${LEDGERS:-/tmp/inngest-ledgers}"
KILL_AFTER="${KILL_AFTER:-8}"
RESTART_WAIT="${RESTART_WAIT:-45}"
TSX="${TSX:-./node_modules/.bin/tsx}"
mkdir -p "$LEDGERS"

free_port() {
  # A leftover app on :3000 answers wait_up, so the script proceeds against a
  # process it did not start and every ledger stays empty. Clear the port first
  # (2026-09-08).
  # -sTCP:LISTEN or this kills the dev server too: it holds an outbound connection
  # to :3000 to poll the app, and a bare `lsof -ti :3000` matches clients as well as
  # listeners (2026-09-08).
  lsof -ti tcp:3000 -sTCP:LISTEN 2>/dev/null | while read -r pid; do kill -9 "$pid" 2>/dev/null; done
  sleep 0.5
}
wait_up() {  # $1 = pid of the app WE started, $2 = its log
  for _ in $(seq 1 40); do
    kill -0 "$1" 2>/dev/null || { echo "the app exited during startup:"; tail -5 "$2"; return 1; }
    if curl -sf -o /dev/null http://localhost:3000/api/inngest; then
      # PUT registers the app with the dev server immediately. With --no-discovery
      # the server otherwise syncs on its own schedule, and an app restarted between
      # phases misses that window, so the event publishes and nothing ever invokes
      # the function (2026-09-08).
      curl -sf -o /dev/null -X PUT http://localhost:3000/api/inngest || true
      for _ in $(seq 1 20); do
        curl -s http://localhost:8288/v0/gql -X POST -H 'content-type: application/json' \
          -d '{"query":"{ functions { name } }"}' 2>/dev/null | grep -q research-agent && return 0
        sleep 0.5
      done
      echo "the app is serving but the dev server never registered its function"
      return 1
    fi
    sleep 0.5
  done
  return 1
}
wait_down() {
  for _ in $(seq 1 20); do
    curl -sf -o /dev/null http://localhost:3000/api/inngest || return 0
    sleep 0.5
  done
  return 1
}

for f in baseline killed restart; do : > "$LEDGERS/$f.jsonl"; done

kill_app() {
  # tsx spawns node as a CHILD. kill -9 on the tsx pid leaves the real server alive,
  # and it happily finishes the run into whichever ledger it started with: the killed
  # phase logged all 14 executions and the restart logged none (2026-09-08). Kill the
  # children first, then the wrapper.
  pkill -9 -P "$1" 2>/dev/null
  kill -9 "$1" 2>/dev/null
  wait "$1" 2>/dev/null
  for _ in $(seq 1 20); do
    curl -sf -o /dev/null http://localhost:3000/api/inngest || return 0
    sleep 0.5
  done
}

# ONE DEV SERVER PER INDEPENDENT PHASE. The baseline gets its own; killed and
# restart SHARE one, because the restart resuming the killed run is the whole
# measurement. Sharing all three let a retry scheduled during the baseline execute
# during the restart, which produced an empty baseline and 22 summarize in the
# restart on 2026-09-08. `inngest dev` defaults to --persist=false, so a fresh
# process starts with an empty queue.
DEV_PID=""
dev_up() {
  dev_down
  npx --yes inngest-cli@latest dev -u http://localhost:3000/api/inngest --no-discovery \
    >>"$LEDGERS/dev.log" 2>&1 &
  DEV_PID=$!
  for _ in $(seq 1 60); do
    curl -sf -o /dev/null http://localhost:8288/health && return 0
    sleep 1
  done
  echo "dev server never came up"; return 1
}
dev_down() {
  [ -n "$DEV_PID" ] && { pkill -9 -P "$DEV_PID" 2>/dev/null; kill -9 "$DEV_PID" 2>/dev/null
                         wait "$DEV_PID" 2>/dev/null; }
  lsof -ti tcp:8288 -sTCP:LISTEN 2>/dev/null | while read -r p; do kill -9 "$p" 2>/dev/null; done
  DEV_PID=""
  for _ in $(seq 1 20); do
    curl -sf -o /dev/null http://localhost:8288/health || return 0
    sleep 0.5
  done
}
cleanup() {
  dev_down
  lsof -ti tcp:3000 -sTCP:LISTEN 2>/dev/null | while read -r p; do kill -9 "$p" 2>/dev/null; done
}
trap cleanup EXIT

echo "== baseline"
dev_up || exit 1
free_port
PROBE_LEDGER="$LEDGERS/baseline.jsonl" PROBE_OFFLINE=1 INNGEST_DEV=1 \
  $TSX inngest_impl/serve.ts >"$LEDGERS/app-baseline.log" 2>&1 &
APP=$!
wait_up "$APP" "$LEDGERS/app-baseline.log" || exit 1
PROBE_OFFLINE=1 INNGEST_DEV=1 $TSX inngest_impl/fire.ts "kv cache eviction" >/dev/null 2>&1
kill_app "$APP"
dev_down

echo "== killed (a fresh dev server: the baseline's queue must not bleed in)"
dev_up || exit 1
free_port
PROBE_LEDGER="$LEDGERS/killed.jsonl" PROBE_OFFLINE=1 INNGEST_DEV=1 \
  $TSX inngest_impl/serve.ts >"$LEDGERS/app-killed.log" 2>&1 &
APP=$!
wait_up "$APP" "$LEDGERS/app-killed.log" || exit 1
PROBE_OFFLINE=1 INNGEST_DEV=1 $TSX inngest_impl/fire.ts "kv cache eviction" >/dev/null 2>&1 &
FIRE=$!
sleep "$KILL_AFTER"
kill_app "$APP"
pkill -9 -P "$FIRE" 2>/dev/null; kill -9 "$FIRE" 2>/dev/null; wait "$FIRE" 2>/dev/null

echo "== restart (no new event; the dev server retries the same run)"
free_port
PROBE_LEDGER="$LEDGERS/restart.jsonl" PROBE_OFFLINE=1 INNGEST_DEV=1 \
  $TSX inngest_impl/serve.ts >"$LEDGERS/app-restart.log" 2>&1 &
APP=$!
wait_up "$APP" "$LEDGERS/app-restart.log" || exit 1
sleep "$RESTART_WAIT"
kill_app "$APP"

echo "== table"
python3 - "$LEDGERS" <<'PY'
import json, os, sys, collections
d = sys.argv[1]
def read(n):
    rows = [json.loads(l) for l in open(os.path.join(d, n)) if l.strip()]
    return collections.Counter(r["step"] for r in rows if r.get("phase") == "completed")
b, k, r = read("baseline.jsonl"), read("killed.jsonl"), read("restart.jsonl")
print(f"  {'step':<14}{'once':>6}{'killed':>8}{'restart':>9}{'repaid':>8}")
worst = 0
for s in sorted(set(b) | set(k) | set(r)):
    extra = (k[s] + r[s]) - b[s]
    worst = max(worst, extra)
    print(f"  {s:<14}{b[s]:>6}{k[s]:>8}{r[s]:>9}{extra:>+8}")
print(f"\n  worst repaid on any step: {worst:+d}")
PY
