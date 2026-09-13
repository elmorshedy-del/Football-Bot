import http from "node:http";

// ---------------------------------------------------------------------------
// Config. Credentials are read from the environment only; they are never
// logged, echoed, or included in any response body.
// ---------------------------------------------------------------------------
const PORT = Number(process.env.PORT || 8080);
const CENSUS_INTERVAL_MS = Number(process.env.CENSUS_INTERVAL_MS || 15_000);
const CENSUS_TIMEOUT_MS = Number(process.env.CENSUS_TIMEOUT_MS || 8_000);
const RETAIN_HOURS = Number(process.env.RETAIN_HOURS || 48);
const ALLOW_PROBE = process.env.DIAG_ALLOW_PROBE === "1";
const PROBE_MAX_MS = Number(process.env.PROBE_MAX_MS || 60_000);
const PROBE_IDLE_MS = Number(process.env.PROBE_IDLE_MS || 15_000);
const CF_TOKEN = process.env.CF_API_TOKEN || "";
const CF_ZONE = process.env.CF_ZONE_TAG || "";
const MEDIA_PATH_LIKE = process.env.MEDIA_PATH_LIKE || "/api/xtream/media/%";

const MAX_SAMPLES = Math.max(60, Math.ceil((RETAIN_HOURS * 3600_000) / CENSUS_INTERVAL_MS));

function log(event, data = {}) {
  console.log(JSON.stringify({ ts: new Date().toISOString(), event, ...data }));
}

function mask(value) {
  const s = String(value || "");
  if (s.length <= 2) return "**";
  return `${s.slice(0, 2)}${"*".repeat(Math.max(2, s.length - 2))}`;
}

function safeHost(url) {
  try {
    return new URL(url).host;
  } catch {
    return "invalid-url";
  }
}

// ---------------------------------------------------------------------------
// Portal config. Accepts the same shapes the Worker secret uses:
//   {url,username,password} | [ {...} ] | {portals:[ {...} ]}
// ---------------------------------------------------------------------------
function loadPortals() {
  const raw = process.env.XTREAM_PORTALS_JSON || process.env.IPTV_LAB_JSON || "";
  if (!raw.trim()) return { ok: false, error: "XTREAM_PORTALS_JSON is not set", portals: [] };
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    return { ok: false, error: `XTREAM_PORTALS_JSON is not valid JSON: ${error.message}`, portals: [] };
  }
  const list = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.portals) ? parsed.portals : [parsed];
  const portals = [];
  for (const [index, item] of list.entries()) {
    const url = String(item?.url || "").replace(/\/+$/, "");
    const username = String(item?.username || item?.user || "");
    const password = String(item?.password || item?.pass || "");
    if (!url || !username || !password) continue;
    portals.push({ id: String(item?.id || `p${index + 1}`), url, username, password });
  }
  if (!portals.length) return { ok: false, error: "no portal entry had url + username + password", portals: [] };
  return { ok: true, error: null, portals };
}

const PORTALS = loadPortals();
const maskedPortals = PORTALS.portals.map((p) => ({ id: p.id, host: safeHost(p.url), user: mask(p.username) }));

// ---------------------------------------------------------------------------
// Census. player_api.php is a metadata endpoint: it reports the line's active
// connection count WITHOUT opening a stream, so polling it costs no slot.
// ---------------------------------------------------------------------------
const samples = []; // { ts, portalId, ok, activeCons, maxCons, status, latencyMs, error }

