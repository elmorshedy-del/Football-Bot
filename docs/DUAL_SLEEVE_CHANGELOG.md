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

## 2026-08-30 — Independent final code review

Status: `PASSED`

Evidence reviewed:

- Pull request: `#8`, `codex/dual-sleeve-audit-dashboard` into `main`; scoped implementation and
  audit-record commits across 32 implementation/review paths,
  mergeable without conflicts, no unresolved review threads, and independently revertible from
  the original price-only sleeve pull request.
- GitHub Continuous Integration runs `33322017843` and `33322118820` completed successfully. The
  latest head executed all 98 tests, compile checks, and the repository's pinned Ruff fatal-error
  checks on Python 3.12.
- GitHub tree `2666249838148d64a6f3b44c72773dc88744464b` exactly matched the locally tested
  commit tree; the connector-created commit changed precisely the 32 reviewed paths.

Final audit conclusions:

- **Entry mathematics:** the sleeve normalizes exactly three positive executable midpoints,
  requires a timed baseline, fresh legs, bounded spreads, target gain/post-state, sibling outflow,
  and positive direction. Labels remain inferred states, not observed scores.
- **Simulation independence:** strategy identity is durable; lockouts key by strategy and market;
  Gate A and price-only have separate shadow books, positions, exit queues, and P&L allocation;
  the live order book is never mutated.
- **Execution realism:** entry/exit latency, latest valid books, IOC depth walking, partial fills,
  per-level verified taker fees, unsupported-fee rejection, sequence-gap invalidation, settlement,
  retries, and restart restoration are covered by deterministic tests.
- **Leakage and safety:** no match-event, score-before/after, or normalized-event reference exists
  in the sleeve, detector, or paper modules. No real-order/portfolio endpoint exists. Export uses an
  explicit non-secret allowlist and does not serialize the environment.
- **Audit semantics:** raw IDs/payloads remain intact; canonical events are versioned; provider
  receipt uncertainty is explicit; nearest-event joins are same-match/fixed-window and always
  state `causality=not_established`.
- **Failure visibility:** stream, recorder, diagnostic feed, paper adapter, database, credentials,
  dashboard WebSocket, stale polls, recent backend faults, and client API errors all have visible
  states; a recent recorded backend error prevents a false green banner.
- **Frontend and export:** additive human labels cover provider title variants; full UTC times and
  match-feed response latency are visible; phone breakpoints contain no ellipsis; exported tables,
  database, raw gzip, hashes, and backtest handoff reconcile and parse.

Non-blocking limitations accepted:

- No strategy can be guaranteed profitable or lossless; latency and disappearing liquidity can
  turn a scratch trigger into a loss.
- Provider observation time bounds feed receipt, not exact on-field event occurrence.
- Legacy rows created before additive display metadata may retain a humanized ticker suffix when
  the provider's historical contract subtitle is unavailable; all new discoveries persist it.
- External replay/backtesting and any later real execution module are intentionally not part of
  this pull request.

Remaining operational gate:

- Merge only the green pull request, deploy with both sleeves paper-only and event data diagnostic-
  only, then complete desktop/public rendered and persistent-volume checks.

## 2026-08-30 — Production deployment and rendered acceptance

Status: `PASSED`

Deployment evidence:

- Pull request `#8` was squash-merged into `main` as commit
  `633e3d083e29bfc14e1e92ea66c9d3e750d7a01f`; the implementation is revertible as one merge
  commit and remains separate from the original price-only sleeve change.
- GitHub Continuous Integration run `33322165031` passed the 98-test suite, compile checks, and
  pinned Ruff checks for the final pull-request head.
- Railway production deployment `7ea5361e-24b4-49e8-85df-6b15a55075f2` completed with
  `SUCCESS`. Only the `football-bot` service was changed. The persistent volume remains mounted
  at `/srv/data`.
- Production variables enable `PRICE_ONLY_SLEEVE_MODE=parallel`, `PAPER_EXECUTION_V2=true`, and
  `GOAL_LATENCY_OBSERVER=true`; both sleeves are paper simulations and the match-event feed remains
  diagnostic-only. A new admin token protects study exports and is stored only in Railway.
- Public `/api/status` reported live mode, `health.ok=true`, WebSocket connected, recorder
  recording, match-event observer observing, paper execution ready, database connected,
  credentials configured, no recent backend faults, and zero recent errors. The observer reported
  a 250 ms target poll interval and a 12.1 ms latest response during acceptance.
- Public `config`, `stats`, `equity`, `signals`, `trades`, and `goal-latency` endpoints returned 200.
  The export endpoint returned 401 without its admin token, confirming the production boundary.
- Match-event row `701`, observed on the prior UTC day, remained queryable after the consecutive
  production deployments. This verifies SQLite study data persisted through restart on the mounted
  volume; the live raw recorder also continued writing without a recorded failure.

Rendered acceptance:

- The public dashboard rendered at 1363 x 936 with no horizontal page overflow and no application
  console warning or error. The visible page showed `ALL SYSTEMS GOOD`, both independent sleeve
  P&L panels, full UTC timestamps, feed response latency, human-readable match and contract names,
  raw-ID audit details, decision reasons, and the study-download control.
