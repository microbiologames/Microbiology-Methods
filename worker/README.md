# Chat proxy (Cloudflare Worker)

Turns a plain-language question about the catalogue into a **filter** that
`web/chat.js` applies to the browser's own copy of `data.json`.

Two files, same logic — deploy whichever suits you:

| File | For |
|---|---|
| **`worker.js`** | The Cloudflare dashboard editor. One file, no dependencies, nothing to install. **Start here.** |
| `src/index.ts` | The `wrangler` CLI, written against the Anthropic SDK. Needs Node.js and a build step. |

## Why it exists

The frontend is a static GitHub Pages site. An Anthropic API key placed in its
JavaScript would be readable — and spendable — by every visitor. The key lives
in the Worker and never reaches the browser.

## Why it returns a filter instead of an answer

The model is sent the catalogue's **vocabulary** (organism names,
manufacturers, laboratories) and never its **values**. It cannot state that a
method's sensitivity is 96.7% because it has not been shown a single
performance figure — it names a filter, and the page renders the matching rows
itself.

That matters here specifically: microbiologists use these numbers to choose a
validated method, and a chatbot that paraphrases a sensitivity and misplaces a
digit would be worse than no chatbot. This makes that class of error
impossible rather than unlikely.

It is also what makes it cheap — a measured **612 input tokens** for a real
question, against the ~18,400 the catalogue itself would cost or ~225,000 for
the full `data.json`. On Haiku 4.5 that is on the order of a dollar per
thousand questions.

---

## Deploying from the dashboard (no terminal)

Cloudflare's own labels shift occasionally; the shape of the flow does not.

**1. Create a free account** at [dash.cloudflare.com](https://dash.cloudflare.com).
No card required for this.

**2. Create the Worker.** In the sidebar: **Compute (Workers)** → **Create** →
start from the Hello World template → give it a name (e.g.
`microbio-methods-chat`) → **Deploy**. You now have a placeholder Worker at a
URL like `https://microbio-methods-chat.<your-subdomain>.workers.dev`.

**3. Paste the code** (or skip to *Deploy from GitHub* below and let Cloudflare pull it). Open the Worker → **Edit code**. Select everything in
the editor, delete it, and paste the entire contents of **`worker.js`** from
this folder. Click **Deploy**.

It will not work yet — it needs its three settings.

**4. Add the settings.** Worker → **Settings** → **Variables and Secrets**:

| Name | Type | Value |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Secret** | your key from console.anthropic.com |
| `FACETS_URL` | Text | `https://microbiologames.github.io/Microbiology-Methods/facets.json` |
| `ALLOWED_ORIGINS` | Text | `https://microbiologames.github.io` |
| `ANTHROPIC_WORKSPACE_ID` | Text | *Only if your key is identity-linked* — e.g. `wrkspc_01...` |

The last row is not optional for every account. An **identity-linked** key
must name the workspace it acts in, and without it the API answers `400
anthropic-workspace-id is required when authenticating with an
identity-linked API key`. If you see that, add the row; if your key is an
ordinary one, leave it out and no such header is sent. The id starts with
`wrkspc_` and appears in the Anthropic console next to the key, or in the URL
when you open the workspace.

Choose **Secret** (not Text) for the key — that is what keeps it out of the
dashboard and the logs. Deploy again after saving.

**5. Add the rate limit.** Sidebar → **Storage & Databases** → **KV** →
**Create a namespace**, name it `RATE_LIMIT`. Then back in the Worker →
**Settings** → **Bindings** → **Add** → **KV namespace**, with variable name
`RATE_LIMIT` pointing at it. Deploy again.

Skipping this leaves the endpoint uncapped — see the warning below.

**6. Wire up the site.** Copy the Worker's URL and put it in `CHAT_ENDPOINT` at
the top of `web/chat.js`, then commit. **While `CHAT_ENDPOINT` is empty the
panel removes itself**, so the catalogue works exactly as it does today until
this last step.

To check it before wiring the site up, the Worker's own **Preview** tab can
send a POST with `{"question": "PCR methods for Listeria"}` — a working
deployment answers with a `filter` object.

---

## Deploy from GitHub (stop copy-pasting)

Cloudflare can watch this repository and redeploy the Worker on every push
that touches `worker/`. One-time setup, then the repo is the only place the
code lives.

**Before connecting, read this** — a Git deploy makes `wrangler.toml`
authoritative, and **plain variables and bindings that exist only in the
dashboard are removed on the next deploy**. Two consequences:

Both are already handled in this repo:

- `[[kv_namespaces]]` is filled in with the real `RATE_LIMIT` id, so the rate
  limit survives the switch. (The id names a store and grants nothing without
  account access, so it is not a secret.)
- `ANTHROPIC_API_KEY` and `ANTHROPIC_WORKSPACE_ID` are dashboard **Secrets**,
  the one class of setting a deploy cannot touch. Neither belongs in this file.

Every variable the code reads is therefore accounted for: `FACETS_URL` and
`ALLOWED_ORIGINS` from `[vars]`, `RATE_LIMIT` from the binding, the two keys
from Secrets.

Then: Worker → **Settings** → **Build** → **Connect** → authorise the
Cloudflare Workers & Pages GitHub App → pick this repository → set the root
directory to `worker` — `wrangler.toml` lives there, not at the repo root, and
without this the deploy command cannot find it. Set build watch paths to
`worker/*` so a weekly data scrape does not redeploy the Worker, and leave
preview builds off: the scrapers open automated PRs that would each trigger
one. Connect it to the **existing** Worker rather than creating a new one, so
the URL, the secrets and the KV binding all carry over.

After the first automatic deploy, check that a question still works on the
site — that confirms the bindings survived.

---

## Read this before deploying

**CORS is not access control.** `ALLOWED_ORIGINS` stops another *website* from
using your endpoint from a browser. It does nothing about `curl` — anyone who
learns the URL can call it directly and spend your credit. The real guards:

| Guard | What it covers |
|---|---|
| `MAX_QUESTION_CHARS` (500) | Caps what one request can cost |
| `MAX_TOKENS` (1024) | Caps the response side |
| KV rate limit (30/IP/hour) | The only real cap on volume — **inactive until step 5** |

Given how this project's first API budget went, do step 5.

Cloudflare's free plan covers 100,000 Worker requests/day and the KV
operations this uses. The Anthropic calls bill to your key as usual.

## Keeping it in sync

The Worker reads `facets.json` (~3 KB), published next to the site by
`pipeline/build_frontend_data.py`, and caches it for 15 minutes. The tool
schema is built from it per request, so a newly scraped manufacturer or
laboratory becomes filterable as soon as the data rebuilds — no edit here, and
no way for the model to name something that isn't in the data.

## The CLI route instead

```bash
cd worker
npm install
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler kv namespace create RATE_LIMIT   # paste the id into wrangler.toml
npx wrangler deploy
npm run typecheck                             # tsc --noEmit
```

This deploys `src/index.ts` (the SDK version) per `wrangler.toml`.
