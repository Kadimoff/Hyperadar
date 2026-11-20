"use strict";
/* Hype Radar dashboard — talks to the engine over REST + SSE. No build step. */

const PLATFORMS = [
  { id: "twitch", label: "Twitch", color: "var(--twitch)" },
  { id: "kick", label: "Kick", color: "var(--kick)" },
  { id: "youtube", label: "YouTube", color: "var(--youtube)" },
];
const KIND_LABEL = { clip: "Klip", video: "Video", chat_spike: "Chat partlayışı" };
const SIGNAL_LABEL = { size: "İzləyici sayı", growth: "Artım, 10 dəq", chat: "Chat sürəti", clips: "Klip aktivliyi" };

const state = {
  tags: [], tag: null, tab: "live", showRejected: false, hiddenCount: 0,
  platforms: new Set(PLATFORMS.map(p => p.id)), lang: "", minQ: 0, q: "",
  items: [], liveCount: 0, momentCount: 0, clipCount: 0, updated: null,
  selected: null, selectedItem: null, status: null, settings: null, active: {}, progress: [],
};

/* ---------- helpers ---------- */
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = u => (/^https?:\/\//i.test(u || "") ? u : "");
function compact(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "K";
  return String(n);
}
function duration(sec) {
  if (sec == null) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h} saat ${m} dəq` : `${m} dəq`;
}
function clock(sec) {
  if (!sec) return "";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return sec >= 3600 ? `${Math.floor(sec / 3600)}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function ago(iso) {
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  if (!t) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)} san əvvəl`;
  if (s < 3600) return `${Math.round(s / 60)} dəq əvvəl`;
  if (s < 86400) return `${Math.round(s / 3600)} saat əvvəl`;
  return `${Math.round(s / 86400)} gün əvvəl`;
}
function embedFor(item) {
  const e = item && item.embed;
  if (!e) return null;
  return e.replace("parent=localhost", "parent=" + encodeURIComponent(location.hostname));
}
function linkFor(item) {
  return item.vod_url || item.url;
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null;
  try { body = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((body && body.detail) || `Xəta ${r.status}`);
  return body;
}
function toast(msg, kind = "", action) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  if (action) {
    const b = document.createElement("button");
    b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.append(b);
  }
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 4200);
}

/* ---------- tags ---------- */
function renderTags() {
  const nav = $("#tags");
  nav.innerHTML = state.tags.map(t => `
    <span class="tag ${t.pending ? "pending" : ""}" role="button" tabindex="0" data-id="${t.id}" aria-current="${t.id === state.tag}">
      ${esc(t.name)}
      ${t.pending ? "" : `<button class="edit" data-edit="${t.id}" aria-label="${esc(t.name)}: platforma eşləşməsi" title="Platforma eşləşməsi">
        <svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4Z"/></svg></button>`}
    </span>`).join("");
}
$("#tags").addEventListener("click", e => {
  const edit = e.target.closest("[data-edit]");
  if (edit) { e.stopPropagation(); openMapping(Number(edit.dataset.edit)); return; }
  const t = e.target.closest(".tag");
  if (t && !t.classList.contains("pending")) selectTag(Number(t.dataset.id));
});
$("#tags").addEventListener("keydown", e => {
  const t = e.target.closest(".tag");
  if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectTag(Number(t.dataset.id)); }
});

async function loadTags() {
  state.tags = await api("/api/tags");
  if (!state.tags.find(t => t.id === state.tag)) state.tag = state.tags[0]?.id ?? null;
  renderTags();
}

function selectTag(id) {
  state.tag = id;
  state.selected = null; state.selectedItem = null;
  try { localStorage.setItem("hr.tag", String(id)); } catch { /* private mode */ }
  renderTags(); renderStage(); refresh(true);
}

async function addTag(name) {
  name = name.trim();
  if (!name) return;
  const temp = { id: -Date.now(), name, pending: true };
  state.tags.push(temp); renderTags();
  toast(`"${name}" Twitch, Kick və YouTube-da axtarılır…`);
  try {
    const tag = await api("/api/tags", { method: "POST", body: JSON.stringify({ name }) });
    state.tags = state.tags.filter(t => t !== temp);
    state.tags.push(tag);
    const found = PLATFORMS.filter(p => tag.mapping[p.id]).map(p => `${p.label}: ${tag.mapping[p.id].name}`);
    toast(found.length ? `Tapıldı. ${found.join("; ")}` : "Heç bir platformada uyğun kateqoriya tapılmadı. Eşləşməni əl ilə düzəldin.", found.length ? "" : "err");
    selectTag(tag.id);
  } catch (err) {
    state.tags = state.tags.filter(t => t !== temp); renderTags();
    toast(err.message, "err");
  }
}
$("#tagForm").addEventListener("submit", e => {
  e.preventDefault();
  const v = $("#tagInput").value;
  $("#tagInput").value = "";
  addTag(v);
});

/* ---------- mapping dialog ---------- */
let mappingTag = null;
function openMapping(id) {
  const tag = state.tags.find(t => t.id === id);
  if (!tag) return;
  mappingTag = tag;
  $("#mappingTitle").textContent = tag.name;
  $("#mappingRows").innerHTML = PLATFORMS.map(p => {
    const c = tag.mapping[p.id];
    const hint = p.id === "youtube" ? "Axtarış sorğusu" : p.id === "kick" ? "Kick kateqoriya slug-u" : "Twitch kateqoriya ID-si";
    return `<div class="map-row">
      <span class="pbadge ${p.id}">${p.label}</span>
      <label>${hint}
        <input data-map="${p.id}" value="${esc(c ? c.id : "")}" placeholder="${c ? "" : "Tapılmadı"}">
        <span class="found ${c ? "" : "off"}">${c ? esc(c.name) + (c.viewers ? `, ${compact(c.viewers)} izləyici` : "") : "Bu platformada izlənmir"}</span>
      </label>
      <label class="switch"><input type="checkbox" data-on="${p.id}" ${c ? "checked" : ""}> Aktiv</label>
    </div>`;
  }).join("");
  $("#mapping").showModal();
}
$("#mapping").addEventListener("close", async () => {
  const v = $("#mapping").returnValue;
  const tag = mappingTag;
  if (!tag) return;
  if (v === "delete") {
    if (!confirm(`"${tag.name}" kateqoriyası və tarixçəsi silinsin?`)) return;
    await api(`/api/tags/${tag.id}`, { method: "DELETE" });
    toast(`"${tag.name}" silindi`);
    state.tag = null; await loadTags(); refresh(true); renderStage();
    return;
  }
  if (v !== "save") return;
  for (const p of PLATFORMS) {
    const on = $(`[data-on="${p.id}"]`).checked;
    const val = $(`[data-map="${p.id}"]`).value.trim();
    const cur = tag.mapping[p.id];
    let next = null;
    if (on && val) {
      next = cur && cur.id === val ? cur : { id: val, name: p.id === "youtube" ? `Axtarış: "${val}"` : val, terms: p.id === "youtube" ? [val] : undefined };
    }
    if (JSON.stringify(next) !== JSON.stringify(cur ?? null)) {
      await api(`/api/tags/${tag.id}`, { method: "PATCH", body: JSON.stringify({ platform: p.id, category: next }) });
    }
  }
  toast("Eşləşmə yadda saxlanıldı, yenilənir");
  await loadTags();
});

/* ---------- filters ---------- */
$("#platToggles").innerHTML = PLATFORMS.map(p =>
  `<button class="plat" data-p="${p.id}" aria-pressed="true"><i style="background:${p.color}"></i>${p.label}</button>`).join("");
$("#platToggles").addEventListener("click", e => {
  const b = e.target.closest(".plat");
  if (!b) return;
  const p = b.dataset.p;
  if (state.platforms.has(p) && state.platforms.size === 1) return;   // keep at least one
  state.platforms.has(p) ? state.platforms.delete(p) : state.platforms.add(p);
  b.setAttribute("aria-pressed", state.platforms.has(p));
  refresh(true);
});
$("#rejSel").addEventListener("change", e => { state.showRejected = e.target.checked; refresh(true); });
$("#langSel").addEventListener("change", e => { state.lang = e.target.value; refresh(true); });
$("#qualSel").addEventListener("change", e => { state.minQ = Number(e.target.value); refresh(true); });
let searchT;
$("#search").addEventListener("input", e => { clearTimeout(searchT); searchT = setTimeout(() => { state.q = e.target.value.trim(); refresh(true); }, 200); });
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  state.tab = t.dataset.tab;
  document.querySelectorAll(".tab").forEach(x => x.setAttribute("aria-selected", x === t));
  $("#qualSel").disabled = state.tab !== "live";
  $("#langSel").disabled = state.tab === "clips";
  $("#rejWrap").hidden = state.tab !== "moments";
  state.selected = null; state.selectedItem = null; renderStage();
  refresh(true);
}));

/* ---------- data ---------- */
let inflight = null;
async function refresh(reset = false) {
  if (state.tag == null) { state.items = []; renderBoard(); return; }
  if (reset) { state.items = []; renderBoard(true); }
  const qs = new URLSearchParams({ tag: state.tag, platforms: [...state.platforms].join(",") });
  if (state.lang) qs.set("lang", state.lang);
  if (state.q) qs.set("q", state.q);
  const ctl = new AbortController();
  inflight?.abort(); inflight = ctl;
  try {
    if (state.tab === "live") {
      if (state.minQ) qs.set("min_quality", state.minQ);
      const d = await (await fetch("/api/live?" + qs, { signal: ctl.signal })).json();
      state.items = d.items; state.updated = d.updated; state.liveCount = d.total;
    } else if (state.tab === "moments") {
      if (state.showRejected) qs.set("show_rejected", "true");
      const d = await (await fetch("/api/moments?" + qs, { signal: ctl.signal })).json();
      state.items = d.items; state.momentCount = d.items.length; state.hiddenCount = d.hidden || 0;
    } else {
      const all = await (await fetch("/api/clips", { signal: ctl.signal })).json();
      const q = state.q.toLowerCase();
      state.items = all.map(c => ({ ...c, key: c.id }))
        .filter(c => !c.source.platform || state.platforms.has(c.source.platform))
        .filter(c => !q || (c.title + " " + (c.source.display_name || "")).toLowerCase().includes(q));
      state.clipCount = all.length;
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    toast("Motorla əlaqə yoxdur. Server işləyir?", "err");
    return;
  }
  if (state.selected) {
    const fresh = state.items.find(i => i.key === state.selected);
    if (fresh) { state.selectedItem = fresh; updateStageStats(); }
  }
  renderBoard();
}

/* ---------- board ---------- */
function heatBar(v) {
  const pct = Math.max(2, Math.min(100, v || 0));
  return `<div class="heat"><div class="track"><div class="fill" style="--v:${pct}%"></div></div><div class="score">${Math.round(v || 0)}</div></div>`;
}
function rankCell(it) {
  let ch = "";
  if (it.rank_change == null) ch = Object.keys(prevRanks).length && !(it.key in prevRanks) ? `<small class="new">yeni</small>` : "";
  else if (it.rank_change > 0) ch = `<small class="up">▲ ${it.rank_change}</small>`;
  else if (it.rank_change < 0) ch = `<small class="down">▼ ${-it.rank_change}</small>`;
  return `<div class="rank"><b>${it.rank}</b>${ch}</div>`;
}
function qualityChip(q) {
  if (!q) return "";
  return `<span class="chip ${q.score >= 100 ? "q-top" : ""}" title="${q.bitrate_kbps} kbps">${esc(q.label)}</span>`;
}
function permChip(p) {
  if (p === "allowed") return `<span class="chip perm-allowed">İcazə var</span>`;
  if (p === "denied") return `<span class="chip perm-denied">İcazə yoxdur</span>`;
  if (p === "asked") return `<span class="chip">İcazə istənib</span>`;
  return "";
}
let prevRanks = {};
function liveRow(it) {
  const g = it.growth;
  const gTxt = g == null ? `<small>ölçülür</small>` : `<small class="${g > 0.005 ? "up" : g < -0.005 ? "down" : ""}">${g > 0 ? "+" : ""}${(g * 100).toFixed(1)}%</small>`;
  const chat = it.chat ? `<b>${compact(it.chat.per_min)}</b><small>mesaj/dəq</small>` : `<b>–</b><small>top deyil</small>`;
  const rose = it.rank_change > 0 && prevRanks[it.key] && prevRanks[it.key] > it.rank;
  return `<article class="row ${rose ? "rose" : ""}" data-key="${esc(it.key)}" role="option" aria-selected="${it.key === state.selected}" tabindex="-1">
    ${rankCell(it)}
    <div class="thumb">${it.thumbnail ? `<img src="${esc(safeUrl(it.thumbnail))}" alt="" loading="lazy">` : ""}<span class="pbadge ${it.platform}">${PLATFORMS.find(p => p.id === it.platform)?.label}</span></div>
    <div class="who">
      <div class="name">${esc(it.display_name)} ${it.language ? `<span class="lang">${esc(it.language.toUpperCase().slice(0, 2))}</span>` : ""}</div>
      <div class="title" title="${esc(it.title)}">${esc(it.title)}</div>
      <div class="chips">${qualityChip(it.quality)}${it.uptime ? `<span class="chip">${duration(it.uptime)} canlı</span>` : ""}${permChip(it.permission)}</div>
    </div>
    <div class="num numcol"><b>${compact(it.viewers)}</b>${gTxt}</div>
    <div class="num chatcol">${chat}</div>
    ${heatBar(it.hype)}
  </article>`;
}
function rejectChips(it) {
  if (it.rejected) return (it.reasons || []).map(r => `<span class="chip perm-denied" title="Klip üçün uyğun deyil">${esc(r)}</span>`).join("");
  if (it.extra?.flags?.length) return it.extra.flags.map(f => `<span class="chip" title="Diqqət">${esc(f)}</span>`).join("");
  return "";
}
function momentRow(it, i) {
  const age = ago(it.created_at);
  const kind = KIND_LABEL[it.kind] || it.kind;
  return `<article class="row" data-key="${esc(it.key)}" role="option" aria-selected="${it.key === state.selected}" tabindex="-1">
    <div class="rank"><b>${i + 1}</b></div>
    <div class="thumb">${it.thumbnail ? `<img src="${esc(safeUrl(it.thumbnail))}" alt="" loading="lazy">` : ""}<span class="pbadge ${it.platform}">${PLATFORMS.find(p => p.id === it.platform)?.label}</span>${it.duration ? `<span class="dur">${clock(it.duration)}</span>` : ""}</div>
    <div class="who">
      <div class="name">${esc(it.display_name)} ${it.language ? `<span class="lang">${esc(it.language.toUpperCase().slice(0, 2))}</span>` : ""}</div>
      <div class="title" title="${esc(it.title)}">${esc(it.title)}</div>
      <div class="chips"><span class="chip ${it.kind === "chat_spike" ? "spike" : ""}">${kind}</span><span class="chip">${age}</span>${it.vod_url ? `<span class="chip">VOD anı var</span>` : ""}${permChip(it.permission)}${rejectChips(it)}</div>
    </div>
    <div class="num numcol"><b>${compact(it.views)}</b><small>${it.kind === "chat_spike" ? "izləyici" : "baxış"}</small></div>
    <div class="num chatcol">${it.views_per_hour ? `<b>${compact(it.views_per_hour)}</b><small>baxış/saat</small>` : `<b>–</b><small></small>`}</div>
    ${heatBar(it.hype)}
  </article>`;
}
function renderBoard(loading = false) {
  const board = $("#board");
  $("#liveCount").textContent = state.liveCount ? compact(state.liveCount) : "";
  $("#momentCount").textContent = state.momentCount ? compact(state.momentCount) : "";
  renderProgressBadge();
  if (state.tag == null) {
    board.innerHTML = `<div class="empty"><h2>Nəyi izləyək?</h2>
      <p>Kateqoriya əlavə edin. Motor onu Twitch, Kick və YouTube-da tapacaq, canlı yayımları hype-a görə sıralayacaq və hər dəqiqə yeniləyəcək.</p>
      <div class="suggest">${["GTA 6", "Fortnite", "Counter-Strike 2", "Just Chatting"].map(s => `<button data-suggest="${s}">${s}</button>`).join("")}</div></div>`;
    return;
  }
  if (state.tab === "clips" && !loading) {
    const working = state.progress.map(progressCard).join("");
    board.innerHTML = state.items.length || state.progress.length ? `<div class="clip-grid" role="listbox" aria-label="Hazır kliplər">${working}${state.items.map(clipCard).join("")}</div>`
      : `<div class="empty"><h2>Hələ klip yoxdur</h2><p>"Anlar" bölməsindən bir klip və ya chat partlayışı seçib <kbd>K</kbd> basın. Motor onu 9:16 Shorts formatına çevirəcək: facecam üstdə, oyun altda, subtitrlə.</p></div>`;
    return;
  }
  if (loading || (!state.items.length && !state.updated && state.tab === "live")) {
    board.innerHTML = Array.from({ length: 7 }, () => `<div class="skeleton"></div>`).join("");
    return;
  }
  if (!state.items.length) {
    board.innerHTML = `<div class="empty"><p>${state.q || state.lang || state.minQ || state.platforms.size < 3
      ? "Bu filtrlərlə nəticə yoxdur. Filtrləri genişləndirin."
      : state.tab === "live" ? "Bu kateqoriyada indi canlı yayım tapılmadı. Platforma eşləşməsini yoxlayın." : "Son 24 saatda an tapılmadı. Anlar hər 5 dəqiqədən bir yenilənir."}</p></div>`;
    return;
  }
  const html = state.tab === "live" ? state.items.map(liveRow).join("") : state.items.map(momentRow).join("");
  board.innerHTML = `<div role="listbox" aria-label="Ranking">${html}</div>`;
  if (state.tab === "live") prevRanks = Object.fromEntries(state.items.map(i => [i.key, i.rank]));
  updateFreshness();
}
function progressCard(p) {
  const pct = p.percent == null ? null : Math.max(0, Math.min(100, p.percent));
  return `<article class="clip-card working" data-job="${p.id}" aria-busy="true">
    <div class="vthumb">${p.thumbnail ? `<img src="${esc(safeUrl(p.thumbnail))}" alt="" loading="lazy">` : ""}
      ${p.platform ? `<span class="pbadge ${esc(p.platform)}">${PLATFORMS.find(x => x.id === p.platform)?.label || ""}</span>` : ""}
      <div class="veil">
        <b class="pct">${pct == null ? "Canlı" : pct + "%"}</b>
        <div class="pbar ${pct == null ? "indeterminate" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" ${pct == null ? "" : `aria-valuenow="${pct}"`}><i style="width:${pct == null ? 100 : pct}%"></i></div>
        <span class="pstage">${esc(p.stage)}</span>
      </div>
      <button class="pstop" data-stop="${p.id}" aria-label="Dayandır" title="Dayandır">×</button>
    </div>
    <div class="clip-meta"><b>${esc(p.title || p.display_name || "Klip")}</b><span>${esc(p.display_name || "")}, ${clock(p.elapsed) || "0:00"} keçib</span></div>
  </article>`;
}
function updateProgressInPlace() {
  // patch numbers without re-rendering the grid (keeps scroll position and hover)
  for (const p of state.progress) {
    const card = document.querySelector(`.clip-card.working[data-job="${p.id}"]`);
    if (!card) return false;
    const pct = p.percent == null ? null : Math.max(0, Math.min(100, p.percent));
    card.querySelector(".pct").textContent = pct == null ? "Canlı" : pct + "%";
    if (pct != null) { card.querySelector(".pbar i").style.width = pct + "%"; card.querySelector(".pbar").setAttribute("aria-valuenow", pct); }
    card.querySelector(".pstage").textContent = p.stage;
    card.querySelector(".clip-meta span").textContent = `${p.display_name || ""}, ${clock(p.elapsed) || "0:00"} keçib`;
  }
  return document.querySelectorAll(".clip-card.working").length === state.progress.length;
}
let progressT = null;
async function pollProgress() {
  clearTimeout(progressT);
  let list = [];
  try { list = await api("/api/jobs/progress"); } catch { /* engine offline */ }
  const before = state.progress.map(p => p.id).join(",");
  state.progress = list;
  state.active = Object.fromEntries(list.map(p => [p.key, p.id]));
  renderProgressBadge();
  if (state.tab === "clips") {
    if (before !== list.map(p => p.id).join(",") || !updateProgressInPlace()) refresh();
  }
  const btn = document.querySelector('[data-act="working"]');
  const mine = state.selectedItem && list.find(p => p.key === state.selectedItem.key);
  if (btn && mine) btn.querySelector("span").textContent = mine.percent == null ? mine.stage : `Hazırlanır ${mine.percent}%`;
  else if (btn && !mine) renderStage();
  if (list.length) progressT = setTimeout(pollProgress, 1500);
}
function renderProgressBadge() {
  const n = state.progress.length;
  const el = $("#clipCount");
  el.innerHTML = `${state.clipCount ? state.clipCount : ""}${n ? ` <span class="working-badge">${n} hazırlanır</span>` : ""}`;
}
function clipCard(c) {
  const src = c.source || {};
  return `<article class="clip-card" data-key="${esc(c.key)}" role="option" aria-selected="${c.key === state.selected}" tabindex="-1">
    <div class="vthumb">${c.poster ? `<img src="${esc(c.poster)}" alt="" loading="lazy">` : ""}
      ${src.platform ? `<span class="pbadge ${esc(src.platform)}">${PLATFORMS.find(p => p.id === src.platform)?.label || ""}</span>` : ""}
      ${c.duration ? `<span class="dur">${clock(c.duration)}</span>` : ""}</div>
    <div class="clip-meta"><b>${esc(c.title)}</b><span>${esc(src.display_name || "")}, ${ago(c.created)}</span></div>
  </article>`;
}
$("#board").addEventListener("click", e => {
  const sug = e.target.closest("[data-suggest]");
  if (sug) { addTag(sug.dataset.suggest); return; }
  const stop = e.target.closest("[data-stop]");
  if (stop) {
    api(`/api/jobs/${stop.dataset.stop}/stop`, { method: "POST" }).then(() => { toast("Klip işi dayandırıldı"); pollProgress(); });
    return;
  }
  const card = e.target.closest(".clip-card");
  if (card && !card.classList.contains("working")) { select(card.dataset.key); return; }
  const row = e.target.closest(".row");
  if (row) select(row.dataset.key);
});

function updateFreshness() {
  const el = $("#freshness");
  if (state.tab !== "live" || !state.updated) { el.textContent = ""; return; }
  el.textContent = `Yeniləndi ${ago(state.updated)}`;
}
setInterval(updateFreshness, 5000);

/* ---------- stage ---------- */
let hlsPlayer = null;
function select(key, scroll = false) {
  const it = state.items.find(i => i.key === key);
  if (!it) return;
  state.selected = key; state.selectedItem = it;
  document.querySelectorAll(".row, .clip-card").forEach(r => r.setAttribute("aria-selected", r.dataset.key === key));
  if (scroll) document.querySelector(`.row[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({ block: "nearest" });
  renderStage();
}
function renderStage() {
  const st = $("#stage");
  hlsPlayer?.destroy(); hlsPlayer = null;
  const it = state.selectedItem;
  if (!it) {
    st.classList.remove("open");
    st.innerHTML = `<div class="placeholder"><h3>Yayım seçin</h3>
      <p>Ranking-dən bir sətrə klikləyin: burada izləyəcək, linki kopyalayacaq, OBS-ə göndərəcək və ya klip hazırladacaqsınız.</p>
      <div class="keys"><kbd>↑ ↓</kbd><span>ranking-də hərəkət</span><kbd>C</kbd><span>linki kopyala</span><kbd>O</kbd><span>OBS-ə göndər</span><kbd>K</kbd><span>klip hazırla</span><kbd>R</kbd><span>OBS-də yaz / dayandır</span><kbd>/</kbd><span>axtarış</span></div></div>`;
    return;
  }
  if (state.tab === "clips") { renderClipStage(st, it); return; }
  const emb = embedFor(it);
  const hls = it.extra && it.extra.hls;
  let player;
  if (emb) player = `<iframe src="${esc(emb)}" allow="autoplay; fullscreen; encrypted-media" title="${esc(it.display_name)}"></iframe>`;
  else if (hls) player = `<video id="hlsVideo" controls autoplay playsinline></video>`;
  else player = `<div class="note">Bu element üçün embed yoxdur. Linki açın.</div>`;
  const isLive = state.tab === "live";
  st.innerHTML = `
    <button class="close-stage" aria-label="Bağla">×</button>
    <div class="player">${player}</div>
    <div class="stage-body">
      <div class="stage-head">
        ${it.avatar ? `<img src="${esc(safeUrl(it.avatar))}" alt="">` : ""}
        <div><h3>${esc(it.display_name)}</h3><p id="stageMeta"></p></div>
      </div>
      <p class="stage-title">${esc(it.title)}</p>
      <div class="bigscore"><b id="stageScore">${Math.round(it.hype)}</b><span>${isLive ? "hype skoru, bu kateqoriyada" : "an skoru, son 24 saatda"}</span></div>
      <div id="stageBreak"></div>
      ${isLive ? `<div class="spark" id="spark"></div>` : ""}
      <div class="actions">
        <button class="btn primary" data-act="copy"><svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>Linki kopyala <kbd>C</kbd></button>
        <button class="btn" data-act="obs"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/></svg>OBS-ə göndər <kbd>O</kbd></button>
        ${state.active[it.key] && !it.kind ? `<button class="btn rec on" data-act="stopclip"><i class="dot"></i>Canlı klipi dayandır</button>`
          : state.active[it.key] ? `<button class="btn" data-act="working" disabled><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span>Hazırlanır ${state.progress.find(p => p.key === it.key)?.percent ?? 0}%</span></button>`
          : `<button class="btn" data-act="clip" ${it.permission === "denied" ? "disabled" : ""}>`}<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/></svg>${state.active[it.key] ? "" : `Klip hazırla <kbd>K</kbd></button>`}
        <a class="btn" href="${esc(safeUrl(linkFor(it)))}" target="_blank" rel="noopener">Platformada aç</a>
        <button class="btn rec ${state.recording ? "on" : ""}" data-act="record" style="grid-column:1/-1"><i class="dot"></i>${state.recording ? "Yazını dayandır" : "OBS-də göstər və yazmağa başla"} <kbd>R</kbd></button>
      </div>
      <div class="perm">Yayımçının icazəsi
        <select id="permSel">
          ${[["unknown", "Bilinmir"], ["asked", "İstənib"], ["allowed", "Var"], ["denied", "Yoxdur"]].map(([v, l]) => `<option value="${v}" ${it.permission === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </div>
      <div class="links">${it.vod_url ? `<a href="${esc(safeUrl(it.vod_url))}" target="_blank" rel="noopener">VOD-da həmin an</a>` : ""}<a href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener">${it.kind ? "Mənbə" : "Kanal"}</a></div>
    </div>`;
  if (!emb && hls) {
    const v = $("#hlsVideo");
    if (window.Hls && Hls.isSupported()) { hlsPlayer = new Hls(); hlsPlayer.loadSource(hls); hlsPlayer.attachMedia(v); }
    else v.src = hls;
  }
  st.classList.add("open");
  updateStageStats();
  if (isLive) loadSpark(it.key);
}
function renderClipStage(st, c) {
  const src = c.source || {};
  st.innerHTML = `
    <button class="close-stage" aria-label="Bağla">×</button>
    <div class="vplayer"><video src="${esc(c.file)}" ${c.poster ? `poster="${esc(c.poster)}"` : ""} controls autoplay playsinline loop></video></div>
    <div class="stage-body">
      <h3 class="clip-title">${esc(c.title)}</h3>
      <p class="hint">${esc(src.display_name || "")}${c.duration ? `, ${clock(c.duration)}` : ""}, 1080×1920, ${(c.size / 1048576).toFixed(1)} MB</p>
      <label class="desc">Paylaşım təsviri
        <textarea id="clipDesc" rows="4">${esc(c.description)}</textarea></label>
      <div class="actions">
        <a class="btn primary" href="${esc(c.file)}" download><svg viewBox="0 0 24 24"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>MP4 yüklə</a>
        <button class="btn" data-act="copydesc"><svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>Təsviri kopyala</button>
      </div>
      ${src.url ? `<div class="links"><a href="${esc(safeUrl(src.url))}" target="_blank" rel="noopener">Mənbə klip</a></div>` : ""}
      <p class="hint" style="margin-top:14px">Paylaşmazdan əvvəl yayımçının icazəsini yoxlayın; kredit sətri təsvirə avtomatik əlavə olunub.</p>
    </div>`;
  st.classList.add("open");
}

function updateStageStats() {
  const it = state.selectedItem;
  if (!it || !$("#stageBreak")) return;
  $("#stageScore").textContent = Math.round(it.hype);
  const meta = state.tab === "live"
    ? [`${compact(it.viewers)} izləyici`, it.uptime ? `${duration(it.uptime)} canlı` : "", it.quality ? it.quality.label : ""]
    : [`${compact(it.views)} ${it.kind === "chat_spike" ? "izləyici" : "baxış"}`, ago(it.created_at), it.duration ? clock(it.duration) : ""];
  $("#stageMeta").textContent = meta.filter(Boolean).join(", ");
  if (state.tab === "live" && it.components) {
    $("#stageBreak").innerHTML = `<div class="breakdown">${Object.entries(SIGNAL_LABEL).map(([k, l]) => {
      const v = it.components[k];
      const na = v == null;
      const why = k === "growth" ? "2.5 dəq tarixçə lazımdır" : k === "chat" ? "yalnız top kanallar izlənir" : "platformada yoxdur";
      return `<div class="${na ? "na" : ""}"><div class="lbl"><span>${l}</span><span>${na ? "neytral" : Math.round(v * 100)}</span></div>
        <div class="mini"><i style="width:${na ? 50 : Math.round(v * 100)}%;${na ? "opacity:.3" : ""}"></i></div>${na ? `<small style="color:var(--faint)">${why}</small>` : ""}</div>`;
    }).join("")}</div>`;
  } else if (it.kind === "chat_spike" && it.extra) {
    $("#stageBreak").innerHTML = `<p class="hint">10 saniyədə ${it.extra.msgs} mesaj, normadan ${it.extra.z} standart sapma yuxarı. ${it.extra.hype} mesajda hype sözləri var.${it.extra.quality != null ? ` Keyfiyyət balı: ${Math.round(it.extra.quality)}.` : ""}${it.rejected ? ` <b>Rədd: ${esc((it.reasons || []).join(", "))}.</b>` : ""}</p>`;
  } else {
    $("#stageBreak").innerHTML = it.views_per_hour ? `<p class="hint">Saatda ${compact(it.views_per_hour)} baxış. Bu tempə görə sıralanıb.</p>` : "";
  }
}
async function loadSpark(key) {
  const el = $("#spark");
  if (!el) return;
  const rows = await api(`/api/history?key=${encodeURIComponent(key)}&minutes=60`).catch(() => []);
  if (state.selected !== key) return;
  if (rows.length < 2) { el.innerHTML = `<p class="hint">Tarixçə toplanır. Qrafik bir neçə dəqiqəyə görünəcək.</p>`; return; }
  const W = 400, Hh = 70, t0 = rows[0].ts, t1 = rows[rows.length - 1].ts || t0 + 1;
  const vmax = Math.max(...rows.map(r => r.viewers)), vmin = Math.min(...rows.map(r => r.viewers));
  const x = t => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const yv = v => Hh - 4 - ((v - vmin) / Math.max(1, vmax - vmin)) * (Hh - 8);
  const yh = h => Hh - 4 - ((h || 0) / 100) * (Hh - 8);
  const pv = rows.map(r => `${x(r.ts).toFixed(1)},${yv(r.viewers).toFixed(1)}`).join(" ");
  const ph = rows.map(r => `${x(r.ts).toFixed(1)},${yh(r.hype).toFixed(1)}`).join(" ");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${Hh}" preserveAspectRatio="none" role="img" aria-label="Son 60 dəqiqədə izləyici və hype">
      <polyline class="v" points="${pv}"/><polyline class="h" points="${ph}"/></svg>
    <div class="legend"><span><i></i>İzləyici, ${compact(vmin)}–${compact(vmax)}</span><span class="lh"><i></i>Hype</span><span>son ${Math.round((t1 - t0) / 60)} dəq</span></div>`;
}

$("#stage").addEventListener("click", e => {
  if (e.target.closest(".close-stage")) { state.selected = null; state.selectedItem = null; renderStage(); document.querySelectorAll(".row").forEach(r => r.setAttribute("aria-selected", "false")); return; }
  const b = e.target.closest("[data-act]");
  if (b) act(b.dataset.act);
});
$("#stage").addEventListener("change", async e => {
  if (e.target.id !== "permSel" || !state.selectedItem) return;
  const it = state.selectedItem;
  const key = `${it.platform}:${it.channel}`;
  await api("/api/permissions", { method: "PUT", body: JSON.stringify({ key, status: e.target.value }) });
  it.permission = e.target.value;
  toast(`${it.display_name}: icazə statusu yeniləndi`);
  renderStage(); refresh();
});

async function act(kind) {
  const it = state.selectedItem;
  if (!it) return;
  if (kind === "copydesc") {
    const t = $("#clipDesc")?.value || "";
    try { await navigator.clipboard.writeText(t); toast("Təsvir kopyalandı"); } catch { prompt("Təsviri kopyalayın:", t); }
    return;
  }
  if (state.tab === "clips" && kind !== "copy") return;
  if (kind === "record") {
    try {
      if (!state.recording) {
        await api("/api/actions/obs", { method: "POST", body: JSON.stringify({ key: it.key }) });
        const r = await api("/api/actions/record", { method: "POST", body: JSON.stringify({ action: "start" }) });
        state.recording = r.recording;
        toast(`Yazılır: ${it.display_name}. Fayl ${r.folder} qovluğuna düşəcək.`);
      } else {
        const r = await api("/api/actions/record", { method: "POST", body: JSON.stringify({ action: "stop" }) });
        state.recording = false;
        toast(r.file ? `Yazı saxlanıldı: ${r.file}` : "Yazı dayandırıldı");
      }
    } catch (err) { toast(err.message, "err"); }
    renderStage();
    return;
  }
  if (kind === "stopclip") {
    const id = state.active[it.key];
    if (id) { await api(`/api/jobs/${id}/stop`, { method: "POST" }); delete state.active[it.key]; toast("Canlı klip sessiyası dayandırıldı"); renderStage(); }
    return;
  }
  if (kind === "copy") {
    const url = linkFor(it);
    try { await navigator.clipboard.writeText(url); toast("Link kopyalandı"); }
    catch { prompt("Linki kopyalayın:", url); }
    return;
  }
  const btn = document.querySelector(`[data-act="${kind}"]`);
  btn && (btn.disabled = true);
  try {
    if (kind === "obs") {
      const slow = setTimeout(() => toast("OBS bağlı idi, açılır. Bir neçə saniyə gözləyin…"), 1200);
      const r = await api("/api/actions/obs", { method: "POST", body: JSON.stringify({ key: it.key }) }).finally(() => clearTimeout(slow));
      const what = r.detail.includes("created") ? "HypeRadar mənbəyi yaradıldı" : "HypeRadar mənbəyi yeniləndi";
      toast(`${r.detail.startsWith("OBS açıldı") ? "OBS açıldı. " : ""}${it.display_name} OBS-ə göndərildi: ${what}`);
    } else if (kind === "clip") {
      const r = await api("/api/actions/clip", { method: "POST", body: JSON.stringify({ key: it.key, force: !!it._force }) });
      state.active[it.key] = r.job; renderStage(); pollProgress();
      toast(it.kind ? `Klip hazırlanır. Faizi "Hazır kliplər" bölməsində görəcəksiniz.` : `Canlı klip sessiyası başladı: hype anları avtomatik "Hazır kliplər"ə düşəcək.`);
    }
  } catch (err) {
    const settingsHint = /Ayarlar|qoşulmayıb|parol/i.test(err.message);
    if (/^Uyğun deyil/.test(err.message)) {
      toast(err.message, "err", { label: "Yenə də klip et", run: () => { it._force = true; act("clip"); } });
      return;
    }
    toast(err.message, "err", settingsHint ? { label: "Ayarlar", run: openSettings } : null);
  } finally {
    btn && (btn.disabled = it.permission === "denied" && kind === "clip");
  }
}

/* ---------- keyboard ---------- */
document.addEventListener("keydown", e => {
  if (e.target.closest("input, select, textarea, dialog")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const idx = state.items.findIndex(i => i.key === state.selected);
  if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); const n = state.items[Math.min(state.items.length - 1, idx + 1)]; n && select(n.key, true); }
  else if (e.key === "ArrowUp") { e.preventDefault(); const n = state.items[Math.max(0, idx - 1)]; n && select(n.key, true); }
  else if (e.key === "c" || e.key === "C") act("copy");
  else if (e.key === "o" || e.key === "O") act("obs");
  else if (e.key === "k" || e.key === "K") act("clip");
  else if ((e.key === "r" || e.key === "R") && state.tab === "live") act("record");
  else if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
  else if (e.key === "Escape" && state.selected) { state.selected = null; state.selectedItem = null; renderStage(); }
});

