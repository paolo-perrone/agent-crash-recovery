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

export const research = inngest.createFunction(
  {
    id: "research-agent",
    // flow control is configuration: five in-flight runs per tenant, no scheduler to write
    concurrency: { key: "event.data.tenantId", limit: 5 },
  },
  { event: "research/requested" },
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
