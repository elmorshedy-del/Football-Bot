"use strict";

const byId = id => document.getElementById(id);
const state = {
  status: null,
  config: {},
  stats: {},
  matches: [],
  trades: {open: [], closed: []},
  signals: [],
  events: [],
  latency: {},
  equity: {combined: [], gate_a: [], price_only_late_score: []},
  activity: [],
  hydrated: false,
};
const clientErrors = [];
const activeClientFaults = new Map();
let socket = null;
let socketConnected = false;
let reconnectTimer = null;
let refreshTimer = null;
let refreshInFlight = false;
let soundEnabled = false;
let killEnabled = false;
let toastTimer = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeJson(value) {
  return escapeHtml(JSON.stringify(value, null, 2));
}

function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function money(value) {
  const n = finite(value) ? value : 0;
  return `${n >= 0 ? "+" : "−"}$${Math.abs(n).toFixed(2)}`;
}

function cents(value) {
  return finite(value) ? `${value.toFixed(1)}¢` : "—";
}

function integer(value) {
  return finite(value) ? Math.round(value).toLocaleString() : "—";
}

function percent(value) {
  return finite(value) ? `${value.toFixed(1)}%` : "—";
}

function fullDate(timestamp) {
  if (!finite(timestamp)) return "Not recorded";
  const date = new Date(timestamp > 1e12 ? timestamp : timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "Invalid timestamp";
  return date.toISOString().replace("T", " ").replace("Z", " UTC");
}

function duration(seconds) {
  if (!finite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)} seconds`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours}h ${minutes}m`;
}

function strategyLabel(strategy) {
  return strategy === "price_only_late_score" ? "Price-only late-score" :
    strategy === "gate_a" ? "Gate A" : "Detector episode";
}

function strategyClass(strategy) {
  return strategy === "price_only_late_score" ? "price" : "gate";
}

const outcomeLabels = {
  filled: "Paper order filled",
  queued: "Paper order queued",
  executing: "Checking executable depth",
  rejected_cap: "Rejected: executable price exceeded cap",
  no_book: "Rejected: no valid arrival order book",
  killed: "Rejected: kill switch active",
  expired: "Rejected: market expired before arrival",
  unsupported_fee: "Rejected: fee schedule could not be verified",
  unconfirmed: "Ignored: no coherent sibling confirmation",
  not_late: "Ignored: outside Gate A late window",
  strategy_lockout: "Ignored: this sleeve is in its re-entry lockout",
  sleeve_outside_window: "Ignored: outside the minute-88 study window",
  sleeve_no_baseline: "Ignored: no timed triplet baseline",
  sleeve_stale_baseline: "Ignored: triplet baseline was stale",
  sleeve_stale_triplet_leg: "Ignored: one match contract was stale",
  sleeve_incoherent_sibling_rise: "Ignored: sibling prices did not reallocate coherently",
  sleeve_insufficient_triplet_shift: "Ignored: normalized probability shift was too small",
  sleeve_weak_post_state: "Ignored: inferred post-event state was too weak",
  sleeve_weak_triplet_coherence: "Ignored: sibling outflow explained too little of the move",
  sleeve_wide_spread: "Ignored: spread was too wide",
  sleeve_not_triplet: "Ignored: match did not have exactly three contracts",
  sleeve_incomplete_book: "Ignored: a contract order book was incomplete",
  sleeve_ambiguous_draw_leg: "Ignored: draw contract could not be identified",
  sleeve_not_rising_leg: "Ignored: target contract was not rising",
  execution_error: "Execution adapter error",
};

const exitLabels = {
  target: "Profit target reached",
  timeout: "Gate A time limit",
  sleeve_timeout: "Price-only time limit",
  sleeve_profit_lock: "Trailing profit lock",
  sleeve_scratch: "Fee-aware scratch exit",
  sleeve_oscillation: "Oscillation exit",
  sleeve_reversal: "Fast price reversal",
  stop: "Configured stop",
  settle: "Market settlement",
  flatten: "Manual flatten",
  kill: "Kill switch flatten",
};

const consistencyLabels = {
  equalizer_consistent: "Consistent with an equalizer",
  one_goal_lead_consistent: "Consistent with a one-goal lead",
  correction_or_reversal: "Provider recorded a correction or reversal",
  state_mismatch: "Nearby event does not match the inferred state",
  goal_consistent_state_unknown: "Nearby goal; inferred state not confirmable",
  time_match_only: "Time proximity only",
};

function humanOutcome(value) {
  return outcomeLabels[value] || String(value || "Unknown outcome").replaceAll("_", " ");
}

function humanExit(value) {
  return exitLabels[value] || String(value || "Unknown exit").replaceAll("_", " ");
}

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

function clearClientFault(component) {
  activeClientFaults.delete(component);
}

