"use strict";

const byId = id => document.getElementById(id);
const state = {
  status: null, config: {}, stats: {}, matches: [],
  trades: {open: [], closed: []}, signals: [], events: [], latency: {},
  equity: {combined: [], gate_a: [], price_only_late_score: []},
  activity: [], clocks: {coverage: {}, observations: []}, hydrated: false,
};
const filters = {query: "", strategy: "all", match: "all", result: "all", association: "all", gate: "all", period: "all"};
const visibleEquitySeries = new Set(["combined", "gate_a", "price_only_late_score"]);
const clientErrors = [];
const activeClientFaults = new Map();
let leagueSleeve = "combined";
let socket = null;
let socketConnected = false;
let reconnectTimer = null;
let refreshTimer = null;
let refreshInFlight = false;
let soundEnabled = false;
let killEnabled = false;
let toastTimer = null;

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
function safeJson(value) { return escapeHtml(JSON.stringify(value, null, 2)); }
function finite(value) { return typeof value === "number" && Number.isFinite(value); }
function money(value) {
  const amount = finite(value) ? value : 0;
  return `${amount >= 0 ? "+" : "−"}$${Math.abs(amount).toFixed(2)}`;
}
function cents(value) { return finite(value) ? `${value.toFixed(1)}¢` : "Not supplied"; }
function integer(value) { return finite(value) ? Math.round(value).toLocaleString() : "Not supplied"; }
function percent(value) { return finite(value) ? `${value.toFixed(1)}%` : "Not supplied"; }
function formatBytes(value) {
  if (!finite(value) || value < 0) return "size unavailable";
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value, unit = -1;
  do { amount /= 1024; unit += 1; } while (amount >= 1024 && unit < units.length - 1);
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[unit]}`;
}
function timestampSeconds(timestamp) {
  if (!finite(timestamp)) return null;
  return timestamp > 1e12 ? timestamp / 1000 : timestamp;
}
function fullDate(timestamp) {
  const seconds = timestampSeconds(timestamp);
  if (seconds == null) return "Not supplied by provider";
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return "Invalid timestamp";
  return date.toISOString().replace("T", " ").replace("Z", " UTC");
}
function shortDate(timestamp) {
  const seconds = timestampSeconds(timestamp);
  if (seconds == null) return "Unknown time";
  return new Date(seconds * 1000).toISOString().slice(5, 16).replace("T", " ");
}
function duration(seconds) {
  if (!finite(seconds)) return "Collecting";
  if (seconds < 60) return `${Math.round(seconds)} seconds`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}
function relativeMs(milliseconds) {
  if (!finite(milliseconds)) return "timing unavailable";
  if (Math.abs(milliseconds) < 1000) return `${Math.abs(milliseconds).toFixed(0)} ms`;
  if (Math.abs(milliseconds) < 60000) return `${Math.abs(milliseconds / 1000).toFixed(1)} s`;
  return `${Math.abs(milliseconds / 60000).toFixed(1)} min`;
}
function relativeTo(timestamp, origin) {
  const time = timestampSeconds(timestamp), base = timestampSeconds(origin);
  if (time == null || base == null) return "relative time unavailable";
  const delta = (time - base) * 1000;
  if (Math.abs(delta) < 1) return "at signal receipt";
  return `${relativeMs(delta)} ${delta >= 0 ? "after" : "before"} signal`;
}
function strategyLabel(strategy) {
  return strategy === "price_only_late_score" ? "Price-only late-score" : strategy === "gate_a" ? "Gate A" : "Detector episode";
}
function strategyClass(strategy) { return strategy === "price_only_late_score" ? "price" : "gate"; }
function leagueName(series) { return state.config.league_names?.[series] || series || "Unknown league"; }

const outcomeLabels = {
  filled: "Paper order filled", queued: "Paper order queued", executing: "Checking executable depth",
  rejected_cap: "Declined: executable price exceeded cap", no_book: "Declined: no valid arrival order book",
  killed: "Declined: kill switch active", expired: "Declined: market expired before arrival",
  unsupported_fee: "Declined: fee schedule could not be verified", unconfirmed: "Ignored: no coherent sibling confirmation",
  not_late: "Ignored: outside Gate A late window", strategy_lockout: "Ignored: sleeve re-entry lockout",
  sleeve_outside_window: "Ignored: outside the minute-88 study window", sleeve_no_baseline: "Ignored: no timed triplet baseline",
  sleeve_stale_baseline: "Ignored: triplet baseline was stale", sleeve_stale_triplet_leg: "Ignored: one match contract was stale",
  sleeve_incoherent_sibling_rise: "Ignored: sibling prices did not reallocate coherently",
  sleeve_insufficient_triplet_shift: "Ignored: normalized probability shift was too small",
  sleeve_weak_post_state: "Ignored: inferred post-event state was too weak",
  sleeve_weak_triplet_coherence: "Ignored: sibling outflow explained too little of the move",
  sleeve_wide_spread: "Ignored: spread was too wide", sleeve_not_triplet: "Ignored: match did not have exactly three contracts",
  sleeve_incomplete_book: "Ignored: a contract order book was incomplete",
  sleeve_ambiguous_draw_leg: "Ignored: draw contract could not be identified",
  sleeve_not_rising_leg: "Ignored: target contract was not rising", execution_error: "Execution adapter error",
  sleeve_clock_88_plus: "Price-only 88+ clock accepted",
  sleeve_clock_pre_88: "Declined: persisted clock is before minute 88",
  sleeve_clock_unmapped: "Declined: match is not mapped to a live clock",
  sleeve_clock_missing: "Declined: live clock is missing",
  sleeve_clock_malformed: "Declined: live clock is malformed",
  sleeve_clock_stale: "Declined: live clock is stale",
  sleeve_clock_not_live: "Declined: provider status is not live",
  sleeve_clock_final: "Declined: match clock is final",
  sleeve_clock_suspended: "Declined: match clock is suspended",
  sleeve_clock_abandoned: "Declined: match clock is abandoned",
  sleeve_clock_first_half: "Declined: clock is still first half",
  sleeve_clock_half_time: "Declined: clock is half-time",
  sleeve_clock_pre_match: "Declined: clock is pre-match",
  sleeve_clock_period_unusable: "Declined: clock period is unusable",
};
const exitLabels = {
  target: "Profit target reached", timeout: "Gate A time limit", sleeve_timeout: "Price-only time limit",
  sleeve_profit_lock: "Trailing profit lock", sleeve_scratch: "Fee-aware scratch exit",
  sleeve_oscillation: "Oscillation exit", sleeve_reversal: "Fast price reversal", stop: "Configured stop",
  settle: "Market settlement", flatten: "Manual flatten", kill: "Kill-switch flatten",
};
const associationLabels = {
  state_consistent: "State-consistent match event", nearby_goal: "Nearby goal; state not confirmed",
  nearby_correction: "Nearby score correction", state_mismatch: "Nearby event conflicts with inference",
  time_only: "Time proximity only", unmatched: "No nearby same-match event",
  temporally_associated: "Temporally associated", no_nearby_same_match_event: "No nearby same-match event",
};
const clockGateLabels = {
  clock_88_plus: "88+ clock accepted", clock_pre_88: "Clock before minute 88",
  clock_unmapped: "Clock unmapped", clock_missing: "Clock missing", clock_malformed: "Clock malformed",
  clock_stale: "Clock stale", clock_not_live: "Clock not live", clock_final: "Clock final",
  clock_suspended: "Clock suspended", clock_abandoned: "Clock abandoned",
  clock_first_half: "First-half clock", clock_half_time: "Half-time clock",
  clock_pre_match: "Pre-match clock", clock_period_unusable: "Clock period unusable",
};
const latencyLabels = {
  feed_ingress_ms: "Feed ingress", feed_lag: "Feed ingress",
  decision_ms: "Decision", paper_entry_ms: "Paper entry", paper_entry: "Paper entry",
  order_arrival_ms: "Order arrival (K4)", order_arrival: "Order arrival (K4)",
  paper_exit_ms: "Paper exit", paper_exit: "Paper exit",
  match_response_ms: "Match-feed response", goal_provider_response: "Match-feed response",
  match_clock_age_ms: "Match-clock age", scheduler_lag_ms: "Scheduler lag",
};
function humanOutcome(value) { return outcomeLabels[value] || String(value || "Unknown outcome").replaceAll("_", " "); }
function humanExit(value) { return exitLabels[value] || String(value || "Unknown exit").replaceAll("_", " "); }
function humanAssociation(value) { return associationLabels[value] || String(value || "unmatched").replaceAll("_", " "); }
function humanClockGate(value) { return clockGateLabels[value] || String(value || "not recorded").replaceAll("_", " "); }
function humanStatus(value) {
  const text = String(value || "unknown").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function showToast(message, error = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 4500);
}
function recordClientError(component, error) {
  const now = Date.now() / 1000;
  const message = error instanceof Error ? error.message : String(error);
  const latest = clientErrors[0];
  if (!latest || latest.component !== component || latest.message !== message || now - latest.ts > 30) {
    clientErrors.unshift({ts: now, component, message});
    clientErrors.splice(20);
  }
  activeClientFaults.set(component, {ts: now, component, message});
  renderHealth();
  showToast(`${component}: ${message}`, true);
}
function clearClientFault(component) { activeClientFaults.delete(component); }
async function apiJson(path) {
  const component = `API ${path.split("?")[0]}`;
  try {
    const response = await fetch(path, {headers: {Accept: "application/json"}});
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try { detail = (await response.json()).detail || detail; }
      catch (parseError) { detail += `; response body was not JSON (${parseError.message})`; }
      throw new Error(detail);
    }
    const data = await response.json();
    clearClientFault(component);
    return data;
  } catch (error) {
    recordClientError(component, error);
    throw error;
  }
}

function activateTab(name, focus = false) {
  const safeName = byId(`panel-${name}`) ? name : "overview";
  document.querySelectorAll("[data-tab]").forEach(button => {
    const active = button.dataset.tab === safeName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    if (active && focus) button.focus();
  });
  document.querySelectorAll(".tab-panel").forEach(panel => { panel.hidden = panel.id !== `panel-${safeName}`; });
  history.replaceState(null, "", `#${safeName}`);
  if (safeName === "overview") requestAnimationFrame(renderEquity);
}
function initializeTabs() {
  document.querySelectorAll("[data-tab]").forEach(button => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const tabs = [...document.querySelectorAll("[data-tab]")];
      const current = tabs.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
        (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      activateTab(tabs[next].dataset.tab, true);
      event.preventDefault();
    });
  });
  document.querySelectorAll("[data-tab-target]").forEach(button => button.addEventListener("click", () => activateTab(button.dataset.tabTarget)));
  activateTab(location.hash.slice(1) || "overview");
}