- The same public deployment rendered inside a 320 px phone viewport. The document measured
  305 px client width and 305 px scroll width, proving no horizontal overflow; `Sound off`,
  `Download study data`, and `Kill switch` remained visible and the sleeve panels stacked.
- Deterministic health tests prove recorder/database/WebSocket faults and recent backend errors
  make health non-green; frontend contract tests prove fetch, initial-load, scheduled-refresh,
  export, and WebSocket errors are recorded and displayed instead of swallowed.

Final result:

- All nine checklist sections now pass. Live collection is running for evidence gathering, not
  evidence of profitability. No return, scratch exit, or loss limit is guaranteed because latency,
  gaps, fees, and disappearing liquidity remain real execution risks.

## 2026-08-30 — Large-volume export hardening

Status: `PASSED` locally; production acceptance pending.

Finding:

- An authenticated production stress check kept collection healthy but received no response bytes
  before a 90-second client timeout while the full historical archive was being prepared. The
  synchronous compatibility endpoint did not lose data, but its idle request made the dashboard
  download unsuitable for a growing persistent volume.

Correction:

- Added a protected prepare/status/download job flow. The dashboard now receives a job identifier
  immediately, shows elapsed preparation time, polls a short authenticated status endpoint, and
  starts a native browser download only when the file is ready.
- The native download uses an HttpOnly, Secure, SameSite-strict, path-scoped, job-specific cookie;
  neither the admin token nor job download token is placed in a URL or returned in JSON.
- Removed browser `Blob` buffering so large archives stream through the browser's download path.
- Stored already-gzipped raw feed members without redundant ZIP deflation and computed each raw
  SHA-256 once instead of reading every segment twice.
- Preserved `/api/export` as an authenticated compatibility endpoint and retained explicit failure
  recording without interrupting market collection.

Validation performed:

- Unit coverage proves prepare, polling, ready size, cookie-authorized download, missing-auth
  rejection, missing-token fail-closed behavior, and non-fatal preparation failure.
- Archive coverage proves raw gzip members use `ZIP_STORED` while all existing parsing, table-count,
  hash, SQLite, raw-gzip, and secret-exclusion assertions still pass.
- Frontend contract coverage proves the prepare/poll route is used and whole-archive Blob buffering
  is absent.

## 2026-08-30 — Operator dashboard and Al-Shabab event trace

Status: `PASSED` locally; pull-request and production acceptance pending.

Production finding reproduced from the preserved study ledger:

- The Al-Hazm vs Al-Shabab equalizer was not missing. Gate A signal `1169` opened trade `51` on
  Draw at 50c and exited at the 90c target for $80.00 gross, $4.80 fees, and +$75.20 net.
- The market signal reached the service at `2026-08-30 18:01:19.811772 UTC`, paper entry filled at
  `18:01:20.022890 UTC`, and the target exit filled at `18:01:32.569554 UTC`.
- Provider observation `746` was received 18.635 seconds after the signal. Its preserved last-play
  timestamp was 13.188 seconds after the signal and its description says Afimico Pululu scored
  from the spot to level the match 1-1 at 90+5.
- The independent price-only sleeve recorded the same market episode but declined it as
  `sleeve_outside_window`. The market's expected-expiration value was about 64 minutes after the
  90+5 event, proving that the global schedule proxy is not a reliable live minute-88 clock for
  this series. The trading logic was not changed without backtest evidence.

Changes made:

- Replaced the long dashboard with Overview, Trades & Events, Signals, League Performance, Live
  Markets, and System & Data tabs. Full health stays visible in the header and live markets are
  compact until expanded.
- Added synchronized trade/signal filters and sentence-first decisions. A trade now presents its
  trigger, entry, exit, economics, nearest canonical event, provider occurrence, provider receipt,
  and full UTC timeline together, while explicitly stating that causation is not established.
- Extended deterministic normalization for same-game score fields, stoppage clocks, scorer names,
  and explicit penalty language. Historical rows gain the improved display by re-reading their
  preserved raw payload; storage names and payloads remain unchanged.
- Restored sortable per-league results with combined/Gate A/price-only splits, small-sample
  warnings, and reconciled totals. Added cumulative net, drawdown, exact chart points, event-link
  outcomes, exit distributions, and a league-level timing-proxy diagnostic.
- Preserved independent sleeves and added a regression guard proving score/event-feed fields are
  absent from the price-only classifier, detector, and paper-execution modules.

Validation performed:

- `107` unit, migration, parser, execution, export, health, and frontend-contract tests pass.
- `node --check static/app.js`, `git diff --check`, demo startup, `/api/status`, `/api/config`,
  `/api/stats`, and all six rendered route markers pass locally.
- Local `/api/config` exposes the 20-second event-match audit window, 8c maximum sleeve spread,
  and 46 human-readable league names. Demo stats expose independent Gate A and price-only buckets.

## 2026-08-31 — Production integrity: clock gate, highs, split exports, UI

Status: implementation `PASSED` locally; independent final review, deploy, and § 11 production
evidence are pending. Paper-only. Do not merge until the reviewer clears specification §§ 11–12.

Motivating production evidence (from the specification's § 1, verified again against the deployed
schema before implementation):

