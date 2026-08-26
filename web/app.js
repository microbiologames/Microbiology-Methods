const NOT_MINED_LABEL = "Not yet mined";

const state = {
  axis: "method_category",
  status: "active",
  search: "",
  selected: null, // { organism, category }
  methods: [],
  foodCategories: [], // ISO 16140-2 Annex A's fixed category list, in Annex A order
};

function labelize(s) {
  if (!s) return "Other";
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function statusBadgeClass(status) {
  if (status === "active") return "active";
  if (status === "expired") return "expired";
  return "unknown";
}

async function init() {
  const resp = await fetch("data.json");
  const data = await resp.json();
  state.methods = data.methods;
  state.foodCategories = data.food_categories || [];

  document.getElementById("axis-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-axis]");
    if (!btn) return;
    state.axis = btn.dataset.axis;
    state.selected = null;
    updateToggleUI("axis-toggle", "axis", state.axis);
    render();
  });

  document.getElementById("status-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-status]");
    if (!btn) return;
    state.status = btn.dataset.status;
    state.selected = null;
    updateToggleUI("status-toggle", "status", state.status);
    render();
  });

  document.getElementById("search-box").addEventListener("input", (e) => {
    state.search = e.target.value.trim().toLowerCase();
    state.selected = null;
    render();
  });

  document.getElementById("detail-close").addEventListener("click", closeDetail);
  document.getElementById("detail-overlay").addEventListener("click", (e) => {
    if (e.target.id === "detail-overlay") closeDetail();
  });

  render();
}

function updateToggleUI(groupId, dataAttr, value) {
  const group = document.getElementById(groupId);
  group.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset[dataAttr] === value);
  });
}

function filteredMethods() {
  return state.methods.filter((m) => {
    if (state.status === "active" && m.status !== "active") return false;
    if (state.search) {
      const hay = `${m.organism} ${m.commercial_name} ${m.manufacturer_name || ""}`.toLowerCase();
      if (!hay.includes(state.search)) return false;
    }
    return true;
  });
}

function categoriesForMethod(m) {
  if (state.axis === "method_category") {
    return [labelize(m.method_category)];
  }
  return m.tested_categories.length ? m.tested_categories : [NOT_MINED_LABEL];
}

function render() {
  const methods = filteredMethods();

  const orgCounts = new Map();
  const catSet = new Set();
  const grid = new Map(); // key: organism|||category -> [methods]

  for (const m of methods) {
    orgCounts.set(m.organism, (orgCounts.get(m.organism) || 0) + 1);
    for (const cat of categoriesForMethod(m)) {
      catSet.add(cat);
      const key = `${m.organism}|||${cat}`;
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(m);
    }
  }

  const organisms = [...orgCounts.keys()].sort((a, b) => orgCounts.get(b) - orgCounts.get(a));
  let categories;
  if (state.axis === "tested_categories") {
    // Fixed column set (ISO 16140-2 Annex A's own 18 categories, in Annex
    // A's order) rather than whatever subset happens to have data today --
    // the whole point of normalizing onto Annex A server-side is a stable,
    // comparable set of columns instead of one that reshuffles as more
    // reports get mined. Categories with zero methods still show, empty --
    // that's a real signal (which Annex A categories are under-tested),
    // not clutter to hide.
    categories = [...state.foodCategories, NOT_MINED_LABEL];
  } else {
    categories = [...catSet].sort((a, b) => a.localeCompare(b));
  }

  renderAxisNote(methods);
  renderHeatmap(organisms, categories, grid);
  renderResults(grid);
}

function renderAxisNote(methods) {
  const note = document.getElementById("axis-note");
  if (state.axis !== "tested_categories") {
    note.hidden = true;
    return;
  }
  const mined = methods.filter((m) => m.has_performance_data).length;
  note.hidden = false;
  note.textContent =
    `Tested food categories come from mined validation-study data, not the certificate's own scope ` +
    `(which is almost always "all food products" once 5+ categories are validated, per ISO 16140-2's ` +
    `Broad Range of Food rule). Only ${mined} of ${methods.length} methods shown have been mined so far -- ` +
    `"${NOT_MINED_LABEL}" means not yet, not "no categories".`;
}

