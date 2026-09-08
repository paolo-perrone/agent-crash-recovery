"""Start one workflow. Run worker.py in another shell first.

The id is STABLE by default (2026-09-07). It used to default to a fresh uuid4 on
every invocation, so the probe's restart run began a brand new workflow instead of
resuming the killed one, re-executed all thirteen calls, and reported Temporal as
repaying the entire run. Resetting between the baseline and the killed run is the
reset command's job, not the id's: see run-demo.sh."""
import argparse, asyncio, os
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporal_impl.worker import Research


async def run(query: str, wf_id: str):
    """Start the workflow, or ATTACH to the one already on the server.

    A killed worker leaves the workflow Running. Re-submitting the same id then
    raises WorkflowAlreadyStartedError, the client dies, and the worker that would
    have resumed it never gets the chance. The resume is not a new submission: a
    worker comes back, Temporal replays the history, and the client waits on the
    handle for the result. Found on the first real run, 2026-09-08, where the
    restart logged zero executions and the failure looked like a durability
    finding."""
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    handle = client.get_workflow_handle(wf_id)
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:
            raise
        desc = None
    if desc is not None and desc.status == WorkflowExecutionStatus.RUNNING:
        return await handle.result()
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