async function apiJson(path) {
  const component = `API ${path.split("?")[0]}`;
  try {
    const response = await fetch(path, {headers: {Accept: "application/json"}});
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch (parseError) {
        detail += `; response body was not JSON (${parseError.message})`;
      }
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

function modeTag(status) {
  const badge = byId("mode-badge");
  const mode = status?.mode || "starting";
  badge.textContent = mode === "live" ? "Live paper" : mode === "demo" ? "Demo replay" : "Starting";
  badge.className = `status-pill ${mode === "live" ? "live" : mode === "demo" ? "demo" : "neutral"}`;
}

function healthCheckLabel(key) {
  return ({
    websocket: "Market WebSocket",
    recorder: "Raw recorder",
    match_event_feed: "Match-event diagnostic",
    paper_execution: "Paper execution",
    database: "Database",
    credentials: "Credentials",
    recent_backend_faults: "Recent backend faults",
    dashboard_websocket: "Dashboard live link",
  })[key] || key.replaceAll("_", " ");
}

function renderHealth() {
  const panel = byId("health-panel");
  const backend = state.status?.health;
  const checks = {...(backend?.checks || {})};
  checks.dashboard_websocket = {
    healthy: socketConnected,
    status: socketConnected ? "connected" : "disconnected",
  };
  const currentFaults = [...activeClientFaults.values()];
  const checkRows = Object.entries(checks);
  const ready = state.hydrated && checkRows.length > 1;
  const allHealthy = ready && checkRows.every(([, check]) => check.healthy) && currentFaults.length === 0;
  panel.className = `health-panel ${!ready ? "checking" : allHealthy ? "healthy" : "fault"}`;
  byId("health-title").textContent = !ready ? "Checking every connection" :
    allHealthy ? "ALL SYSTEMS GOOD" : "ATTENTION REQUIRED";
  byId("health-indicator").textContent = !ready ? "Checking" : allHealthy ? "Healthy" : "Fault visible";
  const failed = checkRows.filter(([, check]) => !check.healthy).map(([key]) => healthCheckLabel(key));
  byId("health-summary").textContent = !ready ? "Waiting for the first complete status response." :
    allHealthy ? "Market stream, recorder, diagnostic feed, execution, database, and dashboard link report healthy." :
    `Current issue${failed.length + currentFaults.length === 1 ? "" : "s"}: ${[
      ...failed, ...currentFaults.map(row => row.component),
    ].join(", ") || "see recent errors"}.`;
  byId("health-checks").innerHTML = checkRows.map(([key, check]) => `
    <div class="health-check ${check.healthy ? "good" : "bad"}">
      <span>${escapeHtml(healthCheckLabel(key))}</span>
      <strong>${escapeHtml(humanStatus(check.status))}</strong>
    </div>`).join("");
  const backendErrors = backend?.recent_errors || [];
  const errors = [...currentFaults, ...clientErrors, ...backendErrors]
    .sort((a, b) => (b.ts || 0) - (a.ts || 0))
    .filter((row, index, rows) => index === rows.findIndex(other =>
      other.component === row.component && other.message === row.message && other.ts === row.ts))
    .slice(0, 20);
  byId("error-count").textContent = String(errors.length);
  byId("error-list").innerHTML = errors.length ? errors.map(row => `
    <div class="error-row"><strong>${escapeHtml(row.component || "system")}</strong> ·
      ${escapeHtml(fullDate(row.ts))}<br>${escapeHtml(row.message || "Unknown error")}</div>`).join("") :
    '<div class="empty-state">No errors or disconnects have been reported.</div>';
  if (errors.length && !allHealthy) byId("error-details").open = true;
}

function renderRuntime() {
  const status = state.status || {};
  modeTag(status);
  const marketConnected = String(status.ws || "").startsWith("connected") || status.ws === "demo";
  const eventStatus = status.health?.checks?.match_event_feed?.status ||
    (status.goal_latency?.enabled ? "observing" : "diagnostic_disabled");
  const eventCheck = status.health?.checks?.match_event_feed || {};
  const values = {
    "runtime-dashboard": [socketConnected ? "Connected" : "Disconnected", socketConnected],
    "runtime-market": [humanStatus(status.ws || "checking"), marketConnected],
    "runtime-event": [humanStatus(eventStatus), status.health?.checks?.match_event_feed?.healthy !== false],
    "runtime-event-poll": [fullDate(eventCheck.last_poll_ts), eventCheck.healthy !== false],
    "runtime-event-response": [finite(eventCheck.last_response_ms) ?
      `${eventCheck.last_response_ms.toFixed(1)} ms` : "Collecting", eventCheck.healthy !== false],
    "runtime-feed-lag": [finite(status.feed_lag_p50) ? `${status.feed_lag_p50.toFixed(1)} ms` : "Collecting", true],
    "runtime-matches": [integer(status.matches || 0), true],
    "runtime-recorded": [integer(status.recorded || 0), status.recorder?.healthy !== false],
    "runtime-uptime": [duration(status.uptime_s), true],
  };
  Object.entries(values).forEach(([id, [text, good]]) => {
    const element = byId(id);
    element.textContent = text;
    element.className = good ? "good" : "bad";
  });
  killEnabled = Boolean(status.kill);
  byId("kill-button").classList.toggle("active", killEnabled);
  byId("kill-button").textContent = killEnabled ? "Kill switch engaged" : "Kill switch";
}

function sleeveCard(strategy, summary) {
  const positions = (state.trades.open || []).filter(row => row.strategy === strategy);
  const openMark = positions.reduce((sum, row) => sum + (finite(row.upnl) ? row.upnl : 0), 0);
  const evidence = summary.evidence || {};
  const k2 = evidence.k2_ci || {};
  const status = k2.status || "COLLECTING";
  const description = strategy === "price_only_late_score" ?
    "Price-only minute-88 inference with reversal, scratch, oscillation, trailing-profit, and timeout exits." :
    "Original confirmed sweep strategy with its own lockout, shadow depth, positions, and exits.";
  return `
    <article class="sleeve-card ${strategyClass(strategy)}">
      <div class="sleeve-name">
        <div><h3>${escapeHtml(strategyLabel(strategy))}</h3><p>${escapeHtml(description)}</p></div>
        <span class="tag ${status === "PASS" ? "good" : status === "FAIL" ? "bad" : "warn"}">${escapeHtml(humanStatus(status))}</span>
      </div>
      <div class="net-value ${(summary.net || 0) >= 0 ? "positive" : "negative"}">${money(summary.net || 0)}</div>
      <div class="secondary-text">Closed realized net after $${Math.abs(summary.fees || 0).toFixed(2)} in fees</div>
      <div class="metric-grid">
        <div class="metric-cell"><span class="metric-label">Closed trades</span><strong>${integer(summary.closed || 0)}</strong></div>
        <div class="metric-cell"><span class="metric-label">Win rate</span><strong>${summary.closed ? percent(summary.win_pct) : "Collecting"}</strong></div>
        <div class="metric-cell"><span class="metric-label">Net per trade</span><strong>${summary.closed ? money(summary.net_per_fill || 0) : "Collecting"}</strong></div>
        <div class="metric-cell"><span class="metric-label">Open positions</span><strong>${integer(summary.open || 0)}</strong></div>
        <div class="metric-cell"><span class="metric-label">Current open mark</span><strong class="${openMark >= 0 ? "positive" : "negative"}">${money(openMark)}</strong></div>
        <div class="metric-cell"><span class="metric-label">Partial realized net</span><strong>${money(summary.open_partial_realized_net || 0)}</strong></div>
        <div class="metric-cell"><span class="metric-label">95% interval / trade</span><strong>${summary.ci95 ? `${money(summary.ci95[0])} to ${money(summary.ci95[1])}` : "Collecting"}</strong></div>
        <div class="metric-cell"><span class="metric-label">Confirmed samples</span><strong>${integer(k2.n_signals || 0)} / ${integer(k2.needed || 50)}</strong></div>
      </div>
    </article>`;
}

function renderSleeves() {
  const sleeves = state.stats.sleeves || {
    gate_a: state.stats.combined || state.stats,
    price_only_late_score: {},
  };
  byId("sleeve-cards").innerHTML = ["gate_a", "price_only_late_score"]
    .map(strategy => sleeveCard(strategy, sleeves[strategy] || {})).join("");
  const combined = state.stats.combined || state.stats || {};
  byId("combined-summary").innerHTML = `<strong class="${(combined.net || 0) >= 0 ? "positive" : "negative"}">${money(combined.net || 0)}</strong>
    combined closed realized net · ${integer(combined.closed || 0)} closed · ${integer(combined.open || 0)} open ·
    $${Math.abs(combined.fees || 0).toFixed(2)} fees`;
}

function rawDetails(label, value) {
  return `<details class="raw-details"><summary>${escapeHtml(label)}</summary><pre>${safeJson(value)}</pre></details>`;
}

function renderMatches() {
  const rows = [...(state.matches || [])].sort((a, b) => Number(b.late) - Number(a.late));
  byId("match-list").innerHTML = rows.length ? rows.map(match => {
    const legs = Object.entries(match.legs || {}).sort(([, a], [, b]) =>
      String(a.display_name).localeCompare(String(b.display_name)));
    return `<article class="match-card ${match.late ? "late" : ""}">
      <div class="match-head">
        <div><h3 class="match-title">${escapeHtml(match.title || "Unnamed match")}</h3>
          <div class="time-text">Scheduled end: ${escapeHtml(fullDate(Date.parse(match.close_time) / 1000))}</div></div>
        <span class="tag ${match.late ? "good" : "neutral"}">${match.late ? "Late window" : "Watching"}</span>
      </div>
      <div class="contract-list">${legs.map(([ticker, leg]) => `
        <div class="contract-row">
          <span class="contract-name">${escapeHtml(leg.display_name || "Unnamed contract")}</span>
          <span class="price-group"><span>Last <strong>${cents(leg.last)}</strong></span>
            <span>Bid <strong>${cents(leg.bid)}</strong></span><span>Ask <strong>${cents(leg.ask)}</strong></span></span>
        </div>`).join("")}</div>
      ${rawDetails("Raw market identifiers", {event: match.event, series: match.series,
        contracts: legs.map(([ticker]) => ticker)})}
    </article>`;
  }).join("") : '<div class="empty-state">No matches are currently inside the discovery window.</div>';
}

function renderPositions() {
  const rows = state.trades.open || [];
  byId("position-list").innerHTML = rows.length ? rows.map(position => `
    <article class="position-card ${strategyClass(position.strategy)}">
      <div class="position-head">
        <div><h3 class="primary-title">${escapeHtml(position.display_game || "Unnamed match")}</h3>
          <div class="secondary-text">${escapeHtml(position.display_contract || position.display_leg || "Unnamed contract")} ·
            ${escapeHtml(strategyLabel(position.strategy))}</div></div>
        <span class="tag ${position.side === "yes" ? "good" : "warn"}">${position.side === "yes" ? "Bought Yes" : "Bought No"}</span>
      </div>
      <div class="time-text">Entered ${escapeHtml(fullDate(position.entry_ts))}</div>
      <div class="position-values">
        <div><span class="data-label">Remaining</span><strong>${integer(position.size)} contracts</strong></div>
        <div><span class="data-label">Entry average</span><strong>${cents(position.entry_px)}</strong></div>
        <div><span class="data-label">Executable bid</span><strong>${cents(position.bid)}</strong></div>
        <div><span class="data-label">Open mark</span><strong class="${(position.upnl || 0) >= 0 ? "positive" : "negative"}">${money(position.upnl || 0)}</strong></div>
      </div>
      ${rawDetails("Raw position identifiers", {trade_id: position.id, signal_id: position.signal_id,
        market: position.market, event: position.event, series: position.series})}
    </article>`).join("") : '<div class="empty-state">No open paper positions. Both strategies remain ready.</div>';
}

function timingRelation(match) {
  if (!match || match.match_status !== "nearest_same_match_event") {
    return `No same-match provider event inside ±${match?.window_s ?? state.config.event_match_window_s ?? 20} seconds`;
  }
  const delta = match.event_minus_signal_ms;
  if (match.timing_relation === "market_signal_first") return `Market signal arrived ${Math.abs(delta).toFixed(1)} ms before provider observation`;
  if (match.timing_relation === "match_feed_first") return `Provider observation arrived ${Math.abs(delta).toFixed(1)} ms before market signal`;
  return "Market signal and provider observation have the same recorded time";
}

function inferredStateLabel(value) {
  if (value === "equal_score_0") return "Equal score inferred from draw-contract reallocation";
  if (value === "one_goal_lead_+1") return "One-goal lead inferred from team-contract reallocation";
  return "No score state inferred";
}

function signalCard(signal) {
  const trigger = signal.trigger || {};
  const observed = trigger.observed || {};
  const thresholds = trigger.thresholds || {};
  const inference = trigger.price_only_inference || null;
  const matched = signal.matched_event || {};
  const canonical = matched.canonical_event;
  const consistency = matched.state_consistency;
  const eventClass = consistency === "correction_or_reversal" ? "correction" :
    consistency === "state_mismatch" ? "mismatch" : consistency?.includes("consistent") ? "consistent" : "";
  const outcome = signal.outcome || "unknown";
  const cardClass = outcome === "filled" ? "filled" : outcome.includes("error") ? "error" :
    outcome === "queued" || outcome === "executing" ? "collecting" : "rejected";
  const timing = signal.timing || {};
  return `<article class="audit-card ${cardClass}">
    <div class="audit-main">
      <div class="audit-header">
        <div><h3 class="primary-title">${escapeHtml(signal.display_game || "Unnamed match")}</h3>
          <div class="secondary-text">${escapeHtml(signal.display_contract || signal.display_leg || "Unnamed contract")}</div>
          <div class="time-text">Signal received ${escapeHtml(fullDate(signal.local_ts))}</div></div>
        <div class="audit-tags"><span class="tag neutral">${escapeHtml(strategyLabel(signal.strategy))}</span>
          <span class="tag ${outcome === "filled" ? "good" : outcome.includes("error") ? "bad" : "warn"}">${escapeHtml(humanStatus(outcome))}</span></div>
      </div>
      <div class="reason-box"><span class="data-label">Decision reason</span>
        <strong>${escapeHtml(humanOutcome(outcome))}</strong></div>
      <div class="trigger-box">
        <span class="data-label">Market trigger · observed versus required</span>
        <div class="data-grid">
          <div><span class="data-label">Log-odds move</span><strong>${finite(observed.log_odds_displacement) ? observed.log_odds_displacement.toFixed(3) : "—"} / ≥ ${thresholds.min_log_odds_displacement ?? "—"}</strong></div>
          <div><span class="data-label">Price levels swept</span><strong>${integer(observed.distinct_price_levels)} / ≥ ${integer(thresholds.min_distinct_price_levels)}</strong></div>
          <div><span class="data-label">Contracts swept</span><strong>${integer(observed.contracts)} / ≥ ${integer(thresholds.min_contracts)}</strong></div>
          <div><span class="data-label">Sibling confirmation</span><strong>${finite(observed.sibling_confirmation_lag_ms) ? `${observed.sibling_confirmation_lag_ms >= 0 ? "+" : ""}${observed.sibling_confirmation_lag_ms.toFixed(1)} ms` : "Not confirmed"} / ±${integer(thresholds.sibling_confirmation_window_ms)} ms</strong></div>
          <div><span class="data-label">Reference price</span><strong>${cents(observed.reference_price_c)}</strong></div>
          <div><span class="data-label">Sweep extreme</span><strong>${cents(observed.extreme_price_c)}</strong></div>
          <div><span class="data-label">Entry price cap</span><strong>${cents(thresholds.price_cap_c)}</strong></div>
          <div><span class="data-label">Price-only inference</span><strong>${escapeHtml(inferredStateLabel(inference?.inferred_state))}</strong></div>
        </div>
        ${inference ? `<div class="explain-text">Normalized target gain: ${finite(inference.target_gain_pp) ? `${(inference.target_gain_pp * 100).toFixed(1)} percentage points` : "—"} ·
          sibling flow explained: ${finite(inference.sibling_explanation) ? percent(inference.sibling_explanation * 100) : "—"} ·
          target spread: ${cents(inference.target_spread_c)} · baseline age: ${finite(inference.baseline_age_ms) ? `${inference.baseline_age_ms.toFixed(1)} ms` : "—"}</div>` : ""}
      </div>
      <div class="event-match-box ${eventClass}">
        <span class="data-label">Nearest same-match event · diagnostic only</span>
        <strong>${escapeHtml(canonical?.human_label || "No nearby canonical match event")}</strong>
        <div class="explain-text">${escapeHtml(timingRelation(matched))}${consistency ? ` · ${escapeHtml(consistencyLabels[consistency] || humanStatus(consistency))}` : ""}.
          Causation is not established.</div>
      </div>
      <div class="timeline-box"><span class="data-label">Recorded timeline</span><div class="timeline-grid">
        <div><span class="data-label">Exchange trigger</span><strong>${escapeHtml(fullDate(timing.exchange_signal_ts))}</strong></div>
        <div><span class="data-label">Local signal receipt</span><strong>${escapeHtml(fullDate(timing.signal_received_ts))}</strong></div>
        <div><span class="data-label">Paper order arrival</span><strong>${escapeHtml(fullDate(timing.paper_order_arrival_ts))}</strong></div>
        <div><span class="data-label">Provider observation</span><strong>${escapeHtml(fullDate(matched.event_observed_ts))}</strong></div>
        <div><span class="data-label">Paper entry</span><strong>${escapeHtml(fullDate(timing.entry_ts))}</strong></div>
        <div><span class="data-label">Paper exit</span><strong>${escapeHtml(fullDate(timing.exit_ts))}</strong></div>
        <div><span class="data-label">Settlement</span><strong>${escapeHtml(fullDate(timing.settlement_ts))}</strong></div>
        <div><span class="data-label">Provider poll uncertainty</span><strong>${finite(matched.provider_poll_uncertainty_ms) ? `${matched.provider_poll_uncertainty_ms.toFixed(1)} ms` : "Not recorded"}</strong></div>
      </div></div>
    </div>
    ${rawDetails("Raw identifiers and normalized evidence", {
      signal_id: signal.id, market: signal.market, event: signal.event, series: signal.series,
      raw_detail: signal.detail, canonical_match: matched,
    })}
  </article>`;
}

function renderSignals() {
  const rows = state.signals || [];
  byId("signal-list").innerHTML = rows.length ? rows.slice(0, 60).map(signalCard).join("") :
    '<div class="empty-state">No detector episodes have been recorded.</div>';
}

function providerClock(event) {
  const normalized = event.normalized_event || {};
  if (normalized.provider_clock != null) return String(normalized.provider_clock);
  if (normalized.provider_minute != null) {
    const extra = normalized.provider_stoppage != null ? `+${normalized.provider_stoppage}` : "";
    return `${normalized.provider_minute}${extra} minute`;
  }
  return "Not supplied by provider";
}

function leadText(value, beforeLabel, none = "Not observed") {
  return finite(value) ? `${Math.abs(value).toFixed(1)} ms ${beforeLabel}` : none;
}

function renderEvents() {
  const rows = state.events || [];
  byId("event-list").innerHTML = rows.length ? rows.map(event => {
    const normalized = event.normalized_event || {};
    const correction = String(normalized.canonical_type || "").startsWith("score_correction");
    return `<article class="event-card ${correction ? "correction" : ""}">
      <div class="event-head"><div><h3 class="primary-title">${escapeHtml(event.display_game || "Unnamed match")}</h3>
        <div class="secondary-text">${escapeHtml(normalized.human_label || humanStatus(event.change_kind))}</div></div>
        <span class="tag ${correction ? "warn" : "neutral"}">${escapeHtml(providerClock(event))}</span></div>
      <div class="time-text">Observed ${escapeHtml(fullDate(event.observed_ts))}</div>
      <div class="event-metrics">
        <div><span class="data-label">Provider response</span><strong>${finite(event.response_ms) ? `${event.response_ms.toFixed(1)} ms` : "—"}</strong></div>
        <div><span class="data-label">Last book change</span><strong>${leadText(event.last_book_lead_ms, "before feed observation")}</strong></div>
        <div><span class="data-label">Last market trade</span><strong>${leadText(event.last_trade_lead_ms, "before feed observation")}</strong></div>
        <div><span class="data-label">First book change after</span><strong>${leadText(event.first_book_after_ms, "after feed observation")}</strong></div>
        <div><span class="data-label">First trade after</span><strong>${leadText(event.first_trade_after_ms, "after feed observation")}</strong></div>
        <div><span class="data-label">Polling uncertainty</span><strong>${finite(event.detail?.poll_uncertainty_ms) ? `${event.detail.poll_uncertainty_ms.toFixed(1)} ms` : "—"}</strong></div>
      </div>
      ${rawDetails("Raw provider observation", {observation_id: event.id, event: event.event,
        milestone_id: event.milestone_id, normalized_event: normalized,
        raw_provider_payload: event.detail?.live_data})}
    </article>`;
  }).join("") : '<div class="empty-state">No score change has been observed by the diagnostic feed.</div>';
}

function renderEquity() {
  const holder = byId("equity-chart");
  const series = [
    {key: "gate_a", name: "Gate A", color: "#40d98a", values: state.equity.gate_a || []},
    {key: "price_only_late_score", name: "Price-only", color: "#52c7ea", values: state.equity.price_only_late_score || []},
    {key: "combined", name: "Combined", color: "#b7c2ca", values: state.equity.combined || [], dash: "6 5"},
  ];
  const points = series.flatMap(item => item.values);
  if (!points.length) {
    holder.innerHTML = '<div class="empty-state">The chart begins after the first paper position closes.</div>';
    return;
  }
  const width = 820, height = 280, left = 64, right = 18, top = 35, bottom = 42;
  let minX = Math.min(...points.map(point => point[0]));
  let maxX = Math.max(...points.map(point => point[0]));
  if (minX === maxX) { minX -= 1000; maxX += 1000; }
  let minY = Math.min(0, ...points.map(point => point[1]));
  let maxY = Math.max(0, ...points.map(point => point[1]));
  if (minY === maxY) { minY -= 1; maxY += 1; }
  const padY = Math.max((maxY - minY) * 0.08, 0.25);
  minY -= padY; maxY += padY;
  const x = value => left + (value - minX) / (maxX - minX) * (width - left - right);
  const y = value => top + (maxY - value) / (maxY - minY) * (height - top - bottom);
  const ticks = Array.from({length: 5}, (_, index) => minY + (maxY - minY) * index / 4);
  const paths = series.map(item => {
    if (!item.values.length) return "";
    const d = item.values.map((point, index) => `${index ? "L" : "M"}${x(point[0]).toFixed(1)},${y(point[1]).toFixed(1)}`).join(" ");
    const last = item.values[item.values.length - 1];
    return `<path d="${d}" fill="none" stroke="${item.color}" stroke-width="2.5" ${item.dash ? `stroke-dasharray="${item.dash}"` : ""}/>
      <circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="3.5" fill="${item.color}"/>`;
  }).join("");
  holder.innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">
    ${ticks.map(value => `<line x1="${left}" x2="${width - right}" y1="${y(value)}" y2="${y(value)}" stroke="#26343e" stroke-width="1"/>
      <text x="${left - 9}" y="${y(value) + 4}" fill="#9eabb6" font-size="11" text-anchor="end">${value.toFixed(2)}</text>`).join("")}
    <line x1="${left}" x2="${width - right}" y1="${y(0)}" y2="${y(0)}" stroke="#66737e" stroke-width="1"/>
    ${paths}
    <text x="${left}" y="${height - 13}" fill="#9eabb6" font-size="11">${escapeHtml(fullDate(minX / 1000).slice(0, 19))}</text>
    <text x="${width - right}" y="${height - 13}" fill="#9eabb6" font-size="11" text-anchor="end">${escapeHtml(fullDate(maxX / 1000).slice(0, 19))}</text>
    ${series.map((item, index) => `<circle cx="${left + index * 150}" cy="16" r="4" fill="${item.color}"/>
      <text x="${left + 10 + index * 150}" y="20" fill="#c8d4dc" font-size="11">${item.name}</text>`).join("")}
    <text x="15" y="${top - 8}" fill="#9eabb6" font-size="10">USD</text>
  </svg>`;
}

function renderLatency() {
  const definitions = [
    ["order_arrival", "Exchange trigger → paper arrival"],
    ["paper_entry", "Queued paper order → simulated fill"],
    ["paper_exit", "Exit decision → simulated fill"],
    ["feed_lag", "Exchange trade timestamp → service"],
  ];
  const rows = definitions.map(([key, label]) => ({key, label, ...(state.latency[key] || {})}))
    .filter(row => row.n);
  const maxValue = Math.max(1, ...rows.map(row => row.p95 ?? row.p50 ?? 0));
  byId("latency-chart").innerHTML = rows.length ? rows.map(row => `
    <div class="bar-group"><div class="bar-head"><span>${escapeHtml(row.label)}</span>
      <strong>median ${finite(row.p50) ? row.p50.toFixed(1) : "—"} ms · 95th ${finite(row.p95) ? row.p95.toFixed(1) : "collecting"} ms</strong></div>
      <div class="bar-track"><i class="${row.key === "feed_lag" ? "warn" : ""}" style="width:${Math.max(1, (row.p95 ?? row.p50 ?? 0) / maxValue * 100).toFixed(1)}%"></i></div>
      <div class="bar-note">${integer(row.n)} recorded samples${row.key === "feed_lag" ? "; includes clock skew" : ""}</div></div>`).join("") :
    '<div class="empty-state">Latency samples are still collecting.</div>';
}

function renderExits() {
  const sleeves = state.stats.sleeves || {};
  const rows = ["gate_a", "price_only_late_score"].flatMap(strategy =>
    Object.entries(sleeves[strategy]?.exit_reasons || {}).map(([reason, count]) => ({strategy, reason, count})));
  const maxCount = Math.max(1, ...rows.map(row => row.count));
  byId("exit-chart").innerHTML = rows.length ? rows.sort((a, b) => b.count - a.count).map(row => `
    <div class="bar-group"><div class="bar-head"><span>${escapeHtml(humanExit(row.reason))}</span>
      <strong>${escapeHtml(strategyLabel(row.strategy))} · ${integer(row.count)}</strong></div>
      <div class="bar-track"><i class="${strategyClass(row.strategy)}" style="width:${Math.max(2, row.count / maxCount * 100).toFixed(1)}%"></i></div></div>`).join("") :
    '<div class="empty-state">Exit reasons appear after positions close.</div>';
}

function evidenceCard(strategy, key, gate) {
  const isFill = key === "k1_fill_integrity";
  const current = isFill ? gate.n_fills || 0 : gate.n_signals || 0;
  const needed = gate.needed || (isFill ? 25 : 50);
  const progress = Math.min(100, current / needed * 100);
  const label = isFill ? "Arrival-book fill integrity" : "Event-clustered confidence interval";
  const interval = gate.ci ? ` · ${money(gate.ci[0])} to ${money(gate.ci[1])}` : "";
  return `<div class="evidence-card"><div class="evidence-head"><span>${escapeHtml(strategyLabel(strategy))} · ${label}</span>
    <strong class="${gate.status === "PASS" ? "positive" : gate.status === "FAIL" ? "negative" : ""}">${escapeHtml(humanStatus(gate.status || "collecting"))}</strong></div>
    <div class="bar-track"><i class="${gate.status === "FAIL" ? "bad" : gate.status === "PASS" ? strategyClass(strategy) : "warn"}" style="width:${Math.max(1, progress).toFixed(1)}%"></i></div>
    <div class="bar-note">${integer(current)} of ${integer(needed)} required${escapeHtml(interval)}${gate.failures?.length ? ` · failed trade IDs ${escapeHtml(gate.failures.join(", "))}` : ""}</div></div>`;
}

function renderEvidence() {
  const sleeves = state.stats.sleeves || {};
  const cards = [];
  ["gate_a", "price_only_late_score"].forEach(strategy => {
    const evidence = sleeves[strategy]?.evidence || {};
    if (evidence.k1_fill_integrity) cards.push(evidenceCard(strategy, "k1_fill_integrity", evidence.k1_fill_integrity));
    if (evidence.k2_ci) cards.push(evidenceCard(strategy, "k2_ci", evidence.k2_ci));
  });
  const latency = state.stats.combined?.evidence?.k4_latency;
  if (latency) cards.push(`<div class="evidence-card"><div class="evidence-head"><span>Shared execution adapter · arrival latency</span>
    <strong class="${latency.status === "BREACH" ? "negative" : "positive"}">${escapeHtml(humanStatus(latency.status))}</strong></div>
    <div class="bar-note">95th percentile ${finite(latency.p95_ms) ? `${latency.p95_ms.toFixed(1)} ms` : "collecting"} · source ${escapeHtml(humanStatus(latency.source))}</div></div>`);
  byId("evidence-list").innerHTML = cards.length ? cards.join("") :
    '<div class="empty-state">Evidence gates are still initializing.</div>';
}

function renderTrades() {
  const rows = state.trades.closed || [];
  byId("trade-list").innerHTML = rows.length ? rows.slice(0, 100).map(trade => `
    <article class="trade-card ${strategyClass(trade.strategy)}">
      <div class="trade-header"><div><h3 class="primary-title">${escapeHtml(trade.display_game || "Unnamed match")}</h3>
        <div class="secondary-text">${escapeHtml(trade.display_contract || trade.display_leg || "Unnamed contract")} · ${escapeHtml(strategyLabel(trade.strategy))}</div></div>
        <strong class="${(trade.net || 0) >= 0 ? "positive" : "negative"}">${money(trade.net || 0)}</strong></div>
      <div class="time-text">Entered ${escapeHtml(fullDate(trade.entry_ts))}<br>Closed ${escapeHtml(fullDate(trade.exit_ts))}</div>
      <div class="trade-values">
        <div><span class="data-label">Position</span><strong>${escapeHtml(trade.side === "yes" ? "Bought Yes" : "Bought No")} · ${integer(trade.size)}</strong></div>
        <div><span class="data-label">Entry → exit</span><strong>${cents(trade.entry_px)} → ${cents(trade.exit_px)}</strong></div>
        <div><span class="data-label">Exit reason</span><strong>${escapeHtml(humanExit(trade.exit_reason))}</strong></div>
        <div><span class="data-label">Gross</span><strong>${money(trade.gross || 0)}</strong></div>
        <div><span class="data-label">Fees</span><strong>${money(-(trade.fees || 0))}</strong></div>
        <div><span class="data-label">Net</span><strong class="${(trade.net || 0) >= 0 ? "positive" : "negative"}">${money(trade.net || 0)}</strong></div>
      </div>
      ${rawDetails("Raw trade identifiers and matched event", {trade_id: trade.id, signal_id: trade.signal_id,
        market: trade.market, event: trade.event, series: trade.series, matched_event: trade.matched_event})}
    </article>`).join("") : '<div class="empty-state">No paper positions have closed yet.</div>';
}

function humanizeActivity(text) {
  let result = String(text || "");
  (state.matches || []).forEach(match => Object.entries(match.legs || {}).forEach(([ticker, leg]) => {
    result = result.replaceAll(ticker, `${match.title} / ${leg.display_name}`);
  }));
  return result;
}

function renderActivity() {
  const rows = state.activity || [];
  byId("activity-list").innerHTML = rows.length ? rows.map(row => `
    <div class="activity-row"><span>${escapeHtml(fullDate(row.ts))}</span>
      <span class="tag ${row.kind === "error" ? "bad" : "neutral"}">${escapeHtml(humanStatus(row.kind))}</span>
      <span class="activity-text">${escapeHtml(humanizeActivity(row.text))}</span></div>`).join("") :
    '<div class="empty-state">No service activity has been recorded.</div>';
}

function renderAll() {
  renderHealth();
  renderRuntime();
  renderSleeves();
  renderMatches();
  renderPositions();
  renderSignals();
  renderEvents();
  renderEquity();
  renderLatency();
  renderExits();
  renderEvidence();
  renderTrades();
  renderActivity();
}

async function refreshAll() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  const requests = [
    ["status", "/api/status"],
    ["config", "/api/config"],
    ["matches", "/api/matches"],
    ["trades", "/api/trades?limit=200"],
    ["signals", "/api/signals?limit=60"],
    ["stats", "/api/stats"],
    ["events", "/api/goal-latency?limit=100"],
    ["latency", "/api/latency"],
    ["equity", "/api/equity"],
    ["activity", "/api/eventlog?limit=80"],
  ];
  const results = await Promise.allSettled(requests.map(([, path]) => apiJson(path)));
  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      const key = requests[index][0];
      state[key] = key === "equity" && Array.isArray(result.value) ?
        {combined: result.value, gate_a: [], price_only_late_score: []} : result.value;
    }
  });
  state.hydrated = true;
  refreshInFlight = false;
  renderAll();
}

function scheduleRefresh(delay = 250) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshAll().catch(error => recordClientError("dashboard refresh", error));
  }, delay);
}

function connectWebSocket() {
  clearTimeout(reconnectTimer);
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  try {
    socket = new WebSocket(`${protocol}//${location.host}/ws`);
  } catch (error) {
    recordClientError("Dashboard WebSocket", error);
    reconnectTimer = setTimeout(connectWebSocket, 2500);
    return;
  }
  socket.onopen = () => {
    socketConnected = true;
    clearClientFault("Dashboard WebSocket");
    renderHealth();
    renderRuntime();
  };
  socket.onmessage = event => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "hello" || message.type === "stats") {
        state.status = message.status || state.status;
        state.stats = message.stats || state.stats;
        renderHealth();
        renderRuntime();
        renderSleeves();
        renderEvidence();
      }
      if (["signal", "signal_update", "trade_open", "trade_partial", "trade_close", "system_error"].includes(message.type)) {
        if (message.type === "system_error" && message.error) {
          clientErrors.unshift(message.error);
          clientErrors.splice(20);
        }
        scheduleRefresh();
      }
      if (message.type === "prices") scheduleRefresh(900);
      if (message.type === "log") scheduleRefresh(500);
    } catch (error) {
      recordClientError("Dashboard WebSocket message", error);
    }
  };
  socket.onerror = () => {
    recordClientError("Dashboard WebSocket", new Error("live dashboard connection reported an error"));
  };
  socket.onclose = event => {
    socketConnected = false;
    recordClientError("Dashboard WebSocket", new Error(`disconnected (code ${event.code || "unknown"})`));
    renderRuntime();
    reconnectTimer = setTimeout(connectWebSocket, 2500);
  };
}

