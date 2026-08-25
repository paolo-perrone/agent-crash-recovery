#!/usr/bin/env python3
"""Kill a run mid-flight and count what you pay for twice.

    python probe.py --run "python -m langgraph_impl.main --query x" --kill-after 8 \\
                    --reset "psql -c 'truncate checkpoints'"
    python probe.py --self-test          # no API key, no docker, proves both directions

Exit 0 = nothing expensive was repaid. Exit 1 = what was, named, with the bill.

HOW IT MEASURES (changed 2026-08-21, see WHY below)
Three runs, three separate ledgers:

    baseline   one clean run, uninterrupted     -> what the work costs once
    killed     same run, SIGKILLed mid-flight   -> what you paid before the crash
    restart    the resume                       -> what you paid again

--reset runs between the baseline and the killed run, and is REQUIRED against
anything durable. Without it the baseline leaves a completed checkpoint behind
and the killed run resumes from it instantly, which reads as a finished run and
aborts the probe. Against Postgres-backed LangGraph that is a truncate; against
Temporal it is a fresh workflow id.

    repaid[step] = (killed[step] + restart[step]) - baseline[step]

Anything above zero is work you bought twice. Nothing is excluded from that
subtraction, which is the whole point.

The FLOOR is one repaid step. Whatever was in flight when the kill landed had
logged its execution and not yet checkpointed, so even a perfectly durable
system buys it again. Exit 0 means the repaid set is exactly that one step.
Exit 1 means something else was repaid too, or the boundary is so coarse that
"only the in-flight step" stops meaning anything (see the COARSE guards).

WHY IT CHANGED
The previous version compared one cumulative ledger against `> 1` and then
excluded whichever step was running when the kill landed. Two bugs that hid
each other:

  1. `summarize` runs once PER PAGE, eleven times in a single clean run, so the
     `> 1` test flagged it as repaid on a perfect resume. A false positive on
     the one step this whole repo is about.
  2. Excluding the interrupted step meant the coarser your boundary, the better
     you scored. Wrap the entire agent in one step and the kill lands inside it
     by definition, it gets excluded, and the probe prints "nothing expensive
     ran twice" for a system with no durability at all.

Bug 2 masked bug 1, so the demo looked correct. Subtracting a baseline removes
the need for the exclusion and fixes both. The interrupted step is now REPORTED
rather than hidden: partial work inside it is genuinely lost, and how much of
the run it swallowed is exactly the number that tells you the boundary is too
coarse.
"""
import argparse, collections, json, os, signal, subprocess, sys, tempfile, textwrap, time

DEFAULT_LEDGER = "/tmp/probe-ledger.jsonl"


def run_once(cmd, ledger, kill_after=None):
    """Run cmd with PROBE_LEDGER=ledger. Returns ('killed'|'finished', seconds)."""
    open(ledger, "w").close()
    env = {**os.environ, "PROBE_LEDGER": ledger}
    t0 = time.time()
    p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid, env=env)
    if kill_after:
        time.sleep(kill_after)
        if p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)   # no cleanup, like a real eviction
            return "killed", time.time() - t0
    p.wait()
    return "finished", time.time() - t0


def read(ledger):
    """Counter of executions per step, plus the ordered list."""
    try:
        rows = [json.loads(l) for l in open(ledger) if l.strip()]
    except FileNotFoundError:
        rows = []
    return collections.Counter(r["step"] for r in rows), rows


def measure(cmd, kill_after, ledger_dir, reset=None):
    def do_reset():
        if reset:
            subprocess.run(reset, shell=True, check=False)

    base_l = os.path.join(ledger_dir, "baseline.jsonl")
    kill_l = os.path.join(ledger_dir, "killed.jsonl")
    rest_l = os.path.join(ledger_dir, "restart.jsonl")

    do_reset()
    state, base_secs = run_once(cmd, base_l, None)
    if state == "killed":
        sys.exit("baseline run was killed; that should not happen")
    baseline, base_rows = read(base_l)
    if not baseline:
        sys.exit(f"baseline run logged nothing to {base_l}. Is the work wrapped in @probe?")
    print(f"  baseline    {base_secs:5.1f}s  {sum(baseline.values())} executions")

    do_reset()
    state, _ = run_once(cmd, kill_l, kill_after)
    if state == "finished":
        sys.exit(f"the run finished inside {kill_after}s; raise --kill-after "
                 f"(a clean run takes about {base_secs:.0f}s)")
    killed, kill_rows = read(kill_l)
    interrupted = kill_rows[-1]["step"] if kill_rows else None
    lost = kill_after - (kill_rows[-1]["t"] - kill_rows[0]["t"]) if len(kill_rows) > 1 else kill_after
    print(f"  killed      {kill_after:5.1f}s  {sum(killed.values())} executions, "
          f"cut during '{interrupted}'")

    run_once(cmd, rest_l, None)
    restart, _ = read(rest_l)
    print(f"  restart            {sum(restart.values())} executions")
    return baseline, killed, restart, interrupted, lost


