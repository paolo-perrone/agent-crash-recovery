"""Start one workflow. Run worker.py in another shell first.

The id is STABLE by default (2026-09-07). It used to default to a fresh uuid4 on
every invocation, so the probe's restart run began a brand new workflow instead of
resuming the killed one, re-executed all thirteen calls, and reported Temporal as
repaying the entire run. Resetting between the baseline and the killed run is the
reset command's job, not the id's: see run-demo.sh."""
import argparse, asyncio, os
from temporalio.client import Client
from temporal_impl.worker import Research


async def run(query: str, wf_id: str):
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    return await client.execute_workflow(
        Research.run, query, id=wf_id, task_queue="research")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--query", required=True)
    p.add_argument("--id", default="research-demo",
                   help="stable so the restart RESUMES; the probe's --reset clears it "
                        "between the baseline and the killed run")
    a = p.parse_args()
    print(asyncio.run(run(a.query, a.id))[:400])


if __name__ == "__main__":
    main()
