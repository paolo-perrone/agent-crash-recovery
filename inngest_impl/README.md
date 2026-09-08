# Inngest

Three shells. The dev server, the app that serves the function, and whatever fires
the event.

```bash
npm install inngest
npx inngest-cli@latest dev            # dev server on :8288
npx tsx inngest_impl/serve.ts         # this app, on :3000/api/inngest
npx tsx inngest_impl/fire.ts "durable execution"
```

`serve.ts` did not exist until 2026-09-07 and this file told you to run it, so the
documented path exited on a missing module.

`agent.ts` is the TypeScript twin of `shared/agent.py`, same four calls, same two-phase
probe ledger, so `probe.py` counts executions the same way.

**The probe cannot kill this the way it kills the others.** The function runs inside the
dev server, so `fire.ts` is only a client. See "Measuring Inngest" in the root README
before you read a number off this path.