function renderHeatmap(organisms, categories, grid) {
  const el = document.getElementById("heatmap");
  el.style.gridTemplateColumns = `200px repeat(${categories.length}, minmax(90px, 1fr))`;
  el.innerHTML = "";

  const corner = document.createElement("div");
  corner.className = "hm-corner";
  corner.textContent = "Organism";
  el.appendChild(corner);

  for (const cat of categories) {
    const h = document.createElement("div");
    h.className = "hm-colhead";
    h.textContent = cat;
    el.appendChild(h);
  }

  for (const org of organisms) {
    const rh = document.createElement("div");
    rh.className = "hm-rowhead";
    rh.textContent = org;
    el.appendChild(rh);

    for (const cat of categories) {
      const key = `${org}|||${cat}`;
      const items = grid.get(key) || [];
      const cell = document.createElement("div");
      cell.className = "hm-cell" + (items.length === 0 ? " empty" : "");
      cell.textContent = items.length || "";
      if (state.selected && state.selected.organism === org && state.selected.category === cat) {
        cell.classList.add("selected");
      }
      if (items.length) {
        const intensity = Math.min(4, Math.ceil((items.length / 10) * 4) || 1);
        cell.style.background = `var(--heat-${intensity})`;
        cell.addEventListener("click", () => {
          state.selected = { organism: org, category: cat };
          render();
        });
      }
      el.appendChild(cell);
    }
  }
}