function modeTag(status) {
  const badge = byId("mode-badge"), mode = status?.mode || "starting";
  badge.textContent = mode === "live" ? "Live paper" : mode === "demo" ? "Demo replay" : "Starting";
  badge.className = `status-pill ${mode === "live" ? "live" : mode === "demo" ? "demo" : "neutral"}`;
}
function healthCheckLabel(key) {
  return ({websocket: "Market stream", recorder: "Raw recorder", match_event_feed: "Match-event feed",
    paper_execution: "Paper execution", database: "Database", credentials: "Credentials",
    recent_backend_faults: "Backend faults", dashboard_websocket: "Dashboard live link"})[key] || key.replaceAll("_", " ");
}
// Banner keys come from engine.status().health.banner; keep the literal
// "ALL SYSTEMS GOOD" in JS so tests/test_frontend_contract.py can assert it.
const bannerClass = {
  all_systems_good: "healthy", evidence_not_ready: "collecting",
  latency_breach: "fault", attention_required: "fault",
};
const bannerTitle = {
  all_systems_good: "ALL SYSTEMS GOOD",
  evidence_not_ready: "Runtime healthy · paper evidence not ready",
  latency_breach: "Runtime healthy · execution latency breached",
  attention_required: "ATTENTION REQUIRED",
};
const bannerChip = {
  all_systems_good: "Healthy", evidence_not_ready: "Collecting evidence",
  latency_breach: "Latency breach", attention_required: "Fault visible",
};
function renderHealth() {
  const panel = byId("health-panel");
  if (!panel) return;
  const backend = state.status?.health;
  const checks = {...(backend?.checks || {})};
  checks.dashboard_websocket = {healthy: socketConnected, status: socketConnected ? "connected" : "disconnected"};
  const currentFaults = [...activeClientFaults.values()], rows = Object.entries(checks);
  const ready = state.hydrated && rows.length > 1;
  const runtimeOk = backend?.runtime_ok !== false;
  const clientFault = currentFaults.length > 0;
  // Banner state comes from backend when we have it; fall back to a conservative
  // classification while checking or when a client-side fault has fired.
  let banner = backend?.banner || "attention_required";
  if (!ready) banner = "checking";
  else if (clientFault && banner === "all_systems_good") banner = "attention_required";
  else if (clientFault && banner === "evidence_not_ready") banner = "attention_required";
  const kind = banner === "checking" ? "checking" : bannerClass[banner] || "fault";
  const title = banner === "checking" ? "Checking every connection" :
    (banner === "all_systems_good" ? "ALL SYSTEMS GOOD" : bannerTitle[banner] || "ATTENTION REQUIRED");
  const chip = banner === "checking" ? "Checking" : bannerChip[banner] || "Fault visible";
  panel.className = `health-panel ${kind}`;
  byId("health-title").textContent = title;
  byId("health-indicator").textContent = chip;
  const failed = rows.filter(([, check]) => !check.healthy).map(([key]) => healthCheckLabel(key));
  const backendText = backend?.banner_text || "";
  let summary;
  if (!ready) summary = "Waiting for the first complete status response.";
  else if (banner === "all_systems_good")
    summary = "Market stream, recorder, diagnostic feed, execution, database, and dashboard link report healthy; execution latency is within threshold.";
  else if (banner === "evidence_not_ready")
    summary = backendText || "Runtime is healthy but K4 order-arrival latency is still collecting samples.";
  else if (banner === "latency_breach")
    summary = backendText || "Runtime is healthy but K4 order-arrival p95 exceeds its 250 ms threshold.";
  else summary = `Current issue${failed.length + currentFaults.length === 1 ? "" : "s"}: ${[...failed, ...currentFaults.map(row => row.component)].join(", ") || "see recent errors"}.`;
  byId("health-summary").textContent = summary;
  byId("health-checks").innerHTML = rows.map(([key, check]) => {
    const bits = [];
    if (finite(check.p95_ms)) bits.push(`p95 ${check.p95_ms.toFixed(1)} ms`);
    if (finite(check.threshold_ms)) bits.push(`threshold ${check.threshold_ms.toFixed(0)} ms`);
    if (finite(check.n)) bits.push(`${integer(check.n)} samples`);
    const detail = bits.length ? `<br><small>${escapeHtml(bits.join(" · "))}</small>` : "";
    return `<div class="health-check ${check.healthy ? "good" : "bad"}"><span>${escapeHtml(healthCheckLabel(key))}</span><strong>${escapeHtml(humanStatus(check.status))}${detail}</strong></div>`;
  }).join("");
  const errors = [...currentFaults, ...clientErrors, ...(backend?.recent_errors || [])]
    .sort((a, b) => (b.ts || 0) - (a.ts || 0))
    .filter((row, index, all) => index === all.findIndex(other => other.component === row.component && other.message === row.message && other.ts === row.ts)).slice(0, 20);
  byId("error-count").textContent = String(errors.length);
  byId("error-list").innerHTML = errors.length ? errors.map(row => `<div class="error-row"><strong>${escapeHtml(row.component || "system")}</strong> · ${escapeHtml(fullDate(row.ts))}<br>${escapeHtml(row.message || "Unknown error")}</div>`).join("") : '<div class="empty-state">No errors or disconnects have been reported.</div>';
  if (errors.length && banner !== "all_systems_good") byId("error-details").open = true;
  const global = byId("global-health");
  global.className = `health-link ${kind}`;
  byId("global-health-text").textContent = banner === "checking" ? "Checking systems" :
    banner === "all_systems_good" ? "All systems good" :
    banner === "evidence_not_ready" ? "Evidence collecting" :
    banner === "latency_breach" ? "Latency breach" :
    `${errors.length + failed.length} issue${errors.length + failed.length === 1 ? "" : "s"}`;
}
function renderRuntime() {
  const status = state.status || {};
  modeTag(status);
  const marketConnected = String(status.ws || "").startsWith("connected") || status.ws === "demo";
  const eventCheck = status.health?.checks?.match_event_feed || {};
  const values = {
    "runtime-dashboard": [socketConnected ? "Connected" : "Disconnected", socketConnected],
    "runtime-market": [humanStatus(status.ws || "checking"), marketConnected],
    "runtime-event": [humanStatus(eventCheck.status || (status.goal_latency?.enabled ? "observing" : "diagnostic disabled")), eventCheck.healthy !== false],
    "runtime-event-poll": [finite(eventCheck.last_poll_ts) ? fullDate(eventCheck.last_poll_ts) : "Not supplied by provider", eventCheck.healthy !== false],
    "runtime-event-response": [finite(eventCheck.last_response_ms) ? `${eventCheck.last_response_ms.toFixed(1)} ms` : "Collecting", eventCheck.healthy !== false],
    "runtime-feed-lag": [finite(status.feed_lag_p50) ? `${status.feed_lag_p50.toFixed(1)} ms` : "Collecting", true],
    "runtime-matches": [integer(status.matches || 0), true], "runtime-recorded": [integer(status.recorded || 0), status.recorder?.healthy !== false],
    "runtime-uptime": [duration(status.uptime_s), true],
  };
  Object.entries(values).forEach(([id, [text, good]]) => { byId(id).textContent = text; byId(id).className = good ? "good" : "bad"; });
  killEnabled = Boolean(status.kill);
  byId("kill-button").classList.toggle("active", killEnabled);
  byId("kill-button").textContent = killEnabled ? "Kill switch engaged" : "Kill switch";
}

