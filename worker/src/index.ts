/**
 * Cloudflare Worker: turns a natural-language question about the validated-
 * methods catalogue into a filter the browser applies to its own copy of
 * data.json.
 *
 * Why a proxy at all: the frontend is a static GitHub Pages site, so an API
 * key placed in its JavaScript would be readable — and spendable — by every
 * visitor. The key lives here as a Worker secret and never reaches the
 * browser.
 *
 * Why a filter instead of an answer: the model is given the catalogue's
 * VOCABULARY (organism names, manufacturers, laboratories) but never its
 * VALUES. It cannot state a sensitivity of 96.7% because it is never shown
 * one — it can only name a filter, and the page renders the matching rows
 * from data.json. For a tool microbiologists use to choose a validated
 * method, a model that paraphrases a performance figure and misplaces a
 * digit would be worse than no chatbot at all. This design makes that class
 * of error structurally impossible rather than merely unlikely.
 *
 * It is also what makes the thing cheap: ~600 tokens per question instead of
 * the ~18,400 the catalogue itself would cost, and ~225,000 for the full
 * data.json.
 */
import Anthropic from "@anthropic-ai/sdk";

// Haiku 4.5 at the project owner's request. The task is closed-vocabulary
// classification, which is what the small model is for; the schema does the
// work that a larger model's judgement would otherwise have to.
const MODEL = "claude-haiku-4-5";

// A filter is a handful of short strings. 1024 is generous for it and caps
// what a single request can cost.
const MAX_TOKENS = 1024;

// Refuse absurd input before it reaches the API — the cost of a request is
// driven by what a caller can put in it.
const MAX_QUESTION_CHARS = 500;

// Per-IP allowance when a KV namespace is bound (see wrangler.toml). Without
// KV the Worker still runs, but nothing stops a determined caller from
// looping on the endpoint — see README.
const RATE_LIMIT_PER_HOUR = 30;

interface Env {
  ANTHROPIC_API_KEY: string;
  /** Required only for an identity-linked key — see the client below. */
  ANTHROPIC_WORKSPACE_ID?: string;
  FACETS_URL: string;
  ALLOWED_ORIGINS: string;
  RATE_LIMIT?: KVNamespace;
}

interface Facets {
  organisms: string[];
  manufacturers: string[];
  expert_laboratories: string[];
  method_categories: string[];
  sources: string[];
}

/** Cached per isolate: the vocabulary changes only when the data is rebuilt. */
let facetsCache: { value: Facets; fetchedAt: number } | null = null;
const FACETS_TTL_MS = 15 * 60 * 1000;

async function loadFacets(env: Env): Promise<Facets> {
  const now = Date.now();
  if (facetsCache && now - facetsCache.fetchedAt < FACETS_TTL_MS) {
    return facetsCache.value;
  }
  const resp = await fetch(env.FACETS_URL, { cf: { cacheTtl: 900 } });
  if (!resp.ok) throw new Error(`facets fetch failed: ${resp.status}`);
  const body = (await resp.json()) as { facets: Facets };
  facetsCache = { value: body.facets, fetchedAt: now };
  return body.facets;
}

/**
 * Build the tool schema from the live vocabulary, so every enum contains
 * exactly the values present in the data today. A new manufacturer becomes
 * filterable the moment the data rebuilds — no code change, and no way for
 * the model to name something that isn't there.
 *
 * Note on `strict: true`: an enum may NOT be paired with a nullable type
 * array — that combination returns a 400 from the schema validator. (Learned
 * the hard way on this project: it broke every record of the first real
 * calibration run of scrapers/llm_report_miner.py.) Hence empty arrays and
 * empty strings as the "no filter on this field" signal, never null.
 */
function buildTool(facets: Facets): Anthropic.Beta.BetaTool {
  const enumArray = (values: string[], description: string) => ({
    type: "array" as const,
    items: { type: "string" as const, enum: values },
    description,
  });

  return {
    name: "filter_catalogue",
    description:
      "Filter the validated-methods catalogue to the rows that answer the " +
      "user's question. Leave a field as an empty array (or empty string) to " +
      "not filter on it. Combining fields narrows the result.",
    strict: true,
    input_schema: {
      type: "object",
      properties: {
        organisms: enumArray(facets.organisms, "Target organisms to include."),
        manufacturers: enumArray(facets.manufacturers, "Manufacturers to include."),
        technologies: enumArray(
          facets.method_categories,
          "Detection technologies to include.",
        ),
        expert_laboratories: enumArray(
          facets.expert_laboratories,
          "Laboratories that ran the validation study.",
        ),
        sources: enumArray(facets.sources, "Certification schemes to include."),
        statuses: enumArray(
          ["active", "expired", "unknown"],
          "Certificate status to include.",
        ),
        expires_before: {
          type: "string",
          description:
            "Only certificates expiring strictly before this date (YYYY-MM-DD). " +
            "Empty string for no upper bound.",
        },
        expires_after: {
          type: "string",
          description:
            "Only certificates expiring on or after this date (YYYY-MM-DD). " +
            "Empty string for no lower bound.",
        },
        text: {
          type: "string",
          description:
            "Free-text term matched against method name, certificate number and " +
            "reference standard. Use for product names the other fields cannot " +
            "express. Empty string for none.",
        },
        answer: {
          type: "string",
          description:
            "One short sentence, in the SAME LANGUAGE as the question, saying " +
            "what you filtered for. Never state a performance figure, a count, " +
            "or any specific data value — you have not been shown the data, " +
            "only the vocabulary. The page displays the matching rows itself.",
        },
        understood: {
          type: "boolean",
          description:
            "false if the question cannot be expressed as a filter over this " +
            "catalogue (e.g. it asks for a performance number, or is unrelated). " +
            "Say so in `answer` and leave the filter fields empty.",
        },
      },
      required: [
        "organisms",
        "manufacturers",
        "technologies",
        "expert_laboratories",
        "sources",
        "statuses",
        "expires_before",
        "expires_after",
        "text",
        "answer",
        "understood",
      ],
      additionalProperties: false,
    },
  };
}

