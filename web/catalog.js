/* Certificate-level catalogue.
 *
 * Deliberately stops short of validation performance data: that lives in
 * index.html, is only as complete as the summary-report mining is, and
 * mixing "what this certificate is" with "how well it performed" made the
 * one view answer neither question cleanly. This page answers the first:
 * which validated methods exist, who makes them, what they target, which
 * laboratory ran the study, and where to read the paperwork.
 *
 * Every identity field rendered here is already canonicalized upstream by
 * pipeline/taxonomy.py, so the filters list one entry per real organism /
 * company / laboratory rather than one per spelling.
 *
 * Every facet is a multi-select chip group carrying its own count, matching
 * the performance explorer's rail. Chips rather than dropdowns because a
 * dropdown can only express one choice and hides the distribution: "how
 * many Salmonella methods are there" is answerable at a glance from a chip
 * and not at all from a collapsed <select>. The long facets (24 organisms,
 * 32 manufacturers) start collapsed to their most-populated entries so the
 * rail stays readable, with the rest one click away.
 */
const COLLAPSED_CHIP_COUNT = 8;

// Same display names the performance explorer uses, so a reader moving
// between the two pages sees "NF-Validation", not "NF-VALIDATION" on one
// and "NF-Validation" on the other.
const SOURCE_LABELS = { "NF-VALIDATION": "NF-Validation", "MICROVAL": "MicroVal" };
const STATUS_LABELS = { active: "Active", expired: "Expired", unknown: "Unknown" };

const state = {
  data: null,
  search: "",
  // Every facet is a Set: empty means "no filter on this facet", not
  // "exclude everything".
  source: new Set(),
  status: new Set(),
  technology: new Set(),
  organism: new Set(),
  manufacturer: new Set(),
  lab: new Set(),
  expanded: new Set(), // facet ids the reader has opened past the first few
  sortKey: "commercial_name",
  sortDir: 1,
};

const $ = (id) => document.getElementById(id);