def report(baseline, killed, restart, interrupted, lost, kill_after):
    steps = sorted(set(baseline) | set(killed) | set(restart))
    repaid = {}
    print()
    print(f"  {'step':<14}{'once':>6}{'killed':>8}{'restart':>9}{'repaid':>8}")
    for s in steps:
        b, k, r = baseline[s], killed[s], restart[s]
        extra = (k + r) - b
        if extra > 0:
            repaid[s] = extra
        print(f"  {s:<14}{b:>6}{k:>8}{r:>9}{extra:>+8}")

    # The interrupted step's partial work is always lost. That is not a bug in the
    # system under test, but how MUCH of the run it represents is the verdict on
    # whether the boundary is drawn tightly enough to be worth anything.
    swallowed = lost / kill_after if kill_after else 0
    print(f"\n  '{interrupted}' was mid-flight at the kill: about {lost:.1f}s of "
          f"partial work lost ({swallowed:.0%} of the time before the crash).")
    # COARSE guards. These exist because "only the in-flight step was repaid" is
    # trivially true for a system with no boundaries at all: wrap everything in one
    # step and the kill lands inside it by definition. That is the exact hole the
    # pre-2026-08-21 version had, so it gets checked before the verdict, not after.
    if len(baseline) == 1:
        print("  COARSE: the whole run is one step, so a crash anywhere repays everything. "
              "There is no boundary here to measure.")
        return 1
    if swallowed > 0.5:
        print(f"  COARSE: {swallowed:.0%} of the pre-crash time sat inside a single step. "
              "Split it before trusting the verdict below.")
        return 1

    beyond = {k: v for k, v in repaid.items() if k != interrupted}
    if beyond:
        total = sum(beyond.values())
        print(f"\n  {total} execution(s) across {len(beyond)} step(s) were bought twice "
              "on top of the in-flight one: "
              + ", ".join(f"{k} x{v}" for k, v in sorted(beyond.items())))
        print("  Each sat outside a boundary, or inside one too coarse to have saved yet.")
        return 1
    if repaid:
        print(f"\n  only '{interrupted}' was repaid, which is the floor. "
              "Everything completed before the crash was saved.")
    else:
        print("\n  nothing was repaid at all (the kill landed between steps)")
    return 0


SELF_TEST_COARSE = '''
import json, os, time
LEDGER = os.environ["PROBE_LEDGER"]
def log(step):
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"step": step, "t": time.time()}) + "\\n")
# One step wrapping the entire agent. Durable in the sense that it checkpoints,
# but the boundary is so coarse that a crash anywhere repays everything.
log("do_everything")
time.sleep(3.6)
'''

SELF_TEST_WORK = '''
import json, os, sys, time
LEDGER = os.environ["PROBE_LEDGER"]
STATE = os.environ["SELF_TEST_STATE"]
DURABLE = os.environ["SELF_TEST_DURABLE"] == "1"
def log(step):
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"step": step, "t": time.time()}) + "\\n")
done = set()
if DURABLE and os.path.exists(STATE):
    done = set(json.load(open(STATE)))
def step(name, secs):
    if name in done:
        return
    log(name); time.sleep(secs); done.add(name)
    if DURABLE:
        json.dump(sorted(done), open(STATE, "w"))
step("search", 0.6)
for i in range(4):
    step("summarize-%d" % i, 0.6)
step("publish", 0.6)
'''


def self_test():
    """No API key, no docker. Runs the same probe against a durable stand-in and a
    non-durable one, and requires it to separate them."""
    ok = True
    with tempfile.TemporaryDirectory() as d:
        work = os.path.join(d, "work.py")
        open(work, "w").write(textwrap.dedent(SELF_TEST_WORK))
        cases = [(1, 0, "durable stand-in", SELF_TEST_WORK),
                 (0, 1, "no-durability stand-in", SELF_TEST_WORK),
                 (1, 1, "coarse single-step stand-in", SELF_TEST_COARSE)]
        for durable, want, label, src in cases:
            open(work, "w").write(textwrap.dedent(src))
            state = os.path.join(d, f"state-{durable}.json")
            for f in (state,):
                if os.path.exists(f):
                    os.remove(f)
            env_prefix = (f"SELF_TEST_STATE={state} SELF_TEST_DURABLE={durable} "
                          f"{sys.executable} {work}")
            reset = f"rm -f {state}"
            print(f"\n--- {label} (expect exit {want})")
            sub = os.path.join(d, "ledgers-" + label.split()[0] + str(durable))
            os.makedirs(sub, exist_ok=True)
            b, k, r, i, lost = measure(env_prefix, 1.6, sub, reset)
            got = report(b, k, r, i, lost, 1.6)
            verdict = "PASS" if got == want else "FAIL"
            ok &= got == want
            print(f"  --> {verdict} (exit {got}, wanted {want})")
    print("\nself-test PASS" if ok else "\nself-test FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run")
    p.add_argument("--kill-after", type=float, default=8.0)
    p.add_argument("--ledger-dir", default=None)
    p.add_argument("--reset", default=None,
                   help="shell command run before the baseline and before the killed run; "
                        "required against anything durable (see module docstring)")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if not a.run:
        p.error("--run is required (or use --self-test)")
    d = a.ledger_dir or tempfile.mkdtemp(prefix="probe-")
    os.makedirs(d, exist_ok=True)
    b, k, r, i, lost = measure(a.run, a.kill_after, d, a.reset)
    sys.exit(report(b, k, r, i, lost, a.kill_after))
