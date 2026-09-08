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
wait_up() {  # $1 = pid of the app WE started
  for _ in $(seq 1 40); do
    kill -0 "$1" 2>/dev/null || { echo "the app exited during startup:"; tail -5 "$2"; return 1; }
    curl -sf -o /dev/null http://localhost:3000/api/inngest && return 0
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

# The script owns the dev server too. Leaving it to another shell means the whole
# measurement depends on a process nothing here can see, and a dev server that dies
# mid-run looks exactly like a durability finding (2026-09-08).
DEV_PID=""
if ! curl -sf -o /dev/null http://localhost:8288/health; then
  npx --yes inngest-cli@latest dev -u http://localhost:3000/api/inngest --no-discovery \
    >"$LEDGERS/dev.log" 2>&1 &
  DEV_PID=$!
  for _ in $(seq 1 60); do
    curl -sf -o /dev/null http://localhost:8288/health && break
    sleep 1
  done
  curl -sf -o /dev/null http://localhost:8288/health || { echo "dev server never came up"; exit 1; }
fi
cleanup() {
  [ -n "$DEV_PID" ] && kill -9 "$DEV_PID" 2>/dev/null
  lsof -ti tcp:3000 -sTCP:LISTEN 2>/dev/null | while read -r p; do kill -9 "$p" 2>/dev/null; done
}
trap cleanup EXIT

echo "== baseline"
free_port
PROBE_LEDGER="$LEDGERS/baseline.jsonl" PROBE_OFFLINE=1 INNGEST_DEV=1 \
  $TSX inngest_impl/serve.ts >"$LEDGERS/app-baseline.log" 2>&1 &
APP=$!
wait_up "$APP" "$LEDGERS/app-baseline.log" || exit 1
PROBE_OFFLINE=1 INNGEST_DEV=1 $TSX inngest_impl/fire.ts "kv cache eviction" >/dev/null 2>&1
kill -9 "$APP" 2>/dev/null; wait "$APP" 2>/dev/null; wait_down

echo "== killed"
free_port
PROBE_LEDGER="$LEDGERS/killed.jsonl" PROBE_OFFLINE=1 INNGEST_DEV=1 \
  $TSX inngest_impl/serve.ts >"$LEDGERS/app-killed.log" 2>&1 &
APP=$!
wait_up "$APP" "$LEDGERS/app-killed.log" || exit 1
PROBE_OFFLINE=1 INNGEST_DEV=1 $TSX inngest_impl/fire.ts "kv cache eviction" >/dev/null 2>&1 &
FIRE=$!
sleep "$KILL_AFTER"
kill -9 "$APP" 2>/dev/null; kill -9 "$FIRE" 2>/dev/null
wait "$APP" 2>/dev/null; wait "$FIRE" 2>/dev/null; wait_down

echo "== restart (no new event; the dev server retries the same run)"
free_port
PROBE_LEDGER="$LEDGERS/restart.jsonl" PROBE_OFFLINE=1 INNGEST_DEV=1 \
  $TSX inngest_impl/serve.ts >"$LEDGERS/app-restart.log" 2>&1 &
APP=$!
wait_up "$APP" "$LEDGERS/app-restart.log" || exit 1
sleep "$RESTART_WAIT"
kill -9 "$APP" 2>/dev/null; wait "$APP" 2>/dev/null

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
