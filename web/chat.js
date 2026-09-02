/* Ask-a-question panel for the catalogue.
 *
 * The model never answers from the data — it returns a FILTER, which this
 * script applies to the facets the page already has. Two consequences worth
 * being deliberate about:
 *
 *   1. Nothing on screen is model-generated except one sentence saying what
 *      it filtered for. Every method name, expiry date and performance figure
 *      still comes from data.json, so the assistant cannot misquote a
 *      validation result — it was never shown one.
 *   2. The filter lands in the same chips and pills as a manual search, so
 *      the reader can see exactly what was applied and correct it by hand.
 *      An assistant that silently changed what you were looking at would be
 *      worse than no assistant.
 *
 * Set CHAT_ENDPOINT to the deployed Worker URL (see worker/README.md). While
 * it is empty the panel stays hidden rather than showing a control that
 * cannot work.
 */
const CHAT_ENDPOINT = "https://microbio-methods-chat.nicolas-nguyenvl.workers.dev/";

const chat = {
  busy: false,
  el: {},
};

function chatSay(text, kind) {
  const log = chat.el.log;
  const line = document.createElement("p");
  line.className = `chat-line chat-${kind}`;
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

/**
 * Describe the result set from the data itself.
 *
 * Everything here is counted from data.json in the browser, so it is exact
 * and costs nothing. That division is the point: the model supplies the
 * sentence, the page supplies every number in it. Asking the model to be
 * more detailed would have meant asking it to characterise data it has never
 * been shown — the one thing this design exists to prevent — and would have
 * cost tokens for a worse answer.
 *
 * Only facts a person choosing a validated method actually acts on: how many,
 * from whom, what lapses soon, and what can be compared on performance.
 */
function summarize(rows) {
  if (!rows.length) return [];

  const facts = [];
  const makers = [...new Set(rows.map((r) => r.manufacturer_name).filter(Boolean))];
  if (makers.length === 1) {
    facts.push(makers[0]);
  } else if (makers.length > 1) {
    const shown = makers.slice(0, 3).join(", ");
    facts.push(`${makers.length} manufacturers (${shown}${makers.length > 3 ? "…" : ""})`);
  }

  // Expiry is the fact this catalogue has that a supplier's own page doesn't,
  // and it changes a purchasing decision.
  const now = new Date();
  const soon = rows.filter((r) => {
    if (!r.current_expiry) return false;
    const months = (new Date(r.current_expiry) - now) / (1000 * 60 * 60 * 24 * 30.44);
    return months >= 0 && months < 12;
  }).length;
  const lapsed = rows.filter((r) => r.status === "expired").length;
  if (soon) facts.push(`${soon} expiring within a year`);
  if (lapsed) facts.push(`${lapsed} already expired`);

  const withPerf = rows.filter((r) => r.has_performance_data).length;
  facts.push(
    withPerf === rows.length
      ? "all with performance data"
      : `${withPerf} with performance data`,
  );

  return facts;
}

/** The model's sentence, then the page's own counted facts beneath it. */
function chatAnswer(sentence, rows) {
  const line = document.createElement("div");
  line.className = "chat-line chat-bot";

  const said = document.createElement("p");
  said.className = "chat-said";
  said.textContent = sentence;
  line.appendChild(said);

  const headline = document.createElement("p");
  headline.className = "chat-count";
  headline.textContent = rows.length === 0
    ? "No method matches — try removing one of the filters above."
    : `${rows.length} method${rows.length > 1 ? "s" : ""}`;
  line.appendChild(headline);

  const facts = summarize(rows);
  if (facts.length) {
    const detail = document.createElement("p");
    detail.className = "chat-facts";
    detail.textContent = facts.join(" · ");
    line.appendChild(detail);
  }

  chat.el.log.appendChild(line);
  chat.el.log.scrollTop = chat.el.log.scrollHeight;
}

/** Apply the model's filter to the page's own filter state. */
function applyFilter(filter) {
  [state.source, state.status, state.technology,
   state.organism, state.manufacturer, state.lab].forEach((s) => s.clear());
  state.search = "";
  state.expiresBefore = "";
  state.expiresAfter = "";

  (filter.organisms || []).forEach((v) => state.organism.add(v));
  (filter.manufacturers || []).forEach((v) => state.manufacturer.add(v));
  (filter.technologies || []).forEach((v) => state.technology.add(v));
  (filter.expert_laboratories || []).forEach((v) => state.lab.add(v));
  (filter.sources || []).forEach((v) => state.source.add(v));
  (filter.statuses || []).forEach((v) => state.status.add(v));
  if (filter.text) state.search = String(filter.text).toLowerCase();
  if (filter.expires_before) state.expiresBefore = filter.expires_before;
  if (filter.expires_after) state.expiresAfter = filter.expires_after;

  syncControls();
  render();
  return state.data.methods.filter(matches);
}

async function ask(question) {
  if (chat.busy || !question.trim()) return;
  chat.busy = true;
  chat.el.input.disabled = true;
  chat.el.send.disabled = true;
  chatSay(question, "you");
  chatSay("…", "pending");
  const pending = chat.el.log.lastElementChild;

  try {
    const resp = await fetch(CHAT_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const body = await resp.json().catch(() => ({}));
    pending.remove();

    if (!resp.ok) {
      // Show the whole diagnosis, not just the headline. The proxy returns
      // the upstream status/type/message precisely so the person running
      // this can tell an exhausted balance from a bad key without opening
      // the Cloudflare logs — dropping them here wasted that entirely.
      const detail = [
        body.upstream_type,
        body.upstream_message || body.detail,
      ].filter(Boolean).join(" — ");
      const status = body.upstream_status ? ` [${body.upstream_status}]` : "";
      chatSay(
        (body.error || `Error ${resp.status}.`) + status + (detail ? `\n${detail}` : ""),
        "error",
      );
      return;
    }
    const filter = body.filter || {};
    if (filter.understood === false) {
      // A refusal is still useful information — show it, and leave the
      // table exactly as the reader had it rather than clearing their view.
      chatSay(filter.answer || "I can't turn that into a filter.", "error");
      return;
    }
    const matched = applyFilter(filter);
    chatAnswer(filter.answer || "Here's what I found.", matched);
  } catch (err) {
    pending.remove();
    chatSay(`Could not reach the assistant: ${err.message}`, "error");
  } finally {
    chat.busy = false;
    chat.el.input.disabled = false;
    chat.el.send.disabled = false;
    chat.el.input.focus();
  }
}

function initChat() {
  const panel = document.getElementById("chat-panel");
  if (!panel) return;
  if (!CHAT_ENDPOINT) {
    panel.remove(); // no endpoint configured: don't show a dead control
    return;
  }
  panel.hidden = false;
  chat.el = {
    log: document.getElementById("chat-log"),
    input: document.getElementById("chat-input"),
    send: document.getElementById("chat-send"),
  };

  const submit = () => {
    const q = chat.el.input.value;
    chat.el.input.value = "";
    ask(q);
  };
  chat.el.send.addEventListener("click", submit);
  chat.el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
  });
  document.querySelectorAll(".chat-example").forEach((b) => {
    b.addEventListener("click", () => ask(b.textContent));
  });
}

document.addEventListener("DOMContentLoaded", initChat);
