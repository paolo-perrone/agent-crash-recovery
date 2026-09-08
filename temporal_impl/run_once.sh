#!/usr/bin/env bash
# Start the worker and the starter in ONE process group, so probe.py's SIGKILL
# reaches the process that is actually doing the work.
#
# Temporal executes activities in the WORKER, not in the client that starts the
# workflow. probe.py kills the process group it spawned, so a worker started in
# another shell is untouched by the kill and its ledger is a different file. The
# first real run on 2026-09-08 logged an empty baseline for exactly this reason.
set -uo pipefail
PY="${PYTHON:-python}"
# The worker's stderr goes to a file, never to /dev/null. A worker that dies on
# import takes the whole measurement with it and leaves no trace, which cost a
# debugging round on 2026-09-08.
$PY -m temporal_impl.worker > "${WORKER_LOG:-/tmp/temporal-worker.log}" 2>&1 &
WORKER=$!
trap 'kill -9 $WORKER 2>/dev/null' EXIT
for _ in $(seq 1 40); do
  $PY - <<'PROBE' && break
import socket, os, sys
host, _, port = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").partition(":")
try:
    socket.create_connection((host, int(port or 7233)), 1).close()
except OSError:
    sys.exit(1)
PROBE
  sleep 0.5
done
sleep 1
$PY -m temporal_impl.main --query "${1:-kv cache eviction}" --id "${WORKFLOW_ID:-research-demo}"
