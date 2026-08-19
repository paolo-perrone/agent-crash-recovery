"""Start one workflow. Run worker.py in another shell first."""
import argparse, asyncio, os, uuid
from temporalio.client import Client
from temporal_impl.worker import Research


async def run(query: str, wf_id: str):
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    return await client.execute_workflow(
        Research.run, query, id=wf_id, task_queue="research")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--query", required=True)
    p.add_argument("--id", default=None, help="reuse to resume the same workflow")
    a = p.parse_args()
    print(asyncio.run(run(a.query, a.id or f"research-{uuid.uuid4()}"))[:400])


if __name__ == "__main__":
    main()