- The 88+ sleeve was not producing a valid study. Production carried 86 `sleeve_outside_window`
  records and zero price-only classified or fill samples. The Al-Hazm vs Al-Shabab 90+5 equalizer
  signal was rejected because `expected_expiration_time` was 3,820.188 seconds after the signal —
  expected expiration is not the match clock.
- Downloads never reached the `/download` route. Railway HTTP logs showed accepted prepare
  requests and repeated status polling (single polls blocked for 32.4 and 93.6 seconds), but no
  `/download` request; RSS was 2.54 GB during review. A single multi-GB archive cannot be the
  only browser export path.
- Latency evidence was breached while the health banner said all was good. `/api/stats` reported
  total order-arrival p95 = 3,642.1875 ms with K4 `BREACH`; `/api/status` reported
  `health.ok = true`. `/api/latency` sampled only the newest 1,000 rows across all kinds, so
  frequent feed-lag rows crowded out order-arrival rows and left four visible K4 samples.
- The event table was sparse by construction: it stored score-signature changes, not a match-clock
  timeline or all provider events. Most signals therefore could not receive a match-minute stamp
  and could not legitimately be associated with a stored event.
- Closed trades did not retain a maximum favorable executable price: entry, exit, and MAE did not
  answer what highest bid was available or how many seconds after entry it occurred.

Changes made (six-commit stack on `cursor/production-integrity-clock-export-aaf8`, one revertible
draft PR):

1. `d0a02aa` — Persist match clocks and canonical provider events. New
   `app/match_clock.py` (parser + tracker), `match_clock_observations` and
   `provider_match_events` tables, immutable `signals.match_clock_snapshot`
   (`football.match_clock_stamp.v1`) on every new signal, `/api/match-clocks` and
   `/api/provider-events` endpoints. Config: `MATCH_CLOCK_MAX_AGE_MS = 2500`. Demo replay
   injects a synthetic 90+5 stamp.
2. `89d15d0` — Gate paper 88+ sleeve on persisted live clock. Narrow `MatchClockGate`
   accepts on mapping + live status + fresh age + second-half (or equivalent) + minute ≥ 88;
   `90'` and `90+N'` stay eligible while the market is open. Distinct decline outcomes are
   persisted as `sleeve_<outcome>`. Expected expiration is now a UI-only calibration diagnostic;
   Gate A detection, sizing, entry, exit, fee, lockout, and settlement behavior are unchanged.
   An AST/import allowlist test proves the classifier and paper desk cannot read
   score/scorer/goal/penalty/VAR/correction/narrative or canonical-event fields.
3. `f669841` — Record executable trade highs and latency readiness. `trades.max_executable_bid`,
   `trades.max_executable_bid_ts`, `trades.mfe_c`; held-side executable best bid only;
   ask/mid/last/settlement cannot update; equal high keeps first timestamp; partial-exit tracking;
   restart recovery. `/api/latency` samples per canonical kind (`feed_ingress_ms`, `decision_ms`,
   `paper_entry_ms`, `order_arrival_ms`, `paper_exit_ms`, `match_response_ms`,
   `match_clock_age_ms`, `scheduler_lag_ms`) instead of one global `LIMIT`. States: `PASS`,
   `BREACH`, `COLLECTING`, `STALE`, `INVALID`. K4 threshold stays 250 ms, minimum sample count
   stays 20. `/api/status.health` now carries `banner` (`all_systems_good` / `evidence_not_ready`
   / `latency_breach` / `attention_required`) and `banner_text`; `health.ok` is false when K4 is
   `BREACH` or `INVALID`.
4. `d828357` — Split reliable audit and raw exports. `POST /api/export/prepare?scope=audit|full`
   → 202 + job id. `audit` (default) prepares tables + snapshot + schema + hashes + raw inventory
   only. `full` copies raw recorder segments in one ZIP64 `STORED` pass with SHA-256 and
   per-segment progress. `POST /api/export/jobs/{id}/cancel` sets `cancel_requested`. Download
   uses `FileResponse` with HTTP Range and either the admin header or a job-scoped HttpOnly
   cookie set on prepare. `GET /api/export/raw` lists immutable segments (scoped cookie);
   `GET /api/export/raw/{name}` streams one segment natively with `safe_raw_segment_path`
   rejecting traversal. One `full` job at a time; audit jobs remain usable while a full runs;
   TTL cleanup respects active leases so a served file is never deleted mid-transfer.
5. `e551e6d` — Expose clock, high, event, latency, and export audit UI. Frontend consumes
   `/api/match-clocks`, renders persisted clock stamps and executable-bid highs on trade and
   signal cards (losers get an entry → high → exit `.loss-path`), renders every canonical
   latency kind including `COLLECTING` in a table plus the chart, renders the clock coverage
   panel and per-event faults, wires `renderHealth` to `banner`/`banner_text` while keeping the
   literal `ALL SYSTEMS GOOD`, adds a `gate` filter select, and rewrites `downloadExport` to
   take a `scope`, poll `queued` and `preparing`, show progress, cancel via
   `/jobs/{id}/cancel` and an `AbortController`, and clear the admin token only on a 401.
