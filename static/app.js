"use strict";

let BOOT = null;            // bootstrap data
let LANE = null;            // last lane info
let quoted = false;

const $ = (id) => document.getElementById(id);

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

// ---------- per-carrier runtime state ----------
const carrierState = {}; // id -> { zone, volume, service? }

async function init() {
  BOOT = await getJSON("/api/bootstrap");

  const whSel = $("wh");
  whSel.innerHTML = "";
  for (const w of BOOT.warehouses) {
    const o = document.createElement("option");
    o.value = w.whid;
    o.textContent = `${w.code} — WH ${w.whid} · ${w.state} (origin ${w.origin_pin})`;
    whSel.appendChild(o);
  }
  if (BOOT.warehouses.length) whSel.value = BOOT.warehouses[0].whid;

  const movSel = $("movement");
  fillMovement(movSel);

  for (const c of BOOT.carriers) {
    carrierState[c.id] = {
      zone: null,
      volume: c.defaults.volume || null,
      service: c.id === "elastic" ? "standard" : null,
    };
  }
  renderCarrierOptions();
  buildBulk();

  $("load").addEventListener("click", run);
  $("rebuild").addEventListener("click", rebuild);
  $("assumptions-toggle").addEventListener("click", toggleAssumptions);
  $("movement").addEventListener("change", () => { if (quoted) requote(); });
  $("rvp").addEventListener("change", () => {
    if (quoted) requote();
    else if (/^\d{6}$/.test(($("pin").value || "").replace(/[^0-9]/g, ""))) run();
  });
  $("weight").addEventListener("change", () => { if (quoted) requote(); });
  $("pin").addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });

  $("bulk-toggle").addEventListener("click", toggleBulk);
  $("bmode").addEventListener("click", onBulkMode);
  $("borient").addEventListener("click", onBulkOrient);
  $("bload").addEventListener("click", runBulk);
  $("bexport").addEventListener("click", exportBulkCsv);
  $("bpins").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) runBulk(); });
  $("bpin").addEventListener("keydown", (e) => { if (e.key === "Enter") runBulk(); });
}

function toggleBulk() {
  const open = $("bulk").classList.toggle("open");
  $("bulk-toggle").setAttribute("aria-expanded", open ? "true" : "false");
}