/* ---------- health ---------- */
function renderHealth() {
  const s = state.status;
  if (!s) return;
  $("#health").innerHTML = PLATFORMS.map(p => {
    const h = s.platforms[p.id] || {};
    const cls = h.ok === true ? "ok" : h.ok === false ? "bad" : "";
    const chat = s.chat[p.id];
    const tip = h.ok === false ? `Xəta: ${h.error}` : h.ok ? `İşləyir, cavab ${h.ms} ms${chat ? `; chat ${chat.channels} kanal (${chat.status})` : ""}` : "Gözlənilir";
    return `<span class="${cls}" title="${esc(tip)}"><i></i><b>${p.label}</b></span>`;
  }).join("");
}
async function loadStatus() { try { state.status = await api("/api/status"); renderHealth(); } catch { /* offline */ } }
setInterval(loadStatus, 15000);

/* ---------- settings ---------- */
async function openSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  const f = $("#settingsForm");
  for (const k of Object.keys(SIGNAL_LABEL)) { f[`w_${k}`].value = s.weights[k]; f[`w_${k}`].nextElementSibling.textContent = Number(s.weights[k]).toFixed(2); }
  f.obs_host.value = s.obs.host; f.obs_port.value = s.obs.port; f.obs_password.value = s.obs.password || "";
  f.chopify_dir.value = s.chopify_dir || "";
  f.live_interval.value = s.live_interval; f.moments_interval.value = s.moments_interval; f.chat_top_n.value = s.chat_top_n;
  $("#settings").showModal();
}
$("#settingsBtn").addEventListener("click", openSettings);
$("#settingsForm").addEventListener("input", e => {
  if (e.target.type === "range") e.target.nextElementSibling.textContent = Number(e.target.value).toFixed(2);
});
$("#settings").addEventListener("close", async () => {
  if ($("#settings").returnValue !== "save") return;
  const f = $("#settingsForm");
  const num = (v, d, lo, hi) => Math.min(hi, Math.max(lo, Number(v) || d));
  const body = {
    weights: Object.fromEntries(Object.keys(SIGNAL_LABEL).map(k => [k, Number(f[`w_${k}`].value)])),
    obs: { host: f.obs_host.value.trim() || "localhost", port: num(f.obs_port.value, 4455, 1, 65535), password: f.obs_password.value },
    chopify_dir: f.chopify_dir.value.trim(),
    live_interval: num(f.live_interval.value, 60, 20, 600),
    moments_interval: num(f.moments_interval.value, 300, 60, 3600),
    chat_top_n: num(f.chat_top_n.value, 15, 0, 40),
  };
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify(body) }); toast("Ayarlar yadda saxlanıldı. Yeni çəkilər növbəti yeniləmədə tətbiq olunacaq."); }
  catch (err) { toast(err.message, "err"); }
});

