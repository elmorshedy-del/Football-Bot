# Dual-Sleeve Study Change and Validation Log

Append entries as work happens. Each entry must state what changed, why, tests/evidence,
and remaining risk. This is the reviewer-facing record for the original repository developer.

## 2026-08-30 — Baseline verification

Status: `PASSED`

Observed without modifying stored data:

- Railway project: `Football-Bot`; service: `football-bot`; environment: `production`.
- Source: `elmorshedy-del/Football-Bot`, branch `main`, commit
  `51eaf718d4f3e277e71d496bb8eda0fa83cdcd6b`.
- Successful deployment: `546ba249-55c6-4119-b4da-159243d285c5`.
- Persistent volume mount: `/srv/data`.
- Public health response: live and healthy.
- Runtime status at verification: WebSocket connected; 9 markets across 3 matches;
  recorder healthy with 45,261 recorded messages and zero recorder failures;
  no credential error; median feed lag 14.0 ms and p95 156.1 ms.
- Study switches at verification: realistic paper execution enabled;
  price-only sleeve enforced; diagnostic goal observer disabled pending the auditable UI.

Validation performed:

- Railway deployment and deployment history inspected.
- `GET /api/health` returned `{"ok":true,"mode":"live"}`.
- `GET /api/status` returned `ws=connected`, `recorder.healthy=true`, and `cred_error=""`.

Remaining risk:

- Only the price-only sleeve is currently active; independent parallel Gate A execution is not yet implemented.
- The current dashboard does not expose canonical event data or study health clearly.

## 2026-08-30 — Canonical event schema

Status: `PASSED`

Work completed:

- Added deterministic, versioned `football.match_event.v1` normalization.
- Canonical records include type, side, before/after score, transition, human label, and
  provider minute/stoppage/period/clock fields when present.
- Added additive SQLite migration fields for canonical type, side, and normalized event JSON.
- Preserved the provider observation as an unchanged raw JSON source record beside normalization.
- Added event/state consistency classification for later temporal audit matching.

Validation performed:

- Ran `.venv/bin/python -m unittest tests.test_match_events tests.test_goal_latency tests.test_store_execution -v`.
- All 16 tests passed, including home goal, away equalizer, score correction, nested total-score
  preference, missing/provider clock fields, fresh schema, and old-volume additive migration.
- SQL insert paths were exercised by the store execution suite.

Remaining risk:

- Provider schemas not represented in captured fixtures may add clock or score keys; their raw
  observations remain available for a versioned normalizer update without mutating history.

## 2026-08-30 — Independent paper sleeves

Status: `PASSED`

Work completed:

- Added explicit `gate_a` and `price_only_late_score` identity to signals, trades,
  positions, entry timing, exit broadcasts, and additive migration support.
- Added `parallel` mode, which dispatches copies of one confirmed market episode to both
  strategy admission paths without mutating the source candidate.
- Isolated simulated entry and exit depth into one counterfactual shadow book per strategy.
- Moved fill lockouts out of the shared detector and keyed them by `(strategy, market)`.
- Reconstructed strategy-specific lockouts from restored open positions at startup.

Validation performed:

- Ran `.venv/bin/python -m unittest discover -s tests -v`; all 71 tests passed.
- Added tests proving both strategies receive the same full source depth, while each consumes
  only its own shadow depth and never the displayed live order book.
- Added tests proving a price-sleeve rejection does not block Gate A, a Gate A exit cannot
  consume sleeve exit depth, and the price-sleeve lockout cannot lock Gate A.
- Added restart coverage for strategy identity, sleeve detail, remaining size, realized gross,
  entry/exit fees, accumulated exit quantity, exit value, adverse excursion, and stop state.
- `git diff --check` passed.

Remaining risk:

- Shadow-book depletion is intentionally process-local; after a restart, fresh exchange depth is
  the new counterfactual baseline. Durable open-position quantities and economics are restored.
- `parallel` remains paper-only; no real order submission path exists.

## 2026-08-30 — Trigger-to-event audit matching

Status: `PASSED`

Work completed:

- Added a deterministic nearest-observation matcher using the unchanged stored event ID and a
  configured ±20-second local-receipt window.
- Added exact exchange signal, local signal receipt, simulated order arrival, entry, exit,
  settlement, and provider observation timestamps where each boundary exists.
