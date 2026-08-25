# The Same Agent on LangGraph, Inngest, DBOS and Temporal

> **Status: written from each vendor's documentation and not yet executed end to end.**
> The API calls are verified against current docs. If a signature has drifted, open an
> issue and it gets fixed the same week.

One research agent, implemented on four durable execution layers, so you can run the one
you picked, kill it mid-run, and see which steps you pay for twice.

The agent is the same in all four: search a query, summarize each page it finds, build an
outline, publish. Forty-ish steps on a real corpus. The only thing that changes between
implementations is **where the step boundaries go**, which is the whole argument.

Companion to [Inngest vs Temporal vs DBOS vs LangGraph](https://theaiengineer.substack.com/)
in The AI Engineer.

## The rule every implementation follows

**One boundary per expensive call.** Anything outside a boundary is bought again on every
retry. `shared/agent.py` marks each expensive call with `@probe`, so the harness can tell
you which ones actually survived.

## Run one

```bash
cp .env.example .env          # add your keys
docker compose up -d postgres # DBOS and LangGraph need it

pip install -r requirements.txt
python -m langgraph_impl.main --query "how do durable execution engines differ"
```

Swap `langgraph_impl` for `dbos_impl`, `inngest_impl` or `temporal_impl`. Each has its own
README section below with the extra service it needs.

## Prove it survives

Start here. It needs no API key, no Postgres and no docker:

```bash
python probe.py --self-test
```

Three stand-in agents, one durable, one with no durability, one wrapping everything in a
single step. The probe has to separate all three or the self-test fails. That last case is
the one worth understanding, because it is what this repo got wrong until 2026-08-21.

Then against a real implementation:

```bash
python probe.py --run "python -m langgraph_impl.main --query x" --kill-after 8 \
                --reset "docker compose exec -T postgres psql -U postgres -c 'truncate checkpoints'"
```

`--reset` is required against anything durable. The probe runs the work three times: once
clean to learn what it costs, once killed mid-flight, once restarted. Without a reset
between the first and second, the clean run leaves a finished checkpoint behind and the
killed run resumes from it instantly.

```
  step            once  killed  restart  repaid
  search             1       1        0      +0
  summarize          11      4        7      +0
  outline            1       0        1      +0
  publish            1       0        1      +0
```

`repaid = (killed + restart) - once`. Anything above zero is work you bought twice.

**The floor is one repaid step.** Whatever was in flight when the kill landed had already
started and not yet checkpointed, so even a perfect system pays for it again. Exit 0 means
the repaid set is exactly that one step. Exit 1 means something else was repaid too, or
the boundary is so coarse the verdict stops meaning anything.

That last clause is a real guard, not a caveat. Wrap the whole agent in one step and the
kill lands inside it by definition, so "only the in-flight step was repaid" becomes
trivially true for a system with no durability at all. The probe now refuses that: one
step total, or one step swallowing more than half the pre-crash time, exits 1 and says
COARSE.

Run it three times with different `--kill-after` values. A step that re-executes at eight
seconds and not at twelve is a boundary too coarse to have saved yet.

### What is not measured here

`probe.py --self-test` and `test_probe.py` run in CI on every push. The four
implementations do not: they need Postgres, a Temporal server, an Inngest dev server and
an OpenAI key. **No numbers from a real run are published in this repo yet.** When they
are, they will name the SDK versions they came from.

## What each one needs

| implementation | extra service | setup |
|---|---|---|
| `langgraph_impl` | Postgres | `docker compose up -d postgres` |
| `dbos_impl` | Postgres | same |
| `inngest_impl` | Inngest dev server | `npx inngest-cli@latest dev` |
| `temporal_impl` | Temporal dev server | `temporal server start-dev` |