6. `f7e0000` (this commit) — Documentation-only. Adds this changelog entry and appends
   sections 11–16 to `docs/DUAL_SLEEVE_IMPLEMENTATION_CHECKLIST.md`. No code changes.

Validation performed locally:

- `python -X dev -m unittest discover -s tests` — 146 tests pass (baseline 145 + one new
  `test_trade_and_signal_surface_persisted_clock_and_high`).
- `python -m compileall -q app tests` — clean.
- `ruff check --select E9,F63,F7,F82 app tests` — clean.
- `node --check static/app.js` — clean.
- `git diff --check` — clean.
- Deterministic Al-Hazm 90+5 replay (unit test) — the price-only classifier is reached instead
  of `sleeve_outside_window`; the match-clock stamp records `90+5'` and the gate outcome is
  `clock_88_plus`; no score, event, or narrative field appears anywhere in the price-only
  decision record. Independence is enforced by the AST/import allowlist test in
  `tests/test_late_score_sleeve.py`.

Live acceptance not yet run (needs deploy + production `ADMIN_TOKEN`):

- 100 % of new signals carrying a structured clock stamp.
- 100 % of new price-only fills carrying a fresh `usable_for_88_gate = true` stamp at minute 88+.
- No new price-only record using `sleeve_outside_window` on expected expiration.
- K4 state identical across `/api/stats`, `/api/status`, and the UI.
- Audit bundle ready in under 10 seconds; ZIP valid; manifest and table counts reconciled.
- One protected raw-segment range download.
- Runtime requests remain responsive during a `full` prepare.
- Rendered desktop and 360 px mobile screenshots for the trade, signal, system, and download
  states.

Rollback:

- Additive schema. Application rollback is `git revert <merge-sha>`; old code tolerates the added
  tables and columns. Do not drop clock, event, or high data.
- Cancel any in-flight export worker before reverting the app.
- For immediate containment without a revert, set `PRICE_ONLY_SLEEVE_MODE=off`. Gate A is
  unaffected.
- The unrelated Railway service `kalchi-kill` was not touched.

Known limitations recorded before final review:

- The specification's freshness threshold defaults to `MATCH_CLOCK_MAX_AGE_MS = 2500`; the
  spec also asks for it to be measured against p95 poll interval per deployment. Adjust after
  the first production `/api/status.clock_coverage` sample.
- Association windows in `app/audit.py::match_signal_event` still use the pre-existing
  `EVENT_MATCH_WINDOW_S` default; consider a tighter default for goal.observed → signal after
  reviewing one deployment's associations.
- Frontend uses the browser's `sessionStorage` for the admin token; the rewrite now leaves the
  token in place on transient 5xx and network errors, but the token is still cleared on 401 or
  when a user cancels then re-prompts.

## 2026-08-31 — Audit of the production-integrity stack

Status: seven defects found by review of commits 1-5 and fixed in `4c9701b`. All
carry regression tests that fail against the previous behavior. Paper-only; no
change to Gate A, sizing, entry, exit, fees, lockout, or settlement.

| # | Severity | Defect | Fix |
|---|---|---|---|
| 1 | High | Clock parser read the first integer anywhere in the string, so `"2nd Half 90+5'"` parsed as minute 2 and `"1-0 90+5'"` as minute 1 | Require an explicit minute mark or stoppage; accept a bare integer only as the whole value |
| 2 | Medium | `_period_from_text` / `_status_from_text` returned raw prose as a period/status label, declining valid clocks as `clock_period_unusable` | Return `None` and defer to minute-based inference |
| 3 | Medium | `details.get("time")` was an exact-key lookup while every other clock field used `_direct_field` | Resolve `time` the same way as the other fields |
| 4 | Medium | `adminPost` cleared the admin token on any error; its only caller is the kill switch | Clear only on 401 |
| 5 | Medium | `refreshRawSegments` was reachable only after a completed full export | Populate when the System tab opens |
| 6 | Medium | `/api/match-clocks` was fetched and never read | Render the observation timeline; drop the limit from 200 to 60 |
| 7 | Low | `timedFetch` leaked one abort listener per poll iteration | Remove the listener explicitly |

Also: `.clock-fault-row.warn` and `.clock-stamp.warn` referenced an undefined
`var(--yellow)`, so both borders rendered with the wrong colour; they now use
`var(--amber)`. Dead `.clock-fault` and `.gate-chip` rules removed.

Why defect 1 mattered most: `details.status_text` is precedence 3 in the
specification (§ 4.1), so the author already expected real payloads to carry the
clock there, and sports scoreboard strings routinely lead with a period ordinal
or a score. The mis-parse declined a genuine 90+5 as `clock_pre_88`, reproducing
the exact Al-Hazm failure this work exists to remove — while the acceptance
criterion "no price-only record uses `sleeve_outside_window`" would still have
passed. The stamp persisted `provider_clock: "2′"`, so the audit trail looked
healthy rather than showing a null with a reason, which violates § 2. The score
digit reaching the minute is score contamination in effect, and the AST import
allowlist cannot detect a parse artifact.

The single existing `status_text` test passed only because its fixture placed
the clock first (`"90+3' 2nd half live"`). Reordering the same words returns 2.
The new coverage is table-driven across both orderings.

