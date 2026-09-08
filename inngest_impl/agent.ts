/** TypeScript twin of shared/agent.py. Same four calls, same probe ledger. */
import { appendFileSync } from "fs"
import OpenAI from "openai"

const LEDGER = process.env.PROBE_LEDGER ?? `/tmp/probe-ledger-${process.pid}.jsonl`
// PROBE_OFFLINE=1 swaps the model call for a sleep of the same shape, matching
// shared/agent.py. Durability stays real; only the HTTP call is stubbed, so the
// repaid table can be produced without a key.
const OFFLINE = process.env.PROBE_OFFLINE === "1"
const openai = OFFLINE ? (null as unknown as OpenAI) : new OpenAI()

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

/**
 * `attempt` before the call, `completed` after it resolves. A memoized step reaches
 * neither. Two lines, not one (2026-09-07): the Python twin changed for the same
 * reason, that an attempt killed before the API call is not a charge.
 */
function write(step: string, phase: "attempt" | "completed") {
  appendFileSync(LEDGER, JSON.stringify({ step, phase, t: Date.now() / 1000 }) + "\n")
}

async function probe<T>(name: string, fn: () => Promise<T> | T): Promise<T> {
  write(name, "attempt")
  const out = await fn()
  write(name, "completed")
  return out
}

export type Page = { id: string; url: string; text: string }

export const search = (query: string): Promise<Page[]> =>
  probe("search", async () => {
    await new Promise((r) => setTimeout(r, 2000))
    return Array.from({ length: 11 }, (_, i) => ({
      id: `p${i}`, url: `https://example.com/${i}`, text: `page ${i} about ${query}`,
    }))
  })

export const summarize = (page: Page): Promise<string> =>
  probe("summarize", async () => {
    if (OFFLINE) { await wait(1200); return `[offline summary of ${page.id}]` }
    const r = await openai.chat.completions.create({
      model: "gpt-4o-mini", max_tokens: 200,
      messages: [{ role: "user", content: `Summarize in two sentences:\n\n${page.text}` }],
    })
    return r.choices[0].message.content ?? ""
  })

export const outline = (summaries: string[]): Promise<string> =>
  probe("outline", async () => {
    if (OFFLINE) { await wait(1000); return "[offline outline]" }
    const r = await openai.chat.completions.create({
      model: "gpt-4o-mini", max_tokens: 400,
      messages: [{ role: "user", content: "Outline a report from:\n\n" + summaries.join("\n") }],
    })
    return r.choices[0].message.content ?? ""
  })

export const publish = (summaries: string[], outlineText: string): Promise<string> =>
  probe("publish", async () => `# Report\n\n${outlineText}\n\n${summaries.join("\n\n")}`)