function text(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- filtering ---------- */

function matches(entry) {
  if (state.source.size && !state.source.has(entry.source)) return false;
  if (state.status.size && !state.status.has(entry.status)) return false;
  if (state.technology.size && !state.technology.has(entry.method_category)) return false;
  if (state.organism.size && !state.organism.has(entry.organism)) return false;
  if (state.manufacturer.size && !state.manufacturer.has(entry.manufacturer_name)) return false;
  if (state.lab.size && !state.lab.has(entry.expert_laboratory)) return false;

  if (state.search) {
    const haystack = [
      entry.commercial_name, entry.manufacturer_name, entry.organism,
      entry.method_category_label, entry.expert_laboratory,
      entry.source_certificate_number, entry.reference_method,
    ].filter(Boolean).join(" ").toLowerCase();
    if (!haystack.includes(state.search)) return false;
  }
  return true;
}

function sorted(entries) {
  const key = state.sortKey;
  return [...entries].sort((a, b) => {
    // Blanks always sort last regardless of direction: an unknown expert
    // lab is not "before A", it's absent, and letting it lead the table
    // when sorting by that column buries the rows that do have one.
    const av = a[key], bv = b[key];
    if (!av && !bv) return 0;
    if (!av) return 1;
    if (!bv) return -1;
    return String(av).localeCompare(String(bv), undefined, { numeric: true }) * state.sortDir;
  });
}

/* ---------- rendering ---------- */

function expiryCell(entry) {
  const raw = entry.current_expiry;
  if (!raw) return `<span class="muted-cell">—</span>`;
  const monthsLeft = (new Date(raw) - new Date()) / (1000 * 60 * 60 * 24 * 30.44);
  // Expiry is the one field where the number alone hides the point: a
  // certificate lapsing in two months is a different procurement decision
  // from one running three more years.
  let cls = "ok";
  if (Number.isNaN(monthsLeft)) cls = "";
  else if (monthsLeft < 0) cls = "bad";
  else if (monthsLeft < 12) cls = "warn";
  return `<span class="expiry ${cls}">${escapeHtml(raw)}</span>`;
}

function linksCell(entry) {
  const links = entry.links || {};
  const parts = [];
  if (links.summary_report_url) {
    // The direct/registry distinction is shown, not hidden: a reader
    // following a fallback link lands on a search page and needs to know
    // that up front rather than wondering where their report went.
    const direct = links.summary_report_is_direct;
    parts.push(
      `<a href="${escapeHtml(links.summary_report_url)}" target="_blank" rel="noopener" ` +
      `class="doclink${direct ? "" : " indirect"}">${direct ? "Study report" : "Registry search"}</a>`
    );
  }
  if (links.certificate_url) {
    parts.push(`<a href="${escapeHtml(links.certificate_url)}" target="_blank" rel="noopener" class="doclink">Certificate</a>`);
  }
  if (links.source_page_url) {
    parts.push(`<a href="${escapeHtml(links.source_page_url)}" target="_blank" rel="noopener" class="doclink subtle">Listing</a>`);
  }
  return parts.join(" ");
}

function render() {
  const rows = sorted(state.data.methods.filter(matches));

  $("catalog-body").innerHTML = rows.map((entry) => `
    <tr>
      <td>
        <span class="method-name">${escapeHtml(text(entry.commercial_name))}</span>
        <span class="cert-number">${escapeHtml(text(entry.source_certificate_number, ""))}</span>
      </td>
      <td>${escapeHtml(text(entry.manufacturer_name))}</td>
      <td>${escapeHtml(text(entry.organism))}</td>
      <td><span class="tech-tag tech-${escapeHtml(entry.method_category)}">${escapeHtml(text(entry.method_category_label))}</span></td>
      <td>${escapeHtml(text(entry.expert_laboratory))}</td>
      <td>${expiryCell(entry)}</td>
      <td class="doc-cell">${linksCell(entry)}</td>
    </tr>
  `).join("");

  $("result-count").textContent = `${rows.length} of ${state.data.methods.length} methods`;
  $("empty-note").hidden = rows.length > 0;
  renderActivePills();
}

function renderActivePills() {
  const pills = [];
  const add = (label, clear) => pills.push({ label, clear });

  state.source.forEach((v) => add(SOURCE_LABELS[v] || v, () => state.source.delete(v)));
  state.status.forEach((v) => add(STATUS_LABELS[v] || v, () => state.status.delete(v)));
  state.technology.forEach((v) =>
    add(state.data.category_labels[v] || v, () => state.technology.delete(v)));
  state.organism.forEach((v) => add(v, () => state.organism.delete(v)));
  state.manufacturer.forEach((v) => add(v, () => state.manufacturer.delete(v)));
  state.lab.forEach((v) => add(v, () => state.lab.delete(v)));
  if (state.search) add(`"${state.search}"`, () => { state.search = ""; });

  const container = $("active-pills");
  container.innerHTML = "";
  pills.forEach(({ label, clear }) => {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.innerHTML = `${escapeHtml(label)} <span aria-hidden="true">&times;</span>`;
    pill.setAttribute("aria-label", `Remove filter ${label}`);
    pill.addEventListener("click", () => { clear(); syncControls(); render(); });
    container.appendChild(pill);
  });
  $("clear-filters").hidden = pills.length === 0;
}

/* ---------- facet chips ---------- */

function countBy(keyFn) {
  const counts = new Map();
  for (const entry of state.data.methods) {
    const key = keyFn(entry);
    if (key === null || key === undefined || key === "") continue;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
}

/* Counts are computed against the full dataset, not the current selection --
 * the same choice the performance explorer makes. It answers "how many
 * methods target Salmonella", which is the question a reader scanning the
 * rail is actually asking. */
function buildChipFacet({ hostId, toggleId, keyFn, stateSet, labelFor = (v) => v, order }) {
  const counts = countBy(keyFn);
  let values = order ? order.filter((v) => counts.has(v)) : [...counts.keys()];
  if (!order) values.sort((a, b) => counts.get(b) - counts.get(a) || String(a).localeCompare(String(b)));

  const host = $(hostId);
  const toggle = $(toggleId);
  const expanded = state.expanded.has(hostId);
  // A selected chip is always rendered even when collapsed, so a filter can
  // never be active-but-invisible.
  const shown = expanded
    ? values
    : values.filter((v, i) => i < COLLAPSED_CHIP_COUNT || stateSet.has(v));

  host.innerHTML = "";
  shown.forEach((value) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (stateSet.has(value) ? " on" : "");
    chip.dataset.value = value;
    chip.innerHTML = `<span>${escapeHtml(labelFor(value))}</span><span class="ct">${counts.get(value)}</span>`;
    chip.setAttribute("aria-pressed", stateSet.has(value) ? "true" : "false");
    chip.addEventListener("click", () => {
      stateSet.has(value) ? stateSet.delete(value) : stateSet.add(value);
      syncControls();
      render();
    });
    host.appendChild(chip);
  });

  const hidden = values.length - shown.length;
  if (values.length > COLLAPSED_CHIP_COUNT) {
    toggle.hidden = false;
    toggle.textContent = expanded ? "Show fewer" : `Show ${hidden} more`;
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  } else {
    toggle.hidden = true;
  }
}

function buildAllFacets() {
  const facets = state.data.facets || {};
  buildChipFacet({
    hostId: "f-source", toggleId: "more-source",
    keyFn: (m) => m.source, stateSet: state.source, order: facets.sources,
    labelFor: (v) => SOURCE_LABELS[v] || v,
  });
  buildChipFacet({
    hostId: "f-status", toggleId: "more-status",
    keyFn: (m) => m.status, stateSet: state.status,
    labelFor: (v) => STATUS_LABELS[v] || v,
  });
  buildChipFacet({
    hostId: "f-technology", toggleId: "more-technology",
    keyFn: (m) => m.method_category, stateSet: state.technology,
    labelFor: (v) => state.data.category_labels[v] || v,
  });
  buildChipFacet({
    hostId: "f-organism", toggleId: "more-organism",
    keyFn: (m) => m.organism, stateSet: state.organism,
  });
  buildChipFacet({
    hostId: "f-manufacturer", toggleId: "more-manufacturer",
    keyFn: (m) => m.manufacturer_name, stateSet: state.manufacturer,
  });
  buildChipFacet({
    hostId: "f-lab", toggleId: "more-lab",
    keyFn: (m) => m.expert_laboratory, stateSet: state.lab,
  });

  // The expert lab is filled by a separate pass over the study reports
  // (pipeline/extract_expert_labs.py). Saying so beats leaving an empty
  // facet that reads as broken.
  const note = $("lab-note");
  const haveLabs = state.data.methods.some((m) => m.expert_laboratory);
  note.hidden = haveLabs;
  if (!haveLabs) note.textContent = "Not yet extracted from the study reports.";
}

function syncControls() {
  buildAllFacets();
  $("search-box").value = state.search;
  document.querySelectorAll("th.sortable").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sortKey);
    th.dataset.dir = th.dataset.sort === state.sortKey ? (state.sortDir === 1 ? "asc" : "desc") : "";
  });
}

/* ---------- init ---------- */

function init(data) {
  state.data = data;

  ["source", "status", "technology", "organism", "manufacturer", "lab"].forEach((name) => {
    const toggle = $(`more-${name}`);
    if (!toggle) return;
    toggle.addEventListener("click", () => {
      const hostId = `f-${name}`;
      state.expanded.has(hostId) ? state.expanded.delete(hostId) : state.expanded.add(hostId);
      syncControls();
    });
  });

  $("search-box").addEventListener("input", (e) => {
    state.search = e.target.value.trim().toLowerCase();
    render();
  });

  document.querySelectorAll("th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sortDir = state.sortKey === key ? -state.sortDir : 1;
      state.sortKey = key;
      syncControls();
      render();
    });
  });

  $("clear-filters").addEventListener("click", () => {
    [state.source, state.status, state.technology,
     state.organism, state.manufacturer, state.lab].forEach((s) => s.clear());
    state.search = "";
    syncControls();
    render();
  });

  syncControls();
  render();
}

fetch("data.json")
  .then((r) => r.json())
  .then(init)
  .catch((err) => {
    $("catalog-body").innerHTML =
      `<tr><td colspan="7">Could not load data.json: ${escapeHtml(err)}</td></tr>`;
  });
