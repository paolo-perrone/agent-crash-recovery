# Inngest

```bash
npm install inngest
npx inngest-cli@latest dev            # dev server on :8288
npx tsx serve.ts                      # your app, serving the function
```

Then fire it:

```bash
curl -X POST http://localhost:8288/e/dev \
  -d '{"name":"research/requested","data":{"query":"durable execution","tenantId":"acme"}}'
```

`agent.ts` is the TypeScript twin of `shared/agent.py`, same four calls, same probe
ledger, so `probe.py` counts executions the same way.
