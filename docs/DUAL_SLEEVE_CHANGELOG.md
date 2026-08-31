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
