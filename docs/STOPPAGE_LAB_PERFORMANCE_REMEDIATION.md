# Stoppage Lab — dashboard responsiveness remediation

_2026-09-02_

This addresses the production failure carried over from PR #15 / commit
`af24bf9`: the dashboard degrades until requests that should be trivial take
10–40 s and the WebSocket drops with code 1006, "Fetch aborted", "Load failed",
and HTTP 499.

It is an infrastructure / data-serving refactor. It does **not** touch the
trading strategy, paper-execution semantics, signal generation, fee model,
captured study evidence, mode separation, or the raw recorder. The one
computation that changed — the event-clustered bootstrap — is proven to return
byte-for-byte identical output (`test_event_cluster_ci_matches_reference`).

## Root cause (confirmed in code)

Every REST handler in `app/main.py` was `async def` yet called the synchronous
SQLite layer (`store.q`, `store.stats`) **directly on the event loop**, and
`store.q`/`store.ex` shared one connection behind one global lock. So:

- One expensive read (`/api/stats` runs a 2,000-iteration event-clustered
  bootstrap; `/api/signals`/`/api/trades` scan and decorate up to 500 rows;
  `/api/latency` scanned an unindexed, fast-growing table eight times) blocked
  the **entire** application while it ran.
- That is why intrinsically trivial endpoints — `/api/config` (a static dict)
  and `/api/status` — were themselves taking 20 s: they were starved, not slow.
- The WebSocket `hello` and the 5 s stats broadcast also computed `store.stats()`
  on the loop, so a reconnect storm (the client retries every 2.5 s) and every
  active-match tick piled more blocking work onto the same loop.

The 1006 / aborted / 499 / "load failed" errors were one starvation cascade,
not four independent bugs.

## Changes

### Data layer (`app/store.py`)
- **Read/write isolation.** Dashboard reads now run on per-thread **read-only**
  connections (WAL, autocommit, `query_only`) that do **not** take the writer
  lock, so a slow analytics scan can no longer stall a live collector write, or
  vice versa. The collector/event-loop path is unchanged: it still uses the
  single writer under `_lock` with all its existing transactions.
- **`store.read(fn, …)`** dispatches a read function onto a worker thread
  (`asyncio.to_thread`) — the seam that keeps the event loop free.
- **`stats()` memoisation** keyed on the writer's total change count: concurrent
  dashboard/WebSocket callers share one computation while the study is
  unchanged, and **any write invalidates it**, so it is never served stale. The
  cache is dropped on `init()`.
- **Bootstrap made O(clusters), not O(fills)** per iteration by resampling
  per-cluster `(sum, count)` instead of rebuilding a flat list of every fill.
  Same seed, same draw order, identical interval — verified against a reference
  implementation of the old algorithm.
- **Missing indexes added:** `trades(signal_id)` (the `signals ⋈ trades` join in
  `stats()` was O(signals×trades) — a full trades scan per signal), `trades(status)`,
  `paper_fills(trade_id)`, and `latency(kind, ts)`.

### Application layer (`app/main.py`, `app/engine.py`)
- Every read endpoint now does its DB work via `store.read(...)` off the loop.
  In-memory engine state that the loop mutates (live position marks in
  `/api/trades`, clock coverage in `/api/match-clocks`) is snapshotted on the
  loop first, so nothing races the engine.
- The WebSocket `hello` and the engine's 5 s stats broadcast compute `stats()`
  off the loop.
- **Instrumentation:** a pure-ASGI timing middleware stamps a `Server-Timing`
  header (so a browser separates server time from edge/network time) and feeds
  a new **`GET /api/perf`** endpoint that reports per-endpoint server time and
  read-connection SQLite time — the observability the handoff asked for.

### Client (`static/app.js`)
- WebSocket-driven refreshes are coalesced to at most one full reload per 3 s, so
  a burst of live events no longer fans out into a dozen historical scans every
  few hundred milliseconds. Stats/status still arrive live on every socket
  message; the 30 s safety poll and direct `refreshAll()` are unchanged.

## Before / after (production-sized DB: 3,200 trades, 3,200 signals, 60,000 latency rows)

Measured end-to-end through the real ASGI stack, saturating the heavy endpoints
with 6 concurrent workers while sampling `/api/config`:

| Metric | Before (reads on loop) | After |
| --- | --- | --- |
| `/api/config` under load | **0 completions in 4 s** (fully starved) | p50 **1.6 ms**, p95 **3.0 ms** |
| `/api/stats` (cold, single request) | ~13 s | **277 ms** |
| `/api/latency` (cold) | full scan ×8 | **5 ms** |
| `/api/signals?limit=500` (cold) | — | 218 ms |
| `/api/trades?limit=500` (cold) | — | 272 ms |

The event loop served ~2,300 `/api/config` requests during the same 4 s window
in which heavy reads ran — under the old model it served none.

## How to verify in production
- `GET /api/perf` — recent per-endpoint server time, the slowest requests, and
  read-connection timing (`db_reads`).
- `Server-Timing: app;dur=<ms>` on every API response (browser devtools →
  Network → Timing) isolates server processing from edge/network latency.
- Slow requests (≥ 1 s) are logged at WARNING as `slow request …`.

## Acceptance criteria
- Lightweight endpoints respond well under 1 s under load — **met** (≈2 ms).
- DB reads no longer block the async loop for multi-second periods — **met**.
- A busy live event stream no longer collapses REST responsiveness — **met**.
- No data lost or omitted for performance; evidence integrity preserved — **met**
  (serving-layer only; bootstrap output identical).
- Before/after measurements and per-operation server timing included — **met**.
