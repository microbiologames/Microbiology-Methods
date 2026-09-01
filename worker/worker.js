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
  if (!resp.ok) {
    // Named distinctly so the operator sees "the vocabulary URL is wrong"
    // rather than a generic upstream failure — they are different fixes.
    throw new Error(`FACETS_${resp.status}`);
  }
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

/**
 * Keep only what the filter can legitimately contain, and drop the rest.
 *
 * `strict: true` guarantees the SHAPE of the tool input, never its MEANING:
 * a date field declared as a string is satisfied by any string at all. A real
 * response from the deployed proxy put markup fragments
 * (`</antml incogn> <parameter name="text">`) into expires_before, expires_after
 * and text, which -- since filters combine with AND -- drove a correct
 * organism+technology match down to zero rows.
 *
 * So the worker checks the values against the same vocabulary it handed the
 * model, the way the deterministic aggregate-row filter backstops the mining
 * prompt elsewhere in this project. Anything unrecognised is dropped and
 * logged rather than passed to the page, because a filter nobody asked for is
 * indistinguishable from "no results" once it reaches the table.
 */
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function sanitizeFilter(raw, facets) {
  const dropped = [];

  const pickEnum = (value, allowed, field) => {
    if (!Array.isArray(value)) return [];
    const set = new Set(allowed);
    return value.filter((v) => {
      if (set.has(v)) return true;
      dropped.push(`${field}=${JSON.stringify(v)}`);
      return false;
    });
  };

  const pickDate = (value, field) => {
    if (typeof value !== "string" || value === "") return "";
    if (ISO_DATE_RE.test(value)) return value;
    dropped.push(`${field}=${JSON.stringify(value.slice(0, 40))}`);
    return "";
  };

  // Free text is the most dangerous field: it ANDs against everything and the
  // model is tempted to restate the question in it. Markup, and anything long
  // enough to be a sentence rather than a product name, is not a search term.
  const pickText = (value) => {
    if (typeof value !== "string") return "";
    const t = value.trim();
    if (!t) return "";
    if (t.length > 60 || /[<>{}]/.test(t)) {
      dropped.push(`text=${JSON.stringify(t.slice(0, 40))}`);
      return "";
    }
    return t;
  };

  const clean = {
    organisms: pickEnum(raw.organisms, facets.organisms, "organism"),
    manufacturers: pickEnum(raw.manufacturers, facets.manufacturers, "manufacturer"),
    technologies: pickEnum(raw.technologies, facets.method_categories, "technology"),
    expert_laboratories: pickEnum(raw.expert_laboratories, facets.expert_laboratories, "lab"),
    sources: pickEnum(raw.sources, facets.sources, "source"),
    statuses: pickEnum(raw.statuses, ["active", "expired", "unknown"], "status"),
    expires_before: pickDate(raw.expires_before, "expires_before"),
    expires_after: pickDate(raw.expires_after, "expires_after"),
    text: pickText(raw.text),
    answer: typeof raw.answer === "string" ? raw.answer.slice(0, 300) : "",
    understood: raw.understood !== false,
  };

  if (dropped.length) console.error("dropped invalid filter values:", dropped.join(", "));
  return { clean, dropped };
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

Every field is required, so a field you are not filtering on must be exactly \
an empty array [] or an empty string "" — never a placeholder, never a \
restatement of the question, never any markup. In particular \`text\` is a \
short product name to search for, not a description of your answer: if the \
organism and technology fields already express the question, leave \`text\` \
empty. Dates are strictly YYYY-MM-DD.

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

      // An identity-linked API key must say which workspace it acts in, or
      // the API answers 400 "anthropic-workspace-id is required". Ordinary
      // keys don't need it, so the header is only sent when the variable is
      // set — leaving it unset keeps this working with either kind of key.
      const apiHeaders = {
        "x-api-key": env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      };
      if (env.ANTHROPIC_WORKSPACE_ID) {
        apiHeaders["anthropic-workspace-id"] = env.ANTHROPIC_WORKSPACE_ID;
      }

      const apiResp = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: apiHeaders,
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
        // Surface the API's own error type and message. Neither contains the
        // key; both are exactly what tells the operator whether this is an
        // exhausted balance, a bad key, or a malformed request. Capped, and
        // the request body is never echoed.
        let type = "", message = "";
        try {
          const parsed = JSON.parse(detail);
          type = parsed?.error?.type || "";
          message = String(parsed?.error?.message || "").slice(0, 300);
        } catch {
          message = detail.slice(0, 200);
        }
        return json(
          {
            error: apiResp.status === 429 ? "Upstream rate limit." : "Anthropic API rejected the request.",
            upstream_status: apiResp.status,
            upstream_type: type,
            upstream_message: message,
          },
          apiResp.status === 429 ? 429 : 502,
          cors,
        );
      }

      const body = await apiResp.json();
      const toolUse = (body.content || []).find((b) => b.type === "tool_use");
      if (!toolUse) {
        return json({ error: "No filter returned; please rephrase." }, 502, cors);
      }
      const { clean, dropped } = sanitizeFilter(toolUse.input || {}, facets);
      return json(
        { filter: clean, dropped_values: dropped, usage: body.usage },
        200, cors,
      );
    } catch (err) {
      console.error("chat proxy error", err);
      const msg = String(err && err.message);
      if (msg.startsWith("FACETS_")) {
        return json(
          {
            error: "Could not load the catalogue vocabulary.",
            detail: `FACETS_URL returned ${msg.slice(7)} — check that variable on the Worker.`,
          },
          502, cors,
        );
      }
      return json({ error: "Worker error.", detail: msg.slice(0, 200) }, 502, cors);
    }
  },
};