async function adminPost(path, body) {
  let token = sessionStorage.getItem("footballbot_admin_token");
  if (!token) token = window.prompt("Admin token");
  if (!token) return null;
  sessionStorage.setItem("footballbot_admin_token", token);
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Admin-Token": token},
      body: body == null ? null : JSON.stringify(body),
    });
    if (!response.ok) {
      const payload = await response.json().catch(error => ({detail: `Unable to read error response: ${error.message}`}));
      throw new Error(payload.detail || `${response.status} ${response.statusText}`);
    }
    clearClientFault(`Admin ${path}`);
    return await response.json();
  } catch (error) {
    sessionStorage.removeItem("footballbot_admin_token");
    recordClientError(`Admin ${path}`, error);
    return null;
  }
}

async function downloadExport() {
  let token = sessionStorage.getItem("footballbot_admin_token");
  if (!token) token = window.prompt("Admin token for protected study download");
  if (!token) return;
  sessionStorage.setItem("footballbot_admin_token", token);
  const button = byId("export-button");
  button.disabled = true;
  button.textContent = "Preparing download…";
  try {
    const response = await fetch("/api/export", {headers: {"X-Admin-Token": token}});
    if (!response.ok) {
      const payload = await response.json().catch(error => ({detail: `Unable to read export error: ${error.message}`}));
      throw new Error(payload.detail || `${response.status} ${response.statusText}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const matchedName = disposition.match(/filename="?([^";]+)"?/i);
    const filename = matchedName?.[1] || "football-study-export.zip";
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    clearClientFault("Study export");
    showToast("Study data download started.");
  } catch (error) {
    sessionStorage.removeItem("footballbot_admin_token");
    recordClientError("Study export", error);
  } finally {
    button.disabled = false;
    button.textContent = "Download study data";
  }
}

function playSignalTone() {
  if (!soundEnabled) return;
  try {
    const context = new (window.AudioContext || window.webkitAudioContext)();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(330, context.currentTime);
    oscillator.frequency.exponentialRampToValueAtTime(520, context.currentTime + 0.18);
    gain.gain.setValueAtTime(0.08, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.35);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.35);
  } catch (error) {
    recordClientError("Audio notification", error);
  }
}

byId("kill-button").addEventListener("click", async () => {
  const result = await adminPost("/api/kill", {on: !killEnabled});
  if (result) scheduleRefresh(0);
});

byId("sound-button").addEventListener("click", () => {
  soundEnabled = !soundEnabled;
  byId("sound-button").textContent = soundEnabled ? "Sound on" : "Sound off";
  if (soundEnabled) playSignalTone();
});

byId("export-button").addEventListener("click", () => {
  downloadExport().catch(error => recordClientError("Study export", error));
});

refreshAll().catch(error => recordClientError("Initial dashboard load", error));
connectWebSocket();
setInterval(() => {
  refreshAll().catch(error => recordClientError("Scheduled dashboard refresh", error));
}, 15000);
