# Chat proxy (Cloudflare Worker)

Turns a plain-language question about the catalogue into a **filter** that
`web/chat.js` applies to the browser's own copy of `data.json`.

## Why it exists

The frontend is a static GitHub Pages site. An Anthropic API key placed in its
JavaScript would be readable — and spendable — by every visitor. The key lives
here as a Worker secret and never reaches the browser.

## Why it returns a filter instead of an answer

The model is sent the catalogue's **vocabulary** (organism names,
manufacturers, laboratories) and never its **values**. It cannot state that a
method's sensitivity is 96.7% because it has not been shown a single
performance figure — it can only name a filter, and the page renders the
matching rows itself.

That matters for this project specifically: microbiologists use these numbers
to choose a validated method, and a chatbot that paraphrases a sensitivity and
misplaces a digit would be worse than no chatbot. The design makes that class
of error impossible rather than unlikely.

It is also what makes it cheap — roughly **600 tokens per question** instead of
the ~18,400 the catalogue itself would cost, or ~225,000 for the full
`data.json`. On Haiku 4.5 that is on the order of a dollar per thousand
questions.

## Deploy

```bash
cd worker
npm install

# 1. Store the key (prompts for the value; never commit it)
npx wrangler secret put ANTHROPIC_API_KEY

# 2. Rate limiting — strongly recommended, see the warning below
npx wrangler kv namespace create RATE_LIMIT
#    paste the returned id into wrangler.toml and uncomment the block

# 3. Ship it
npx wrangler deploy
```

Then put the deployed URL into `CHAT_ENDPOINT` at the top of `web/chat.js` and
rebuild/redeploy the site. **While `CHAT_ENDPOINT` is empty the panel removes
itself**, so the catalogue works exactly as before until you are ready.

## Read this before deploying

**CORS is not access control.** `ALLOWED_ORIGINS` stops another *website* from
using your endpoint from a browser. It does nothing about `curl` — anyone who
learns the URL can call it directly and spend your credit. The protections in
place are:

| Guard | What it covers |
|---|---|
| `MAX_QUESTION_CHARS` (500) | Caps what one request can cost |
| `MAX_TOKENS` (1024) | Caps the response side |
| KV rate limit (30/IP/hour) | The only real cap on volume — **inactive until you create the namespace** |

If you skip step 2, the endpoint is effectively uncapped. Given how this
project's API budget went the first time, do step 2.

Cloudflare's free plan covers 100,000 Worker requests/day and the KV
operations this uses; the Anthropic API calls are billed to your key as usual.

## Keeping it in sync

The Worker reads `facets.json` (~3 KB), published next to the site by
`pipeline/build_frontend_data.py`, and caches it for 15 minutes. The tool
schema is built from it at request time, so a newly scraped manufacturer or
laboratory becomes filterable as soon as the data rebuilds — no code change
here, and no way for the model to name something that isn't in the data.

## Local development

```bash
npx wrangler dev          # needs the secret set, or a .dev.vars file
npm run typecheck         # tsc --noEmit
```