const SYSTEM_PROMPT = `You turn questions about a catalogue of ISO 16140-2 \
validated food-microbiology methods into a filter, by calling \
filter_catalogue exactly once.

You are given the catalogue's vocabulary, not its contents. You do not know \
any method's performance figures, expiry dates or counts, and you must never \
state one — the page shows the matching rows itself.

Today's date is provided; use it for relative questions ("expiring next \
year", "still valid").

Map the question onto the closest vocabulary values. "PCR" and "molecular" \
both mean the molecular_pcr technology; a genus and a species are different \
targets, so "Listeria" alone means Listeria spp., not Listeria \
monocytogenes. If the question names something outside the vocabulary \
entirely, put it in \`text\` rather than forcing a wrong enum value.

If the question asks for something a filter cannot express — a performance \
value, a comparison, anything not about which methods exist — set \
understood=false and explain briefly in \`answer\`.`;

function corsHeaders(origin: string | null, env: Env): Record<string, string> {
  const allowed = env.ALLOWED_ORIGINS.split(",").map((o) => o.trim());
  const ok = origin && allowed.includes(origin);
  return {
    "Access-Control-Allow-Origin": ok ? origin : allowed[0] ?? "",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

async function rateLimited(request: Request, env: Env): Promise<boolean> {
  if (!env.RATE_LIMIT) return false; // not configured: see README
  const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
  const hour = new Date().toISOString().slice(0, 13); // YYYY-MM-DDTHH
  const key = `rl:${ip}:${hour}`;
  const used = parseInt((await env.RATE_LIMIT.get(key)) ?? "0", 10);
  if (used >= RATE_LIMIT_PER_HOUR) return true;
  // 2h TTL so the bucket outlives its own hour without needing cleanup.
  await env.RATE_LIMIT.put(key, String(used + 1), { expirationTtl: 7200 });
  return false;
}

function json(body: unknown, status: number, headers: Record<string, string>) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const origin = request.headers.get("Origin");
    const cors = corsHeaders(origin, env);

    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "POST") {
      return json({ error: "POST a JSON body: {question}" }, 405, cors);
    }

    let question: unknown;
    try {
      ({ question } = (await request.json()) as { question?: unknown });
    } catch {
      return json({ error: "Body must be JSON." }, 400, cors);
    }
    if (typeof question !== "string" || !question.trim()) {
      return json({ error: "Missing 'question'." }, 400, cors);
    }
    if (question.length > MAX_QUESTION_CHARS) {
      return json(
        { error: `Question too long (max ${MAX_QUESTION_CHARS} characters).` },
        400,
        cors,
      );
    }

    if (await rateLimited(request, env)) {
      return json(
        { error: "Rate limit reached — try again in a little while." },
        429,
        cors,
      );
    }

    try {
      const facets = await loadFacets(env);
      // An identity-linked API key must say which workspace it acts in, or
      // the API answers 400 "anthropic-workspace-id is required". Ordinary
      // keys don't need it, so the header only goes out when the variable is
      // set — that keeps this working with either kind of key.
      const client = new Anthropic({
        apiKey: env.ANTHROPIC_API_KEY,
        defaultHeaders: env.ANTHROPIC_WORKSPACE_ID
          ? { "anthropic-workspace-id": env.ANTHROPIC_WORKSPACE_ID }
          : undefined,
      });
      const today = new Date().toISOString().slice(0, 10);

      // client.beta.messages: in SDK 0.71 `strict` lives on the beta tool
      // type only (verified against the installed type definitions, not
      // assumed) -- and strict is the whole point here, since it is what
      // guarantees the filter validates against the enums above.
      const response = await client.beta.messages.create({
        model: MODEL,
        max_tokens: MAX_TOKENS,
        system: SYSTEM_PROMPT,
        tools: [buildTool(facets)],
        // Forced: the only useful outcome of this call is a filter, and
        // `understood: false` is how the model declines within the schema
        // rather than by writing prose we would then have to parse.
        tool_choice: { type: "tool", name: "filter_catalogue" },
        messages: [
          { role: "user", content: `Today is ${today}.\n\nQuestion: ${question}` },
        ],
      });

      const toolUse = response.content.find(
        (b): b is Anthropic.Beta.BetaToolUseBlock => b.type === "tool_use",
      );
      if (!toolUse) {
        return json({ error: "No filter returned; please rephrase." }, 502, cors);
      }

      return json(
        { filter: toolUse.input, usage: response.usage },
        200,
        cors,
      );
    } catch (err) {
      // Surface the shape of the failure without leaking key or prompt.
      const status =
        err instanceof Anthropic.APIError && typeof err.status === "number"
          ? err.status
          : 500;
      console.error("chat proxy error", err);
      return json(
        { error: status === 429 ? "Upstream rate limit." : "Upstream error." },
        status === 429 ? 429 : 502,
        cors,
      );
    }
  },
};
