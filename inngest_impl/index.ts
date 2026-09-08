/**
 * Inngest. Durability is step.run(), and the function is re-invoked once per step.
 *
 * Inngest calls this function, it runs to the first step, that step executes, and
 * the function is interrupted. On the next call the SDK hands back stored results
 * for steps that already ran. So the body runs once per step, and ANYTHING OUTSIDE
 * A STEP RUNS EVERY SINGLE TIME. `llm.outline()` below is deliberately unwrapped so
 * the probe can show you what that costs.
 */
import { Inngest } from "inngest"
import { search, summarize, outline, publish } from "./agent"

export const inngest = new Inngest({ id: "durable-agents-four-ways" })

// inngest 4.x takes the trigger INSIDE the first argument. The three-argument form
// below was the 3.x API and throws at import time on 4.x, which is where the
// documented serve command died on 2026-09-08.
export const research = inngest.createFunction(
  {
    id: "research-agent",
    triggers: [{ event: "research/requested" }],
    // flow control is configuration: five in-flight runs per tenant, no scheduler to write
    concurrency: { key: "event.data.tenantId", limit: 5 },
  },
  async ({ event, step }) => {
    const pages = await step.run("search", () => search(event.data.query))

    const summaries = await Promise.all(
      pages.map((p) => step.run(`summarize-${p.id}`, () => summarize(p)))
    )

    // NOT wrapped, on purpose. Runs on every invocation, billed every time.
    const outlineText = await outline(summaries)

    return step.run("publish", () => publish(summaries, outlineText))
  }
)