function smoothScrollTo(el) {
  if (!el) return;
  const start = window.scrollY;
  const top = el.getBoundingClientRect().top + start - 16;
  const dist = top - start;
  if (Math.abs(dist) < 2) return;
  const dur = 280;
  const t0 = performance.now();
  const ease = (t) => 1 - Math.pow(1 - t, 3);
  const step = (now) => {
    const p = Math.min(1, (now - t0) / dur);
    window.scrollTo(0, start + dist * ease(p));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function fillMovement(sel) {
  sel.innerHTML = "";
  for (const m of ["Fwd", "RTO"]) {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = m;
    sel.appendChild(o);
  }
}

function toggleAssumptions() {
  const el = $("assumptions");
  const btn = $("assumptions-toggle");
  const open = el.hidden;
  el.hidden = !open;
  btn.classList.toggle("active", open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  btn.textContent = open ? "Hide notes" : "Notes";
  if (open) smoothScrollTo(el);
}

function renderCarrierOptions() {
  const host = $("volgrid");
  const frag = document.createDocumentFragment();
  const sorted = [...BOOT.carriers].sort((a, b) => a.name.localeCompare(b.name));

  for (const c of sorted) {
    const cell = document.createElement("div");
    cell.className = "volcell";
    const name = document.createElement("div");
    name.className = "volname";
    name.textContent = c.name;
    cell.appendChild(name);

    if (c.id === "elastic") {
      const sv = document.createElement("select");
      sv.className = "inp vol-sel";
      sv.dataset.carrier = "elastic";
      sv.dataset.kind = "service";
      for (const s of ["standard", "sdd", "ndd_regional"]) {
        const o = document.createElement("option");
        o.value = s;
        o.textContent = s === "standard" ? "Auto (Local Rs 37 / Regional Rs 40)" : s === "sdd" ? "SDD Local (Rs 37)" : "NDD Regional (Rs 40)";
        sv.appendChild(o);
      }
      cell.appendChild(sv);
      frag.appendChild(cell);
      continue;
    }

    if (c.id === "shadowfax") {
      const chk = document.createElement("label");
      chk.className = "chk rvp-sel";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.id = "rvp";
      cb.title = "Quote Shadowfax 'RVP with QC' reverse-pickup for this lane";
      chk.appendChild(cb);
      chk.appendChild(document.createTextNode("RVP with QC (reverse pickup)"));
      cell.appendChild(chk);
    } else if (c.volume_options.length) {
      const s = document.createElement("select");
      s.className = "inp vol-sel";
      s.dataset.carrier = c.id;
      s.dataset.kind = "volume";
      for (const v of c.volume_options) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = v;
        s.appendChild(o);
      }
      s.value = carrierState[c.id].volume;
      cell.appendChild(s);
    } else {
      const n = document.createElement("div");
      n.className = "flat";
      n.textContent = "flat rate card — no volume tiers";
      cell.appendChild(n);
    }
    frag.appendChild(cell);
  }
  host.replaceChildren(frag);

  document.querySelectorAll(".vol-sel").forEach((sel) => {
    sel.addEventListener("change", () => {
      const st = carrierState[sel.dataset.carrier];
      if (sel.dataset.kind === "volume") st.volume = sel.value;
      else st.service = sel.value;
      if (quoted) requote();
    });
  });
}

async function run() {
  const whid = parseInt($("wh").value, 10);
  const pin = parseInt(($("pin").value || "").replace(/[^0-9]/g, ""), 10);

  if (!pin) { alert("Enter a destination pincode."); return; }

  // 1) lookup the lane's carrier zones from the Zone Master
  try {
    const res = await getJSON(`/api/zones?whid=${whid}&pin=${pin}`);
    LANE = res;
    for (const c of BOOT.carriers) carrierState[c.id].zone = null;
    if (res.found) {
      for (const c of res.carriers) carrierState[c.id].zone = c.master_zone;
    }
  } catch (e) {
    LANE = { found: false, lane: { whid, pin } };
    for (const c of BOOT.carriers) carrierState[c.id].zone = null;
  }

  // 2) quote on the lane's master zones
  await requote();
  smoothScrollTo($("laneinfo"));
  $("pin").setCustomValidity("");
}

async function requote() {
  const whid = parseInt($("wh").value, 10);
  const pin = parseInt(($("pin").value || "").replace(/[^0-9]/g, ""), 10);
  const weight = parseFloat($("weight").value) || 0;
  const movement = $("movement").value;

  const body = {
    whid, pin, weight_kg: weight, movement,
    rvp: $("rvp").checked,
    zones: {},
    volume: {},
    elastic_service: carrierState.elastic.service || "standard",
  };
  for (const c of BOOT.carriers) if (carrierState[c.id].volume != null) body.volume[c.id] = carrierState[c.id].volume;

  const quote = await getJSON("/api/quote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  renderLane();
  renderQuote(quote);
  quoted = true;
}

function renderLane() {
  const el = $("laneinfo");
  if (!LANE) return;
  if (!LANE.found) {
    el.hidden = false;
    el.innerHTML = `<span class="miss">Lane ${LANE.lane.whid}_${LANE.lane.pin} not found in the Zone Master — carriers show as not served. Enter a valid lane to compare costs.</span>`;
    return;
  }
  const L = LANE.lane;
  el.hidden = false;
  el.innerHTML = `
    <div class="route">
      <div class="rnode">
        <span class="rtag">Origin</span>
        <span class="rlab">${esc(L.wh_code || "WH")} · WH ${L.whid}</span>
        <span class="rsub">${esc(L.wh_state || "")} · ${esc(L.origin_pin)}</span>
      </div>
      <span class="rarrow">&rarr;</span>
      <div class="rnode">
        <span class="rtag">Destination</span>
        <span class="rlab">pin ${esc(L.pin)}</span>
        <span class="rsub">${esc(L.city)}, ${esc(L.state)}</span>
      </div>
    </div>
    <div class="laneid">Lane <b>${esc(L.lane)}</b></div>
  `;
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

/* Split a rate-basis string into themed sections:
   - square-bracket notes become italicised "note" lines
   - semicolon / " + " / dash-suffixed segments become sub lines
   The first segment is the bolded main line. */
function splitBasis(s) {
  const parts = [];
  let txt = String(s || "").replace(/\s+/g, " ").trim();
  const notes = [];
  const re = /\[([^\]]*)\]/g;
  let m;
  while ((m = re.exec(txt))) notes.push(m[1].trim());
  txt = txt.replace(/\[[^\]]*\]/g, " ").replace(/\s+/g, " ").trim();

  txt.split(";").forEach((seg) => {
    seg.split("+").forEach((piece) => {
      piece = piece.trim();
      if (!piece) return;
      const dm = piece.match(/^(.*?)\s+-\s+(\d+(?:\.\d+)?%?\s*.*)$/);
      if (dm) {
        if (dm[1].trim()) parts.push(dm[1].trim());
        parts.push("\u2212 " + dm[2].trim());
      } else {
        parts.push(piece);
      }
    });
  });
  if (parts.length === 0 && notes.length === 0) return [{ text: String(s || ""), kind: "main" }];

  const noteStart = parts.length;
  notes.forEach((n) => parts.push(n));
  return parts.map((t, i) => {
    let kind = "sub";
    if (i === 0) kind = "main";
    else if (i >= noteStart) kind = "note";
    return { text: t, kind };
  });
}

function renderQuote(quote) {
  $("results").hidden = false;
  const tbody = $("tbody");
  const frag = document.createDocumentFragment();

  const winId = quote.cheapest ? quote.cheapest.id : null;
  for (const c of quote.carriers) {
    const tr = document.createElement("tr");
    if (c.id === winId) tr.className = "winner";

    const name = document.createElement("td");
    if (c.id === winId) {
      const nm = document.createElement("span");
      nm.className = "cname";
      nm.textContent = c.name;
      const win = document.createElement("span");
      win.className = "win";
      win.textContent = "★ Lowest";
      name.append(nm, win);
    } else {
      name.textContent = c.name;
    }
    tr.appendChild(name);

    const zone = document.createElement("td");
    if (c.master_zone) {
      const b = document.createElement("span");
      b.className = "zbadge";
      b.textContent = c.master_zone;
      zone.appendChild(b);
    } else if (c.served) {
      zone.innerHTML = '<span class="flat">no lane zone (WH-based rate)</span>';
    } else {
      zone.textContent = "—";
    }
    tr.appendChild(zone);

    const fin = document.createElement("td");
    fin.textContent = c.final_zone || "—";
    tr.appendChild(fin);

    const basis = document.createElement("td");
    const box = document.createElement("div");
    box.className = "ratebasis";
    if (!c.served) {
      box.classList.add("notserved");
      box.textContent = c.rate_basis || "not served";
    } else {
      for (const ln of splitBasis(c.rate_basis)) {
        const d = document.createElement("div");
        d.className = "rb-line" + (ln.kind === "main" ? " rb-main" : ln.kind === "note" ? " rb-note" : " rb-sub");
        d.textContent = ln.text;
        box.appendChild(d);
      }
    }
    if (c.id === "shadowfax" && quote.rvp) {
      const rv = document.createElement("div");
      rv.className = "rb-line rb-rvp";
      if (quote.rvp.served) {
        const rb = String(quote.rvp.rate_basis || "");
        const mCps = rb.match(/CPS Rs ([\d.]+)/);
        const mQc = rb.match(/QC Rs ([\d.]+)/);
        const mCat = rb.match(/: CPS Rs [\d.]+ \(([^)]+)\)/);
        const bits = [];
        if (mCps) bits.push("CPS " + mCps[1] + (mCat ? " (" + mCat[1] + ")" : ""));
        if (mQc) bits.push("QC " + mQc[1]);
        const probe = bits.length ? "  [" + bits.join(" + ") + "]" : "";
        rv.textContent = `Shadowfax RVP with QC (reverse pickup): Rs ${quote.rvp.cost.toFixed(2)} (${quote.rvp.zone})${probe}`;
      } else {
        rv.textContent = "Shadowfax RVP with QC: not quotable for this lane";
      }
      box.appendChild(rv);
    }
    basis.appendChild(box);
    tr.appendChild(basis);

    const cost = document.createElement("td");
    cost.className = "num";
    cost.innerHTML = c.cost != null ? `&#8377;${c.cost.toFixed(2)}` : "—";
    tr.appendChild(cost);

    frag.appendChild(tr);
  }
  tbody.replaceChildren(frag);

  const ch = $("cheapest");
  if (quote.cheapest) {
    ch.classList.remove("empty");
    ch.textContent = `Cheapest: ${quote.cheapest.name} at ${quote.cheapest.cost.toFixed(2)} — Zone: ${quote.cheapest.master_zone || "n/a"}`;
  } else {
    ch.classList.add("empty");
    ch.textContent = "No served carrier for this combination.";
  }
}

async function rebuild() {
  const st = $("buildstat");
  st.textContent = "Rebuilding data from Excel… (can take a minute)";
  try {
    await fetch("/api/rebuild", { method: "POST" });
    st.textContent = "Rebuilt. Reloading…";
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    st.textContent = "Rebuild failed: " + e.message;
  }
}

// ---------- bulk quote ----------
let BULK = null; // last bulk response
const bulkWhSel = new Set(); // warehouse ids toggled on in the chip picker

const CARRIER_SHORT = {
  delhivery: "Delhivery", bluedart: "Bluedart", dtdc: "DTDC", ekart: "Ekart",
  shadowfax: "Shadowfax", amazon: "Amazon", elastic: "Elastic",
};

function buildBulk() {
  const bwh = $("bwh");
  for (const w of BOOT.warehouses) {
    const o = document.createElement("option");
    o.value = w.whid;
    o.textContent = `${w.code} — WH ${w.whid} · ${w.state} (${w.origin_pin})`;
    bwh.appendChild(o);
  }
  if (BOOT.warehouses.length) bwh.value = BOOT.warehouses[0].whid;

  const bwhs = $("bwhs");
  const countEl = $("bwhs-count");
  for (const w of BOOT.warehouses) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "whchip";
    chip.dataset.whid = w.whid;
    chip.setAttribute("aria-pressed", "false");
    chip.title = `${w.code} — origin ${w.origin_pin} · ${w.state}`;
    chip.textContent = `${w.code} · WH ${w.whid}`;
    chip.addEventListener("click", () => {
      const on = chip.classList.toggle("on");
      chip.setAttribute("aria-pressed", on ? "true" : "false");
      if (on) bulkWhSel.add(w.whid);
      else bulkWhSel.delete(w.whid);
      countEl.textContent = `${bulkWhSel.size} selected`;
    });
    bwhs.appendChild(chip);
  }
  const updateCount = () => {
    let n = 0;
    bwhs.querySelectorAll(".whchip.on").forEach((c) => n++);
    countEl.textContent = `${n} selected`;
  };
  updateCount();
  fillMovement($("bmovement"));
}