function sleeveCard(strategy, summary) {
  const positions = (state.trades.open || []).filter(row => row.strategy === strategy);
  const openMark = positions.reduce((sum, row) => sum + (finite(row.upnl) ? row.upnl : 0), 0);
  const gate = summary.evidence?.k2_ci || {}, status = gate.status || "COLLECTING";
  const description = strategy === "price_only_late_score" ?
    "Independent price-pattern sleeve. It infers a late score state without consuming the match feed." :
    "Original confirmed sweep sleeve with independent positions, lockouts, fills, and exits.";
  return `<article class="sleeve-card ${strategyClass(strategy)}"><div class="sleeve-top"><div><h3>${escapeHtml(strategyLabel(strategy))}</h3><p>${escapeHtml(description)}</p></div><span class="tag ${status === "PASS" ? "good" : status === "FAIL" ? "bad" : "warn"}">${escapeHtml(humanStatus(status))}</span></div><div class="sleeve-net ${(summary.net || 0) >= 0 ? "positive" : "negative"}">${money(summary.net || 0)}</div><p class="muted">Closed realized net after $${Math.abs(summary.fees || 0).toFixed(2)} in recorded fees</p><div class="metric-grid"><div class="metric-cell"><span>Closed trades</span><strong>${integer(summary.closed || 0)}</strong></div><div class="metric-cell"><span>Win rate</span><strong>${summary.closed ? percent(summary.win_pct) : "Collecting"}</strong></div><div class="metric-cell"><span>Net / trade</span><strong>${summary.closed ? money(summary.net_per_fill || 0) : "Collecting"}</strong></div><div class="metric-cell"><span>Open positions</span><strong>${integer(summary.open || 0)}</strong></div><div class="metric-cell"><span>Open mark</span><strong class="${openMark >= 0 ? "positive" : "negative"}">${money(openMark)}</strong></div><div class="metric-cell"><span>Partial realized</span><strong>${money(summary.open_partial_realized_net || 0)}</strong></div><div class="metric-cell"><span>95% interval</span><strong>${summary.ci95 ? `${money(summary.ci95[0])} to ${money(summary.ci95[1])}` : "Collecting"}</strong></div><div class="metric-cell"><span>Study samples</span><strong>${integer(gate.n_signals || 0)} / ${integer(gate.needed || 50)}</strong></div></div></article>`;
}
function renderSleeves() {
  const sleeves = state.stats.sleeves || {gate_a: state.stats.combined || state.stats, price_only_late_score: {}};
  byId("sleeve-cards").innerHTML = ["gate_a", "price_only_late_score"].map(key => sleeveCard(key, sleeves[key] || {})).join("");
  const combined = state.stats.combined || state.stats || {};
  byId("combined-summary").innerHTML = `<strong class="${(combined.net || 0) >= 0 ? "positive" : "negative"}">${money(combined.net || 0)}</strong>combined closed net · ${integer(combined.closed || 0)} trades · ${integer(combined.open || 0)} open`;
}
function rawDetails(label, value) { return `<details class="raw-details"><summary>${escapeHtml(label)}</summary><pre>${safeJson(value)}</pre></details>`; }
function providerClock(normalized) {
  if (normalized?.provider_clock) return normalized.provider_clock;
  if (finite(normalized?.provider_minute)) return `${normalized.provider_minute}${finite(normalized.provider_stoppage) ? `+${normalized.provider_stoppage}` : ""}′`;
  return "Clock not supplied";
}
function clockStampBlock(stamp) {
  // Persisted match-clock snapshot for one signal or trade. Legacy rows and
  // unusable stamps stay honest — no fake minute is invented.
  if (!stamp) return "";
  const clock = providerClock(stamp);
  const usable = stamp.usable_for_88_gate === true;
  const reason = stamp.unusable_reason || (usable ? null : "not recorded");
  const age = finite(stamp.age_ms) ? relativeMs(stamp.age_ms) : "age unavailable";
  const precision = stamp.precision || "unknown precision";
  const status = stamp.provider_status || "status unavailable";
  const legacy = reason === "legacy_signal_recorded_before_clock_stamps";
  const kind = usable ? "good" : legacy ? "warn" : "warn";
  const outcome = stamp.gate_outcome || (usable ? "clock_88_plus" : reason ? `clock_${reason}` : null);
  const chip = outcome ? `<span class="tag ${usable ? "good" : "warn"}">${escapeHtml(humanClockGate(outcome))}</span>` : "";
  return `<div class="clock-stamp ${kind}"><div><span>Match clock</span><strong>${escapeHtml(clock)}</strong></div><div><span>Age</span><strong>${escapeHtml(age)}</strong></div><div><span>Precision</span><strong>${escapeHtml(precision.replaceAll("_", " "))}</strong></div><div><span>Provider status</span><strong>${escapeHtml(status)}</strong></div>${chip ? `<div><span>88+ gate</span><strong>${chip}</strong></div>` : ""}${legacy ? '<div class="clock-legacy">Legacy signal recorded before clock stamps were persisted.</div>' : ""}${!usable && !legacy && reason ? `<div class="clock-reason">${escapeHtml(reason.replaceAll("_", " "))}</div>` : ""}</div>`;
}
function tradeHighBlock(trade) {
  // Executable held-side best bid after entry (never mid/ask/last/settlement).
  if (trade.max_executable_bid == null && trade.mfe_c == null) return "";
  const high = trade.max_executable_bid, highTs = trade.max_executable_bid_ts;
  const mfe = trade.mfe_c, secondsAfter = trade.high_after_entry_s;
  const mfeNet = finite(mfe) ? mfe : (finite(high) && finite(trade.entry_px) ? Math.max(0, high - trade.entry_px) : null);
  return `<div class="trade-high"><div><span>Max executable bid</span><strong>${cents(high)}</strong></div><div><span>MFE from entry</span><strong>${finite(mfeNet) ? cents(mfeNet) : "Not observed"}</strong></div><div><span>UTC time of high</span><strong>${escapeHtml(fullDate(highTs))}</strong></div><div><span>After entry</span><strong>${finite(secondsAfter) ? duration(secondsAfter) : "not derived"}</strong></div></div>`;
}
function lossPath(trade) {
  // Losing trades expose entry → high → exit so the missed profit is legible.
  if ((trade.net || 0) >= 0) return "";
  const high = trade.max_executable_bid;
  const highSuffix = finite(trade.high_after_entry_s) ? `<br><small>${escapeHtml(duration(trade.high_after_entry_s))} after entry</small>` : "";
  return `<div class="loss-path"><div><span>Entry</span><strong>${cents(trade.entry_px)}</strong></div><div><span>Executable high</span><strong>${cents(high)}${highSuffix}</strong></div><div><span>Exit</span><strong>${cents(trade.exit_px)}<br><small>${escapeHtml(humanExit(trade.exit_reason))}</small></strong></div></div>`;
}