function renderResults(grid) {
  const title = document.getElementById("results-title");
  const list = document.getElementById("results-list");
  list.innerHTML = "";

  if (!state.selected) {
    title.textContent = "Click a cell to see matching methods";
    return;
  }

  const key = `${state.selected.organism}|||${state.selected.category}`;
  const items = grid.get(key) || [];
  title.textContent = `${state.selected.organism} — ${state.selected.category} (${items.length})`;

  for (const m of items) {
    const card = document.createElement("div");
    card.className = "result-card";
    card.innerHTML = `
      <div class="result-main">
        <span class="result-name">${escapeHtml(m.commercial_name)}</span>
        <span class="result-meta">${escapeHtml(m.manufacturer_name || "Unknown manufacturer")} &middot; ${escapeHtml(m.source_certificate_number)}</span>
      </div>
      <div>
        <span class="badge source">${escapeHtml(m.source)}</span>
        <span class="badge ${statusBadgeClass(m.status)}">${escapeHtml(m.status)}</span>
      </div>
    `;
    card.addEventListener("click", () => openDetail(m));
    list.appendChild(card);
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function openDetail(m) {
  const r = m.record;
  const content = document.getElementById("detail-content");
  content.innerHTML = `
    <h2>${escapeHtml(r.commercial_name)}</h2>
    <p class="dt-sub">${escapeHtml((r.manufacturer && r.manufacturer.name) || "Unknown manufacturer")}</p>
    <div class="dt-grid">
      ${dtField("Source", `${r.source} &middot; ${escapeHtml(r.source_certificate_number)}`)}
      ${dtField("Status", `${escapeHtml(r.certification.status)}${r.certification.current_expiry ? " (until " + escapeHtml(r.certification.current_expiry) + ")" : ""}`)}
      ${dtField("Organism", escapeHtml((r.target_organism && r.target_organism.normalized) || "Unknown"))}
      ${dtField("Method type", `${labelize(r.method_type.action)} &middot; ${labelize(r.method_type.category)}`)}
      ${dtField("Reference method", escapeHtml((r.reference_method && r.reference_method.standard) || (r.reference_method && r.reference_method.raw) || "—"))}
      ${dtField("Original certification", escapeHtml(r.certification.original_date || "—"))}
    </div>
    <div class="dt-section">
      <h3>Validation scope (certificate)</h3>
      <p>${escapeHtml(r.validation_scope.raw || "—")}</p>
    </div>
    ${renderPerformance(r.performance)}
  `;
  document.getElementById("detail-overlay").hidden = false;
}

function dtField(label, valueHtml) {
  return `<div class="dt-field"><div class="k">${escapeHtml(label)}</div><div class="v">${valueHtml}</div></div>`;
}

function renderPerformance(perf) {
  if (!perf) {
    return `<div class="dt-section"><h3>Performance data</h3><p class="no-data">Not yet mined from the validation-study report.</p></div>`;
  }

  let html = `<div class="dt-section"><h3>Performance data (${escapeHtml(perf.method_nature)})</h3>`;

  if (perf.method_nature === "quantitative" && perf.quantitative) {
    const q = perf.quantitative;
    if (q.relative_trueness_by_category && q.relative_trueness_by_category.length) {
      html += `<table class="dt-table"><thead><tr>
        <th>Category</th><th>n</th><th>Bias (log)</th><th>SD (log)</th><th>95% CI</th>
      </tr></thead><tbody>`;
      for (const c of q.relative_trueness_by_category) {
        html += `<tr><td>${escapeHtml(c.category)}</td><td>${c.n_samples ?? "—"}</td>` +
          `<td>${c.bias_log ?? "—"}</td><td>${c.sd_log ?? "—"}</td>` +
          `<td>${c.lower_limit_95 ?? "?"} , ${c.upper_limit_95 ?? "?"}</td></tr>`;
      }
      html += `</tbody></table>`;
    }
    if (q.accuracy_profile) {
      html += `<p><strong>Acceptability limit:</strong> ±${q.accuracy_profile.acceptability_limit_log ?? "—"} log</p>`;
      if (q.accuracy_profile.by_matrix && q.accuracy_profile.by_matrix.length) {
        html += `<table class="dt-table"><thead><tr><th>Category</th><th>SD rep. (ref)</th><th>SD rep. (alt)</th></tr></thead><tbody>`;
        for (const bm of q.accuracy_profile.by_matrix) {
          html += `<tr><td>${escapeHtml(bm.matrix)}</td><td>${bm.sd_repeatability_reference ?? "—"}</td><td>${bm.sd_repeatability_alternative ?? "—"}</td></tr>`;
        }
        html += `</tbody></table>`;
      }
    }
    html += renderPanel("Inclusivity", q.inclusivity);
    html += renderPanel("Exclusivity", q.exclusivity);
  }

  if (perf.method_nature === "qualitative" && perf.qualitative) {
    const ql = perf.qualitative;
    if (ql.method_comparison_by_category && ql.method_comparison_by_category.length) {
      html += `<table class="dt-table"><thead><tr>
        <th>Category</th><th>Sensitivity (alt)</th><th>Sensitivity (ref)</th><th>RLOD</th>
      </tr></thead><tbody>`;
      for (const c of ql.method_comparison_by_category) {
        html += `<tr><td>${escapeHtml(c.category)}</td><td>${c.sensitivity_alternative_pct ?? "—"}</td>` +
          `<td>${c.sensitivity_reference_pct ?? "—"}</td><td>${c.rlod ?? "—"}</td></tr>`;
      }
      html += `</tbody></table>`;
    }
    html += renderPanel("Inclusivity", ql.inclusivity);
    html += renderPanel("Exclusivity", ql.exclusivity);
  }

  html += `</div>`;
  return html;
}

function renderPanel(title, panel) {
  if (!panel || panel.n_tested == null) return "";
  let html = `<p><strong>${escapeHtml(title)}:</strong> ${panel.n_correctly_detected ?? "?"} / ${panel.n_tested}`;
  if (panel.discrepancies && panel.discrepancies.length) {
    html += ` &mdash; ${panel.discrepancies.length} discrepanc${panel.discrepancies.length === 1 ? "y" : "ies"}`;
  }
  html += `</p>`;
  if (panel.discrepancies && panel.discrepancies.length) {
    html += "<ul>" + panel.discrepancies.map((d) => `<li>${escapeHtml(d.strain)}${d.note ? " — " + escapeHtml(d.note) : ""}</li>`).join("") + "</ul>";
  }
  return html;
}

function closeDetail() {
  document.getElementById("detail-overlay").hidden = true;
}

init();
