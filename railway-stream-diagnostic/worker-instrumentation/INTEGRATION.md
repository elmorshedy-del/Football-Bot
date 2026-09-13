# Wiring live stream telemetry into the Worker

Target: `backend/adapters/xtream-media-safe.js` as it stands **after** commit
`ae812e3` (the version with `readOrTimeout` / `abortUpstream`). Line references
are from that version.

The adapter is covered by `scripts/verify-stream-lock.mjs`, so this needs the
same deliberate unlock-and-rebaseline that `ae812e3` used. It is not a silent edit.

## 1. Set the variables

```
npx wrangler secret put STREAM_TELEMETRY_TOKEN     # must match INGEST_TOKEN on the collector
```

and in `wrangler.toml` under `[vars]`:

```toml
STREAM_TELEMETRY_URL = "https://<collector-domain>/api/ingest"
```

With `STREAM_TELEMETRY_URL` unset, every telemetry call is a no-op and costs
nothing — so it is safe to merge before the collector exists.

## 2. Import and create

```js
import { createStreamTelemetry } from "./stream-telemetry.js";

export async function proxyXtreamMediaSafe(request, env, token, ctx) {
  const telemetry = createStreamTelemetry(env, ctx, { channel: null });
  telemetry.onStart();
  const target = await decodeMediaToken(env, token);
  ...
```

`ctx` is optional — without it the module falls back to a detached promise. Pass
it if you want the beacon guaranteed to drain: in `backend/routes/xtream.js:47`,
forward whatever execution context the route already receives.

## 3. Thread it through `sniffBody`

`sniffBody(response, upstreamController, timeouts)` gains a fourth argument, and
the call site at the bottom of `proxyXtreamMediaSafe` passes `telemetry`.

Inside the sniff loop, mark the first real bytes:

```js
if (next.value?.byteLength) {
  telemetry.onFirstByte();
  chunks.push(next.value);
  total += next.value.byteLength;
}
```

## 4. Instrument the pump

This is the part that matters — it is the only place that knows a live stream
has gone quiet.

```js
const pump = async () => {
  try {
    while (true) {
      const next = await readOrTimeout(reader, timeouts.pump, upstreamController, "media stream");
      if (next.done) {
        telemetry.onEnd("upstream EOF");
        controller.close();
        return;
      }
      if (next.value?.byteLength) {
        telemetry.onChunk(next.value.byteLength);
        controller.enqueue(next.value);
      }
    }
  } catch (error) {
    telemetry.onEnd(error?.message || String(error), "abort");
    abortUpstream(upstreamController, error);
    controller.error(error);
  }
};
```

And in `cancel(reason)`, which fires when the viewer walks away:

```js
cancel(reason) {
  telemetry.onEnd(reason || "downstream cancelled", "abort");
  abortUpstream(upstreamController, reason || "downstream cancelled");
  return reader.cancel(reason);
}
```

## What you get

`GET /api/streams` on the collector, live:

- `open` — streams currently delivering, with running byte totals and gap percentiles
- `stalled-or-lost` — beacons stopped without an end reason: hung right now
- `recent` — completed streams with the reason each one ended

`GET /api/streams/<sid>` gives the full per-stream event timeline.

The distinction the Cloudflare data could never draw — *upstream EOF* versus
*client gone* versus *idle abort* — arrives here as an explicit `endReason` on
every single stream.

## Budget note

Beacons fire on state transitions plus a heartbeat that doubles its interval
(10s, 20s, 40s …), capped at 25 beacons per stream. A four-hour stream stays
well inside the free plan's 50-subrequest limit. One beacon is always held in
reserve so the terminal event is never the one that gets dropped.