function onBulkMode(e) {
  const btn = e.target.closest(".mode-card");
  if (!btn) return;
  const mode = btn.dataset.mode;
  $("bmode").querySelectorAll(".mode-card").forEach((b) => {
    const on = b === btn;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  $("bv_pins").hidden = mode !== "pins";
  $("bv_whs").hidden = mode !== "whs";
}

function onBulkOrient(e) {
  const btn = e.target.closest(".seg-btn");
  if (!btn || !BULK) return;
  $("borient").querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
  renderMatrix(btn.dataset.orient);
}

function bulkPayload() {
  const mode = $("bmode").querySelector(".mode-card.active").dataset.mode;
  const body = {
    mode,
    weight_kg: parseFloat($("bweight").value) || 0,
    movement: $("bmovement").value,
    elastic_service: (carrierState.elastic && carrierState.elastic.service) || "standard",
    volume: {},
  };
  for (const c of BOOT.carriers) if (carrierState[c.id].volume != null) body.volume[c.id] = carrierState[c.id].volume;

  if (mode === "pins") {
    body.whid = parseInt($("bwh").value, 10);
    body.pins = ($("bpins").value || "").split(/[\n,;]+/).map((s) => s.trim()).filter(Boolean);
    if (!body.pins.length) { alert("Enter at least one 6-digit pincode."); return null; }
  } else {
    body.pin = ($("bpin").value || "").replace(/[^0-9]/g, "");
    body.whids = [...bulkWhSel];
    if (!/^\d{6}$/.test(body.pin)) { alert("Enter one 6-digit destination pincode."); return null; }
    if (!body.whids.length) { alert("Select at least one warehouse chip."); return null; }
  }
  return body;
}

async function runBulk() {
  const body = bulkPayload();
  if (!body) return;
  const st = $("bstat");
  st.textContent = "Quoting…";
  try {
    BULK = await getJSON("/api/bulk_quote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderBulk();
  } catch (err) {
    st.textContent = "";
    alert("Bulk quote failed: " + err.message);
  }
}

function renderBulk() {
  $("bresults").hidden = false;
  $("bexport").hidden = false;
  $("bstat").textContent = "";
  const warn = $("bskip");
  warn.hidden = !(BULK.warnings && BULK.warnings.length);
  warn.textContent = (BULK.warnings || []).join(" · ");
  buildBulkSummary();
  renderMatrix($("borient").querySelector(".seg-btn.active").dataset.orient);
}

function bulkCoverage() {
  const counts = {};
  let cells = 0;
  for (const l of BULK.lanes) {
    for (const c of l.carriers) {
      if (c.served) {
        counts[c.id] = (counts[c.id] || 0) + 1;
        cells += 1;
      }
    }
  }
  return { counts, cells };
}

function buildBulkSummary() {
  const { counts, cells } = bulkCoverage();
  const carrierIds = BULK.carrier_order;
  const quoted = Object.keys(counts).length;
  let txt = "";
  if (BULK.mode === "pins") {
    const f = BULK.fixed;
    txt += `From ${f.code} · WH ${f.whid} (${f.state}) — `;
  } else {
    const f = BULK.fixed;
    txt += `To pin ${f.pin} — `;
  }
  txt += `${BULK.lanes.length} lanes · ${cells} carrier prices · ${quoted} of ${carrierIds.length} carriers quoted`;
  $("bsummary").textContent = txt;
}

function bulkCell(c) {
  return c && c.served && c.cost != null ? "₹" + c.cost.toFixed(2) : "—";
}

/* Fill one matrix cell: price on top, final zone on a small line below it.
   com: carrier quote object (or null/undefined); lane has the cheapest ref. */
function fillBulkCell(td, com, lane) {
  td.className = "num";
  if (com && com.served && com.cost != null) {
    if (lane.cheapest && lane.cheapest.id === com.id) td.classList.add("bestc");
    const price = document.createElement("div");
    price.className = "bcell-price";
    price.textContent = bulkCell(com);
    td.appendChild(price);
    if (com.final_zone) {
      const zone = document.createElement("div");
      zone.className = "bcell-zone";
      zone.textContent = com.final_zone;
      td.appendChild(zone);
    }
    td.title = carrierTitle(com);
  } else {
    td.textContent = "—";
    td.classList.add("cellna");
    td.title = (com && com.rate_basis) || "no rate";
  }
}

function laneText(l) {
  if (BULK.mode === "pins") {
    const geo = l.city ? `${l.city}${l.state ? ", " + l.state : ""}` : "";
    return `${l.pin_6}${geo ? " · " + geo : ""}${l.found ? "" : " · no zone-master lane"}`;
  }
  const geo = l.wh_state || "";
  return `WH${l.whid}${geo ? " · " + geo : ""} → ${l.pin_6}${l.found ? "" : " · no zone-master lane"}`;
}

function carrierTitle(c) {
  return `${c.rate_basis || ""} — zone ${c.master_zone || "n/a"} / final ${c.final_zone || "n/a"}`;
}

function renderMatrix(orient) {
  const host = $("bwrap");
  const lanes = BULK.lanes;
  const carriers = BULK.carrier_order;
  const { counts } = bulkCoverage();
  const table = document.createElement("table");
  table.className = "bulkmat";

  if (orient === "lanes") {
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    const th0 = document.createElement("th");
    th0.textContent = BULK.mode === "pins" ? "Pincode · City" : "Warehouse → pin";
    th0.style.minWidth = "190px";
    tr.appendChild(th0);
    for (const id of carriers) {
      const th = document.createElement("th");
      th.className = "num";
      th.textContent = CARRIER_SHORT[id] || id;
      tr.appendChild(th);
    }
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const l of lanes) {
      const tr = document.createElement("tr");
      const byId = {};
      for (const c of l.carriers) byId[c.id] = c;
      const td0 = document.createElement("td");
      td0.textContent = laneText(l);
      tr.appendChild(td0);
      for (const id of carriers) {
        const c = byId[id];
        const td = document.createElement("td");
        fillBulkCell(td, c, l);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);

    const tfoot = document.createElement("tfoot");
    const ftr = document.createElement("tr");
    const fc0 = document.createElement("td");
    fc0.className = "cov-label";
    fc0.textContent = "Coverage";
    fc0.title = "Lanes served by each carrier";
    ftr.appendChild(fc0);
    for (const id of carriers) {
      const n = counts[id] || 0;
      const td = document.createElement("td");
      td.className = "num cov";
      td.textContent = BULK.mode === "pins" ? `${n}/${lanes.length} pins` : `${n}/${lanes.length} WHs`;
      td.title = `${CARRIER_SHORT[id] || id} quoted on ${n} of ${lanes.length} lanes`;
      ftr.appendChild(td);
    }
    tfoot.appendChild(ftr);
    table.appendChild(tfoot);
  } else {
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    const th0 = document.createElement("th");
    th0.textContent = "Carrier";
    tr.appendChild(th0);
    for (const l of lanes) {
      const th = document.createElement("th");
      th.className = "num";
      th.textContent = BULK.mode === "pins" ? l.pin_6 : "WH" + l.whid;
      th.title = laneText(l);
      tr.appendChild(th);
    }
    const thCov = document.createElement("th");
    thCov.className = "num covth";
    thCov.textContent = "Coverage";
    thCov.title = "Lanes served by each carrier";
    tr.appendChild(thCov);
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const id of carriers) {
      const tr = document.createElement("tr");
      const td0 = document.createElement("td");
      td0.className = "cname";
      td0.textContent = CARRIER_SHORT[id] || id;
      tr.appendChild(td0);
      for (const l of lanes) {
        const c = l.carriers.find((x) => x.id === id);
        const td = document.createElement("td");
        fillBulkCell(td, c, l);
        tr.appendChild(td);
      }
      const tc = document.createElement("td");
      tc.className = "num cov";
      const n = counts[id] || 0;
      tc.textContent = `${n}/${lanes.length}`;
      tc.title = `${CARRIER_SHORT[id] || id} quoted on ${n} of ${lanes.length} lanes`;
      tr.appendChild(tc);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
  }
  host.replaceChildren(table);
}

function exportBulkCsv() {
  if (!BULK) return;
  const orient = $("borient").querySelector(".seg-btn.active").dataset.orient;
  const lanes = BULK.lanes;
  const carriers = BULK.carrier_order;
  const rows = [];
  if (orient === "lanes") {
    rows.push(["whid", "pin", "city", "state", "in_zone_master", ...carriers.map((id) => CARRIER_SHORT[id] || id)]);
    for (const l of lanes) {
      const byId = {};
      for (const c of l.carriers) byId[c.id] = c;
      rows.push([
        l.whid, l.pin_6, l.city || "", l.state || "", l.found ? "yes" : "no",
        ...carriers.map((id) => {
          const c = byId[id];
          return c && c.served && c.cost != null ? c.cost.toFixed(2) : "";
        }),
      ]);
    }
  } else {
    rows.push(["carrier", ...lanes.map((l) => (BULK.mode === "pins" ? l.pin_6 : "WH" + l.whid))]);
    for (const id of carriers) {
      rows.push([
        CARRIER_SHORT[id] || id,
        ...lanes.map((l) => {
          const c = l.carriers.find((x) => x.id === id);
          return c && c.served && c.cost != null ? c.cost.toFixed(2) : "";
        }),
      ]);
    }
  }
  const csv = rows.map((r) => r.map((v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`).join(",")).join("\r\n");
  const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "bulk_costs.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

init();