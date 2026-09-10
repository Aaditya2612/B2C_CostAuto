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
  const movs = ["Fwd", "RTO", "DTO"];
  movSel.innerHTML = "";
  for (const m of movs) {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = m;
    movSel.appendChild(o);
  }

  for (const c of BOOT.carriers) {
    carrierState[c.id] = {
      zone: null,
      volume: c.defaults.volume || null,
      service: c.id === "elastic" ? "standard" : null,
    };
  }
  renderCarrierOptions();

  $("load").addEventListener("click", run);
  $("rebuild").addEventListener("click", rebuild);
  $("assumptions-toggle").addEventListener("click", toggleAssumptions);
  $("movement").addEventListener("change", () => { if (quoted) requote(); });
  $("rvp").addEventListener("change", () => { if (quoted) requote(); });
  $("weight").addEventListener("change", () => { if (quoted) requote(); });
  $("pin").addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
}

function toggleAssumptions() {
  const el = $("assumptions");
  const btn = $("assumptions-toggle");
  const open = el.hidden;
  el.hidden = !open;
  btn.classList.toggle("active", open);
  btn.textContent = open ? "Hide assumptions" : "Assumptions & notes";
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
      for (const s of ["standard", "ndd_regional"]) {
        const o = document.createElement("option");
        o.value = s;
        o.textContent = s === "standard" ? "Standard (33/32/31; SDD 37 for WH 2/4/10/12/28)" : "NDD Regional (Rs 40)";
        sv.appendChild(o);
      }
      cell.appendChild(sv);
      frag.appendChild(cell);
      continue;
    }

    if (c.volume_options.length) {
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
    if (c.served) {
      for (const ln of splitBasis(c.rate_basis)) {
        const d = document.createElement("div");
        d.className = "rb-line" + (ln.kind === "main" ? " rb-main" : ln.kind === "note" ? " rb-note" : " rb-sub");
        d.textContent = ln.text;
        box.appendChild(d);
      }
    } else {
      box.classList.add("notserved");
      box.textContent = c.rate_basis || "not served";
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
  renderRvp(quote);
}

function renderRvp(quote) {
  const host = $("rvpquote");
  if (!quote.rvp || !quote.rvp.zone) { host.hidden = true; host.innerHTML = ""; return; }
  const r = quote.rvp;
  host.hidden = false;
  host.innerHTML = `
    <div class="rvphead">Shadowfax <b>RVP with QC</b> (reverse pickup &mdash; W.E.F 01-04-2025)</div>
    <div class="rvinf">
      <span class="zbadge">${esc(r.zone)}</span>
      <span class="${r.served ? "rvok" : "rvmiss"}">${r.served ? "&hearts; " + r.cost.toFixed(2) : "not quotable for this lane"}</span>
    </div>
    <div class="ratebasis${r.served ? "" : " notserved"}">${esc(r.rate_basis)}</div>
  `;
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

init();