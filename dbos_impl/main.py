"""DBOS. Durability is a decorator, and the log lands in your own Postgres.

Every @DBOS.step() output is checkpointed into the same database your product
uses, in the same transaction as the rows that step wrote. That is the one thing
none of the other three offer.
"""
import argparse, os
from dbos import DBOS, DBOSConfig
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
    a = p.parse_args()
    DBOS(config=DBOSConfig(name="durable-agents-four-ways",
                           system_database_url=os.environ["DATABASE_URL"]))
    DBOS.launch()
    print(research(a.query)[:400])


if __name__ == "__main__":
    main()