function numOrNull(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function record(portal, fields) {
  const sample = {
    ts: new Date().toISOString(),
    portalId: portal.id,
    activeCons: null,
    maxCons: null,
    status: null,
    expDate: null,
    ...fields,
  };
  samples.push(sample);
  const cap = MAX_SAMPLES * Math.max(1, PORTALS.portals.length);
  while (samples.length > cap) samples.shift();
  log("census", {
    portal: portal.id,
    ok: sample.ok,
    active: sample.activeCons,
    max: sample.maxCons,
    ms: sample.latencyMs,
    error: sample.error,
  });
  return sample;
}

async function censusOnce(portal) {
  const started = Date.now();
  const url = `${portal.url}/player_api.php?username=${encodeURIComponent(portal.username)}&password=${encodeURIComponent(portal.password)}`;
  try {
    const res = await fetch(url, {
      redirect: "follow",
      signal: AbortSignal.timeout(CENSUS_TIMEOUT_MS),
      headers: { "User-Agent": "KoraZero-StreamDiagnostic/1.0", Accept: "application/json,*/*" },
    });
    const text = await res.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      return record(portal, { ok: false, error: `non-JSON response (HTTP ${res.status})`, latencyMs: Date.now() - started });
    }
    const info = body?.user_info || {};
    return record(portal, {
      ok: true,
      activeCons: numOrNull(info.active_cons),
      maxCons: numOrNull(info.max_connections),
      status: info.status == null ? null : String(info.status),
      expDate: info.exp_date == null ? null : String(info.exp_date),
      latencyMs: Date.now() - started,
      error: null,
    });
  } catch (error) {
    const message = error?.name === "TimeoutError" ? "census timed out" : String(error?.message || error);
    return record(portal, { ok: false, error: message, latencyMs: Date.now() - started });
  }
}

async function censusLoop() {
  for (const portal of PORTALS.portals) {
    await censusOnce(portal).catch((error) => log("census_error", { portal: portal.id, message: String(error?.message || error) }));
  }
}

// ---------------------------------------------------------------------------
// Saturation episodes: contiguous runs where active >= max (line full).
// These are the windows in which a viewer would be refused.
// ---------------------------------------------------------------------------
function finishEpisode(episode) {
  return { ...episode, durationSec: Math.round((new Date(episode.end) - new Date(episode.start)) / 1000) };
}

function episodes(portalId) {
  const rows = samples.filter((s) => s.portalId === portalId && s.ok && s.activeCons != null);
  const out = [];
  let current = null;
  for (const s of rows) {
    const max = s.maxCons && s.maxCons > 0 ? s.maxCons : 1;
    const saturated = s.activeCons >= max;
    if (saturated && !current) {
      current = { start: s.ts, end: s.ts, peak: s.activeCons, max, samples: 1 };
    } else if (saturated && current) {
      current.end = s.ts;
      current.peak = Math.max(current.peak, s.activeCons);
      current.samples += 1;
    } else if (!saturated && current) {
      out.push(finishEpisode(current));
      current = null;
    }
  }
  if (current) out.push(finishEpisode({ ...current, open: true }));
  return out.reverse();
}