Test-quality note: the independence proof now also walks
`engine._run_price_only` and `engine._clock_gate_for`. `engine.py` legitimately
imports both `match_clock` and `goal_latency`, so the module-level scan in
`tests/test_late_score_sleeve.py` could not cover the function that actually
assembles the sleeve payload — a future edit could have passed score data into
`classify()` through `cand` with nothing failing. Verified by mutation: planting
a `live_data` read inside `_run_price_only` fails the test.

Verified sound during the audit and left unchanged: trade highs (strict `>`,
held-side `best_yes_bid`/`best_no_bid` only, unreachable from the settlement
path, all three fields restored on restart); latency quarantine (NaN, ±inf,
negative, and bool all route to `*_invalid`); `safe_raw_segment_path` traversal
defense; `require_admin` fail-closed at 503 with `compare_digest`; export secret
exclusion (byte-scanned against patched `ADMIN_TOKEN` and `KALSHI_PRIVATE_KEY`
markers); and the gate's terminal-status-before-missing-minute ordering.

Suite after this commit: 155 tests pass (was 146). `compileall`, `ruff` with
`E9,F63,F7,F82`, `node --check static/app.js`, and `git diff --check` are clean.

## 2026-08-31 — Persist the execution path, not just its peak

Status: collection only. No change to Gate A or price-only detection, sizing,
entry, exit, fees, lockout, or settlement. Nothing in the trading path reads the
new data.

### Why a scalar high was not enough

`max_executable_bid` answers an audit question — *what was the highest bid, and
when* — and § 3.4 of the specification is written entirely in that register. The
question the study actually needs to answer is a research question: *what should
the exit rule be*. Those need different data.

Two positions with an identical `max_executable_bid` of 90c:

- 90c resting for ~200 ms in size 1: the position could never have filled there,
  and the recorded MFE is fiction.
- 90c resting for 12 s in size 500: the money was genuinely left on the table.

The scalar cannot separate them, and the discriminator — time at price and depth
at price — is gone the moment the quote moves.

The sharper finding: **the sleeve already computed the path and discarded it.**
`Position.bid_path` (a `deque(maxlen=240)`) is appended on every quote inside
`sleeve_exit_reason` and is the basis for four exit decisions —
`sleeve_reversal`, `sleeve_scratch`, `sleeve_profit_lock`, and
`sleeve_oscillation`, the last computing crossings, path length, displacement,
and efficiency. None of it was persisted. Every one of those exits was therefore
unfalsifiable: the card showed the label, never the evidence that produced it.
It was also populated for price-only positions only, so Gate A had no path at
all, and it carried no size, so it could not answer the depth question either.

### What is collected now

New append-only `bid_path_samples` table, one row per *change* in the held-side
quote:

| Column | Meaning |
|---|---|
| `kind` | `position` (entry to final exit) or `decline` (forward window after a signal) |
| `trade_id` / `signal_id` | anchor |
| `anchor_ts`, `dt_ms` | entry fill or signal receipt, and offset from it |
| `bid` | best held-side executable bid |
| `bid_size` | size resting at that bid |
| `exec_px` | size-weighted price to sell the held quantity through the ladder |
| `qty` | the quantity `exec_px` was computed for |

`exec_px` is populated only when the ladder can fill the whole held size; a
partial walk would overstate what the position could have realized, so it stays
null rather than being approximated.

`store.bid_path_summary()` derives `peak_bid`, `ms_at_peak`, `peak_exec_px`,
`trough_bid`, `path_travelled_c`, `displacement_c`, and `path_efficiency`.
`max_executable_bid`, `max_executable_bid_ts`, and `mfe_c` are unchanged and
remain on `trades`; they are now a view over the path rather than the only
record of it.

`Position.bid_path` is deliberately untouched. It is load-bearing for exit logic
at its current shape and length; the persisted buffer is a separate
`Position.exec_path`.

### Declines are now labelled observations

Every signal starts a forward watch for `SIGNAL_PATH_WINDOW_S` (default 300 s),
accepted or declined, bounded by `SIGNAL_PATH_MAX_TRACKED` (default 400) with
flush-on-eviction. Previously a decline was a dead record: the ledger showed
that the sleeve said no and nothing about whether saying no was right. The 86
`sleeve_outside_window` rows in production carry no outcome and cannot be given
one retroactively except by replaying raw tape. From here, every decline is a
labelled observation and the selection bias is gone.

### Cost to the hot path: none

`store.ex()` commits per statement on the asyncio event loop, so a naive
per-quote write would have added an fsync to every book update and made the K4
order-arrival breach worse. Samples buffer in memory and flush in batches:
incrementally every `BID_PATH_FLUSH_EVERY` (250) samples, which bounds what a
crash can lose, and once more at close. `dt_ms` is relative to the restored
`entry_ts`, so partial flushes reassemble in `dt_ms` order after a restart.
Paths are capped at `BID_PATH_MAX_SAMPLES` (4000) with a logged overflow count
rather than silent truncation.

Two tests pin this: recording `BID_PATH_FLUSH_EVERY - 1` quotes performs no
database write at all, and recording three flush windows produces a handful of
batched writes rather than one per quote.

### Surfaced

