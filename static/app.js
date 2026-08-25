/* Football-Bot dashboard */
"use strict";
const $ = id => document.getElementById(id);
const fmt$ = v => (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
const short = t => (t || "").split("-").pop();
const legName = t => { const s = short(t); return s === "TIE" ? "DRAW" : s; };
const lg = s => (s || "").replace(/^KX/, "").replace(/GAME$/, "");
const clock = ts => new Date(ts * 1000).toLocaleTimeString([], {hour12: false});

let sound = false, killOn = false;
const matches = {};          // event -> {el, series, title, late, legs:{ticker:{last,bid,ask,elm}}}
const lastPx = {};
let charts = {};

/* ---------- audio (goal horn, off by default) ---------- */
function horn() {
  if (!sound) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.type = "sawtooth"; o.frequency.value = 220;
    g.gain.setValueAtTime(0.15, ctx.currentTime);
    o.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.25);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.7);
    o.start(); o.stop(ctx.currentTime + 0.7);
  } catch (e) {}
}

/* ---------- charts ---------- */
function mkCharts() {
  const dark = {backgroundColor: "transparent", textStyle: {color: "#5f6f8f", fontFamily: "JetBrains Mono"}};
  charts.equity = echarts.init($("equity"), null, {renderer: "canvas"});
  charts.equity.setOption({...dark, grid: {left: 45, right: 8, top: 8, bottom: 20},
    xAxis: {type: "time", axisLine: {lineStyle: {color: "#1b2438"}}},
    yAxis: {type: "value", splitLine: {lineStyle: {color: "#131b2c"}}, axisLabel: {formatter: "${value}"}},
    series: [{type: "line", step: "end", showSymbol: false, data: [],
      lineStyle: {color: "#00ff87", width: 2},
      areaStyle: {color: {type: "linear", x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{offset: 0, color: "rgba(0,255,135,.25)"}, {offset: 1, color: "rgba(0,255,135,0)"}]}}}]});
  charts.leagues = echarts.init($("leagues"));
  charts.latency = echarts.init($("latency"));
  charts.exits = echarts.init($("exits"));
  window.addEventListener("resize", () => Object.values(charts).forEach(c => c.resize()));
}

function updEquity() {
  fetch("/api/equity").then(r => r.json()).then(d => {
    charts.equity.setOption({series: [{data: d}]});
  }).catch(() => {});
}

function updLeagues(stats, prior) {
  const live = stats.leagues || {};
  const keys = [...new Set([...Object.keys(live), ...Object.keys(prior || {})])]
    .filter(k => (live[k] && live[k].n) || (prior && prior[k] > 0)).slice(0, 14);
  keys.sort((a, b) => ((live[b] || {}).net || 0) - ((live[a] || {}).net || 0));
  charts.leagues.setOption({backgroundColor: "transparent",
    textStyle: {color: "#5f6f8f", fontFamily: "JetBrains Mono"},
    grid: {left: 90, right: 30, top: 8, bottom: 20},
    xAxis: {type: "value", splitLine: {lineStyle: {color: "#131b2c"}}},
    yAxis: {type: "category", inverse: true, data: keys.map(lg),
      axisLabel: {fontSize: 9}},
    series: [
      {name: "live net $", type: "bar", barWidth: 8,
       data: keys.map(k => +(((live[k] || {}).net || 0)).toFixed(2)),
       itemStyle: {color: p => p.value >= 0 ? "#00ff87" : "#ff4d5e"}},
      {name: "prior ¢/ct", type: "bar", barWidth: 4,
       data: keys.map(k => (prior || {})[k] || 0), itemStyle: {color: "#4d3413"}}],
    tooltip: {trigger: "axis"}, legend: {textStyle: {color: "#5f6f8f"}, top: -4}});
}

function updLatency() {
  fetch("/api/latency").then(r => r.json()).then(d => {
    const lag = d.feed_lag || d.demo_lag || {hist: []};
    const hist = {};
    (lag.hist || []).forEach(v => {
      const b = Math.min(1000, Math.round(v / 25) * 25);
      hist[b] = (hist[b] || 0) + 1;
    });
    const bins = Object.keys(hist).map(Number).sort((a, b) => a - b);
    charts.latency.setOption({backgroundColor: "transparent",
      textStyle: {color: "#5f6f8f", fontFamily: "JetBrains Mono"},
      grid: {left: 35, right: 8, top: 24, bottom: 20},
      title: {text: lag.p50 != null ? `p50 ${lag.p50}ms · p95 ${lag.p95 ?? "—"}ms` : "collecting…",
        textStyle: {color: "#5f6f8f", fontSize: 10}, top: 0},
      xAxis: {type: "category", data: bins.map(b => b + "ms")},
      yAxis: {type: "value", splitLine: {lineStyle: {color: "#131b2c"}}},
      series: [{type: "bar", data: bins.map(b => hist[b]), itemStyle: {color: "#ffb454"}}]});
  }).catch(() => {});
}

