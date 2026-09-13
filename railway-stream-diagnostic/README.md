# KoraZero stream diagnostic

Provider-side diagnostic for the Xtream line behind `korazero.com`. It answers the
one question Cloudflare analytics cannot: **when the line was full, were any of
those connections ours?**

## Why this exists

Cloudflare can only see requests that reach the edge. If someone else is holding
the provider's connection slot, their traffic never touches Cloudflare and is
invisible there. This service polls the provider's own connection census and
correlates it against our edge traffic.

## How it works

`player_api.php` is a **metadata** endpoint — it reports `active_cons` and
`max_connections` without opening a stream, so polling it **costs no connection
slot**. Samples are kept in memory and grouped into *saturation episodes*:
contiguous runs where `active_cons >= max_connections`, i.e. windows in which a
viewer would be refused.

For each episode the service asks Cloudflare how many of our own viewers were
streaming at that moment:

- `streamingViewerIPs = 0` → the line was full while **none** of the connections
  came through our site. Occupancy is not ours.
- `streamingViewerIPs >= 1` → our own viewers filled the line. That is the
  single-slot limit, not an intruder.

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET /health` | liveness plus config validity |
| `GET /api/summary` | current census, saturation episodes, Cloudflare correlation |
| `GET /api/census?limit=N` | raw sample series |
| `POST /api/probe?stream=<id>` | byte-timing probe (**off by default**) |

## Configuration

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `XTREAM_PORTALS_JSON` | yes | — | same value/shape as the Worker secret |
| `CF_API_TOKEN` | no | — | enables Cloudflare correlation (Zone → Analytics → Read) |
| `CF_ZONE_TAG` | no | — | zone id for `korazero.com` |
| `CENSUS_INTERVAL_MS` | no | `15000` | poll cadence |
| `RETAIN_HOURS` | no | `48` | in-memory retention |
| `DIAG_ALLOW_PROBE` | no | unset | set to `1` to permit the slot-consuming probe |
| `PROBE_IDLE_MS` | no | `15000` | abort the probe after this much upstream silence |

Credentials are read from the environment only. They are never logged, never
returned in a response, and the username is masked in all output.

## The probe consumes a slot

`POST /api/probe` opens a real stream and therefore **takes the line's single
connection**. It is disabled unless `DIAG_ALLOW_PROBE=1`, refuses to run when the
latest census shows the line already saturated, and always aborts the upstream
fetch when it stops. It reports first-byte latency, inter-chunk gap percentiles,
throughput, and why the stream ended — which is what distinguishes a provider
that goes silent from one that closes cleanly.
