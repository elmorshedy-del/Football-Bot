// Standalone, read-only Barcelona vs Real Betis Sportradar goal-latency observer.
// This script has no Kalshi trading integration and never sends orders.
// Uses only the SPORTRADAR_API_KEY environment variable; do NOT commit API keys.
const apiKey = process.env.SPORTRADAR_API_KEY;
const accessLevel = process.env.SPORTRADAR_ACCESS_LEVEL ?? "trial";
const pollMs = Number(process.env.POLL_MS ?? (accessLevel === "trial" ? 10000 : 1500));
const maxCalls = Number(process.env.MAX_CALLS ?? (accessLevel === "trial" ? 850 : 20000));
const maxMinutes = Number(process.env.MAX_MINUTES ?? 150);
const matchIdOverride = process.env.MATCH_ID ?? "";
const deadline = Date.now() + maxMinutes * 60000;
const endpoint = "https://api.sportradar.com/soccer/" +
  encodeURIComponent(accessLevel) + "/v4/en/schedules/live/timelines.json";
let calls = 0, failures = 0, targetId = matchIdOverride, lastStatus = "",
  lastScore = "", firstTargetResponse = true, lastHeartbeat = 0;
const observed = new Map();
const iso = (ms) => new Date(ms).toISOString();
const log = (kind, data = {}) => console.log(JSON.stringify({ kind, log_time: iso(Date.now()), ...data }));
const epoch = (value) => {
  const t = Date.parse(value ?? "");
  return Number.isFinite(t) ? t : null;
};
const teams = (row) =>
  (row?.sport_event?.competitors ?? row?.competitors ?? []).map(x => x?.name ?? "");
