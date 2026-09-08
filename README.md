# The Same Agent on LangGraph, Inngest, DBOS and Temporal

> **Status: written from each vendor's documentation and not yet executed end to end.**
> Reviewed 2026-09-07, which found five paths that could not have run: a missing
> `serve.ts`, an Inngest command that fired nothing, one shared reset used for four state
> models, a Temporal workflow id that made every restart a fresh run, and
> `durability="sync"` passed to `compile()` where no published langgraph version accepts
> it. All five are fixed above. **Still nobody has run the four implementations against
> live services**, and until someone does, treat every path here as documentation.

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

## Start here: read your own agent, no services

`probe.py` answers "what did I pay for twice?" by killing a real run, which costs you four
services, an API key and an afternoon. `boundaries.py` answers it by reading the code you
already have, in a second, with nothing installed:

Install it once and point it at any repo, no clone:

```bash
pipx install git+https://github.com/paolo-perrone/agent-crash-recovery
boundaries path/to/your/agent/
```

or run it from this clone with `python boundaries.py path/to/your/agent/`. It is one
stdlib-only file, so there is no resolver to fight with your own pins.

As a pre-commit hook, which is the moment this is cheapest to fix:

```yaml
- repo: https://github.com/paolo-perrone/agent-crash-recovery
  rev: main
  hooks:
    - id: boundaries
      args: [--cost, "0.0004"]
```

It finds every expensive call your orchestrator reaches and tells you which ones sit
outside a durability boundary. Two verdicts:

**UNPROTECTED**, a model call your workflow reaches with no boundary in between. Every
retry buys it again.
**SHARED**, one boundary holding several expensive calls. A crash inside it repays all of
them, so the boundary is coarser than the bill.

It knows eight frameworks: LangGraph nodes, DBOS steps, Temporal Activities, Celery tasks,
Prefect tasks, Hatchet steps, Airflow tasks and Restate `ctx.run()`, plus Inngest
`step.run()` in TypeScript. Adding one is a data edit in `FRAMEWORKS`, never a code edit.
Three more are named in `CONFIG_ONLY` and deliberately not supported, because AWS Step
Functions, Cloudflare Workflows and Azure Durable Functions put the boundary in
configuration where reading your source proves nothing.

Run it on this repo and it names the one deliberately unwrapped call in
`inngest_impl/index.ts` and clears the LangGraph, DBOS and Temporal implementations, which
is the ground truth CI asserts on every push.

### It makes the edit, or says why it will not

```bash
boundaries my_agent/ --fix           # show the change
boundaries my_agent/ --fix --write   # make it
```

Where a framework declares a boundary with a decorator, `--fix` writes the decorator above
the function and the next run comes back clean. Where it does not, it prints the change and
refuses to make it, with the reason:

```
  edits this refuses to make:

  wf.py:11  run() calls outline() with no boundary
      an @activity.defn decorator is half of it; the workflow must also call it
      through workflow.execute_activity()
```

Temporal, LangGraph, Hatchet, Restate and Inngest all land in that second list, because
each needs a call-site change and a half-correct edit to durability code is worse than a
report. DBOS, Prefect, Celery and Airflow land in the first.

### It gives you the bill, not a count

```bash
python boundaries.py my_agent/ --cost 0.0004 --fanout 11
```

A call inside `for page in pages` is not repaid once, it is repaid once per page, and the
report says so with the name of the collection. `--cost` is the price of one model call and
`--fanout` is how many items you expect in a loop the source cannot size, so both
assumptions are yours and both are printed:

```
  every retry repays 0 fixed calls plus once per item in pages.
  at 11 items per loop and $0.0004 a call, one crash repays 11 calls, $0.0044.
  A thousand crashes: $4.40.
```

It is strictly weaker than the probe: it cannot see runtime behaviour and it cannot price
anything. It is also the one you will actually run before you ship. What it cannot see is
listed in the header of the file, and a call it could not resolve is reported as
UNREADABLE rather than passed over in silence.

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

**Every system needs its own reset**, and `run-demo.sh` carries all four. They are not
interchangeable: LangGraph keeps checkpoints in four Postgres tables, DBOS keeps workflow
state in its own `dbos` schema, Temporal keeps history server-side under a workflow id, and
Inngest keeps run state in the dev server. Handing one of them another's reset leaves the
previous run intact, and the probe then reports a durability finding about its own setup.

```
  # EXAMPLE OUTPUT, NOT A MEASUREMENT. No numbers from a real run are published
  # in this repo yet; see "What is not measured here" below.
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

### Measuring Inngest

Inngest executes the function **inside its dev server**, not in the process you start. The
probe kills the process it launched, so killing the client that fired the event proves
nothing at all. `run-demo.sh inngest` therefore passes `--kill-cmd`, which kills the served
app where the steps actually run:

```bash
npx inngest-cli@latest dev            # dev server on :8288, shell one
npx tsx inngest_impl/serve.ts         # your app, shell two
./run-demo.sh inngest                 # fires the event, kills shell two mid-run
```

This is a weaker test than the other three. Killing the served app leaves the dev server's
own run state intact, so what you are measuring is recovery of an app process rather than
recovery from a lost node. Read the repaid table for Inngest as a floor, not a verdict.

### What is not measured here

`probe.py --self-test` and `test_probe.py` run in CI on every push. The four
implementations do not: they need Postgres, a Temporal server, an Inngest dev server and
an OpenAI key. Everything CI cannot reach is exactly where the 2026-09-07 review found
five broken paths, so treat "CI is green" as a statement about the probe and nothing else. **No numbers from a real run are published in this repo yet.** When they
are, they will name the SDK versions they came from.

## What each one needs

| implementation | extra service | setup |
|---|---|---|
| `langgraph_impl` | Postgres | `docker compose up -d postgres` |
| `dbos_impl` | Postgres | same |
| `inngest_impl` | Inngest dev server + the served app | `npx inngest-cli@latest dev`, then `npx tsx inngest_impl/serve.ts` |
| `temporal_impl` | Temporal dev server | `temporal server start-dev` |
