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
      chatSay(body.error || `Error ${resp.status}.`, "error");
      return;
    }
    const filter = body.filter || {};
    if (filter.understood === false) {
      // A refusal is still useful information — show it, and leave the
      // table exactly as the reader had it rather than clearing their view.
      chatSay(filter.answer || "I can't turn that into a filter.", "error");
      return;
    }
    applyFilter(filter);
    const shown = document.getElementById("result-count").textContent;
    chatSay(`${filter.answer || "Filtered."} (${shown})`, "bot");
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
