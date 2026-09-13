/**
 * Live stream telemetry for the Xtream media proxy.
 *
 * Why beacons instead of console.log:
 * Workers flush an invocation's console output when the invocation ENDS. A
 * stream that hangs for 45 minutes therefore logs nothing for those 45 minutes
 * — exactly the window worth watching. These beacons leave the isolate over the
 * wire while the stream is still open.
 *
 * Why the beacons are rationed:
 * Workers cap subrequests per request (50 on the free plan). A fixed 10s
 * heartbeat on a 45-minute stream would need ~270 and would break the stream
 * itself. So beacons fire on state transitions plus a heartbeat that backs off
 * exponentially, under a hard budget. A four-hour stream still fits.
 *
 * Safety: every send is fire-and-forget and swallows its own errors. Telemetry
 * can never delay, throw into, or break the media path. With no STREAM_TELEMETRY_URL
 * configured every entry point is a no-op and costs nothing.
 */

const MAX_BEACONS = 25;
const FIRST_HEARTBEAT_MS = 10_000;
const MAX_HEARTBEAT_MS = 600_000;
const TERMINAL_PHASES = new Set(["end", "abort", "error"]);

function noop() {}

const DISABLED = Object.freeze({
  sid: null,
  onStart: noop,
  onFirstByte: noop,
  onChunk: noop,
  onEnd: noop,
});

/**
 * @param {object} env    Worker env. Needs STREAM_TELEMETRY_URL, optionally STREAM_TELEMETRY_TOKEN.
 * @param {object} ctx    Execution context, for waitUntil. Optional.
 * @param {object} meta   { channel } — free-form label for the stream being watched.
 */
export function createStreamTelemetry(env, ctx, meta = {}) {
  const endpoint = env?.STREAM_TELEMETRY_URL;
  if (!endpoint) return DISABLED;

  const token = env?.STREAM_TELEMETRY_TOKEN || "";
  const sid = (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`).slice(0, 64);
  const started = Date.now();

  let budget = MAX_BEACONS;
  let bytes = 0;
  let lastChunkAt = started;
  let nextHeartbeatAt = started + FIRST_HEARTBEAT_MS;
  let heartbeatInterval = FIRST_HEARTBEAT_MS;
  let firstByteMs = null;
  let finished = false;

  function send(phase, extra = {}) {
    const terminal = TERMINAL_PHASES.has(phase);
    // Always reserve enough budget to report how the stream actually ended.
    if (!terminal && budget <= 1) return;
    if (terminal && finished) return;
    if (terminal) finished = true;
    budget -= 1;

    const body = JSON.stringify({
      sid,
      phase,
      t: Date.now() - started,
      bytes,
      ttfb: firstByteMs,
      ch: meta.channel ?? null,
      ...extra,
    });

    const headers = { "content-type": "application/json" };
    if (token) headers["x-ingest-token"] = token;

    const pending = fetch(endpoint, { method: "POST", headers, body }).catch(noop);
    if (ctx?.waitUntil) ctx.waitUntil(pending);
  }

  return {
    sid,

    onStart() {
      send("open");
    },

    onFirstByte() {
      if (firstByteMs != null) return;
      firstByteMs = Date.now() - started;
      send("first-byte");
    },

    /** Call once per chunk with the chunk's byte length. Cheap; usually a no-op. */
    onChunk(length) {
      const now = Date.now();
      const gap = now - lastChunkAt;
      lastChunkAt = now;
      bytes += length || 0;
      if (now < nextHeartbeatAt) return;
      heartbeatInterval = Math.min(heartbeatInterval * 2, MAX_HEARTBEAT_MS);
      nextHeartbeatAt = now + heartbeatInterval;
      send("heartbeat", { gap });
    },

    /** reason: "upstream EOF", "upstream idle > 15000ms", "client gone", … */
    onEnd(reason, phase = "end") {
      send(phase, { reason: String(reason || "unknown"), gap: Date.now() - lastChunkAt });
    },
  };
}