`/api/trades` returns `bid_path` and `bid_path_summary`; `/api/signals` returns
`forward_path` and `forward_path_summary`. `bid_path_samples` is in the audit
bundle. The trade card renders the path as a sparkline with the entry line, the
peak marker, time held at peak, the fillable price at peak for the actual held
size, and round-trip distance with path efficiency.

Suite: 167 tests pass (was 155). `compileall`, `ruff E9,F63,F7,F82`,
`node --check static/app.js`, and `git diff --check` are clean.

Rollback: additive. Revert the commit; `bid_path_samples` can stay. Set
`SIGNAL_PATH_WINDOW_S=0` to stop decline-path collection without a deploy.

## 2026-09-01 — Independent final review reopened PR 12

Status: `BLOCKED`. Documentation-only handoff; no strategy logic, deployment, merge, database, or
Railway service changed in this entry.

Reviewed baseline:

- Pull request `#12`, draft branch `cursor/production-integrity-clock-export-aaf8`.
- Head `cd4d36e1adeb01d63381fce79b58d6311cfc7b2d`.
- Tree `70b550d8f620db82e8ca22ee58c4ea294eb5d925`.
- The existing local suite passed 193 tests and the existing GitHub Actions run passed, but those
  results did not exercise the failures below. One full-suite run also printed `Task exception was
  never retrieved` while returning success, which is itself a test-harness blocker.

Executable review findings:

- A new clock identity becomes decision-visible before its SQLite insert completes. An id-less
  minute-88 clock is accepted, and an insert failure leaves an unchanged id-less identity that is
  not retried. Reconfirmation can report negative poll uncertainty.
- Clock health marks ordinary mapped pre-match waiting as unhealthy, treats a cumulative candidate
  miss as a permanent current fault, and can count id-less state as present/fresh.
- Goal/provider insert paths omit `mode`; live restart deletes their null-mode rows, while demo
  fills/latency contaminate live evidence. Legacy null history is deleted rather than preserved.
- Provider occurrence parsing assumes only nested `details.last_play.occurence_ts`, although the
  persisted significant-event raw row commonly carries the value at its root. Correction linkage
  is not restart-safe and its current tests inspect source strings rather than behavior.
- SQLite backup holds the same global lock used by event-loop status/store calls. Moving the owner
  to a worker thread does not prevent those event-loop calls from blocking. Legacy `GET /api/export`
  still snapshots inline, and background-task failure can escape unobserved.
- Final trade/signal path write failure orphans the retained buffer; the terminal row can exceed and
  then fall outside the 4,000-row query cap; summary/chart logic bridges quote gaps; and the frontend
  path cache mixes string and numeric ids so **Show path** can fetch but display nothing.

Binding remediation:

- Added `docs/PR12_BLOCKER_RESOLUTION_HANDOFF.md`, which specifies exact state/schema/API behavior,
  named behavioral regression tests, failure injection, mutation proof, migration checks, real
  SQLite export contention, browser interaction, production/restart evidence, rollback, and final
  reviewer stop/go criteria.
- Updated `AGENTS.md` so every PR 12 implementer must read that handoff before the production
  specification.
- Reopened checklist sections 11, 12, 14, and 15 and added blocking section 17. Section 16 remains
  blocked on production evidence.

Decision:

- Do not merge or deploy PR 12 from the reviewed head. The next implementer must continue the same
  draft PR and satisfy every `BR-*` item. A green unit suite alone is not acceptance.
- No profitability conclusion can be drawn until collection integrity is fixed and a separate
  leakage-safe backtest has enough evidence.
- The unrelated Railway service `kalchi-kill` was not touched.

Rollback for this documentation-only commit: revert that commit. It creates no schema/data change
and does not alter runtime behavior.

## PR 12 blocker resolution — work package 1: clock publication and current health (`BR-CLOCK`, `BR-HEALTH`)

Implements sections 3 and 4 of `docs/PR12_BLOCKER_RESOLUTION_HANDOFF.md`. Starting head for this
pass was `5f546fe` (the reviewed head `cd4d36e` plus the docs-only handoff commit; no code delta,
so every finding at the reviewed head still applied unchanged).

**Before (`BR-00`).** The named regressions were written first and run against that head. Full
command and output: `docs/evidence/pr12/baseline-cd4d36e/baseline-red-tests.txt` — 22 red
assertions across 19 tests. Representative failures:

- `test_signal_during_blocked_clock_insert_rejects_unpersisted_clock` —
  `'clock_88_plus' == 'clock_88_plus' : an unpersisted candidate was accepted by the 88+ gate`.
  A signal running while `insert_match_clock` was still awaiting read the id-less candidate and
  the gate accepted it.
- `test_unchanged_poll_preserves_id_and_uses_latest_confirmation_interval` — receipts
  10.00/10.25/10.50 reported `poll_uncertainty_ms = -250.0`, reproducing the reviewed defect
  exactly. Reconfirmation advanced `previous_poll_ts` on the cached row while pinning
  `observed_ts`, and uncertainty was measured between those two unrelated endpoints.
- `test_unpersisted_clock_fails_closed_before_minute_logic` — an observation with `id` of `None`,
  `0`, `-1` or `"12"` still reached `clock_88_plus`.
