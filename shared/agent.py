"""The work itself. Identical across all four implementations.

Every expensive call is wrapped in @probe so probe.py can count what actually
re-executed after a crash. That decorator is the measurement, not the durability:
the durability comes from whichever layer calls these functions.
"""
import json, os, time
from dataclasses import dataclass

LEDGER = os.environ.get("PROBE_LEDGER", "/tmp/probe-ledger.jsonl")


def probe(name):
    """Append one line per ACTUAL execution. A memoized call never reaches this."""
    def wrap(fn):
        def inner(*a, **kw):
            with open(LEDGER, "a") as f:
                f.write(json.dumps({"step": name, "t": time.time()}) + "\n")
            return fn(*a, **kw)
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


@probe("summarize")
def summarize(page: Page) -> str:
    """The expensive one. This is the call a crash makes you buy twice."""
    from openai import OpenAI
    r = OpenAI().chat.completions.create(
        model="gpt-4o-mini", max_tokens=200,
        messages=[{"role": "user", "content": f"Summarize in two sentences:\n\n{page.text}"}],
    )
    return r.choices[0].message.content


@probe("outline")
def outline(summaries: list[str]) -> str:
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