function isTarget(row) {
  const id = row?.sport_event?.id ?? row?.id ?? "";
  if (targetId && id === targetId) return true;
  const text = teams(row).join(" ").toLowerCase();
  return text.includes("barcelona") && text.includes("betis");
}
function unpack(data) {
  if (Array.isArray(data?.sport_event_timelines)) return data.sport_event_timelines;
  if (Array.isArray(data?.sport_event_timeline)) return data.sport_event_timeline;
  if (data?.sport_event_timeline) return [data.sport_event_timeline];
  if (data?.sport_event) return [data];
  return [];
}
function handleMatch(row, requestStarted, payloadReceived, rtt) {
  const matchId = row?.sport_event?.id ?? row?.id ?? "";
  if (!isTarget(row)) return;
  if (!targetId && matchId) {
    targetId = matchId;
    log("MATCH_IDENTIFIED", {
      match_id: targetId,
      teams: teams(row),
      scheduled: row?.sport_event?.scheduled ?? row?.sport_event?.start_time ?? null
    });
  }
  const status = row?.sport_event_status ?? {};
  const home = Number(status.home_score), away = Number(status.away_score);
  const score = Number.isFinite(home) && Number.isFinite(away) ? home + "-" + away : null;
  const events = Array.isArray(row?.timeline) ? row.timeline
    : Array.isArray(row?.timeline?.events) ? row.timeline.events
    : Array.isArray(row?.sport_event_timeline?.timeline) ? row.sport_event_timeline.timeline : [];
  if (firstTargetResponse) {
    log("BASELINE", { match_id: targetId, score, existing_events: events.length,
      match_status: status?.status ?? null, sample_event_fields: Object.keys(events[0] ?? {}) });
  }
  if (score && lastScore && score !== lastScore) {
    log("SCORE_CHANGE_FIRST_OBSERVED", {
      match_id: targetId, previous_score: lastScore, new_score: score,
      received_at: iso(payloadReceived), previous_poll_window_ms: pollMs,
      request_started_at: iso(requestStarted), response_round_trip_ms: rtt,
      baseline: firstTargetResponse
    });
  }
  if (score) lastScore = score;
  if (status.status && status.status !== lastStatus) {
    lastStatus = status.status;
    log("MATCH_STATUS", { status: lastStatus, score, match_id: targetId });
  }
  const selected = new Set(["possible_goal", "score_change",
    "decision_to_var", "possible_decision_to_var", "canceled_decision_to_var",
    "video_assistant_referee", "video_assistant_referee_over"]);
  for (const e of events) {
    const kind = String(e?.type ?? "").toLowerCase();
    if (!selected.has(kind)) continue;
    const id = String(e.id ?? [kind, e.match_time, e.match_clock, e.home_score, e.away_score].join(":"));
    const key = [targetId, id, kind].join("/");
    const sourceAt = e.time ?? null;
    if (!observed.has(key)) {
      observed.set(key, sourceAt);
      const sourceMs = epoch(sourceAt);
      log("GOAL_RELATED_EVENT_FIRST_SEEN", {
        match_id: targetId, event_id: id, event_type: kind,
        source_processing_or_update_at: sourceAt,
        received_at: iso(payloadReceived),
        source_to_receipt_ms: sourceMs === null ? null : payloadReceived - sourceMs,
        request_started_at: iso(requestStarted),
        response_round_trip_ms: rtt, poll_period_ms: pollMs,
        is_baseline: firstTargetResponse,
        minute: e.match_time ?? null, clock: e.match_clock ?? null,
        score: e.home_score == null ? null : String(e.home_score) + "-" + String(e.away_score),
        raw_event: e
      });
    } else if (sourceAt && observed.get(key) !== sourceAt) {
      const previousSourceAt = observed.get(key);
      observed.set(key, sourceAt);
      log("SOURCE_EVENT_REVISED", {
        match_id: targetId, event_id: id, event_type: kind,
        first_source_timestamp: previousSourceAt, revised_source_timestamp: sourceAt,
        observed_at: iso(payloadReceived)
      });
    }
  }
  firstTargetResponse = false;
}
if (!apiKey) {
  log("CONFIG_ERROR", { reason: "Set SPORTRADAR_API_KEY in service variables; observer did not start" });
  process.exitCode = 1;
} else {
  log("OBSERVER_START", {
    provider: "Sportradar Soccer v4", target: "Barcelona vs Real Betis",
    access_level: accessLevel, poll_interval_ms: pollMs, max_calls: maxCalls,
    max_minutes: maxMinutes, key_loaded: true, match_id_override: targetId || null
  });
  while (Date.now() < deadline && calls < maxCalls) {
    const started = Date.now();
    try {
      const response = await fetch(endpoint, {
        headers: { "x-api-key": apiKey, "accept": "application/json" },
        signal: AbortSignal.timeout(12000)
      });
      calls++;
      const body = await response.text();
      const received = Date.now();
      if (!response.ok) {
        failures++;
        log("HTTP_ERROR", {
          status: response.status, calls, failures,
          response_excerpt: body.substring(0, 180)
        });
        if ([401, 403, 404, 429].includes(response.status)) break;
      } else {
        const payload = JSON.parse(body), rows = unpack(payload);
        if (calls === 1) {
          log("FEED_CONNECTED", {
            root_keys: Object.keys(payload), available_live_matches: rows.length,
            round_trip_ms: received - started,
            rate_remaining_header: response.headers.get("x-ratelimit-remaining") ?? null
          });
        }
        for (const row of rows) handleMatch(row, started, received, received - started);
        if (!targetId && calls % 18 === 1) {
          log("SEARCHING_FOR_MATCH", { calls, live_matches_seen: rows.length });
        }
        if (Date.now() - lastHeartbeat >= 180000) {
          lastHeartbeat = Date.now();
          log("HEARTBEAT", { calls, failures, match_id: targetId || null,
            match_status: lastStatus || null, score: lastScore || null });
        }
        if (targetId && ["closed", "ended"].includes(lastStatus)) break;
      }
    } catch (error) {
      failures++;
      log("POLL_EXCEPTION", { calls, failures, message: String(error).slice(0, 220) });
    }
    const delay = Math.max(200, pollMs - (Date.now() - started));
    await new Promise(resolve => setTimeout(resolve, delay));
  }
  log("OBSERVER_STOP", {
    calls, failures, target_match_id: targetId || null,
    last_score: lastScore || null, last_match_status: lastStatus || null
  });
}
