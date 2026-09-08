/**
 * The HTTP endpoint Inngest calls back into. This file was referenced by the README
 * and did not exist until 2026-09-07, so the documented Inngest path could not run.
 *
 *   npx inngest-cli@latest dev     # dev server on :8288, in one shell
 *   npx tsx inngest_impl/serve.ts  # this app, in another
 *   npx tsx inngest_impl/fire.ts   # send the event
 */
import { serve } from "inngest/node"
import { createServer } from "http"
import { inngest, research } from "./index"

const handler = serve({ client: inngest, functions: [research] })
const port = Number(process.env.PORT ?? 3000)

createServer(handler).listen(port, () => {
  console.log(`serving research-agent on :${port}/api/inngest`)
  console.log("the dev server discovers it automatically once both are up")
})