/* ---------- live updates (SSE) ---------- */
let refreshT;
function scheduleRefresh() { clearTimeout(refreshT); refreshT = setTimeout(() => refresh(), 150); }
function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("hello", e => { state.status = JSON.parse(e.data); renderHealth(); });
  es.addEventListener("live", e => {
    const d = JSON.parse(e.data);
    if (d.tag_id === state.tag && state.tab === "live") scheduleRefresh();
    const t = state.tags.find(x => x.id === d.tag_id); if (t) t.updated = d.ts;
    loadStatus();
  });
  es.addEventListener("moments", e => { const d = JSON.parse(e.data); if (d.tag_id === state.tag && state.tab === "moments") scheduleRefresh(); });
  let lastSpikeToast = 0;
  es.addEventListener("spike", e => {
    const d = JSON.parse(e.data);
    if (d.tag_id !== state.tag) return;
    if (state.tab === "moments") scheduleRefresh();
    if (Date.now() - lastSpikeToast < 20000) return;
    lastSpikeToast = Date.now();
    toast(`${d.moment.display_name}: chat partladı (${d.moment.extra.msgs} mesaj/10 san)`, "spike", {
      label: "Bax", run: () => { document.querySelector('.tab[data-tab="moments"]').click(); setTimeout(() => select(d.moment.key, true), 400); },
    });
  });
  es.addEventListener("job", e => {
    const d = JSON.parse(e.data);
    pollProgress();
    if (d.key && d.status !== "running" && state.active[d.key] === d.id) { delete state.active[d.key]; if (state.selected === d.key) renderStage(); }
    if (d.status === "failed") toast(`Klip işi #${d.id} alınmadı: ${d.detail || "naməlum səbəb"}`, "err");
    else if (d.status === "done" && d.clips) {
      toast(`Klip hazırdır: ${d.clips.length} video`, "", { label: "Bax", run: () => document.querySelector('.tab[data-tab="clips"]').click() });
      if (state.tab === "clips") scheduleRefresh();
    }
  });
  es.onerror = () => { /* EventSource reconnects on its own */ };
}

/* ---------- boot ---------- */
(async function boot() {
  renderStage();
  try { const saved = Number(localStorage.getItem("hr.tag")); if (saved) state.tag = saved; } catch { /* private mode */ }
  try { await loadTags(); } catch { toast("Motorla əlaqə yoxdur. Server işləyir?", "err"); }
  loadStatus();
  pollProgress();
  refresh(true);
  connect();
})();
