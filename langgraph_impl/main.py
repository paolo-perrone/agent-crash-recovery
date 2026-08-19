"""LangGraph. Durability is a checkpointer you pass to compile().

The setting that matters is durability="sync". The default is "async", which
persists in the background while the next node runs, so a hard crash can lose
the newest checkpoint. On a forty-step agent that is the summary you were
mid-way through paying for.

One node per expensive call. Put four calls in one node and a crash costs all four.
"""
import argparse, os
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres import PostgresSaver
from shared.agent import search, summarize, outline, publish, Page


class State(TypedDict):
    query: str
    pages: list
    summaries: list
    outline: str
    report: str


def search_node(s: State) -> dict:
    return {"pages": search(s["query"])}


def summarize_node(s: State) -> dict:
    # one page per invocation keeps the boundary at one model call
    done = s.get("summaries", [])
    return {"summaries": done + [summarize(s["pages"][len(done)])]}


def more_pages(s: State) -> str:
    return "summarize" if len(s.get("summaries", [])) < len(s["pages"]) else "outline"


def outline_node(s: State) -> dict:
    return {"outline": outline(s["summaries"])}


def publish_node(s: State) -> dict:
    return {"report": publish(s["summaries"], s["outline"])}


def build(checkpointer):
    g = StateGraph(State)
    g.add_node("search", search_node)
    g.add_node("summarize", summarize_node)
    g.add_node("outline", outline_node)
    g.add_node("publish", publish_node)
    g.add_edge(START, "search")
    g.add_conditional_edges("search", more_pages, {"summarize": "summarize", "outline": "outline"})
    g.add_conditional_edges("summarize", more_pages, {"summarize": "summarize", "outline": "outline"})
    g.add_edge("outline", "publish")
    g.add_edge("publish", END)
    return g.compile(checkpointer=checkpointer, durability="sync")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--query", required=True)
    p.add_argument("--thread", default="run-1", help="reuse the same value to resume")
    a = p.parse_args()

    with PostgresSaver.from_conn_string(os.environ["DATABASE_URL"]) as cp:
        cp.setup()
        out = build(cp).invoke({"query": a.query, "summaries": []},
                               {"configurable": {"thread_id": a.thread}})
    print(out["report"][:400])


if __name__ == "__main__":
    main()
