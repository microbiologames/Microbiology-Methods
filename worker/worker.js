/**
 * Chat proxy for the validated-methods catalogue — single file, no build step.
 *
 * PASTE THIS WHOLE FILE into the Cloudflare dashboard editor. See
 * worker/README.md for the click-by-click steps.
 *
 * This is deliberately plain `fetch` rather than the Anthropic SDK: the
 * dashboard's editor cannot install npm packages, and a dependency-free file
 * is the price of skipping the toolchain entirely. worker/src/index.ts is the
 * same logic written against the SDK, for whenever a real build step is worth
 * having.
 *
 * What it does: turns a question into a FILTER, which web/chat.js applies to
 * the browser's own copy of data.json.
 *
 * Why a proxy: the frontend is a static GitHub Pages site, so an API key in
 * its JavaScript would be readable — and spendable — by every visitor. The
 * key lives here and never reaches the browser.
 *
 * Why a filter and not an answer: the model is sent the catalogue's
 * VOCABULARY (organism names, manufacturers, laboratories) and never its
 * VALUES. It cannot state that a method's sensitivity is 96.7% because it has
 * never been shown a performance figure — it names a filter, and the page
 * renders the matching rows itself. For a tool microbiologists use to choose
 * a validated method, that makes a misquoted result impossible rather than
 * merely unlikely. It also costs ~600 tokens per question instead of the
 * ~18,400 the catalogue itself would take.
 */

const MODEL = "claude-haiku-4-5";
const MAX_TOKENS = 1024;          // a filter is a handful of short strings
const MAX_QUESTION_CHARS = 500;   // caps what one request can cost
const RATE_LIMIT_PER_HOUR = 30;   // per IP, only when a KV namespace is bound
const FACETS_TTL_MS = 15 * 60 * 1000;

/** Cached per isolate: the vocabulary changes only when the data is rebuilt. */
let facetsCache = null;

async function loadFacets(env) {
  const now = Date.now();
  if (facetsCache && now - facetsCache.fetchedAt < FACETS_TTL_MS) {
    return facetsCache.value;
  }
  const resp = await fetch(env.FACETS_URL, { cf: { cacheTtl: 900 } });
  if (!resp.ok) throw new Error(`facets fetch failed: ${resp.status}`);
  const body = await resp.json();
  facetsCache = { value: body.facets, fetchedAt: now };
  return body.facets;
}

/**
 * Build the tool schema from the live vocabulary, so every enum holds exactly
 * the values in the data today. A newly scraped manufacturer becomes
 * filterable as soon as the data rebuilds — no edit here, and no way for the
 * model to name something that does not exist.
 *
 * `strict: true` is what enforces that. Note it must NOT be paired with a
 * nullable type array — that combination returns a 400 from the schema
 * validator, which is how it broke every record of this project's first
 * llm_report_miner calibration run. Hence empty arrays and empty strings as
 * the "no filter on this field" signal, never null.
 */
function buildTool(facets) {
  const enumArray = (values, description) => ({
    type: "array",
    items: { type: "string", enum: values },
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
        technologies: enumArray(facets.method_categories, "Detection technologies to include."),
        expert_laboratories: enumArray(
          facets.expert_laboratories,
          "Laboratories that ran the validation study.",
        ),
        sources: enumArray(facets.sources, "Certification schemes to include."),
        statuses: enumArray(["active", "expired", "unknown"], "Certificate status to include."),
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
        "organisms", "manufacturers", "technologies", "expert_laboratories",
        "sources", "statuses", "expires_before", "expires_after", "text",
        "answer", "understood",
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

function corsHeaders(origin, env) {
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((o) => o.trim());
  const ok = origin && allowed.includes(origin);
  return {
    "Access-Control-Allow-Origin": ok ? origin : allowed[0] || "",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

async function rateLimited(request, env) {
  if (!env.RATE_LIMIT) return false; // namespace not bound — see README
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const hour = new Date().toISOString().slice(0, 13); // YYYY-MM-DDTHH
  const key = `rl:${ip}:${hour}`;
  const used = parseInt((await env.RATE_LIMIT.get(key)) || "0", 10);
  if (used >= RATE_LIMIT_PER_HOUR) return true;
  // 2h TTL so a bucket outlives its own hour without needing cleanup.
  await env.RATE_LIMIT.put(key, String(used + 1), { expirationTtl: 7200 });
  return false;
}

function json(body, status, headers) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export default {
  async fetch(request, env) {
    const cors = corsHeaders(request.headers.get("Origin"), env);

    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "POST") {
      return json({ error: "POST a JSON body: {question}" }, 405, cors);
    }

    let question;
    try {
      ({ question } = await request.json());
    } catch {
      return json({ error: "Body must be JSON." }, 400, cors);
    }
    if (typeof question !== "string" || !question.trim()) {
      return json({ error: "Missing 'question'." }, 400, cors);
    }
    if (question.length > MAX_QUESTION_CHARS) {
      return json({ error: `Question too long (max ${MAX_QUESTION_CHARS} characters).` }, 400, cors);
    }
    if (await rateLimited(request, env)) {
      return json({ error: "Rate limit reached — try again in a little while." }, 429, cors);
    }

    try {
      const facets = await loadFacets(env);
      const today = new Date().toISOString().slice(0, 10);

      const apiResp = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          model: MODEL,
          max_tokens: MAX_TOKENS,
          system: SYSTEM_PROMPT,
          tools: [buildTool(facets)],
          // Forced: the only useful outcome here is a filter, and
          // understood=false is how the model declines within the schema
          // rather than by writing prose we would then have to parse.
          tool_choice: { type: "tool", name: "filter_catalogue" },
          messages: [
            { role: "user", content: `Today is ${today}.\n\nQuestion: ${question}` },
          ],
        }),
      });

      if (!apiResp.ok) {
        const detail = await apiResp.text();
        console.error("anthropic error", apiResp.status, detail);
        return json(
          { error: apiResp.status === 429 ? "Upstream rate limit." : "Upstream error." },
          apiResp.status === 429 ? 429 : 502,
          cors,
        );
      }

      const body = await apiResp.json();
      const toolUse = (body.content || []).find((b) => b.type === "tool_use");
      if (!toolUse) {
        return json({ error: "No filter returned; please rephrase." }, 502, cors);
      }
      return json({ filter: toolUse.input, usage: body.usage }, 200, cors);
    } catch (err) {
      console.error("chat proxy error", err);
      return json({ error: "Upstream error." }, 502, cors);
    }
  },
};
