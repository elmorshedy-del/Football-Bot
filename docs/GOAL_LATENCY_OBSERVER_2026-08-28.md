# Kalshi Goal-Latency Observer — implementation record

Date: 2026-08-28

## Purpose

Measure when Kalshi's structured live score first becomes observable relative to
Kalshi order-book and trade changes. This is an observation-only experiment. It does
not provide context to the detector and cannot alter entries, exits, sizing, prices,
fees, or paper execution.

## External API surfaces

- Resolve a watched event ticker with `GET /milestones`, filtered by
  `related_event_ticker`.
- Poll all resolved milestone IDs with one `GET /live_data/batch` request.
- Do not request player statistics.

Kalshi documents `live_data.details` as flexible JSON. The observer walks the object
deterministically and retains numeric values at or below keys containing `score`,
accepting camelCase, snake_case, nested objects, and lists. A numeric increase is
labelled `goal`; a decrease is labelled `score_correction`. The initial response is
baseline and never creates a goal observation.

## Clock and latency definitions

- `previous_poll_ts`: local wall-clock receipt of the prior successful score response.
- `poll_started_ts`: local wall clock immediately before the current request.
- `observed_ts`: local wall clock immediately after the changed response arrives.
- `response_ms`: request duration measured using the local monotonic clock.
- `poll_uncertainty_ms`: interval between prior and changed response receipts. The
  score change occurred somewhere inside this bound; no more exact claim is made.
- `last_book_lead_ms` / `last_trade_lead_ms`: positive local-arrival lead of the nearest
  pre-score best-book change or trade.
- `first_book_after_ms` / `first_trade_after_ms`: first corresponding observation in
  the configured post-score window.

Market and score observations originate inside the same process, so their relative
monotonic timestamps do not depend on synchronizing two server clocks.

## Persistence and inspection

- SQLite table: `goal_latency_observations`.
- API: `GET /api/goal-latency?limit=100`.
- Runtime health: `/api/status` → `goal_latency`.
- The raw Kalshi live-data object and complete pre-score market window are stored in
  each row's `detail` JSON for independent review.

## Isolation and reversal

- The observer is instantiated only from the engine's live-mode startup block.
- It receives callables exposing event tickers and a read-only copy of recent market
  observations. It has no detector or paper-desk reference.
- WebSocket handling performs only bounded in-memory deque appends. SQLite writes and
  score polling occur in the observer task.
- Set `GOAL_LATENCY_OBSERVER=false` to disable it without code changes.
- Reverting this pull request removes the module, table, API route, and observation
  hooks as one isolated unit.