function outcomeGroup(row, isTrade = false) {
  if (isTrade) return (row.net || 0) >= 0 ? "profitable" : "loss";
  return ["filled", "queued", "executing"].includes(row.outcome) ? "executed" : "declined";
}
function rowTimestamp(row) { return row.entry_ts || row.local_ts || row.ts || row.observed_ts || 0; }
function gateOutcome(row) {
  // Trade or signal → normalized 88-gate outcome (e.g. "clock_88_plus", "clock_pre_88").
  const inference = row.trigger?.price_only_inference || {};
  const trigger = inference.match_clock_gate?.outcome;
  if (trigger) return trigger;
  const stamp = row.match_clock || {};
  if (stamp.gate_outcome) return stamp.gate_outcome;
  const outcome = row.outcome || row.exit_reason || "";
  if (typeof outcome === "string" && outcome.startsWith("sleeve_clock_")) return outcome.slice("sleeve_".length);
  if (stamp.usable_for_88_gate === true) return "clock_88_plus";
  if (stamp.unusable_reason) return "clock_" + stamp.unusable_reason;
  return null;
}
function passesFilters(row, isTrade = false) {
  const searchable = [row.display_game, row.display_contract, row.display_leg, row.market, row.event, row.series, row.outcome, row.exit_reason,
    row.matched_event?.canonical_event?.human_label, row.matched_event?.canonical_event?.provider_description].join(" ").toLowerCase();
  if (filters.query && !searchable.includes(filters.query.toLowerCase())) return false;
  if (filters.strategy !== "all" && row.strategy !== filters.strategy) return false;
  if (filters.match !== "all" && (row.display_game || row.event) !== filters.match) return false;
  if (filters.result !== "all" && outcomeGroup(row, isTrade) !== filters.result) return false;
  const association = row.matched_event?.association || "unmatched";
  if (filters.association !== "all" && association !== filters.association) return false;
  if (filters.gate && filters.gate !== "all") {
    const outcome = gateOutcome(row);
    if (filters.gate === "accepted" && outcome !== "clock_88_plus") return false;
    if (filters.gate === "declined" && (!outcome || outcome === "clock_88_plus")) return false;
    if (filters.gate !== "accepted" && filters.gate !== "declined" && outcome !== filters.gate) return false;
  }
  if (filters.period !== "all") {
    const seconds = timestampSeconds(rowTimestamp(row)), cutoff = Date.now() / 1000 - Number(filters.period) * 86400;
    if (seconds == null || seconds < cutoff) return false;
  }
  return true;
}
function filterOptions() {
  const rows = [...(state.trades.closed || []), ...(state.signals || [])];
  return [...new Set(rows.map(row => row.display_game || row.event).filter(Boolean))].sort()
    .map(name => `<option value="${escapeHtml(name)}" ${filters.match === name ? "selected" : ""}>${escapeHtml(name)}</option>`).join("");
}
function filterMarkup(scope, visible, total) {
  const gateOptions = [
    ["accepted", "88+ gate accepted"], ["declined", "88+ gate declined"],
    ...Object.entries(clockGateLabels),
  ];
  return `<label class="filter-field">Search<input type="search" data-filter-field="query" value="${escapeHtml(filters.query)}" placeholder="Team, contract, event"></label><label class="filter-field">Sleeve<select data-filter-field="strategy"><option value="all">Both sleeves</option><option value="gate_a" ${filters.strategy === "gate_a" ? "selected" : ""}>Gate A</option><option value="price_only_late_score" ${filters.strategy === "price_only_late_score" ? "selected" : ""}>Price-only</option></select></label><label class="filter-field">Match<select data-filter-field="match"><option value="all">All matches</option>${filterOptions()}</select></label><label class="filter-field">Result<select data-filter-field="result"><option value="all">All results</option><option value="executed" ${filters.result === "executed" ? "selected" : ""}>Executed signals</option><option value="declined" ${filters.result === "declined" ? "selected" : ""}>Declined signals</option><option value="profitable" ${filters.result === "profitable" ? "selected" : ""}>Profitable trades</option><option value="loss" ${filters.result === "loss" ? "selected" : ""}>Losing trades</option></select></label><label class="filter-field">88+ clock gate<select data-filter-field="gate"><option value="all">Any gate outcome</option>${gateOptions.map(([key, label]) => `<option value="${key}" ${filters.gate === key ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select></label><label class="filter-field">Event link<select data-filter-field="association"><option value="all">All associations</option>${Object.entries(associationLabels).map(([key, label]) => `<option value="${key}" ${filters.association === key ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select></label><label class="filter-field">Period<select data-filter-field="period"><option value="all">All recorded time</option><option value="1" ${filters.period === "1" ? "selected" : ""}>Last 24 hours</option><option value="7" ${filters.period === "7" ? "selected" : ""}>Last 7 days</option><option value="30" ${filters.period === "30" ? "selected" : ""}>Last 30 days</option></select></label><button class="reset-filter" data-reset-filters type="button">Reset</button><div class="filter-count">Showing ${integer(visible)} of ${integer(total)} ${scope}. Filters apply to both audit views.</div>`;
}
function bindFilters() {
  document.querySelectorAll("[data-filter-field]").forEach(control => control.addEventListener(control.type === "search" ? "input" : "change", () => {
    filters[control.dataset.filterField] = control.value;
    renderTrades(); renderSignals();
    if (control.type === "search") {
      document.querySelectorAll('[data-filter-field="query"]').forEach(other => { if (other !== control) other.value = control.value; });
      updateFilterCounts();
    } else {
      renderFilters();
    }
  }));
  document.querySelectorAll("[data-reset-filters]").forEach(button => button.addEventListener("click", () => {
    Object.assign(filters, {query: "", strategy: "all", match: "all", result: "all", association: "all", gate: "all", period: "all"});
    renderFilters(); renderTrades(); renderSignals();
  }));
}
function updateFilterCounts() {
  const tradeCount = (state.trades.closed || []).filter(row => passesFilters(row, true)).length;
  const signalCount = (state.signals || []).filter(row => passesFilters(row, false)).length;
  byId("trade-filters").querySelector(".filter-count").textContent = `Showing ${integer(tradeCount)} of ${integer((state.trades.closed || []).length)} trades. Filters apply to both audit views.`;
  byId("signal-filters").querySelector(".filter-count").textContent = `Showing ${integer(signalCount)} of ${integer((state.signals || []).length)} signals. Filters apply to both audit views.`;
}
function renderFilters() {
  const trades = (state.trades.closed || []).filter(row => passesFilters(row, true));
  const signals = (state.signals || []).filter(row => passesFilters(row, false));
  byId("trade-filters").innerHTML = filterMarkup("trades", trades.length, (state.trades.closed || []).length);
  byId("signal-filters").innerHTML = filterMarkup("signals", signals.length, (state.signals || []).length);
  bindFilters();
}

function triggerSummary(trigger) {
  const observed = trigger?.observed || {}, threshold = trigger?.thresholds || {}, pieces = [];
  if (finite(observed.log_odds_displacement)) pieces.push(`log-odds moved ${observed.log_odds_displacement.toFixed(2)} (minimum ${threshold.min_log_odds_displacement})`);
  if (finite(observed.distinct_price_levels)) pieces.push(`${integer(observed.distinct_price_levels)} distinct price levels`);
  if (finite(observed.contracts)) pieces.push(`${integer(observed.contracts)} contracts`);
  return pieces.length ? pieces.join(" · ") : "The provider did not supply the complete trigger measurements.";
}
function eventAssociationBlock(matched) {
  const event = matched?.canonical_event;
  if (!event) return `<div class="event-association unmatched"><div class="event-summary-line"><h4>No nearby same-match event</h4><span class="tag">Unmatched</span></div><p class="event-caveat">The diagnostic feed did not record a score change inside the fixed ±${integer(matched?.window_s || state.config.event_match_window_s || 20)} second audit window. This does not prove that no football event occurred.</p></div>`;
  const association = matched.association || "time_only";
  const occurrence = finite(matched.occurrence_minus_signal_ms) ? `${relativeMs(matched.occurrence_minus_signal_ms)} ${matched.occurrence_minus_signal_ms >= 0 ? "after" : "before"} the signal` : "provider occurrence time unavailable";
  const received = finite(matched.event_minus_signal_ms) ? `${relativeMs(matched.event_minus_signal_ms)} ${matched.event_minus_signal_ms >= 0 ? "after" : "before"} the signal` : "feed receipt time unavailable";
  return `<div class="event-association ${association === "state_mismatch" ? "unmatched" : ""}"><div class="event-summary-line"><div><h4>${escapeHtml(event.human_label || "Match event observed")}</h4><p class="event-description">${escapeHtml(event.provider_description || "Provider supplied a score change without a narrative.")}</p></div><span class="tag ${association === "state_consistent" ? "good" : association === "state_mismatch" ? "bad" : "info"}">${escapeHtml(humanAssociation(association))}</span></div><p class="event-caveat">${escapeHtml(providerClock(event))}${event.scorer ? ` · ${escapeHtml(event.scorer)}` : ""}${event.event_method === "penalty" ? " · Penalty" : ""} · provider occurrence ${escapeHtml(occurrence)} · feed observed ${escapeHtml(received)}. Causation is not established.</p></div>`;
}
function tradeTimeline(trade) {
  const timing = trade.timing || {}, matched = trade.matched_event || {}, signalTs = timing.signal_received_ts;
  const rows = [
    {ts: signalTs, title: "Market signal received", detail: triggerSummary(trade.trigger)},
    {ts: timing.paper_order_arrival_ts, title: "Paper order arrived", detail: finite(timing.paper_order_arrival_delay_ms) ? `${timing.paper_order_arrival_delay_ms.toFixed(1)} ms arrival delay` : "Arrival delay not supplied", trade: true},
    {ts: timing.entry_ts || trade.entry_ts, title: "Paper entry filled", detail: `${cents(trade.entry_px)} · ${integer(trade.size)} contracts`, trade: true},
    {ts: matched.provider_occurrence_ts, title: "Provider event occurrence", detail: matched.canonical_event?.provider_description || "Provider timestamp only"},
    {ts: timing.exit_ts || timing.settlement_ts || trade.exit_ts, title: timing.settlement_ts ? "Market settled" : "Paper position exited", detail: `${humanExit(trade.exit_reason)} · ${cents(trade.exit_px)}`, trade: true},
    {ts: matched.event_observed_ts, title: "Match feed received", detail: `Observation ${matched.observation_id || "not supplied"}`},
  ].filter(row => timestampSeconds(row.ts) != null).sort((a, b) => timestampSeconds(a.ts) - timestampSeconds(b.ts));
  return `<div class="timeline">${rows.map(row => `<div class="timeline-step ${row.trade ? "trade-step" : ""}"><i class="timeline-dot"></i><span>${escapeHtml(row.title)}</span><time datetime="${escapeHtml(fullDate(row.ts))}">${escapeHtml(fullDate(row.ts))}<br><small>${escapeHtml(relativeTo(row.ts, signalTs))} · ${escapeHtml(row.detail)}</small></time></div>`).join("")}</div>`;
}
function tradeCard(trade) {
  const matched = trade.matched_event || {};
  const matchTime = shortDate(trade.entry_ts || trade.timing?.entry_ts || trade.exit_ts);
  const clock = clockStampBlock(trade.match_clock);
  const highBlock = tradeHighBlock(trade);
  const loss = lossPath(trade);
  return `<article class="trade-story ${strategyClass(trade.strategy)}"><div class="trade-core"><div class="trade-title-row"><div><h3>${escapeHtml(trade.display_game || "Unnamed match")}</h3><p class="contract-line">${escapeHtml(trade.display_contract || trade.display_leg || "Unnamed contract")} · ${escapeHtml(leagueName(trade.series))} · ${escapeHtml(matchTime)}</p></div><span class="tag ${strategyClass(trade.strategy) === "price" ? "info" : "warn"}">${escapeHtml(strategyLabel(trade.strategy))}</span></div><div class="sleeve-net ${(trade.net || 0) >= 0 ? "positive" : "negative"}">${money(trade.net || 0)}</div><p class="muted">Net after ${money(-(trade.fees || 0))} fees</p><div class="trade-economics"><div><span>Entry → exit</span><strong>${cents(trade.entry_px)} → ${cents(trade.exit_px)}</strong></div><div><span>Contracts</span><strong>${integer(trade.size)}</strong></div><div><span>Gross</span><strong>${money(trade.gross || 0)}</strong></div></div>${loss}${clock}${highBlock}<div class="trade-reason"><strong>${escapeHtml(humanExit(trade.exit_reason))}</strong><br>${escapeHtml(triggerSummary(trade.trigger))}</div></div><div class="trade-audit">${eventAssociationBlock(matched)}${tradeTimeline(trade)}<div class="audit-footer">${rawDetails("Raw identifiers and audit record", {trade_id: trade.id, signal_id: trade.signal_id, market: trade.market, event: trade.event, series: trade.series, trigger: trade.trigger, schedule_window: trade.schedule_window, matched_event: matched, match_clock: trade.match_clock, max_executable_bid: trade.max_executable_bid, max_executable_bid_ts: trade.max_executable_bid_ts, mfe_c: trade.mfe_c, high_after_entry_s: trade.high_after_entry_s})}</div></div></article>`;
}
function renderTrades() {
  const rows = (state.trades.closed || []).filter(row => passesFilters(row, true));
  byId("trade-list").innerHTML = rows.length ? rows.map(tradeCard).join("") : '<div class="empty-state">No closed paper trades match these filters.</div>';
}
function renderFeaturedTrades() {
  const linked = [...(state.trades.closed || [])].sort((a, b) => Number(Boolean(b.matched_event?.canonical_event)) - Number(Boolean(a.matched_event?.canonical_event)) || (b.exit_ts || 0) - (a.exit_ts || 0)).slice(0, 4);
  byId("featured-trade-list").innerHTML = linked.length ? linked.map(trade => {
    const event = trade.matched_event?.canonical_event;
    return `<div class="compact-story"><div><strong>${escapeHtml(trade.display_game || "Unnamed match")} · ${escapeHtml(trade.display_contract || trade.display_leg)}</strong><p>${escapeHtml(strategyLabel(trade.strategy))} · ${escapeHtml(humanExit(trade.exit_reason))}</p><p class="event-note">${escapeHtml(event?.human_label || "No nearby same-match event")}${event?.event_method === "penalty" ? " · Penalty" : ""}</p></div><strong class="${(trade.net || 0) >= 0 ? "positive" : "negative"}">${money(trade.net || 0)}</strong></div>`;
  }).join("") : '<div class="empty-state">Explained trades appear after a paper position closes.</div>';
}

function decisionSentence(signal) {
  const outcome = humanOutcome(signal.outcome), inference = signal.trigger?.price_only_inference || {};
  if (signal.outcome === "sleeve_outside_window") {
    const window = signal.schedule_window || {}, seconds = window.seconds_to_expected_expiration;
    const placement = finite(seconds) ? (seconds >= 0 ? `${duration(seconds)} before expected expiration` : `${duration(Math.abs(seconds))} after expected expiration`) : "an unavailable distance from expected expiration";
    return `${outcome}. Price-only recorded the market move but declined because the expected-expiration proxy placed it ${placement}; the configured window is ${window.window_start_before_expiration_min ?? state.config.sleeve_start_before_expiry_min ?? 2} minutes before to ${window.window_end_after_expiration_min ?? state.config.sleeve_after_expiry_min ?? 12} minutes after. This schedule proxy is not a verified live match clock.`;
  }
  if (signal.strategy === "price_only_late_score" && inference.inferred_state) {
    return `${outcome}. The independent price sleeve inferred ${inference.inferred_state === "equal_score_0" ? "an equal score" : "a one-goal lead"} from a coherent three-contract probability shift; it did not read the match feed.`;
  }
  return `${outcome}. ${triggerSummary(signal.trigger)}`;
}
function thresholdItems(signal) {
  const observed = signal.trigger?.observed || {}, thresholds = signal.trigger?.thresholds || {};
  const rows = [["Log-odds shift", observed.log_odds_displacement, thresholds.min_log_odds_displacement, "min"], ["Price levels", observed.distinct_price_levels, thresholds.min_distinct_price_levels, "min"], ["Contracts", observed.contracts, thresholds.min_contracts, "min"], ["Sibling lag", observed.sibling_confirmation_lag_ms, thresholds.sibling_confirmation_window_ms, "max"]];
  const sleeve = signal.trigger?.price_only_inference || {};
  if (finite(sleeve.target_gain_pp)) rows.push(["Triplet gain", sleeve.target_gain_pp * 100, (sleeve.leg_role === "draw" ? state.config.sleeve_min_draw_gain_pp : state.config.sleeve_min_team_gain_pp) * 100, "min"]);
  if (finite(sleeve.sibling_explanation)) rows.push(["Sibling outflow (%)", sleeve.sibling_explanation * 100, (state.config.sleeve_min_explained || .85) * 100, "min"]);
  if (finite(sleeve.target_spread_c)) rows.push(["Target spread", sleeve.target_spread_c, state.config.sleeve_max_spread_c || 6, "max"]);
  return rows.slice(0, 8).map(([label, actual, required, direction]) => {
    const supplied = finite(actual) && finite(required), pass = supplied && (direction === "max" ? actual <= required : actual >= required);
    const ratio = supplied ? (direction === "max" ? required / Math.max(actual, required, .001) : actual / Math.max(required, actual, .001)) : 0;
    return `<div class="threshold-item"><div class="threshold-head"><span>${escapeHtml(label)}</span><strong>${supplied ? `${Number(actual).toFixed(2)} / ${direction} ${Number(required).toFixed(2)}` : "Not supplied by provider"}</strong></div><div class="threshold-track"><div class="threshold-fill ${pass ? "" : "fail"}" style="width:${Math.max(supplied ? 8 : 0, ratio * 100).toFixed(1)}%"></div></div></div>`;
  }).join("");
}
function signalCard(signal) {
  const matched = signal.matched_event || {}, group = outcomeGroup(signal), event = matched.canonical_event;
  const gate = gateOutcome(signal);
  const gateChip = gate ? `<span>·</span><span class="tag ${gate === "clock_88_plus" ? "good" : "warn"}">${escapeHtml(humanClockGate(gate))}</span>` : "";
  return `<article class="decision-card"><div class="story-summary"><div><h3>${escapeHtml(signal.display_game || "Unnamed match")}</h3><p class="contract-line">${escapeHtml(signal.display_contract || signal.display_leg || "Unnamed contract")} · ${escapeHtml(strategyLabel(signal.strategy))}</p></div><span class="tag ${group === "executed" ? "good" : signal.outcome === "execution_error" ? "bad" : "warn"}">${escapeHtml(humanOutcome(signal.outcome))}</span></div><p class="decision-sentence">${escapeHtml(decisionSentence(signal))}</p><div class="decision-meta"><span>${escapeHtml(fullDate(signal.local_ts))}</span><span>·</span><span>${escapeHtml(humanAssociation(matched.association || "unmatched"))}</span>${event ? `<span>·</span><span>${escapeHtml(event.human_label)} at ${escapeHtml(providerClock(event))}</span>` : ""}${gateChip}</div>${clockStampBlock(signal.match_clock)}<div class="threshold-grid">${thresholdItems(signal)}</div>${signal.outcome === "sleeve_outside_window" ? `<div class="schedule-warning"><strong>Timing-proxy rejection:</strong> expected market expiration ${escapeHtml(signal.schedule_window?.expected_expiration_time || "not supplied")}; ${escapeHtml(signal.schedule_window?.assumption || "Schedule proxy only; not a verified live match clock.")}</div>` : ""}${rawDetails("Raw identifiers, thresholds, and event audit", {signal_id: signal.id, market: signal.market, event: signal.event, series: signal.series, outcome: signal.outcome, trigger: signal.trigger, schedule_window: signal.schedule_window, matched_event: matched, match_clock: signal.match_clock})}</article>`;
}
function renderSignals() {
  const rows = (state.signals || []).filter(row => passesFilters(row, false));
  byId("signal-list").innerHTML = rows.length ? rows.map(signalCard).join("") : '<div class="empty-state">No detector decisions match these filters.</div>';
}
function renderTimingDiagnostics() {
  const paired = (state.signals || []).filter(signal =>
    signal.strategy === "price_only_late_score" && signal.matched_event?.canonical_event &&
    finite(signal.schedule_window?.seconds_to_expected_expiration)
  ).sort((a, b) => (b.local_ts || 0) - (a.local_ts || 0));
  if (!paired.length) {
    byId("timing-diagnostics").innerHTML = '<div class="empty-state">No price-only signal has both a nearby provider event and an auditable expiration proxy yet.</div>';
    return;
  }
  const grouped = {};
  paired.forEach(signal => {
    const group = grouped[signal.series] ||= {values: [], outside: 0};
    group.values.push(signal.schedule_window.seconds_to_expected_expiration);
    if (!signal.schedule_window.inside_configured_window) group.outside += 1;
  });
  const summaries = Object.entries(grouped).map(([series, group]) => {
    const values = [...group.values].sort((a, b) => a - b);
    const median = values[Math.floor(values.length / 2)];
    return `<div class="timing-summary"><span>${escapeHtml(leagueName(series))}</span><strong>Median ${escapeHtml(duration(Math.abs(median)))} ${median >= 0 ? "before" : "after"} expected expiration</strong><p>${integer(values.length)} paired observation${values.length === 1 ? "" : "s"} · ${integer(group.outside)} outside configured window</p></div>`;
  }).join("");
  const cases = paired.slice(0, 12).map(signal => {
    const event = signal.matched_event.canonical_event;
    const seconds = signal.schedule_window.seconds_to_expected_expiration;
    const distance = seconds >= 0 ? `${duration(seconds)} before expected expiration` : `${duration(Math.abs(seconds))} after expected expiration`;
    const inside = signal.schedule_window.inside_configured_window;
    return `<div class="timing-row"><div><strong>${escapeHtml(signal.display_game || "Unnamed match")}</strong><p>${escapeHtml(event.human_label)}${event.provider_description ? ` · ${escapeHtml(event.provider_description)}` : ""}</p></div><span>${escapeHtml(providerClock(event))}</span><span>${escapeHtml(distance)}<br><small>Schedule proxy, not live match time</small></span><span class="tag ${inside ? "good" : "warn"}">${inside ? "Inside window" : escapeHtml(humanOutcome(signal.outcome))}</span></div>`;
  }).join("");
  byId("timing-diagnostics").innerHTML = `<div class="timing-summary-grid">${summaries}</div><p class="timing-subheading">Recent paired observations</p>${cases}`;
}
function renderEvents() {
  const rows = state.events || [];
  byId("event-list").innerHTML = rows.length ? rows.map(event => {
    const normalized = event.normalized_event || {}, correction = String(normalized.canonical_type || "").startsWith("score_correction");
    return `<article class="event-row"><time datetime="${escapeHtml(fullDate(event.observed_ts))}">${escapeHtml(fullDate(event.observed_ts))}<br>Feed receipt</time><div><strong>${escapeHtml(event.display_game || "Unnamed match")}</strong><p>${escapeHtml(normalized.human_label || humanStatus(event.change_kind))} · ${escapeHtml(providerClock(normalized))}${normalized.scorer ? ` · ${escapeHtml(normalized.scorer)}` : ""}</p><p>${escapeHtml(normalized.provider_description || "Provider supplied a score change without a narrative.")}</p></div><span class="tag ${correction ? "warn" : normalized.event_method === "penalty" ? "info" : "good"}">${correction ? "Correction" : normalized.event_method === "penalty" ? "Penalty" : "Score event"}</span>${rawDetails("Raw provider observation", {observation_id: event.id, event: event.event, milestone_id: event.milestone_id, normalized_event: normalized, raw_provider_payload: event.detail?.live_data})}</article>`;
  }).join("") : '<div class="empty-state">No score change has been recorded by the diagnostic feed.</div>';
}

function showChartTooltip(event, html) {
  const tooltip = byId("chart-tooltip");
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  tooltip.style.left = `${Math.max(8, Math.min(window.innerWidth - 280, event.clientX + 12))}px`;
  tooltip.style.top = `${Math.max(8, Math.min(window.innerHeight - 120, event.clientY + 12))}px`;
}
function hideChartTooltip() { byId("chart-tooltip").hidden = true; }
function renderEquity() {
  const holder = byId("equity-chart");
  const definitions = [{key: "combined", name: "Combined", color: "#38d996"}, {key: "gate_a", name: "Gate A", color: "#f2bd62"}, {key: "price_only_late_score", name: "Price-only", color: "#56b8ff"}];
  byId("equity-legend").innerHTML = definitions.map(item => `<button class="legend-button series-${item.key} ${visibleEquitySeries.has(item.key) ? "active" : ""}" data-equity-series="${item.key}" type="button"><i class="legend-swatch"></i>${item.name}</button>`).join("");
  document.querySelectorAll("[data-equity-series]").forEach(button => button.addEventListener("click", () => {
    const key = button.dataset.equitySeries;
    if (visibleEquitySeries.has(key) && visibleEquitySeries.size > 1) visibleEquitySeries.delete(key); else visibleEquitySeries.add(key);
    renderEquity();
  }));
  const series = definitions.map(item => ({...item, values: state.equity[item.key] || []})).filter(item => visibleEquitySeries.has(item.key));
  const points = series.flatMap(item => item.values);
  if (!points.length) { holder.innerHTML = '<div class="empty-state">The chart begins after the first paper position closes.</div>'; return; }
  const width = Math.max(620, holder.clientWidth || 820), height = 400, left = 62, right = 20, top = 20, pnlBottom = 268, ddTop = 300, bottom = 368;
  let minX = Math.min(...points.map(point => point[0])), maxX = Math.max(...points.map(point => point[0]));
  if (minX === maxX) { minX -= 1000; maxX += 1000; }
  let minY = Math.min(0, ...points.map(point => point[1])), maxY = Math.max(0, ...points.map(point => point[1]));
  if (minY === maxY) { minY -= 1; maxY += 1; }
  const yPad = Math.max((maxY - minY) * .08, .5);
  minY -= yPad; maxY += yPad;
  const x = value => left + (value - minX) / (maxX - minX) * (width - left - right);
  const y = value => top + (maxY - value) / (maxY - minY) * (pnlBottom - top);
  const combined = state.equity.combined || [];
  let peak = -Infinity;
  const drawdown = combined.map(point => { peak = Math.max(peak, point[1]); return [point[0], point[1] - peak]; });
  const minDrawdown = Math.min(-.01, ...drawdown.map(point => point[1]));
  const ddy = value => ddTop + (0 - value) / (0 - minDrawdown) * (bottom - ddTop);
  const yTicks = Array.from({length: 5}, (_, index) => minY + (maxY - minY) * index / 4);
  const xTicks = Array.from({length: 4}, (_, index) => minX + (maxX - minX) * index / 3);
  const lines = series.map(item => {
    const d = item.values.map((point, index) => `${index ? "L" : "M"}${x(point[0]).toFixed(1)},${y(point[1]).toFixed(1)}`).join(" ");
    return `<path class="chart-line" d="${d}" stroke="${item.color}"/>${item.values.map((point, index) => `<circle class="chart-point" tabindex="0" data-chart-tip="${escapeHtml(`${item.name}<br>${fullDate(point[0])}<br>${money(point[1])} cumulative net`)}" cx="${x(point[0]).toFixed(1)}" cy="${y(point[1]).toFixed(1)}" r="${index === item.values.length - 1 ? 4 : 3}" fill="${item.color}"/>`).join("")}`;
  }).join("");
  const ddPath = drawdown.length ? `${drawdown.map((point, index) => `${index ? "L" : "M"}${x(point[0]).toFixed(1)},${ddy(point[1]).toFixed(1)}`).join(" ")} L${x(drawdown[drawdown.length - 1][0]).toFixed(1)},${ddTop} L${x(drawdown[0][0]).toFixed(1)},${ddTop} Z` : "";
  holder.innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${yTicks.map(value => `<line class="chart-grid" x1="${left}" x2="${width - right}" y1="${y(value)}" y2="${y(value)}"/><text class="chart-axis" x="${left - 9}" y="${y(value) + 4}" text-anchor="end">${money(value)}</text>`).join("")}<line class="chart-zero" x1="${left}" x2="${width - right}" y1="${y(0)}" y2="${y(0)}"/>${lines}<line class="chart-divider" x1="${left}" x2="${width - right}" y1="${ddTop - 14}" y2="${ddTop - 14}"/><text class="chart-axis" x="${left}" y="${ddTop - 18}">Combined drawdown</text>${ddPath ? `<path class="drawdown-area" d="${ddPath}"/>` : ""}<text class="chart-axis" x="${left - 9}" y="${ddTop + 4}" text-anchor="end">$0</text><text class="chart-axis" x="${left - 9}" y="${bottom}" text-anchor="end">${money(minDrawdown)}</text>${xTicks.map((value, index) => `<line class="chart-grid" x1="${x(value)}" x2="${x(value)}" y1="${top}" y2="${bottom}"/><text class="chart-axis" x="${x(value)}" y="${height - 8}" text-anchor="${index === 0 ? "start" : index === 3 ? "end" : "middle"}">${escapeHtml(shortDate(value))}</text>`).join("")}</svg>`;
  holder.querySelectorAll("[data-chart-tip]").forEach(point => {
    point.addEventListener("pointerenter", event => showChartTooltip(event, point.dataset.chartTip));
    point.addEventListener("pointermove", event => showChartTooltip(event, point.dataset.chartTip));
    point.addEventListener("pointerleave", hideChartTooltip);
    point.addEventListener("focus", () => showChartTooltip({clientX: window.innerWidth / 2, clientY: 100}, point.dataset.chartTip));
    point.addEventListener("blur", hideChartTooltip);
  });
}

function renderAssociationChart() {
  const groups = {};
  (state.trades.closed || []).forEach(trade => { const key = trade.matched_event?.association || "unmatched"; const group = groups[key] ||= {count: 0, net: 0}; group.count += 1; group.net += trade.net || 0; });
  const rows = Object.entries(groups).sort((a, b) => b[1].count - a[1].count), max = Math.max(1, ...rows.map(([, value]) => value.count));
  byId("association-chart").innerHTML = rows.length ? rows.map(([key, value]) => `<div class="bar-row"><span class="bar-label">${escapeHtml(humanAssociation(key))}</span><div class="bar-track"><div class="bar-fill ${value.net >= 0 ? "positive" : "negative"}" style="width:${Math.max(3, value.count / max * 100).toFixed(1)}%"></div></div><strong class="bar-value ${value.net >= 0 ? "positive" : "negative"}">${integer(value.count)} · ${money(value.net)}</strong></div>`).join("") : '<div class="empty-state">Association coverage appears after trades close.</div>';
}
function renderExitChart() {
  const groups = {};
  (state.trades.closed || []).forEach(trade => { const group = groups[trade.exit_reason] ||= {count: 0, net: 0}; group.count += 1; group.net += trade.net || 0; });
  const rows = Object.entries(groups).sort((a, b) => b[1].count - a[1].count), max = Math.max(1, ...rows.map(([, value]) => value.count));
  byId("exit-chart").innerHTML = rows.length ? rows.map(([reason, value]) => `<div class="bar-row"><span class="bar-label">${escapeHtml(humanExit(reason))}</span><div class="bar-track"><div class="bar-fill ${value.net >= 0 ? "positive" : "negative"}" style="width:${Math.max(3, value.count / max * 100).toFixed(1)}%"></div></div><strong class="bar-value ${value.net >= 0 ? "positive" : "negative"}">${integer(value.count)} · ${money(value.net)}</strong></div>`).join("") : '<div class="empty-state">Exit reasons appear after positions close.</div>';
}

function leagueRows() {
  return Object.entries(state.stats.leagues || {}).map(([series, league]) => {
    const bucket = leagueSleeve === "combined" ? league : league.sleeves?.[leagueSleeve] || {};
    return {series, name: league.display_name || leagueName(series), n: bucket.n || 0, net: bucket.net || 0, gross: bucket.gross || 0, fees: bucket.fees || 0, win_pct: bucket.win_pct || 0, net_per_trade: bucket.net_per_trade || 0};
  }).filter(row => row.n > 0);
}
function renderLeagues() {
  const sort = byId("league-sort").value;
  const rows = leagueRows().sort((a, b) => sort === "name" ? a.name.localeCompare(b.name) : (b[sort] || 0) - (a[sort] || 0));
  const maxAbs = Math.max(1, ...rows.map(row => Math.abs(row.net)));
  byId("league-chart").innerHTML = rows.length ? rows.slice(0, 14).map(row => `<div class="league-bar-row"><span class="bar-label"><strong>${escapeHtml(row.name)}</strong><small>${integer(row.n)} trade${row.n === 1 ? "" : "s"}</small></span><div class="signed-track"><div class="signed-fill ${row.net >= 0 ? "positive" : "negative"}" style="width:${Math.max(2, Math.abs(row.net) / maxAbs * 50).toFixed(1)}%"></div></div><strong class="bar-value ${row.net >= 0 ? "positive" : "negative"}">${money(row.net)}</strong></div>`).join("") : '<div class="empty-state">No closed trades for this sleeve.</div>';
  const totals = rows.reduce((sum, row) => ({n: sum.n + row.n, net: sum.net + row.net, gross: sum.gross + row.gross, fees: sum.fees + row.fees}), {n: 0, net: 0, gross: 0, fees: 0});
  byId("league-table").innerHTML = rows.length ? `<table><thead><tr><th>League</th><th class="numeric">Trades</th><th class="numeric">Win rate</th><th class="numeric">Gross</th><th class="numeric">Fees</th><th class="numeric">Net</th><th class="numeric">Net / trade</th></tr></thead><tbody>${rows.map(row => `<tr><td data-label="League"><strong>${escapeHtml(row.name)}</strong>${row.n < 10 ? '<br><span class="sample-warning">Small sample</span>' : ""}</td><td data-label="Trades" class="numeric">${integer(row.n)}</td><td data-label="Win rate" class="numeric">${percent(row.win_pct)}</td><td data-label="Gross" class="numeric">${money(row.gross)}</td><td data-label="Fees" class="numeric">${money(-row.fees)}</td><td data-label="Net" class="numeric ${row.net >= 0 ? "positive" : "negative"}">${money(row.net)}</td><td data-label="Net / trade" class="numeric">${money(row.net_per_trade)}</td></tr>`).join("")}<tr><td data-label="League"><strong>Reconciled total</strong></td><td data-label="Trades" class="numeric"><strong>${integer(totals.n)}</strong></td><td data-label="Win rate" class="numeric">—</td><td data-label="Gross" class="numeric">${money(totals.gross)}</td><td data-label="Fees" class="numeric">${money(-totals.fees)}</td><td data-label="Net" class="numeric ${totals.net >= 0 ? "positive" : "negative"}"><strong>${money(totals.net)}</strong></td><td data-label="Net / trade" class="numeric">${totals.n ? money(totals.net / totals.n) : "$0.00"}</td></tr></tbody></table>` : '<div class="empty-state">No closed trades for this sleeve.</div>';
}
function renderMatches() {
  const query = byId("market-search").value.trim().toLowerCase();
  const rows = [...(state.matches || [])].filter(match => [match.title, match.series, leagueName(match.series)].join(" ").toLowerCase().includes(query)).sort((a, b) => Number(b.late) - Number(a.late));
  byId("match-list").innerHTML = rows.length ? rows.map(match => {
    const legs = Object.entries(match.legs || {}).sort(([, a], [, b]) => String(a.display_name).localeCompare(String(b.display_name)));
    return `<details class="market-row"><summary><div class="market-name"><strong>${escapeHtml(match.title || "Unnamed match")}</strong><span>${escapeHtml(leagueName(match.series))}</span></div><span class="market-time">Scheduled expiration<br>${escapeHtml(match.close_time ? fullDate(Date.parse(match.close_time) / 1000) : "Not supplied")}</span><div class="contract-strip">${legs.map(([, leg]) => `<div class="contract-price"><span>${escapeHtml(leg.display_name || "Contract")}</span><strong>${cents(leg.last)}</strong></div>`).join("")}</div><span class="tag ${match.late ? "warn" : "info"}">${match.late ? "Late proxy" : "Watching"}</span></summary><div class="market-detail">${legs.map(([ticker, leg]) => `<div class="contract-detail"><strong>${escapeHtml(leg.display_name || "Contract")}</strong><div class="price-grid"><div><span>Bid</span><strong>${cents(leg.bid)}</strong></div><div><span>Ask</span><strong>${cents(leg.ask)}</strong></div><div><span>Last</span><strong>${cents(leg.last)}</strong></div></div>${rawDetails("Raw identifiers", {ticker, event: match.event, series: match.series})}</div>`).join("")}</div></details>`;
  }).join("") : '<div class="empty-state">No watched matches match this search.</div>';
}
function renderPositions() {
  const rows = state.trades.open || [];
  byId("position-list").innerHTML = rows.length ? rows.map(position => `<div class="position-row"><div class="metric-inline"><strong>${escapeHtml(position.display_game || "Unnamed match")}</strong><span class="tag ${strategyClass(position.strategy) === "price" ? "info" : "warn"}">${escapeHtml(strategyLabel(position.strategy))}</span></div><p>${escapeHtml(position.display_contract || position.display_leg || "Unnamed contract")} · ${integer(position.remaining ?? position.size)} contracts remaining · entry ${cents(position.entry_px)} · best bid ${cents(position.best_bid)}</p><strong class="${(position.upnl || 0) >= 0 ? "positive" : "negative"}">${money(position.upnl || 0)} open mark</strong>${rawDetails("Raw identifiers", {trade_id: position.id, signal_id: position.signal_id, market: position.market, event: position.event, series: position.series})}</div>`).join("") : '<div class="empty-state">No open paper positions.</div>';
}
const LATENCY_STATE_TAG = {PASS: "good", BREACH: "bad", COLLECTING: "warn", STALE: "warn", INVALID: "bad"};
// Canonical kinds are named in tests/test_health.py / app/store.py; render every
// kind including COLLECTING and STALE so K4 is never hidden by a global LIMIT.
const CANONICAL_LATENCY_KINDS = [
  "order_arrival_ms", "paper_entry_ms", "paper_exit_ms", "decision_ms",
  "feed_ingress_ms", "match_response_ms", "match_clock_age_ms", "scheduler_lag_ms",
];
function latencyLabel(kind) { return latencyLabels[kind] || String(kind || "unknown").replaceAll("_", " "); }
function renderLatency() {
  const rows = CANONICAL_LATENCY_KINDS.map(kind => ({kind, label: latencyLabel(kind), ...(state.latency[kind] || {kind, state: "COLLECTING", n: 0})}));
  const barable = rows.filter(row => finite(row.p95) || finite(row.p50));
  const max = Math.max(1, ...barable.map(row => row.p95 ?? row.p50 ?? 0));
  byId("latency-chart").innerHTML = barable.length ? barable.map(row => `<div class="bar-row"><span class="bar-label">${escapeHtml(row.label)}<br><small>${integer(row.n)} samples · ${escapeHtml(row.state || "COLLECTING")}</small></span><div class="bar-track"><div class="bar-fill ${row.state === "BREACH" ? "negative" : ""}" style="width:${Math.max(2, (row.p95 ?? row.p50 ?? 0) / max * 100).toFixed(1)}%"></div></div><strong class="bar-value">p50 ${finite(row.p50) ? row.p50.toFixed(1) : "—"} ms<br><small>p95 ${finite(row.p95) ? row.p95.toFixed(1) : "collecting"}</small></strong></div>`).join("") : '<div class="empty-state">Latency samples are still collecting.</div>';
  byId("latency-table").innerHTML = `<table><thead><tr><th>Metric</th><th class="numeric">n</th><th class="numeric">p50 ms</th><th class="numeric">p95 ms</th><th class="numeric">max ms</th><th class="numeric">Age</th><th class="numeric">Threshold</th><th>State</th></tr></thead><tbody>${rows.map(row => `<tr><td data-label="Metric"><strong>${escapeHtml(row.label)}</strong></td><td data-label="n" class="numeric">${integer(row.n || 0)}</td><td data-label="p50 ms" class="numeric">${finite(row.p50) ? row.p50.toFixed(1) : "—"}</td><td data-label="p95 ms" class="numeric ${row.state === "BREACH" ? "negative" : ""}">${finite(row.p95) ? row.p95.toFixed(1) : "—"}</td><td data-label="max ms" class="numeric">${finite(row.max) ? row.max.toFixed(1) : "—"}</td><td data-label="Age" class="numeric">${finite(row.age_s) ? duration(row.age_s) : "—"}</td><td data-label="Threshold" class="numeric">${finite(row.threshold_ms) ? `${row.threshold_ms.toFixed(0)} ms` : "—"}</td><td data-label="State"><span class="tag ${LATENCY_STATE_TAG[row.state] || "warn"}">${escapeHtml(row.state || "COLLECTING")}</span></td></tr>`).join("")}</tbody></table>`;
}
function renderClockCoverage() {
  const coverage = state.status?.clock_coverage || {};
  const cells = [
    ["Watched matches", coverage.watched],
    ["Mapped to live clock", coverage.mapped],
    ["Clock present", coverage.clock_present],
    ["Clock fresh", coverage.clock_fresh],
    ["88+ gate misses", coverage.clock_gate_candidate_misses],
  ];
  byId("clock-coverage").innerHTML = cells.map(([label, value]) => `<div class="metric-cell"><span>${escapeHtml(label)}</span><strong>${integer(value || 0)}</strong></div>`).join("");
  const faults = coverage.faults || [], mapping = coverage.mapping_errors || [];
  if (!faults.length && !mapping.length) {
    byId("clock-faults").innerHTML = '<div class="empty-state">No live-clock faults reported for watched matches.</div>';
    return;
  }
  byId("clock-faults").innerHTML = [
    ...faults.map(row => `<div class="clock-fault-row warn"><strong>${escapeHtml(row.event || "unknown event")}</strong><span>${escapeHtml(String(row.reason || "unknown reason").replaceAll("_", " "))}</span></div>`),
    ...mapping.map(row => `<div class="clock-fault-row bad"><strong>${escapeHtml(row.event || "unknown event")}</strong><span>Mapping error: ${escapeHtml(String(row.error || "unknown"))}</span></div>`),
  ].join("");
}
function renderEvidence() {
  const cards = [];
  Object.entries(state.stats.sleeves || {}).forEach(([strategy, summary]) => Object.entries(summary.evidence || {}).forEach(([key, gate]) => {
    if (!gate || !["k1_fill_integrity", "k2_ci"].includes(key)) return;
    const current = key === "k1_fill_integrity" ? gate.n_fills || 0 : gate.n_signals || 0, needed = gate.needed || (key === "k1_fill_integrity" ? 25 : 50);
    cards.push(`<div class="evidence-item"><div><strong>${escapeHtml(strategyLabel(strategy))} · ${key === "k1_fill_integrity" ? "Fill integrity" : "Confidence interval"}</strong><p>${integer(current)} of ${integer(needed)} required${gate.ci ? ` · ${money(gate.ci[0])} to ${money(gate.ci[1])}` : ""}</p></div><span class="tag ${gate.status === "PASS" ? "good" : gate.status === "FAIL" ? "bad" : "warn"}">${escapeHtml(humanStatus(gate.status || "collecting"))}</span></div>`);
  }));
  byId("evidence-list").innerHTML = cards.length ? cards.join("") : '<div class="empty-state">Evidence gates are still collecting.</div>';
}
function humanizeActivity(text) {
  let result = String(text || "");
  (state.matches || []).forEach(match => Object.entries(match.legs || {}).forEach(([ticker, leg]) => { result = result.replaceAll(ticker, `${match.title} / ${leg.display_name}`); }));
  return result;
}
function renderActivity() {
  const rows = state.activity || [];
  byId("activity-list").innerHTML = rows.length ? rows.map(row => `<div class="activity-row"><span>${escapeHtml(fullDate(row.ts))}</span><span class="tag ${row.kind === "error" ? "bad" : "info"}">${escapeHtml(humanStatus(row.kind))}</span><span>${escapeHtml(humanizeActivity(row.text))}</span></div>`).join("") : '<div class="empty-state">No service activity has been recorded.</div>';
}
function renderAll() {
  renderHealth(); renderRuntime(); renderSleeves(); renderFilters(); renderTrades(); renderSignals(); renderEvents();
  renderTimingDiagnostics();
  renderEquity(); renderAssociationChart(); renderExitChart(); renderFeaturedTrades(); renderPositions(); renderLeagues();
  renderMatches(); renderLatency(); renderClockCoverage(); renderEvidence(); renderActivity();
}

async function refreshAll() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  const requests = [["status", "/api/status"], ["config", "/api/config"], ["matches", "/api/matches"], ["trades", "/api/trades?limit=500"], ["signals", "/api/signals?limit=500"], ["stats", "/api/stats"], ["events", "/api/goal-latency?limit=200"], ["latency", "/api/latency"], ["equity", "/api/equity"], ["activity", "/api/eventlog?limit=100"], ["clocks", "/api/match-clocks?limit=200"]];
  try {
    const results = await Promise.allSettled(requests.map(([, path]) => apiJson(path)));
    results.forEach((result, index) => {
      if (result.status === "fulfilled") {
        const key = requests[index][0];
        state[key] = key === "equity" && Array.isArray(result.value) ? {combined: result.value, gate_a: [], price_only_late_score: []} : result.value;
      }
    });
    state.hydrated = true;
    renderAll();
  } finally { refreshInFlight = false; }
}
function scheduleRefresh(delay = 250) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshAll().catch(error => recordClientError("dashboard refresh", error)), delay);
}
function connectWebSocket() {
  clearTimeout(reconnectTimer);
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  try { socket = new WebSocket(`${protocol}//${location.host}/ws`); }
  catch (error) { recordClientError("Dashboard WebSocket", error); reconnectTimer = setTimeout(connectWebSocket, 2500); return; }
  socket.onopen = () => { socketConnected = true; clearClientFault("Dashboard WebSocket"); renderHealth(); renderRuntime(); };
  socket.onmessage = event => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "hello" || message.type === "stats") { state.status = message.status || state.status; state.stats = message.stats || state.stats; renderHealth(); renderRuntime(); renderSleeves(); renderEvidence(); }
      if (message.type === "signal") playSignalTone();
      if (["signal", "signal_update", "trade_open", "trade_partial", "trade_close", "system_error"].includes(message.type)) {
        if (message.type === "system_error" && message.error) { clientErrors.unshift(message.error); clientErrors.splice(20); }
        scheduleRefresh();
      }
      if (message.type === "prices") scheduleRefresh(900);
      if (message.type === "log") scheduleRefresh(500);
    } catch (error) { recordClientError("Dashboard WebSocket message", error); }
  };
  socket.onerror = () => recordClientError("Dashboard WebSocket", new Error("live dashboard connection reported an error"));
  socket.onclose = event => { socketConnected = false; recordClientError("Dashboard WebSocket", new Error(`disconnected (code ${event.code || "unknown"})`)); renderRuntime(); reconnectTimer = setTimeout(connectWebSocket, 2500); };
}