- Added the frozen trigger thresholds and observed displacement, levels, contracts, sibling lag,
  reference/extreme prices, inferred state, triplet probabilities/deltas, spread, and coherence.
- Added explicit market-first/feed-first timing, equalizer/+1 consistency, state mismatch, and
  correction/reversal classifications.
- Every match response carries `causality=not_established` and explanatory text; the observer
  remains disconnected from strategy and execution code.
- Added matched observation ID, canonical event, provider polling uncertainty/response time, and
  raw provider payload while preserving stored market/event identifiers.

Validation performed:

- Ran `.venv/bin/python -m unittest tests.test_audit -v`; all 10 audit tests passed.
- Tests cover market-first, feed-first, nearest same-event selection, equalizer consistency,
  one-goal-lead consistency, correction/reversal, state mismatch, no nearby event, exact
  execution/settlement timestamps, and additive identifiers.
- The full suite reached 81 passing tests; `git diff --check` passed.

Remaining risk:

- Provider polling creates an observation interval, not a precise game-event occurrence time;
  polling uncertainty is exposed and must be included in downstream inference.
- Temporal/state consistency is evidence for study, never proof that an event caused a trade.

## 2026-08-30 — Separate sleeve accounting

Status: `PASSED`

Work completed:

- Added durable Gate A, price-only late-score, and combined accounting views.
- Each reports open/closed count, closed gross/fees/net, win rate, net per closed fill,
  confidence interval, signal outcomes, exit reasons, open remaining contracts, and separate
  partially realized gross/accrued fees/net for active positions.
- Added fill-integrity and confidence-interval evidence gates per sleeve. Execution-arrival
  latency is explicitly labeled as a shared-adapter gate rather than falsely allocated.
- Preserved all existing top-level statistics for compatibility and added a structured
  `combined` plus `sleeves` response.
- Mapped null, blank, unknown legacy, and Gate A records to `gate_a`; normalized the prior
  versioned price-sleeve label to `price_only_late_score`.

Validation performed:

- Added exact reconciliation coverage for closed/open count, gross, fees, net, remaining
  contracts, and partial-position economics.
- Added zero-state, legacy null strategy, signal allocation, exit-reason, and per-sleeve
  evidence sample-size tests.
- Ran the full suite; all 83 tests passed. `git diff --check` passed.

Remaining risk:

- Open-position headline marks remain separate from realized P&L. This prevents unfilled or
  non-executable marks from being presented as locked-in profit.
- Statistical evidence is still collecting; no strategy is described as guaranteed.

## 2026-08-30 — Auditable frontend makeover started

Status: `BLOCKED` on rendered browser evidence; implementation complete.

Work completed:

- Replaced the legacy dashboard with a dependency-free, human-readable study console.
- Added independent Gate A and price-only P&L cards with closed realized net, fees, open marks,
  partial-position economics, win rate, confidence interval, and sample state.
- Added full UTC dates, human match/contract names, decision reasons, frozen trigger math,
  price-only inference, canonical matched events, non-causal timing relation, exact execution
  timeline, raw identifiers, and raw normalized/provider evidence.
- Added canonical event-feed, open-position, closed-trade, activity, latency, exit-reason,
  cumulative per-sleeve P&L, and evidence-gate views with labels and units.
- Added a persistent health panel for market WebSocket, browser WebSocket, recorder, match-event
  diagnostic, paper execution, SQLite, credentials, current faults, and recent errors.
- Replaced swallowed fetch/WebSocket failures with visible state, error history, toast, and retry.
- Removed third-party font/chart dependencies and implemented responsive SVG/CSS charts.
- Added 760 px and 420 px layouts, wrap-anywhere audit text, and no ellipsis truncation.

Validation performed:

- `node --check static/app.js` and `git diff --check` passed.
- Seven frontend/health contract tests passed, covering required audit/health surfaces, additive
  display names, visible failure handlers, no swallowed promise failures, phone breakpoints,
  wrap/overflow rules, backend healthy/fault aggregation, and WebSocket error recording.

Blocked validation:

- Browser control rejected the local service URL and a self-contained fixture URL by policy.
  No bypass was attempted. Desktop/phone visual and injected browser-failure checks are deferred
  to the real public URL immediately after deployment in Step 8.

