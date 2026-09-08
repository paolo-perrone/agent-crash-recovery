"""The work itself. Identical across all four implementations.

Every expensive call is wrapped in @probe so probe.py can count what actually
re-executed after a crash. That decorator is the measurement, not the durability:
the durability comes from whichever layer calls these functions.

TWO LINES PER CALL, not one (2026-09-07). The old version logged once, on entry,
so a process killed between the log and the API call counted as an execution that
was never billed. This repo's whole claim is "work you bought twice", and an
instrument that cannot tell an attempt from a charge cannot make it. `attempt`
is written before the call, `completed` after it returns, and probe.py counts
only the completed ones.
"""
import json, os, time
from dataclasses import dataclass

# The pid keeps two runs on one machine out of each other's ledger. probe.py sets
# PROBE_LEDGER explicitly, so this default only applies when you run an
# implementation directly.
LEDGER = os.environ.get("PROBE_LEDGER", f"/tmp/probe-ledger-{os.getpid()}.jsonl")


def _write(step, phase):
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"step": step, "phase": phase, "t": time.time()}) + "\n")


def probe(name):
    """Append `attempt` before the call and `completed` after it returns.

    A memoized call reaches neither. A killed call leaves an attempt with no
    completion, which is exactly the partial work a crash throws away."""
    def wrap(fn):
        def inner(*a, **kw):
            _write(name, "attempt")
            out = fn(*a, **kw)
            _write(name, "completed")
            return out
        inner.__name__ = fn.__name__
        return inner
    return wrap


@dataclass
class Page:
    id: str
    url: str
    text: str


@probe("search")
def search(query: str) -> list[Page]:
    """Stand-in for your retrieval. Deterministic so a replay is comparable."""
    time.sleep(2)
    return [Page(id=f"p{i}", url=f"https://example.com/{i}", text=f"page {i} about {query}")
            for i in range(11)]


# PROBE_OFFLINE=1 swaps the model call for a sleep of the same shape. Everything
# that decides durability stays real: real Postgres, real checkpoints, a real
# SIGKILL, a real resume. Only the HTTP call to OpenAI is stubbed, so the repaid
# table can be produced without a key and without spending anything. The dollars
# still need a key; the boundaries do not (2026-09-08).
OFFLINE = os.environ.get("PROBE_OFFLINE") == "1"


def _fake_model(seconds: float, label: str) -> str:
    time.sleep(seconds)
    return f"[offline {label}]"


@probe("summarize")
def summarize(page: Page) -> str:
    """The expensive one. This is the call a crash makes you buy twice."""
    if OFFLINE:
        return _fake_model(1.2, f"summary of {page.id}")
    from openai import OpenAI
    r = OpenAI().chat.completions.create(
        model="gpt-4o-mini", max_tokens=200,
        messages=[{"role": "user", "content": f"Summarize in two sentences:\n\n{page.text}"}],
    )
    return r.choices[0].message.content


@probe("outline")
def outline(summaries: list[str]) -> str:
    if OFFLINE:
        return _fake_model(1.0, "outline")
    from openai import OpenAI
    r = OpenAI().chat.completions.create(
        model="gpt-4o-mini", max_tokens=400,
        messages=[{"role": "user", "content": "Outline a report from:\n\n" + "\n".join(summaries)}],
    )
    return r.choices[0].message.content


@probe("publish")
def publish(summaries: list[str], outline_text: str) -> str:
    time.sleep(1)
    return f"# Report\n\n{outline_text}\n\n" + "\n\n".join(summaries)