- `test_future_observation_and_confirmation_fail_closed` — a receipt later than the signal was
  labelled `clock_stale` rather than refused as a future timestamp.
- The seven section 4.3 health cases errored: coverage exposed no per-event state at all.
- `test_restart_requires_new_provider_confirmation_for_freshness` passed at the reviewed head and
  is retained as a guard, not claimed as a fixed defect.

**Change.** Files: `app/match_clock.py`, `app/goal_latency.py`, `app/engine.py`, `app/store.py`,
`app/replay.py`.

- Two-phase publication. `MatchClockTracker.observe()` now parks a new identity in `pending` and
  changes no decision-visible value; `promote(event, row_id)` is the only path that publishes, and
  it rejects any non-positive id; `fail_persist(event, error)` discards the candidate, records a
  current `clock_persistence_failed` fault and leaves the identity uncommitted so the next
  identical poll retries. `_record_clock()` persists before publishing and no longer lets an
  insert exception escape.
- Gate invariant. `is_persisted_id()` gates both `stamp_from_observation()` and
  `MatchClockGate.evaluate()`, which fail closed to
  `{"accepted": false, "outcome": "clock_unpersisted", "usable_for_88_gate": false,
  "unusable_reason": "unpersisted"}` before any minute/status logic. A stamp that already failed
  closed upstream keeps its own reason instead of being re-derived from null fields. A receipt
  later than the signal returns `clock_future` / `future_timestamp` with the negative age
  preserved, never coerced to zero.
- Confirmation lineage. The cached row keeps its original `observed_ts` and `previous_poll_ts` and
  tracks `confirmation_previous_poll_ts` separately, so `poll_uncertainty_ms` is
  `(confirmed_ts - confirmation_previous_poll_ts) * 1000`, is null when either endpoint is missing,
  and is never negative.
- Schema. `source`, `confirmed_ts` and `confirmation_previous_poll_ts` added idempotently to
  `match_clock_observations` following the existing `mode` migration pattern. Every new row stores
  its exact clock source; legacy rows keep a null source and are not relabeled.
- Health. `coverage()` returns an `events` array carrying `event`, `mapped`, `provider_status`,
  `observation_id`, `clock_present`, `clock_fresh`, `last_confirmed_ts`, `candidate_active`,
  `current_fault` and `state` (`waiting` / `observing` / `fault`). Presence and freshness count
  only a positive-id decision-visible observation; freshness is derived from the current time on
  every request. Fault reasons are derived at query time rather than latched, so a successful
  persisted reconfirmation clears the event's fault. Cumulative misses moved to
  `clock_gate_candidate_misses_total`, reported but never blocking. `_clock_coverage_check()` reads
  per-event current state instead of count arithmetic, so a mapped pre-match fixture is healthy
  `waiting`; it retains a legacy count path for callers that pass count-only coverage.

**After.** Targeted: `python -m unittest tests.test_match_clock.TwoPhaseClockPublicationTests
tests.test_match_clock.ClockDatabaseLineageTests
tests.test_goal_latency.ClockPersistenceHandoffTests tests.test_health.CurrentClockHealthTests`
— 19 tests, OK. Full suite: `python -X dev -W error::RuntimeWarning -m unittest discover -s tests`
— 212 tests, OK. `compileall`, `ruff check --select E9,F63,F7,F82`, `node --check static/app.js`
and `git diff --check` all clean.

**Mutation.** `is_persisted_id()` was temporarily replaced with `return True`. The suite went to 13
failing assertions across four files, including
`test_unpersisted_clock_fails_closed_before_minute_logic` (all four id shapes),
`test_gate_object_fails_closed_on_unpersisted_id_even_when_mapped`,
`test_candidate_is_invisible_until_promoted` and the five affected health cases. The mutation was
reverted and is not committed.

**Limitations.**

- `MATCH_CLOCK_MAX_AGE_MS = 2500` is still a default, not a measurement, and is unchanged here.
- A pre-existing unretrieved background-export task exception still prints during the suite while
  unittest exits zero. It is `BR-EXPORT` section 7.1 work and is not addressed in this commit; it
  remains a `BR-03` gate failure until then.
- Production evidence for these paths (`BR-PROD`) is not collected: it needs an authorised deploy.
- Existing tests that published a clock by assigning `latest[event]["id"]` were rewritten to call
  `promote()`. That is the intended semantic change, not a test weakening: under the new model an
  unpersisted reading is not decision-visible.

**Rollback.** `git revert <this commit>`. The migration only adds nullable columns; reverting the
code leaves them in place, unread and non-destructive. No collected data is dropped.

## PR 12 blocker resolution — work package 2: evidence modes and provider-event lineage (`BR-MODE`, `BR-EVENT`)

Implements sections 5 and 6 of `docs/PR12_BLOCKER_RESOLUTION_HANDOFF.md`.

**Before (`BR-00`).** Baseline output appended to
`docs/evidence/pr12/baseline-cd4d36e/baseline-red-tests.txt`. `tests/test_evidence_modes.py`
produced 20 red assertions:

- `latency` had no `mode` column at all (`sqlite3.OperationalError: no such column: mode`).
- `provider_match_events` and `goal_latency_observations` wrote `[None]` where `live` was required:
  the migration added the column but neither insert set it.
