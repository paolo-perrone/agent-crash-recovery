#!/usr/bin/env python3
"""Assertions on probe.py's verdict, both directions.

Every case here is one the pre-2026-08-21 probe got wrong. The point of the file
is that removing a guard from report() turns it red, so run it after any edit:

    python test_probe.py
"""
import collections, io, sys
from contextlib import redirect_stdout
import probe

PASS, FAIL = 0, 1
results = []


def verdict(baseline, killed, restart, interrupted, lost=0.4, working=1.6):
    """report() with stdout swallowed. Returns the exit code.

    `working` is the time the killed run spent EXECUTING, first ledger row to kill.
    It replaced kill_after on 2026-09-07: charging SDK imports and a Postgres
    connect to the in-flight step tripped COARSE on correct systems."""
    with redirect_stdout(io.StringIO()):
        return probe.report(collections.Counter(baseline), collections.Counter(killed),
                            collections.Counter(restart), interrupted, lost, working)


def case(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}  (exit {got}, wanted {want})")


REAL = {"search": 1, "summarize": 11, "outline": 1, "publish": 1}

# --- the two bugs that hid each other -------------------------------------
# 1. summarize runs 11x in ONE clean run. The old `> 1` test called that repaid.
case("perfect resume, killed mid-summarize, is not flagged",
     verdict(REAL, {"search": 1, "summarize": 4}, {"summarize": 7, "outline": 1, "publish": 1},
             "summarize"), PASS)
case("perfect resume, killed during outline, is still not flagged",
     verdict(REAL, {"search": 1, "summarize": 11, "outline": 1},
             {"outline": 1, "publish": 1}, "outline"), PASS)

# 2. one giant step scored clean under the old exclusion. This is the hole.
case("coarse single-step run with zero durability is caught",
     verdict({"do_everything": 1}, {"do_everything": 1}, {"do_everything": 1},
             "do_everything"), FAIL)
case("multi-step run where one step swallowed most of the pre-crash time is caught",
     verdict(REAL, {"search": 1, "summarize": 1}, {"summarize": 10, "outline": 1, "publish": 1},
             "summarize", lost=1.4, working=1.6), FAIL)

# --- ordinary detection ---------------------------------------------------
case("no durability at all is caught",
     verdict(REAL, {"search": 1, "summarize": 3},
             {"search": 1, "summarize": 11, "outline": 1, "publish": 1}, "summarize"), FAIL)
case("one uncheckpointed step outside the boundary is caught",
     verdict(REAL, {"search": 1, "summarize": 4},
             {"search": 1, "summarize": 7, "outline": 1, "publish": 1}, "summarize"), FAIL)
case("the in-flight step alone is the floor, not a failure",
     verdict(REAL, {"search": 1, "summarize": 4}, {"summarize": 8, "outline": 1, "publish": 1},
             "summarize"), PASS)
case("a kill landing exactly between steps repays nothing",
     verdict(REAL, {"search": 1, "summarize": 4}, {"summarize": 7, "outline": 1, "publish": 1},
             "search"), PASS)

# --- the ledger counts charges, not attempts (2026-09-07) ----------------
def ledger(rows):
    """read() over a temp ledger written in the two-phase format."""
    import json, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for step, phase, t in rows:
            f.write(json.dumps({"step": step, "phase": phase, "t": t}) + "\n")
        path = f.name
    return probe.read(path)

done, rows = ledger([("search", "attempt", 1.0), ("search", "completed", 1.6),
                     ("summarize", "attempt", 1.7)])
case("an attempt with no completion is not counted as an execution",
     0 if done == collections.Counter({"search": 1}) else 1, 0)
case("the unfinished attempt is identified as the in-flight step",
     0 if probe.unfinished(rows) == ("summarize", 1.7) else 1, 0)

done_old, _ = ledger([("search", "completed", 1.0)])
case("a ledger with no phase field still reads as completed",
     0 if done_old == collections.Counter({"search": 1}) else 1, 0)

_, rows_clean = ledger([("search", "attempt", 1.0), ("search", "completed", 1.6)])
case("a run with nothing in flight reports no interrupted step",
     0 if probe.unfinished(rows_clean) == (None, None) else 1, 0)

# --- lost is measured from the in-flight step, not from process start ----
case("startup time is not charged to the in-flight step",
     verdict(REAL, {"search": 1, "summarize": 4}, {"summarize": 7, "outline": 1, "publish": 1},
             "summarize", lost=0.4, working=1.6), PASS)

# --- the guard actually guards -------------------------------------------
# Removing the COARSE single-step check must turn the third case green again.
_real_report = probe.report
try:
    src = open(probe.__file__).read()
    case("the single-step COARSE guard is present in the source",
         0 if "len(baseline) == 1" in src else 1, 0)
    case("--kill-cmd exists for out-of-process systems",
         0 if "--kill-cmd" in src else 1, 0)
finally:
    probe.report = _real_report

print(f"\n{sum(results)}/{len(results)} assertions passed")
sys.exit(0 if all(results) else 1)
