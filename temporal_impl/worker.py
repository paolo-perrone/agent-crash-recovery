"""Temporal. Every expensive call moves into an Activity.

The workflow function re-runs from the top on every replay, and Temporal compares
each command it issues against the recorded history. So the workflow holds control
flow only: no clock, no random, no HTTP. Anything that costs money goes in an
Activity, whose result is read back from history instead of re-executed.
"""
import asyncio, os
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from shared.agent import search, summarize, outline, publish, Page


@activity.defn
async def search_act(query: str) -> list:
    return [p.__dict__ for p in search(query)]


@activity.defn
async def summarize_act(page: dict) -> str:
    return summarize(Page(**page))


@activity.defn
async def outline_act(summaries: list) -> str:
    return outline(summaries)


@activity.defn
async def publish_act(args: dict) -> str:
    return publish(args["summaries"], args["outline"])


@workflow.defn
class Research:
    @workflow.run
    async def run(self, query: str) -> str:
        t = timedelta(minutes=2)
        pages = await workflow.execute_activity(search_act, query, start_to_close_timeout=t)
        summaries = []
        for p in pages:                      # one Activity per page, one boundary per call
            summaries.append(
                await workflow.execute_activity(summarize_act, p, start_to_close_timeout=t))
        outline_text = await workflow.execute_activity(
            outline_act, summaries, start_to_close_timeout=t)
        return await workflow.execute_activity(
            publish_act, {"summaries": summaries, "outline": outline_text},
            start_to_close_timeout=t)


async def main():
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    async with Worker(client, task_queue="research", workflows=[Research],
                      activities=[search_act, summarize_act, outline_act, publish_act]):
        print("worker running, start a run with temporal_impl/main.py")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