async function adminPost(path, body) {
  let token = sessionStorage.getItem("footballbot_admin_token");
  if (!token) token = window.prompt("Admin token");
  if (!token) return null;
  sessionStorage.setItem("footballbot_admin_token", token);
  try {
    const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Admin-Token": token}, body: body == null ? null : JSON.stringify(body)});
    if (!response.ok) {
      const payload = await response.json().catch(error => ({detail: `Unable to read error response: ${error.message}`}));
      throw new Error(payload.detail || `${response.status} ${response.statusText}`);
    }
    clearClientFault(`Admin ${path}`);
    return await response.json();
  } catch (error) { sessionStorage.removeItem("footballbot_admin_token"); recordClientError(`Admin ${path}`, error); return null; }
}
// One in-flight export at a time per scope; the cancel button targets it.
const activeExports = new Map(); // scope → {jobId, controller}
const EXPORT_ORIGINAL_LABEL = {
  "export-button": "Download audit data",
  "export-audit-button": "Download audit bundle",
  "export-full-button": "Prepare full raw handoff",
};
function exportProgressText(job) {
  const bits = [];
  if (job.total_bytes) bits.push(`${formatBytes(job.processed_bytes || 0)} / ${formatBytes(job.total_bytes)}`);
  if (job.total_segments) bits.push(`${integer(job.processed_segments || 0)} / ${integer(job.total_segments)} raw segments`);
  return bits.join(" · ");
}
function setExportButtonsDisabled(disabled) {
  document.querySelectorAll("[data-export-scope]").forEach(button => { button.disabled = disabled; });
}
function resetExportButtonLabels() {
  Object.entries(EXPORT_ORIGINAL_LABEL).forEach(([id, label]) => { const button = byId(id); if (button) button.textContent = label; });
}
async function timedFetch(url, options = {}, timeoutMs = 15000) {
  // Local AbortController per request so a hung status poll cannot wedge the loop.
  // Caller-supplied signal (for user-driven cancel) still takes precedence.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`request timed out after ${timeoutMs} ms`)), timeoutMs);
  const upstream = options.signal;
  if (upstream) upstream.addEventListener("abort", () => controller.abort(upstream.reason), {once: true});
  try {
    return await fetch(url, {...options, signal: controller.signal});
  } finally { clearTimeout(timer); }
}
async function readExportError(response) {
  try { return (await response.json()).detail || `${response.status} ${response.statusText}`; }
  catch (error) { return `${response.status} ${response.statusText} (body unreadable: ${error.message})`; }
}
async function downloadExport(scope = "audit") {
  scope = scope === "full" ? "full" : "audit";
  let token = sessionStorage.getItem("footballbot_admin_token");
  if (!token) token = window.prompt(scope === "full" ?
    "Admin token for the full raw handoff (multi-GB export, cancellable)" :
    "Admin token for the audit bundle download");
  if (!token) return;
  sessionStorage.setItem("footballbot_admin_token", token);
  if (activeExports.has(scope)) { showToast(`A ${scope} export is already running; cancel it first.`, true); return; }
  const controller = new AbortController();
  const cancelButton = byId("export-cancel-button");
  const progress = byId("export-progress"), errorLabel = byId("export-error");
  progress.hidden = true; errorLabel.hidden = true;
  setExportButtonsDisabled(true);
  if (scope === "full" && cancelButton) cancelButton.hidden = false;
  const headers = {"X-Admin-Token": token};
  const buttonId = scope === "full" ? "export-full-button" : "export-audit-button";
  const button = byId(buttonId) || byId("export-button");
  const scopeLabel = scope === "full" ? "Full raw handoff" : "Audit bundle";
  try {
    const started = await timedFetch(`/api/export/prepare?scope=${encodeURIComponent(scope)}`, {method: "POST", headers, signal: controller.signal}, 30000);
    if (started.status === 401) { sessionStorage.removeItem("footballbot_admin_token"); throw new Error("admin token was rejected (401)"); }
    if (!started.ok) throw new Error(await readExportError(started));
    let job = await started.json();
    activeExports.set(scope, {jobId: job.job_id, controller});
    const beganAt = Date.now();
    while (job.status === "queued" || job.status === "preparing") {
      progress.hidden = false;
      const elapsed = Math.max(1, Math.round((Date.now() - beganAt) / 1000));
      const detail = exportProgressText(job);
      button.textContent = `${scopeLabel}: ${humanStatus(job.status)} · ${elapsed}s`;
      progress.textContent = `${scopeLabel}: ${humanStatus(job.status)} for ${elapsed}s${detail ? ` · ${detail}` : ""}.`;
      await new Promise(resolve => setTimeout(resolve, 2000));
      if (controller.signal.aborted) throw new Error("cancelled");
      const polled = await timedFetch(`/api/export/jobs/${encodeURIComponent(job.job_id)}`, {headers, signal: controller.signal}, 15000);
      if (polled.status === 401) { sessionStorage.removeItem("footballbot_admin_token"); throw new Error("admin token was rejected (401)"); }
      if (!polled.ok) throw new Error(await readExportError(polled));
      job = await polled.json();
    }
    if (job.status === "cancelled") throw new Error("cancelled");
    if (job.status !== "ready") throw new Error(job.error || `${scopeLabel} preparation failed (${job.status})`);
    const anchor = document.createElement("a");
    anchor.href = `/api/export/jobs/${encodeURIComponent(job.job_id)}/download`;
    // rely on the job-scoped HttpOnly cookie set by /api/export/prepare
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    clearClientFault(`Study export ${scope}`);
    showToast(`${scopeLabel} download started (${formatBytes(job.bytes)}).`);
    if (scope === "full") refreshRawSegments();
  } catch (error) {
    const message = error?.message || String(error);
    if (message === "cancelled") showToast(`${scopeLabel} cancelled.`);
    else {
      // Only invalidate the token on an explicit auth failure. Transient 5xx,
      // timeouts, and network errors must not force the operator to re-prompt.
      if (/\b401\b/.test(message)) sessionStorage.removeItem("footballbot_admin_token");
      errorLabel.hidden = false; errorLabel.textContent = `${scopeLabel}: ${message}`;
      recordClientError(`Study export ${scope}`, error);
    }
  } finally {
    activeExports.delete(scope);
    if (!activeExports.size) { setExportButtonsDisabled(false); resetExportButtonLabels(); if (cancelButton) cancelButton.hidden = true; }
  }
}
async function cancelActiveExport() {
  // Prefer cancelling the full job (audit bundles finish in seconds).
  const scope = activeExports.has("full") ? "full" : activeExports.keys().next().value;
  if (!scope) return;
  const active = activeExports.get(scope);
  const token = sessionStorage.getItem("footballbot_admin_token");
  active.controller.abort(new Error("cancelled"));
  if (token && active.jobId) {
    try { await fetch(`/api/export/jobs/${encodeURIComponent(active.jobId)}/cancel`, {method: "POST", headers: {"X-Admin-Token": token}}); }
    catch (error) { recordClientError(`Study export ${scope} cancel`, error); }
  }
}
async function refreshRawSegments() {
  // Lists immutable recorder segments so operators can pull one file at a time
  // instead of waiting on the multi-GB full bundle.
  const holder = byId("raw-segment-list");
  if (!holder) return;
  const token = sessionStorage.getItem("footballbot_admin_token");
  if (!token) { holder.innerHTML = '<div class="empty-state">Enter the admin token in a download button to list raw segments.</div>'; return; }
  try {
    const response = await timedFetch("/api/export/raw", {headers: {"X-Admin-Token": token}, credentials: "same-origin"}, 15000);
    if (response.status === 401) { sessionStorage.removeItem("footballbot_admin_token"); holder.innerHTML = '<div class="empty-state">Admin token was rejected; enter it again.</div>'; return; }
    if (!response.ok) throw new Error(await readExportError(response));
    const payload = await response.json();
    const segments = payload.segments || [];
    if (!segments.length) { holder.innerHTML = '<div class="empty-state">No raw recorder segments recorded yet.</div>'; return; }
    holder.innerHTML = segments.slice(0, 60).map(row => `<div class="raw-segment-row"><span><strong>${escapeHtml(row.name || "segment")}</strong></span><span>${escapeHtml(formatBytes(row.bytes))}</span><a href="/api/export/raw/${encodeURIComponent(row.name)}">Download</a></div>`).join("");
  } catch (error) { recordClientError("Raw segment listing", error); }
}
function playSignalTone() {
  if (!soundEnabled) return;
  try {
    const context = new (window.AudioContext || window.webkitAudioContext)(), oscillator = context.createOscillator(), gain = context.createGain();
    oscillator.connect(gain); gain.connect(context.destination); oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(330, context.currentTime); oscillator.frequency.exponentialRampToValueAtTime(520, context.currentTime + .18);
    gain.gain.setValueAtTime(.08, context.currentTime); gain.gain.exponentialRampToValueAtTime(.001, context.currentTime + .35);
    oscillator.start(); oscillator.stop(context.currentTime + .35);
  } catch (error) { recordClientError("Audio notification", error); }
}

