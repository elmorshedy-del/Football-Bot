// Observer-only soccer goal-feed latency collector, no trading dependencies.
// Capture push arrival BEFORE parsing. Sportradar event.time is system update time,
// NOT the time at which the ball crossed the line.
const key = process.env.SPORTRADAR_API_KEY;
const access = process.env.SPORTRADAR_ACCESS_LEVEL || "trial";
const maxMinutes = Math.max(10, Number(process.env.MAX_MINUTES || 165));
const pollMs = Math.max(5000, Number(process.env.POLL_MS || 10000));
const deadline = Date.now() + maxMinutes * 60000;
const goalKinds = new Set([
  "possible_goal", "score_change", "decision_to_var", "decision_to_var_over",
  "video_assistant_referee", "video_assistant_referee_over",
  "canceled_decision_to_var", "possible_decision_to_var"
]);
const seen = new Set();
let target = process.env.SPORT_EVENT_ID || "";
let initialPoll = true, lastScore = "", lastState = "", stopping = false;
const now = () => new Date().toISOString();
function log(kind, details = {}) {
  process.stdout.write(JSON.stringify({ kind, logged_at: now(), ...details }) + "\n");
}
function parseTime(value) {
  const ms = Date.parse(value ?? "");
  return Number.isFinite(ms) ? ms : null;
}
function teams(row) {
  return (row?.sport_event?.competitors ?? row?.competitors ?? []).map(x => x?.name ?? "");
}
function isBarcelonaBetis(row) {
  const joined = teams(row).join(" ").toLowerCase();
  return joined.includes("barcelona") && joined.includes("betis");
}
function rowsOf(data) {
  if (Array.isArray(data?.sport_event_timelines)) return data.sport_event_timelines;
  if (Array.isArray(data?.sport_event_timeline)) return data.sport_event_timeline;
  if (data?.sport_event_timeline) return [data.sport_event_timeline];
  if (data?.sport_event) return [data];
  return [];
}
function extractEvent(packet) {
  const candidate = packet?.payload?.event ?? packet?.data?.event
    ?? packet?.event ?? packet?.payload?.timeline?.event
    ?? packet?.data?.timeline?.event;
  if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) return candidate;
  const list = packet?.payload?.timeline ?? packet?.data?.timeline;
  if (Array.isArray(list)) return list[0] || null;
  return null;
}
function acceptEvent(event, source, receivedMs, matchId, metadata = {}, baseline = false) {
  const type = String(event?.type ?? metadata?.event_id ?? "").toLowerCase();
  if (!goalKinds.has(type)) return;
  if (target && matchId && matchId !== target) return;
  const eventId = String(event?.id ?? [type, event?.match_clock, event?.match_time,
    event?.home_score, event?.away_score].join("/"));
  const unique = source + "/" + matchId + "/" + type + "/" + eventId;
  if (seen.has(unique)) return;
  seen.add(unique);
  const sourceTime = event?.time ?? null;
  const sourceMs = parseTime(sourceTime);
  log("GOAL_SIGNAL_FIRST_SEEN", {
    source, match_id: matchId || null, event_type: type,
    event_id: eventId, received_at: new Date(receivedMs).toISOString(),
    source_processing_or_update_at: sourceTime,
    source_processing_to_receipt_ms: sourceMs == null ? null : receivedMs - sourceMs,
    match_clock: event?.match_clock ?? null, match_minute: event?.match_time ?? null,
    home_score: event?.home_score ?? null, away_score: event?.away_score ?? null,
    baseline, raw_metadata: metadata, raw_event: event
  });
}
async function discoverMatch() {
  const endpoints = [
    "soccer/" + access + "/v4/en/schedules/live/timelines.json",
    "soccer-extended/" + access + "/v4/en/schedules/live/timelines.json"
  ];
  for (const path of endpoints) {
    try {
      const resp = await fetch("https://api.sportradar.com/" + path,
        { headers: { "x-api-key": key, accept: "application/json" },
          signal: AbortSignal.timeout(12000) });
      if (!resp.ok) {
        log("DISCOVERY_HTTP", { status: resp.status, endpoint: path });
        continue;
      }
      const body = await resp.json();
      const rows = rowsOf(body);
      const row = rows.find(isBarcelonaBetis);
      if (row) {
        target = row?.sport_event?.id || row?.id || target;
        log("MATCH_IDENTIFIED", { match_id: target, teams: teams(row),
          score: row?.sport_event_status?.home_score + "-" + row?.sport_event_status?.away_score,
          matches_in_response: rows.length });
        return;
      }
      log("MATCH_NOT_FOUND_YET", { endpoint: path, active_matches: rows.length });
    } catch (e) {
      log("DISCOVERY_EXCEPTION", { endpoint: path, error: String(e).slice(0, 140) });
    }
  }
}
function ingestPoll(row, receivedMs, startedMs, baseline) {
  const matchId = row?.sport_event?.id ?? row?.id ?? "";
  if (!isBarcelonaBetis(row) && (!target || target !== matchId)) return;
  if (!target && matchId) { target = matchId; log("MATCH_IDENTIFIED", { match_id: target, teams: teams(row) }); }
  const status = row?.sport_event_status || {};
  const home = status.home_score, away = status.away_score;
  const score = Number.isFinite(home) && Number.isFinite(away) ? home + "-" + away : null;
  if (score && lastScore && score !== lastScore) {
    log("SCORE_CHANGE_FIRST_SEEN", {
      source: "rest_poll", match_id: target, previous: lastScore, score,
      received_at: new Date(receivedMs).toISOString(),
      request_started_at: new Date(startedMs).toISOString(),
      round_trip_ms: receivedMs - startedMs, polling_period_ms: pollMs
    });
  }
  if (score) lastScore = score;
  lastState = status?.status || lastState;
  const events = Array.isArray(row?.timeline) ? row.timeline : [];
  for (const event of events) acceptEvent(event, "rest_poll", receivedMs, matchId, {}, baseline);
}
async function runRestPoll() {
  log("REST_POLL_START", { poll_ms: pollMs, caveat: "Polling has up to one interval of sampling delay" });
  let calls = 0;
  const endpoint = "https://api.sportradar.com/soccer/" + access + "/v4/en/schedules/live/timelines.json";
  while (!stopping && Date.now() < deadline && calls < 860) {
    const started = Date.now();
    try {
      const res = await fetch(endpoint, { headers: { "x-api-key": key, accept: "application/json" },
        signal: AbortSignal.timeout(10000) });
      calls++;
      if (!res.ok) {
        log("POLL_HTTP_ERROR", { status: res.status, calls });
        if ([401, 403, 404, 429].includes(res.status)) break;
      } else {
        const raw = await res.json(), arrived = Date.now();
        for (const row of rowsOf(raw)) ingestPoll(row, arrived, started, initialPoll);
        initialPoll = false;
        if (calls % 18 === 0) log("POLL_HEARTBEAT", { calls, match_id: target || null,
          score: lastScore, match_status: lastState });
        if (target && ["ended", "closed"].includes(lastState)) break;
      }
    } catch (e) { log("POLL_EXCEPTION", { error: String(e).slice(0, 180) }); }
    await new Promise(resolve => setTimeout(resolve, Math.max(100, pollMs - (Date.now() - started))));
  }
  log("REST_POLL_STOP", { calls, score: lastScore || null, match_id: target || null });
}
async function openPush(sourceProduct) {
  const url = "https://api.sportradar.com/" + sourceProduct + "/" +
    access + "/v4/stream/events/subscribe?format=json" +
    (target ? "&sport_event_id=" + encodeURIComponent(target) : "");
  const original = new URL(url);
  const subscribe = await fetch(url, { headers: { "x-api-key": key, accept: "application/json" },
    redirect: "manual", signal: AbortSignal.timeout(12000) });
  if (![301, 302, 303, 307, 308].includes(subscribe.status)) {
    log("PUSH_SUBSCRIBE_HTTP", { product: sourceProduct, status: subscribe.status });
    return null;
  }
  const location = subscribe.headers.get("location");
  if (!location) { log("PUSH_NO_LOCATION", { product: sourceProduct }); return null; }
  const redirect = new URL(location, url);
  if (redirect.protocol !== "https:") throw new Error("Non-HTTPS redirect refused");
  const sameHost = redirect.hostname === original.hostname;
  const stream = await fetch(redirect.toString(), {
    headers: sameHost ? { "x-api-key": key } : {},
    signal: AbortSignal.timeout(12000)
  });
  if (!stream.ok || !stream.body) {
    log("PUSH_STREAM_HTTP", { product: sourceProduct, status: stream.status });
    return null;
  }
  log("PUSH_CONNECTED", { product: sourceProduct, match_id: target || null,
    note: "Incoming stream bytes timestamped immediately before JSON parsing" });
  return { stream, product: sourceProduct };
}
async function runPushSession({ stream, product }) {
  const reader = stream.body.getReader(), decoder = new TextDecoder();
  let carry = "", totalMessages = 0, lastHeartbeat = Date.now();
  while (!stopping && Date.now() < deadline) {
    const { done, value } = await reader.read();
    if (done) break;
    const arrival = Date.now();
    carry += decoder.decode(value, { stream: true });
    if (carry.length > 2 * 1024 * 1024) {
      log("PUSH_OVERSIZE_BUFFER", { bytes: carry.length });
      carry = "";
    }
    for (;;) {
      const ix = carry.indexOf("\n");
      if (ix < 0) break;
      const line = carry.slice(0, ix).trim();
      carry = carry.slice(ix + 1);
      if (!line) continue;
      let packet;
      try { packet = JSON.parse(line); }
      catch (error) { log("PUSH_BAD_JSON", { sample: line.slice(0, 110) }); continue; }
      totalMessages++;
      const meta = packet.metadata || {};
      const matchId = meta.sport_event_id || packet.sport_event?.id ||
        packet.payload?.sport_event?.id || packet.data?.sport_event?.id || "";
      const event = extractEvent(packet) || {};
      const type = String(meta.event_id || event.type || "").toLowerCase();
      if (goalKinds.has(type) && (!target || !matchId || target === matchId)) {
        acceptEvent({ ...event, type: event.type || type }, "push", arrival, matchId, meta, false);
        if (!event?.time) log("PUSH_GOAL_RAW_PACKET", {
          match_id: matchId || null, received_at: new Date(arrival).toISOString(), raw_packet: packet
        });
      }
      if (totalMessages === 1) log("PUSH_FIRST_MESSAGE", { product,
        keys: Object.keys(packet), metadata_keys: Object.keys(meta),
        match_id: matchId || null, type });
      if (type === "match_ended" && target && matchId === target) {
        log("MATCH_FINISHED", { match_id: target, source: "push" });
        stopping = true;
        break;
      }
      if (Date.now() - lastHeartbeat > 120000) {
        lastHeartbeat = Date.now();
        log("PUSH_HEARTBEAT", { total_messages: totalMessages, match_id: target || null });
      }
    }
  }
  try { await reader.cancel(); } catch {}
  log("PUSH_DISCONNECTED", { product, messages: totalMessages, stopped: stopping });
}
process.on("SIGTERM", () => { stopping = true; log("SIGTERM"); });
process.on("SIGINT", () => { stopping = true; log("SIGINT"); });
if (!key) {
  log("CONFIG_ERROR", { message: "SPORTRADAR_API_KEY not set" });
  process.exitCode = 1;
} else {
  log("START", { api: "Sportradar", target: "Real Betis vs FC Barcelona",
    access, target_match_id: target || null, max_minutes: maxMinutes });
  if (!target) await discoverMatch();
  let streamed = false;
  for (const product of ["soccer", "soccer-extended"]) {
    try {
      const session = await openPush(product);
      if (!session) continue;
      streamed = true;
      await runPushSession(session);
      if (stopping || Date.now() >= deadline) break;
      // A premature close is evidence, not permission to silently switch to polling.
      log("PUSH_CLOSED_EARLY", { product });
      streamed = false;
      break;
    } catch (e) {
      log("PUSH_FAILURE", { product, error: String(e).slice(0, 190) });
    }
  }
  if (!streamed && !stopping && Date.now() < deadline) await runRestPoll();
  log("STOP", { match_id: target || null, final_score: lastScore || null });
}
