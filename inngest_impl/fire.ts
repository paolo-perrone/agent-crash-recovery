/**
 * Send one research/requested event and wait for the run to finish.
 *
 * The probe measures whatever process it starts, so this is the process it starts.
 * The work itself executes inside the Inngest dev server, which is why the probe
 * needs --kill-cmd to kill the SERVER rather than this client. Killing this file
 * proves nothing about durability: see README, "Measuring Inngest".
 */
import { Inngest } from "inngest"

const inngest = new Inngest({ id: "durable-agents-four-ways-client" })
const DEV = process.env.INNGEST_BASE_URL ?? "http://localhost:8288"

async function main() {
  const { ids } = await inngest.send({
    name: "research/requested",
    data: { query: process.argv[2] ?? "kv cache eviction", tenantId: "acme" },
  })
  const eventId = ids[0]
  process.stdout.write(`sent ${eventId}\n`)

  // Poll the dev server until the run leaves Running. Without this the client
  // exits immediately and the probe times its own startup instead of the work.
  for (;;) {
    const r = await fetch(`${DEV}/v1/events/${eventId}/runs`)
    const body = (await r.json()) as { data?: Array<{ status: string }> }
    const run = body.data?.[0]
    if (run && run.status !== "Running" && run.status !== "Queued") {
      process.stdout.write(`run ${run.status}\n`)
      return
    }
    await new Promise((r) => setTimeout(r, 500))
  }
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
