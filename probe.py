#!/usr/bin/env python3
"""Kill a run mid-flight and count what re-executes.

    python probe.py --run "python -m langgraph_impl.main --query x" --kill-after 8

Exit 0 = nothing expensive ran twice. Exit 1 = the steps that did, named.
The step that was running when you killed it always re-executes, so it is excluded.
"""
import argparse, collections, json, os, signal, subprocess, sys, time

LEDGER = os.environ.get("PROBE_LEDGER", "/tmp/probe-ledger.jsonl")


def run_once(cmd, kill_after):
    p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
    if kill_after:
        time.sleep(kill_after)
        if p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # no cleanup, like a real eviction
            return "killed"
    p.wait()
    return "finished"


def main(a):
    open(LEDGER, "w").close()
    if run_once(a.run, a.kill_after) == "finished":
        sys.exit("the run finished before the kill; raise --kill-after")

    with open(LEDGER) as f:
        before = [json.loads(l)["step"] for l in f]
    interrupted = before[-1] if before else None
    print(f"  killed after {a.kill_after}s during '{interrupted}', restarting")

    run_once(a.run, None)
    with open(LEDGER) as f:
        counts = collections.Counter(json.loads(l)["step"] for l in f)

    repeated = {k: v for k, v in counts.items() if v > 1 and k != interrupted}
    for step, n in sorted(counts.items()):
        tag = "REPAID" if step in repeated else "saved "
        print(f"  {tag}  {step} x{n}")
    if repeated:
        print(f"\n  {len(repeated)} step(s) re-executed. Each sat outside a boundary, or "
              f"inside one too coarse to have saved yet.")
        sys.exit(1)
    print("\n  nothing expensive ran twice")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--kill-after", type=float, default=8.0)
    main(p.parse_args())