function updExits(stats) {
  fetch("/api/trades?limit=500").then(r => r.json()).then(d => {
    const by = {};
    (d.closed || []).forEach(t => { by[t.exit_reason] = (by[t.exit_reason] || 0) + 1; });
    charts.exits.setOption({backgroundColor: "transparent",
      textStyle: {color: "#5f6f8f", fontFamily: "JetBrains Mono"},
      series: [{type: "pie", radius: ["45%", "72%"],
        label: {color: "#5f6f8f", fontSize: 10},
        data: Object.entries(by).map(([k, v]) => ({name: k, value: v})),
        color: ["#00ff87", "#ffb454", "#5f6f8f", "#ff4d5e", "#3aa0ff"]}]});
  }).catch(() => {});
}

/* ---------- kill conditions ---------- */
function updKill(stats) {
  const k = stats.kill || {};
  const ci = (k.k2_ci || {});
  const prog = Math.min(100, ((ci.n_signals || 0) / (ci.needed || 50)) * 100);
  const ciTxt = ci.ci ? `[${fmt$(ci.ci[0])}, ${fmt$(ci.ci[1])}]` : "collecting";
  const lat = k.k4_latency_p95_ms;
  const latPct = lat != null ? Math.min(100, lat / 250 * 100) : 0;
  $("killconds").innerHTML = `
    <div class="kc ${ci.status === "FAIL" ? "bad" : ci.status === "PASS" ? "" : "warn"}">
      <label><span>K2 · LIVE EV CI (need ${ci.needed || 50} signals)</span>
        <span class="st">${ci.status || "…"} ${ciTxt}</span></label>
      <div class="kbar"><i style="width:${prog}%"></i></div></div>
    <div class="kc ${k.k4_status === "BREACH" ? "bad" : ""}">
      <label><span>K4 · FEED LAG P95 vs 250ms</span>
        <span class="st">${lat != null ? lat.toFixed(0) + "ms" : "—"} ${k.k4_status || ""}</span></label>
      <div class="kbar"><i style="width:${latPct}%"></i></div></div>
    <div class="kc warn"><label><span>K1 · FILL MODEL vs REALITY</span>
      <span class="st">recording books…</span></label>
      <div class="kbar"><i style="width:8%"></i></div></div>
    <div class="kc"><label><span>K3 · ROLLING LEAGUE EV vs FEES</span>
      <span class="st">${Object.keys(stats.leagues || {}).length} leagues tracked</span></label>
      <div class="kbar"><i style="width:30%"></i></div></div>`;
}

/* ---------- tiles ---------- */
function updTiles(stats) {
  const el = $("t-net");
  el.textContent = fmt$(stats.net || 0);
  el.className = "v money " + ((stats.net || 0) >= 0 ? "pos" : "neg");
  $("t-trades").textContent = stats.closed || 0;
  $("t-win").textContent = stats.closed ? (stats.win_pct + "%") : "—";
  $("t-npf").textContent = stats.closed ? fmt$(stats.net_per_fill) : "—";
  $("t-ci").textContent = stats.ci95 ? `[${fmt$(stats.ci95[0])}, ${fmt$(stats.ci95[1])}]` : "collecting…";
  const s = stats.signals || {};
  $("t-sigs").textContent = Object.values(s).reduce((a, b) => a + b, 0);
  $("t-open").textContent = stats.open || 0;
  $("t-fees").textContent = "$" + (stats.fees || 0).toFixed(0);
}