- `purge_non_live()` deleted every study observation on a live boot —
  `purge_non_live() deleted rows from signals / trades / bid_path_samples /
  match_clock_observations / provider_match_events / goal_latency_observations`, each `0 != 1`.
- Demo history did not survive a live restart (`'demo' not found in ['live']`, six tables), and
  legacy null-mode rows were destroyed (`0 not greater than or equal to 1`).
- Two migrations changed the historical row hash.

For `BR-EVENT`, a direct run of `match_signal_event()` against a real significant-event row
carrying a root `occurence_ts` returned `provider_occurrence_ts = None`: only
`details.last_play.occurence_ts` was recognised.

**Change.** Files: `app/store.py`, `app/match_events.py`, `app/audit.py`, `app/goal_latency.py`.

- `mode_clause(alias, selector)` and `present_mode()` centralise scoping. `legacy_unknown` maps to
  SQL `mode IS NULL` and is never included in live; `"all"` disables scoping for audit callers.
- `mode` is added idempotently to `latency` and `paper_fills` and written on every insert path,
  including the transactional ones inside `open_paper_trade()`, `record_paper_exit()` and
  `finish_paper_signal()`, plus `insert_goal_latency()` and `upsert_provider_event()`.
- `purge_non_live()` no longer deletes study observations. Isolation moved to the query layer:
  `stats()`, `latency_kind_summary()`, `latency_readiness()` and `_latency_evidence()` take a mode
  selector defaulting to the active mode. Only the operator event log and firehose-era junk
  signals (rows referencing no registered market) are still cleared.
- Provider duplicate identity is mode-scoped: `idx_provider_events_fingerprint` is replaced by
  `idx_provider_events_fingerprint_mode` over
  `(event, fingerprint, COALESCE(mode,'legacy_unknown'))`. The new index is strictly more
  permissive than the one it replaces, so it cannot fail against data the old one accepted; no row
  is deleted and no mode rewritten. The index is created after the `mode` migration, since it
  references that column.
- `provider_occurrence(payload)` resolves occurrence time by the fixed precedence
  `raw.occurence_ts` → `raw.occurrence_ts` → `raw.details.last_play.occurence_ts` →
  `raw.details.last_play.occurrence_ts`, accepting only finite non-negative int/float and
  rejecting booleans, NaN, infinities, strings, containers and negatives. It returns
  `(ts, source, unavailable_reason)` and never substitutes a receipt time.
- `provider_occurrence_ts`, `provider_occurrence_source` and
  `provider_occurrence_unavailable_reason` are added idempotently and written at canonicalization.
  `match_signal_event()` consumes the normalized column and falls back deterministically to the
  preserved raw payload under the same precedence, labelled `legacy_raw_derived:<source>`.
  Occurrence, source and reason are distinct fields in the audit output.
- `store.previous_substantive_fingerprint(event, mode)` resolves the revision link from durable
  history when the in-memory link is absent (as it always is after a restart). It is scoped to the
  same event and the same mode and excludes corrections, so a correction can never link across
  events or modes or to another correction. `_resolve_new_events()` now also clears
  `last_substantive_fingerprint` when an event is dropped.

**After.** `tests/test_evidence_modes.py` — 10 tests, OK. `tests/test_provider_event_audit.py` —
OK, with the `inspect.getsource` assertion in `test_corrections_link_across_polls` replaced by
runtime tests against real SQLite. Full suite 235 tests, OK, under
`-X dev -W error::RuntimeWarning`. `compileall`, `ruff`, `node --check`, `git diff --check` clean.

**Mutation.** Two, both reverted and not committed:

1. `mode_clause()` forced to return no scoping — 3 failures:
   `test_live_latency_readiness_excludes_demo_and_legacy_samples`,
   `test_same_provider_fingerprint_can_exist_once_per_mode_without_overwrite`,
   `test_lineage_lookup_is_mode_scoped`.
2. `_finite_timestamp()` validation bypassed in `provider_occurrence()` — 10 failures across every
   invalid shape in `test_invalid_timestamps_are_refused_not_coerced`.

**Limitations.**

- The API read endpoints in `app/main.py` (`/api/signals`, `/api/trades`, `/api/match-clocks`,
  `/api/provider-events`, `/api/goal-latency`, `/api/latency`, equity) are not yet mode-scoped or
  given an explicit mode selector; that is section 5.2 item 4 for the API layer and item 5 for the
  export manifest, and lands with `BR-EXPORT`. Until then those endpoints show all modes.
- `test_export_manifest_and_rows_reconcile_by_mode_with_no_orphan_fill` from section 5.3 is not
  yet written: it depends on the export rework in `BR-EXPORT`.
- The pre-existing unretrieved background-export task exception still prints during the suite.
- No production evidence (`BR-PROD`); it needs an authorised deploy.

**Rollback.** `git revert <this commit>`. The migration only adds nullable columns and replaces one
unique index with a strictly more permissive one; reverting restores the previous index definition
without deleting rows. Note that reverting also restores the destructive `purge_non_live()`, so any
demo or legacy evidence collected in the meantime would be deleted on the next live boot — back up
`footballbot.db` before reverting.
