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
 */
const state = {
  data: null,
  search: "",
  source: new Set(),
  status: new Set(),
  technology: new Set(),
  organism: "all",
  manufacturer: "all",
  lab: "all",
  sortKey: "commercial_name",
  sortDir: 1,
};

const $ = (id) => document.getElementById(id);

function text(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

/* ---------- filtering ---------- */

function matches(entry) {
  if (state.source.size && !state.source.has(entry.source)) return false;
  if (state.status.size && !state.status.has(entry.status)) return false;
  if (state.technology.size && !state.technology.has(entry.method_category)) return false;
  if (state.organism !== "all" && entry.organism !== state.organism) return false;
  if (state.manufacturer !== "all" && entry.manufacturer_name !== state.manufacturer) return false;
  if (state.lab !== "all" && entry.expert_laboratory !== state.lab) return false;

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
  const expiry = new Date(raw);
  const now = new Date();
  const monthsLeft = (expiry - now) / (1000 * 60 * 60 * 24 * 30.44);
  // Expiry is the one field where the number alone hides the point: a
  // certificate lapsing in two months is a different procurement decision
  // from one running three more years.
  let cls = "ok";
  if (Number.isNaN(monthsLeft)) cls = "";
  else if (monthsLeft < 0) cls = "bad";
  else if (monthsLeft < 12) cls = "warn";
  return `<span class="expiry ${cls}">${raw}</span>`;
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
      `<a href="${links.summary_report_url}" target="_blank" rel="noopener" class="doclink${direct ? "" : " indirect"}">` +
      `${direct ? "Study report" : "Registry search"}</a>`
    );
  }
  if (links.certificate_url) {
    parts.push(`<a href="${links.certificate_url}" target="_blank" rel="noopener" class="doclink">Certificate</a>`);
  }
  if (links.source_page_url) {
    parts.push(`<a href="${links.source_page_url}" target="_blank" rel="noopener" class="doclink subtle">Listing</a>`);
  }
  return parts.join(" ");
}

function render() {
  const rows = sorted(state.data.methods.filter(matches));
  const body = $("catalog-body");

  body.innerHTML = rows.map((entry) => `
    <tr>
      <td>
        <span class="method-name">${text(entry.commercial_name)}</span>
        <span class="cert-number">${text(entry.source_certificate_number, "")}</span>
      </td>
      <td>${text(entry.manufacturer_name)}</td>
      <td>${text(entry.organism)}</td>
      <td><span class="tech-tag tech-${entry.method_category}">${text(entry.method_category_label)}</span></td>
      <td>${text(entry.expert_laboratory)}</td>
      <td>${expiryCell(entry)}</td>
      <td class="doc-cell">${linksCell(entry)}</td>
    </tr>
  `).join("");

  $("result-count").textContent =
    `${rows.length} of ${state.data.methods.length} methods`;
  $("empty-note").hidden = rows.length > 0;
  renderActivePills();
}

function renderActivePills() {
  const pills = [];
  const add = (label, clear) => pills.push({ label, clear });

  state.source.forEach((v) => add(v, () => state.source.delete(v)));
  state.status.forEach((v) => add(v, () => state.status.delete(v)));
  state.technology.forEach((v) =>
    add(state.data.category_labels[v] || v, () => state.technology.delete(v)));
  if (state.organism !== "all") add(state.organism, () => { state.organism = "all"; });
  if (state.manufacturer !== "all") add(state.manufacturer, () => { state.manufacturer = "all"; });
  if (state.lab !== "all") add(state.lab, () => { state.lab = "all"; });
  if (state.search) add(`"${state.search}"`, () => { state.search = ""; });

  const container = $("active-pills");
  container.innerHTML = "";
  pills.forEach(({ label, clear }) => {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.innerHTML = `${label} <span aria-hidden="true">&times;</span>`;
    pill.setAttribute("aria-label", `Remove filter ${label}`);
    pill.addEventListener("click", () => { clear(); syncControls(); render(); });
    container.appendChild(pill);
  });
  $("clear-filters").hidden = pills.length === 0;
}

/* ---------- controls ---------- */

function buildChips(containerId, values, stateSet, labelFor = (v) => v) {
  const container = $(containerId);
  container.innerHTML = "";
  values.forEach((value) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.dataset.value = value;
    chip.textContent = labelFor(value);
    chip.addEventListener("click", () => {
      stateSet.has(value) ? stateSet.delete(value) : stateSet.add(value);
      syncControls();
      render();
    });
    container.appendChild(chip);
  });
}

function fillSelect(id, values, placeholder) {
  const select = $(id);
  select.innerHTML = `<option value="all">${placeholder}</option>` +
    values.map((v) => `<option value="${v.replace(/"/g, "&quot;")}">${v}</option>`).join("");
}

function syncControls() {
  document.querySelectorAll("#f-source .chip").forEach((c) =>
    c.classList.toggle("on", state.source.has(c.dataset.value)));
  document.querySelectorAll("#f-status .chip").forEach((c) =>
    c.classList.toggle("on", state.status.has(c.dataset.value)));
  document.querySelectorAll("#f-technology .chip").forEach((c) =>
    c.classList.toggle("on", state.technology.has(c.dataset.value)));
  $("organism-select").value = state.organism;
  $("manufacturer-select").value = state.manufacturer;
  $("lab-select").value = state.lab;
  $("search-box").value = state.search;
  document.querySelectorAll("th.sortable").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sortKey);
    th.dataset.dir = th.dataset.sort === state.sortKey ? (state.sortDir === 1 ? "asc" : "desc") : "";
  });
}

function init(data) {
  state.data = data;
  const facets = data.facets || {};

  buildChips("f-source", facets.sources || [], state.source);
  buildChips("f-status", [...new Set(data.methods.map((m) => m.status))].sort(), state.status);
  buildChips("f-technology", facets.method_categories || [], state.technology,
    (v) => data.category_labels[v] || v);

  fillSelect("organism-select", facets.organisms || [], "All organisms");
  fillSelect("manufacturer-select", facets.manufacturers || [], "All manufacturers");
  fillSelect("lab-select", facets.expert_laboratories || [], "All laboratories");

  // The expert-lab column is populated by a separate pass over the study
  // reports (pipeline/extract_expert_labs.py). Saying so beats letting the
  // filter look broken while that pass hasn't run for these records.
  const labCount = (facets.expert_laboratories || []).length;
  if (labCount === 0) {
    const note = $("lab-note");
    note.textContent = "Not yet extracted from the study reports for any record.";
    note.hidden = false;
    $("lab-select").disabled = true;
  }

  $("search-box").addEventListener("input", (e) => {
    state.search = e.target.value.trim().toLowerCase();
    render();
  });
  $("organism-select").addEventListener("change", (e) => { state.organism = e.target.value; render(); });
  $("manufacturer-select").addEventListener("change", (e) => { state.manufacturer = e.target.value; render(); });
  $("lab-select").addEventListener("change", (e) => { state.lab = e.target.value; render(); });

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
    state.source.clear(); state.status.clear(); state.technology.clear();
    state.organism = "all"; state.manufacturer = "all"; state.lab = "all"; state.search = "";
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
    document.getElementById("catalog-body").innerHTML =
      `<tr><td colspan="7">Could not load data.json: ${err}</td></tr>`;
  });