## 2026-08-30 — Downloadable study bundle

Status: `PASSED`

Work completed:

- Added an admin-protected dashboard download with a synchronous SQLite/raw-feed capture boundary.
- Rotated the active recorder stream into an immutable, valid gzip segment before selecting files;
  new frames open a new segment and are intentionally reserved for the next export.
- Included a SQLite snapshot and schema plus CSV and JSONL copies of markets, signals, trades,
  fills, latency, canonical match-event observations, and the event/error log.
- Included every selected raw WebSocket gzip segment, a non-secret allowlisted configuration,
  row counts, byte sizes, and SHA-256 hashes for every exported artifact.
- Added a replay/backtest architecture contract covering time-safe reconstruction, independent
  shadow books, walk-forward selection, event-clustered inference, adversarial tests, immutable
  run artifacts, and new-data champion/challenger decisions.
- Kept match-event observations diagnostic-only and explicitly stated that no return is guaranteed.
- Added a visible browser failure path and backend error recording; failed exports do not stop the
  recorder, market stream, or paper engine.

Validation performed:

- Ran `.venv/bin/python -m unittest tests.test_exporter tests.test_recorder tests.test_main_security -v`;
  all 8 tests passed.
- Opened the archive with `ZipFile.testzip`, parsed every CSV and JSONL file, decompressed the raw
  gzip, reopened the SQLite snapshot, and reconciled every table count to the manifest.
- Recomputed and matched every artifact hash and byte size, and scanned all uncompressed archive
  members for injected admin/private-key markers.
- Proved recorder rotation keeps the exported gzip immutable and writes subsequent records to a
  fresh valid file; proved export failure records a system fault and returns a controlled 500.
- `node --check static/app.js` and `git diff --check` passed.

Remaining risk:

- Raw exchange recordings can be large; archive creation is moved off the event loop after the
  short capture boundary, but still consumes temporary disk and CPU. Failures are explicit.
- The archive supplies replay inputs and validation rules; the external backtest engine remains
  intentionally out of scope for this pull request.

## 2026-08-30 — Pre-pull-request verification and review fixes

Status: `PASSED` locally; GitHub Continuous Integration and deployment still pending.

Review findings fixed:

- Preserved the provider's original market title while additively persisting `display_game` and
  `display_leg` from provider metadata. This prevents abbreviated ticker suffixes from becoming
  primary contract labels and handles both legacy `Match Winner?` and newer leg-only title forms.
- Added a deterministic provider-rules fallback for the matchup label when the provider title
  names only one contract; raw title, event, market, and series identifiers remain unchanged.
- Added the last match-feed poll's full UTC time and HTTP response latency to the runtime strip.
- Marked a mapped but stale diagnostic feed unhealthy and exposed target poll interval, response
  duration, mapping count, last poll, and last error in system status.
- Added a recent-backend-fault health check so a recorded export, discovery, settlement, or other
  operational error cannot coexist with an `ALL SYSTEMS GOOD` banner for the next five minutes.
- Corrected fee copy in both sleeve and combined summaries so costs are displayed as unsigned
  dollar amounts instead of an awkward sign-transformation string.
- Removed a split JSON helper and unused imports found during source review.

Validation performed:

- Ran the complete Continuous-Integration test command with Python development warnings enabled;
  all 98 tests passed.
- Ran `python -m compileall -q app tests`, `node --check static/app.js`, and `git diff --check`.
- Started the real FastAPI application in demo mode with both sleeves parallel and realistic
  execution enabled. Verified `health.ok=true`, both sleeve mode, the complete health-check set,
  human labels `Espanyol`, `Draw`, and `Real Madrid`, and both match-feed timing fields in served HTML.
- Separately smoke-tested `/api/export`: missing credentials returned 401, a correct admin token
  returned a named ZIP attachment, and Python's ZIP validator reported no corrupt member.
- Verified the selected GitHub repository is `elmorshedy-del/Football-Bot`, default branch `main`,
  with push/admin permission. The change remains isolated on `codex/dual-sleeve-audit-dashboard`.

Remaining work:

- Push the isolated branch, open the pull request, wait for GitHub Continuous Integration, perform
  the final diff audit, merge, deploy paper-only, and complete rendered checks on the public URL.
