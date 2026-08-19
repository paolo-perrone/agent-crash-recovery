# The Same Agent, Four Ways

One research agent, implemented on four durable execution layers, so you can run the one
you picked and see what a crash actually costs you.

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

```bash
python probe.py --run "python -m langgraph_impl.main --query x" --kill-after 8
```

It starts the run, kills it mid-flight, restarts it, and names every step that executed
twice. Exit 0 means nothing expensive ran again. Exit 1 names the boundaries you drew in
the wrong place.

Run it three times with different `--kill-after` values. A step that re-executes at eight
seconds and not at twelve is a boundary too coarse to have saved yet.

## What each one needs

| implementation | extra service | setup |
|---|---|---|
| `langgraph_impl` | Postgres | `docker compose up -d postgres` |
| `dbos_impl` | Postgres | same |
| `inngest_impl` | Inngest dev server | `npx inngest-cli@latest dev` |
| `temporal_impl` | Temporal dev server | `temporal server start-dev` |

## Status

Written from each vendor's own documentation and not yet executed end to end. If a call
signature has drifted, open an issue and it gets fixed the same week.