initializeTabs();
byId("kill-button").addEventListener("click", async () => { const result = await adminPost("/api/kill", {on: !killEnabled}); if (result) scheduleRefresh(0); });
byId("sound-button").addEventListener("click", () => { soundEnabled = !soundEnabled; byId("sound-button").textContent = soundEnabled ? "Sound on" : "Sound off"; if (soundEnabled) playSignalTone(); });
document.querySelectorAll("[data-export-scope]").forEach(button => button.addEventListener("click", () => {
  const scope = button.dataset.exportScope || "audit";
  downloadExport(scope).catch(error => recordClientError(`Study export ${scope}`, error));
}));
byId("export-cancel-button")?.addEventListener("click", () => cancelActiveExport().catch(error => recordClientError("Study export cancel", error)));
byId("market-search").addEventListener("input", renderMatches);
byId("league-sort").addEventListener("change", renderLeagues);
document.querySelectorAll("[data-league-sleeve]").forEach(button => button.addEventListener("click", () => { leagueSleeve = button.dataset.leagueSleeve; document.querySelectorAll("[data-league-sleeve]").forEach(other => other.classList.toggle("active", other === button)); renderLeagues(); }));
if (window.ResizeObserver) new ResizeObserver(() => { if (!byId("panel-overview").hidden) renderEquity(); }).observe(byId("equity-chart"));
window.addEventListener("hashchange", () => activateTab(location.hash.slice(1) || "overview"));
refreshAll().catch(error => recordClientError("Initial dashboard load", error));
connectWebSocket();
setInterval(() => refreshAll().catch(error => recordClientError("Scheduled dashboard refresh", error)), 30000);