function updStatus(st) {
  const mb = $("modebadge");
  mb.textContent = st.mode === "live" ? "LIVE" : "DEMO";
  mb.className = "badge " + (st.mode === "live" ? "live" : "demo");
  $("demobanner").classList.toggle("hidden", st.mode !== "demo");
  $("demostatus").textContent = st.demo || "";
  const ws = $("chip-ws");
  ws.textContent = "WS " + (st.ws || "—");
  ws.className = "chip " + (String(st.ws).startsWith("connected") || st.ws === "demo" ? "good" : "bad");
  const lagC = $("chip-lag");
  lagC.textContent = st.feed_lag_p50 != null ? `LAG ${st.feed_lag_p50}ms` : "LAG —";
  lagC.className = "chip " + (st.feed_lag_p50 > 500 ? "bad" : "good");
  $("chip-mkts").textContent = `${st.markets || 0} MKTS · ${st.matches || 0} MATCHES`;
  $("chip-rec").textContent = "REC " + (st.recorded || 0).toLocaleString();
  const u = st.uptime_s || 0;
  $("chip-up").textContent = "UP " + (u > 3600 ? (u / 3600).toFixed(1) + "h" : Math.round(u / 60) + "m");
  killOn = !!st.kill;
  $("killbtn").classList.toggle("on", killOn);
  $("killbtn").textContent = killOn ? "KILLED — CLICK TO ARM" : "KILL SWITCH";
}

/* ---------- match cards ---------- */
function renderMatches(list) {
  const box = $("matches");
  if (!list.length) { box.innerHTML = '<div class="empty">no matches in window — discovery runs every 3 min</div>'; return; }
  box.innerHTML = "";
  list.sort((a, b) => (b.late - a.late));
  list.forEach(m => {
    const card = document.createElement("div");
    card.className = "match" + (m.late ? " late" : "");
    card.id = "m-" + m.event;
    const legs = Object.entries(m.legs);
    const order = {0: 0}; // keep API order; draw goes middle if present
    legs.sort((a, b) => (legName(a[0]) === "DRAW") - (legName(b[0]) === "DRAW"));
    card.innerHTML = `<div class="mhead"><span class="mtitle">${m.title}</span>
      <span class="lg ${m.late ? "late" : ""}">${lg(m.series)}${m.late ? " · LATE" : ""}</span></div>` +
      legs.map(([tk, l]) => {
        const p = l.last ?? l.bid ?? 0;
        return `<div class="leg ${legName(tk) === "DRAW" ? "tie" : ""}" id="leg-${tk}">
          <span class="nm">${legName(tk)}</span>
          <div class="bar"><i style="width:${p}%"></i></div>
          <span class="px" id="px-${tk}">${p ? p.toFixed(0) + "¢" : "—"}</span></div>`;
      }).join("");
    box.appendChild(card);
  });
}

function priceUpdate(u) {
  const el = $("px-" + u.ticker);
  if (!el) return;
  const p = u.last ?? u.bid;
  if (p == null) return;
  const prev = lastPx[u.ticker];
  el.textContent = p.toFixed(0) + "¢";
  const bar = document.querySelector(`#leg-${CSS.escape(u.ticker)} .bar i`);
  if (bar) bar.style.width = Math.max(1, Math.min(99, p)) + "%";
  if (prev != null && p !== prev) {
    el.classList.remove("up", "dn");
    el.classList.add(p > prev ? "up" : "dn");
    const leg = $("leg-" + u.ticker);
    if (leg) {
      leg.classList.remove("flash-up", "flash-dn");
      void leg.offsetWidth;
      leg.classList.add(p > prev ? "flash-up" : "flash-dn");
    }
  }
  lastPx[u.ticker] = p;
}

/* ---------- signal wire ---------- */
function addSignal(s) {
  const box = $("signals");
  const el = document.createElement("div");
  el.className = "sig " + s.outcome;
  el.innerHTML = `<span class="t">${clock(s.ts || Date.now() / 1000)}</span>
    <span class="arr">${s.dir > 0 ? "🔺" : "🔻"}</span>
    <span class="mk">${s.market}</span>
    <span class="meta">Δℓ ${(+s.dl).toFixed(2)} · ${s.levels} lvls · ${Math.round(s.size)} cts ·
      ref ${(+s.ref).toFixed(0)}→${(+s.ext).toFixed(0)}¢ ·
      confirm ${s.conf_lag_ms != null ? (s.conf_lag_ms >= 0 ? "+" : "") + s.conf_lag_ms + "ms" : "—"}</span>
    <span class="oc ${s.outcome}">${s.outcome.replace("_", " ").toUpperCase()}</span>`;
  box.prepend(el);
  while (box.children.length > 60) box.removeChild(box.lastChild);
  if (s.outcome === "filled") {
    const gf = $("goalflash");
    gf.classList.remove("go"); void gf.offsetWidth; gf.classList.add("go");
    horn();
  }
}