// ---------------------------------------------------------------------------
// Cloudflare correlation (optional). Answers what the provider census alone
// cannot: when the line was full, were any of those connections OURS?
// ---------------------------------------------------------------------------
async function cloudflareViewers(sinceIso, untilIso) {
  if (!CF_TOKEN || !CF_ZONE) return { configured: false };
  const query = `{viewer{zones(filter:{zoneTag:"${CF_ZONE}"}){httpRequestsAdaptiveGroups(limit:2000,filter:{datetime_geq:"${sinceIso}",datetime_leq:"${untilIso}",clientRequestPath_like:"${MEDIA_PATH_LIKE}",requestSource:"eyeball"}){count dimensions{clientIP edgeResponseStatus}}}}}`;
  try {
    const res = await fetch("https://api.cloudflare.com/client/v4/graphql", {
      method: "POST",
      headers: { Authorization: `Bearer ${CF_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      signal: AbortSignal.timeout(15_000),
    });
    const body = await res.json();
    if (body?.errors?.length) return { configured: true, ok: false, error: String(body.errors[0]?.message || "graphql error") };
    const rows = body?.data?.viewer?.zones?.[0]?.httpRequestsAdaptiveGroups || [];
    const ips = new Set();
    const byStatus = {};
    for (const row of rows) {
      const status = row.dimensions.edgeResponseStatus;
      byStatus[status] = (byStatus[status] || 0) + row.count;
      if (status === 200) ips.add(row.dimensions.clientIP);
    }
    return { configured: true, ok: true, streamingViewerIPs: ips.size, requestsByStatus: byStatus };
  } catch (error) {
    return { configured: true, ok: false, error: String(error?.message || error) };
  }
}

// ---------------------------------------------------------------------------
// Gated byte-timing probe. This DOES consume a stream slot, so it is off by
// default, refuses to run while the line is already saturated, and always
// aborts the upstream fetch when it stops.
// ---------------------------------------------------------------------------
let probeRunning = false;

function probeResult(status, headerMs, firstByteMs, bytes, gaps, endReason, started) {
  const sorted = [...gaps].sort((a, b) => a - b);
  const pick = (p) => (sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))] : null);
  const elapsed = Date.now() - started;
  return {
    ok: true,
    upstreamStatus: status,
    headerMs,
    firstByteMs,
    bytes,
    elapsedMs: elapsed,
    throughputMbps: elapsed > 0 ? Number(((bytes * 8) / elapsed / 1000).toFixed(2)) : null,
    chunkGapMs: {
      count: gaps.length,
      p50: pick(50),
      p90: pick(90),
      p99: pick(99),
      max: sorted.length ? sorted[sorted.length - 1] : null,
    },
    endReason,
  };
}

async function runProbe(streamId) {
  if (!ALLOW_PROBE) return { ok: false, error: "probe disabled (set DIAG_ALLOW_PROBE=1)" };
  if (probeRunning) return { ok: false, error: "a probe is already running" };
  const portal = PORTALS.portals[0];
  if (!portal) return { ok: false, error: "no portal configured" };

  const latest = [...samples].reverse().find((s) => s.portalId === portal.id && s.ok);
  if (!latest) return { ok: false, error: "no successful census yet; refusing to probe blind" };
  const max = latest.maxCons && latest.maxCons > 0 ? latest.maxCons : 1;
  if (latest.activeCons != null && latest.activeCons >= max) {
    return { ok: false, error: `line already saturated (${latest.activeCons}/${max}); refusing to take the slot from a viewer` };
  }

  probeRunning = true;
  const controller = new AbortController();
  const started = Date.now();
  const gaps = [];
  let bytes = 0;
  let firstByteMs = null;
  let lastChunk = Date.now();
  let endReason = "unknown";

  const hardStop = setTimeout(() => {
    endReason = "probe window elapsed";
    controller.abort();
  }, PROBE_MAX_MS);
  let idleTimer = setTimeout(() => {
    endReason = `upstream idle > ${PROBE_IDLE_MS}ms`;
    controller.abort();
  }, PROBE_IDLE_MS);

  const safeId = String(streamId).replace(/[^0-9]/g, "");
  const target = `${portal.url}/live/${encodeURIComponent(portal.username)}/${encodeURIComponent(portal.password)}/${safeId}.ts`;

  try {
    const res = await fetch(target, { signal: controller.signal, headers: { "User-Agent": "KoraZero-StreamDiagnostic/1.0" } });
    const headerMs = Date.now() - started;
    if (!res.ok) {
      endReason = `upstream HTTP ${res.status}`;
      return { ok: true, upstreamStatus: res.status, headerMs, bytes: 0, endReason };
    }
    for await (const chunk of res.body) {
      const now = Date.now();
      if (firstByteMs == null) firstByteMs = now - started;
      else gaps.push(now - lastChunk);
      lastChunk = now;
      bytes += chunk.length;
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        endReason = `upstream idle > ${PROBE_IDLE_MS}ms`;
        controller.abort();
      }, PROBE_IDLE_MS);
    }
    if (endReason === "unknown") endReason = "upstream EOF";
    return probeResult(res.status, headerMs, firstByteMs, bytes, gaps, endReason, started);
  } catch (error) {
    if (endReason === "unknown") endReason = `error: ${error?.message || error}`;
    return probeResult(null, null, firstByteMs, bytes, gaps, endReason, started);
  } finally {
    clearTimeout(hardStop);
    clearTimeout(idleTimer);
    if (!controller.signal.aborted) controller.abort();
    probeRunning = false;
    log("probe_done", { bytes, endReason });
  }
}

// ---------------------------------------------------------------------------
// HTTP surface
// ---------------------------------------------------------------------------
function json(res, status, body) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(JSON.stringify(body, null, 2));
}

async function summary() {
  const perPortal = [];
  for (const portal of PORTALS.portals) {
    const rows = samples.filter((s) => s.portalId === portal.id);
    const ok = rows.filter((s) => s.ok);
    const latest = [...ok].reverse()[0] || null;
    const withCloudflare = [];
    for (const episode of episodes(portal.id).slice(0, 10)) {
      withCloudflare.push({ ...episode, ourTraffic: await cloudflareViewers(episode.start, episode.end) });
    }
    perPortal.push({
      portal: { id: portal.id, host: safeHost(portal.url), user: mask(portal.username) },
      latest: latest && {
        ts: latest.ts,
        activeCons: latest.activeCons,
        maxCons: latest.maxCons,
        status: latest.status,
        expDate: latest.expDate,
      },
      samples: { total: rows.length, ok: ok.length, failed: rows.length - ok.length },
      saturationEpisodes: withCloudflare,
    });
  }
  return {
    service: "korazero-stream-diagnostic",
    now: new Date().toISOString(),
    config: {
      censusIntervalMs: CENSUS_INTERVAL_MS,
      retainHours: RETAIN_HOURS,
      probeEnabled: ALLOW_PROBE,
      cloudflareCorrelation: Boolean(CF_TOKEN && CF_ZONE),
      portals: maskedPortals,
    },
    portalConfigError: PORTALS.ok ? null : PORTALS.error,
    perPortal,
    readingGuide: {
      foreignOccupancy:
        "saturationEpisodes whose ourTraffic.streamingViewerIPs is 0 mean the line was full while none of the connections came through our site.",
      ourOwnContention:
        "episodes with streamingViewerIPs >= 1 are our own viewers filling the line — the single-slot limit, not an intruder.",
    },
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  try {
    if (url.pathname === "/health") {
      return json(res, PORTALS.ok ? 200 : 503, { ok: PORTALS.ok, error: PORTALS.error, samples: samples.length });
    }
    if (url.pathname === "/api/census") {
      const limit = Number(url.searchParams.get("limit") || 500);
      return json(res, 200, { count: samples.length, samples: samples.slice(-limit) });
    }
    if (url.pathname === "/api/summary" || url.pathname === "/") {
      return json(res, 200, await summary());
    }
    if (url.pathname === "/api/probe" && req.method === "POST") {
      const streamId = url.searchParams.get("stream") || "";
      if (!streamId) return json(res, 400, { ok: false, error: "stream query parameter required" });
      return json(res, 200, await runProbe(streamId));
    }
    return json(res, 404, {
      ok: false,
      error: "not found",
      routes: ["/health", "/api/summary", "/api/census", "POST /api/probe?stream=<id>"],
    });
  } catch (error) {
    log("server_error", { message: String(error?.stack || error) });
    return json(res, 500, { ok: false, error: "diagnostic server error" });
  }
});

server.listen(PORT, "0.0.0.0", () => {
  log("listen", {
    port: PORT,
    portals: maskedPortals.length,
    probeEnabled: ALLOW_PROBE,
    cfCorrelation: Boolean(CF_TOKEN && CF_ZONE),
  });
  if (!PORTALS.ok) {
    log("config_error", { error: PORTALS.error });
    return;
  }
  censusLoop();
  const timer = setInterval(censusLoop, CENSUS_INTERVAL_MS);
  timer.unref?.();
});
