"""DBOS. Durability is a decorator, and the log lands in your own Postgres.

Every @DBOS.step() output is checkpointed into the same database your product
uses, in the same transaction as the rows that step wrote. That is the one thing
none of the other three offer.
"""
import argparse, os
from dbos import DBOS, DBOSConfig, SetWorkflowID
from shared.agent import search as _search, summarize as _summarize
from shared.agent import outline as _outline, publish as _publish

step = DBOS.step()
search_s, summarize_s = step(_search), step(_summarize)
outline_s, publish_s = step(_outline), step(_publish)


@DBOS.workflow()
def research(query: str) -> str:
    pages = search_s(query)
    summaries = [summarize_s(p) for p in pages]   # one checkpoint per page
    return publish_s(summaries, outline_s(summaries))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--query", required=True)
    p.add_argument("--id", default="research-demo",
                   help="stable so a restart RESUMES instead of enqueuing a second "
                        "workflow beside the recovered one")
    a = p.parse_args()
    # `database_url`, not `system_database_url` (2026-09-08, found on the first real
    # run). DBOSConfig is a TypedDict, so the wrong key was accepted silently and
    # DBOS fell back to its own default connection, which has no password. The
    # implementation had never reached Postgres at all.
    DBOS(config=DBOSConfig(name="durable-agents-four-ways",
                           database_url=os.environ["DATABASE_URL"]))
    DBOS.launch()
    # A STABLE WORKFLOW ID, or the restart pays twice over (2026-09-08, first real
    # run). Without it DBOS recovered the interrupted workflow AND this call started
    # a fresh one, so the restart logged 23 executions against a 14-execution
    # baseline. With it, the second call is the same workflow and reads its
    # checkpoints back.
    with SetWorkflowID(a.id):
        print(research(a.query)[:400])


if __name__ == "__main__":
    main()