/* ---------- desk ---------- */
function renderOpen(list) {
  $("openpos").innerHTML = list.length ? list.map(p => `
    <div class="pos"><div>
      <b>${p.side.toUpperCase()}</b> ${p.market}<br>
      <span style="color:var(--dim)">${p.size} @ ${p.entry_px}¢ → bid ${p.bid != null ? p.bid.toFixed(0) : "—"}¢</span></div>
      <div class="u ${p.upnl >= 0 ? "pos-v" : "neg-v"}" style="color:${p.upnl >= 0 ? "var(--green)" : "var(--red)"}">${p.upnl != null ? fmt$(p.upnl) : ""}</div>
    </div>`).join("") : '<div class="empty">no open positions — hunting…</div>';
}

function addClosed(t) {
  const tb = $("tradetable").querySelector("tbody");
  const tr = document.createElement("tr");
  tr.innerHTML = `<td>${short(t.market)}·${lg(t.series)}</td><td>${t.side}</td>
    <td>${(+t.entry_px).toFixed(1)}</td><td>${(+t.exit_px).toFixed(1)}</td>
    <td>${t.exit_reason || t.reason}</td>
    <td class="${(t.net >= 0 ? "pos-v" : "neg-v")}">${fmt$(t.net)}</td>`;
  tb.prepend(tr);
  while (tb.children.length > 80) tb.removeChild(tb.lastChild);
}

/* ---------- ticker ---------- */
function setTicker(lines) {
  $("ticker").innerHTML = lines.map(l => `<b>${clock(l.ts)}</b> ${l.text}`).join(" &nbsp;·&nbsp; ");
}

/* ---------- boot + websocket ---------- */
async function hydrate() {
  try {
    const [st, cfg, m, tr, sg, stats, log] = await Promise.all([
      fetch("/api/status").then(r => r.json()), fetch("/api/config").then(r => r.json()),
      fetch("/api/matches").then(r => r.json()), fetch("/api/trades").then(r => r.json()),
      fetch("/api/signals?limit=30").then(r => r.json()), fetch("/api/stats").then(r => r.json()),
      fetch("/api/eventlog").then(r => r.json())]);
    window._cfg = cfg;
    updStatus(st); updTiles(stats); updKill(stats); renderMatches(m); renderOpen(tr.open || []);
    (tr.closed || []).slice().reverse().forEach(addClosed);
    sg.slice().reverse().forEach(s => addSignal({...s, ts: s.local_ts}));
    setTicker(log);
    updEquity(); updLeagues(stats, cfg.league_prior); updLatency(); updExits(stats);
  } catch (e) { console.error(e); }
}

let ws, matchRefreshDue = 0;
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "hello") { updStatus(m.status); updTiles(m.stats); }
    else if (m.type === "prices") {
      m.prices.forEach(priceUpdate);
      if (Date.now() > matchRefreshDue) {
        matchRefreshDue = Date.now() + 30000;
        fetch("/api/matches").then(r => r.json()).then(renderMatches).then(() => {
          Object.keys(lastPx).forEach(k => { const e = $("px-" + k); if (e) e.textContent = lastPx[k].toFixed(0) + "¢"; });
        }).catch(() => {});
      }
    }
    else if (m.type === "signal") addSignal(m.signal);
    else if (m.type === "trade_open") { fetch("/api/trades").then(r => r.json()).then(d => renderOpen(d.open)); }
    else if (m.type === "trade_close") { addClosed(m.trade); updEquity(); fetch("/api/trades").then(r => r.json()).then(d => renderOpen(d.open)); }
    else if (m.type === "stats") { updStatus(m.status); updTiles(m.stats); updKill(m.stats); updLeagues(m.stats, (window._cfg || {}).league_prior); }
    else if (m.type === "log") { fetch("/api/eventlog").then(r => r.json()).then(setTicker); }
  };
  ws.onclose = () => setTimeout(connect, 2500);
}

$("killbtn").onclick = () =>
  fetch("/api/kill", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({on: !killOn})}).then(() => {});
$("soundbtn").onclick = () => { sound = !sound; $("soundbtn").textContent = sound ? "🔊" : "🔇"; };

mkCharts();
hydrate();
connect();
setInterval(updLatency, 15000);
setInterval(updExits, 30000);
