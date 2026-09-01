# Dual-Sleeve Study Implementation Checklist

This file is the authoritative task list for the reversible dual-sleeve paper-study change.
Update the status and evidence links here as work is completed. Do not treat chat history as
the implementation record.

Status values: `PENDING`, `IN PROGRESS`, `PASSED`, `BLOCKED`.

## 1. Stabilize deployment baseline — PASSED

Plan:

- Confirm the current `main` deployment, source commit, environment, volume mount, and study variables.
- Verify the application and live market connection before changing the dashboard or data schema.

Acceptance tests:

- Railway deployment status is `SUCCESS`.
- `/api/health` returns `{"ok": true, "mode": "live"}`.
- `/api/status` reports WebSocket `connected`, recorder healthy, and no credential error.
- Persistent volume remains mounted at `/srv/data`.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Baseline verification`.

## 2. Canonical match-event backend — PASSED

Plan:

- Preserve every raw provider payload unchanged.
- Persist a versioned normalized object with canonical type, side, before/after score,
  score transition, provider minute/stoppage/period/clock when present, and human label.
- Migrate existing SQLite volumes without deleting or rewriting historical observations.

Acceptance tests:

- Fresh-schema and old-schema migration tests both pass.
- Deterministic tests cover home goal, away equalizer, score correction, nested score keys,
  missing clock, and provider clock extraction.
- Raw payload remains available beside the normalized object.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Canonical event schema`.

## 3. Independent paper sleeves — PASSED

Plan:

- Run original Gate A and `price_only_late_score` as two separately tagged strategies.
- Give each strategy independent admission decisions, lockouts, counterfactual shadow liquidity,
  positions, exits, and restart recovery.
- Preserve a shared immutable live book; neither simulation may consume the other's paper depth.

Acceptance tests:

- One market episode can independently fill both strategies.
- Rejecting or exiting one strategy cannot change the other strategy's state.
- The same displayed live book remains unchanged.
- Restart restores strategy identity, partial positions, fees, and exit state.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Independent paper sleeves`.

## 4. Auditable trigger-to-event matching — PASSED

Plan:

- Expose exact signal, order-arrival, entry, event-observation, exit, and settlement timestamps.
- Expose trigger thresholds and observed values: displacement, levels, size, sibling lag,
  inferred state, normalized triplet probabilities/deltas, spread, and coherence.
- Match the nearest same-event canonical feed observation within a fixed time window.
- Label the result as temporal/state consistency, never proven causation.

Acceptance tests:

- Tests cover market-first, feed-first, equalizer consistency, one-goal-lead consistency,
  correction/reversal, no nearby event, and wrong-state event.
- Stored market/event identifiers remain unchanged; display fields are additive only.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Trigger-to-event audit matching`.

## 5. Separate sleeve performance accounting — PASSED

Plan:

- Report open/closed count, gross, fees, net, win rate, net per fill, exit reasons, and
  evidence gates for Gate A, price-only late-score, and combined.
- Treat legacy rows without a strategy tag as Gate A.

Acceptance tests:

- Per-sleeve totals reconcile exactly to combined totals.
- Partial fills, restored positions, zero-trade state, and legacy rows are covered.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Separate sleeve accounting`.

## 6. Frontend makeover — PASSED

Plan:

- Show human-readable match and contract names while retaining raw IDs in expandable audit details.
- Show full date/time, trigger reason, inferred state, matched canonical event, and timing relation.
- Show independent profit-and-loss panels for both sleeves.
- Add intuitive event-feed latency, order latency, exits, and evidence charts with units and explanations.
- Add a persistent health panel for WebSocket, recorder, event feed, execution, database, and recent errors.
- Replace swallowed front-end failures with visible error state and retry behavior.
- Make all grids, labels, tables, and signal rows wrap correctly on phone widths.

Acceptance tests:

- Browser visual checks at desktop and phone widths show no truncation or horizontal page overflow.
- No raw ticker is the primary label; no unexplained abbreviation or unlabeled chart remains.
- Simulated disconnect/API error becomes visible in the health panel.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Production deployment and
rendered acceptance`. The public dashboard was rendered at 1363 px desktop width and inside a
320 px phone viewport. The phone document measured 305 px client width and 305 px scroll width,
with all three header controls visible. Backend fault injection and client failure visibility are
covered by the health and frontend contract tests.

## 7. Downloadable study bundle — PASSED

Plan:

- Add an admin-protected dashboard download containing a manifest, non-secret active configuration,
  markets, signals, trades, paper fills, latency, canonical match events, event log/errors, and raw
  WebSocket recordings required for external replay.

Acceptance tests:

- Archive opens and each CSV/JSON file parses.
- Table row counts reconcile with the database/API.
- Raw gzip files decompress and secrets/private keys are absent.
- Export errors are visible to the user and do not crash live collection.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Downloadable study bundle`.

## 8. Full verification and deployment — PASSED

Plan:

- Run all unit/integration tests, migration tests, static checks, and browser smoke tests.
- Review the pull-request diff and GitHub Continuous Integration results.
- Deploy with both strategies paper-only and the event feed diagnostic-only.

Acceptance tests:

- Local tests and GitHub Continuous Integration are green.
- Railway is healthy and the dashboard reports either `ALL SYSTEMS GOOD` or a specific fault.
- New signals/events persist through a restart on `/srv/data`.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Production deployment and
rendered acceptance`.

## 9. Independent final review — PASSED

Plan:

- Re-review entry mathematics, simulation independence, execution realism, leakage risk,
  match semantics, failure visibility, mobile usability, and export completeness.
- Fix every material issue before merging; record non-blocking limitations explicitly.

Acceptance tests:

- `docs/DUAL_SLEEVE_CHANGELOG.md` contains a final audit with evidence and remaining limitations.
- Pull request stays separately revertible from the original sleeve change.

Evidence: `docs/DUAL_SLEEVE_CHANGELOG.md` entry `2026-08-30 — Independent final code review`.

## 10. Operator dashboard and event trace — IN PROGRESS

This section was added after production use showed that the first audit dashboard exposed the
data but did not make a specific match, trade, or missed sleeve decision easy to understand.

### 10.1 Visual information architecture — PASSED

Plan:

- Replace the single long page with accessible tabs for Overview, Trades & Events, Signals,
  League Performance, Live Markets, and System & Data.
- Keep health visible in the application header and move full operational detail to its own tab.
- Reduce live matches to a compact searchable table because they are context, not the main study.

Acceptance tests:

- Every major section has one tab and one purpose; changing tabs preserves live data.
- Desktop and 320 px phone renders have no page-level horizontal overflow or clipped controls.
- Live Markets consumes no more than one compact row per match until expanded.

Local evidence: six accessible keyboard-navigable tabs, compact expandable market rows, shared
header health, and responsive 760/420 px layouts are implemented. Browser-rendered production
acceptance remains under 10.7.

### 10.2 Trade-to-event story — PASSED

Plan:

- Put each trade, its trigger, entry, exit, profit, and nearest canonical event on one ordered
  timeline with human-readable UTC times and provider match minute.
- Show the provider's explicit last-play description and event method when supplied, while keeping
  raw payload and identifiers expandable.
- Label association strength as exact-state, event-consistent, time-only, mismatch, or unmatched;
  never convert temporal proximity alone into proven causality.

Acceptance tests:

- A trade with a nearby goal visibly shows event description, game minute, time delta, and whether
  market or provider observation arrived first.
- A trade without a valid same-match event visibly says why it is unmatched.
- Al-Hazm vs Al-Shabab trade 51 is rendered as Draw, 50c to 90c, +$75.20 net, target exit, alongside
  Afimico Pululu's 90+5 penalty equalizer and the provider-observation delay.

Local evidence: the trade renderer consumes canonical description, scorer, match clock, provider
occurrence, provider receipt, signal, entry, and exit timestamps in one sorted timeline. The
Al-Hazm fixture and production-record audit are the regression case for 10.7.

### 10.3 Canonical event completeness — PASSED

Plan:

- Normalize the provider's `home_same_game_score`/`away_same_game_score` fields before aggregate
  or period scores.
- Parse provider stoppage clocks such as `90+5'`, scoring player, and explicit penalty wording from
  structured significant-event/last-play fields.
- Preserve the original payload without rewriting historical raw data.

Acceptance tests:

- Deterministic fixture for the Al-Hazm vs Al-Shabab payload produces home penalty scored,
  0-1 to 1-1, minute 90+5, and Afimico Pululu.
- Existing home/away goal, correction, nested score, and missing-clock tests remain green.

Evidence: `tests/test_match_events.py::test_al_hazm_penalty_equalizer_has_complete_canonical_event`
passes and historical presentation is re-derived from preserved raw payloads without rewriting
stored provider data.

### 10.4 Understandable filters and decisions — PASSED

Plan:

- Add match, sleeve, result, event-association, and text filters shared by Trades & Events and
  Signals; provide a clear reset action and visible result count.
- Replace threshold dumps with a sentence-first decision summary, then observed-vs-required visual
  bars and expandable technical evidence.
- Show accepted, rejected, ignored, and filled outcomes as explicit human categories.

Acceptance tests:

- Combining filters returns the correct intersection and reset restores all rows.
- Every decision row answers: what moved, why the sleeve acted or declined, and what happened next.
- Missing values render as `Not supplied by provider`, never the ambiguous `Not observed data`.

Local evidence: synchronized text, sleeve, match, result, association, and period filters are
implemented with intersection and reset behavior; decision cards lead with a sentence and keep
thresholds/raw identifiers secondary.

### 10.5 Analytical charts and league performance — PASSED

Plan:

- Restore per-league trade count, win rate, net, and net per trade, split by sleeve and sortable.
- Replace the minimal equity line with cumulative net plus drawdown, trade markers, zero reference,
  readable time axes, and sleeve toggles.
- Add event-linked versus unmatched outcome comparison and exit-reason distribution.

Acceptance tests:

- League totals reconcile exactly to closed trades and combined totals.
- Chart domains include all points, axes carry units, hover/focus exposes exact values, and zero/
  one-trade states remain legible.
- Desktop and phone chart labels do not collide or clip.

Local evidence: league accounting now includes combined and independent sleeve buckets with a
reconciled total; the equity view includes toggles, zero line, exact focus/hover values, markers,
and combined drawdown. Static, accounting, and zero-state tests pass; rendered acceptance remains
under 10.7.

### 10.6 Minute-88 timing diagnosis — PASSED

Plan:

- Surface the exact reason the price-only sleeve accepted or declined each candidate, including its
  calculated time-to-expiration window.
- Add a calibration view comparing market `expected_expiration_time` to provider match clock/event
  timing by series so incorrect schedule proxies can be measured instead of silently assumed.
- Keep live score/event content out of price-only entry and exit logic; any future timing correction
  must use a frozen, pre-match schedule calibration and be separately backtested.

Acceptance tests:

- Al-Hazm vs Al-Shabab signals show Gate A filled while the price-only sleeve recorded
  `outside_minute_88_window`, with the erroneous expiration proxy visible.
- A regression test prevents the UI from describing an expiration-window rejection as no signal.
- No score, normalized event, or live match-feed field is referenced from the price-only detector or
  paper execution modules.

Evidence: every signal exposes its frozen expected-expiration calculation and configured window;
the diagnostic tab groups paired observations by league and shows recent provider match clocks.
`tests/test_late_score_sleeve.py::test_price_only_path_does_not_import_match_feed_fields` passes.

### 10.7 Final production review — IN PROGRESS

Plan:

- Run the full suite, static checks, API reconciliation, rendered desktop/phone review, and a
  specific Al-Hazm vs Al-Shabab trace review.
- Merge through a new reversible pull request and confirm Railway health, persistence, and export.

Acceptance tests:

- GitHub Continuous Integration passes and the public deployment reports healthy.
- The production UI makes trade 51 and its event trace findable in at most two interactions.
- The repo changelog records findings, exact tests, deployment identifier, and limitations.

## 11. Persist match clocks and canonical provider events — REOPENED / BLOCKED BY §17

Plan:

- Add an append-only `match_clock_observations` table and a parser that reads current match clock
  from `details.time` → `match_clock`/`game_clock`/`clock` → the clock portion of
  `details.status_text`; never from `last_play` or historical significant-event times.
- Store an immutable `signals.match_clock_snapshot` (`football.match_clock_stamp.v1`) on every new
  signal, including declines, unmapped, and stale clocks (unusable stamps carry a complete
  `unusable_reason`).
- Persist a canonical `provider_match_events` ledger with a stable fingerprint that de-duplicates
  refreshes and links score corrections to their prior fingerprint.
- Expose `/api/match-clocks` and `/api/provider-events`; the trade API returns the immutable
  signal stamp as `match_clock`.

Acceptance tests:

- Table-driven parser tests cover `87'`, `88'`, `90'`, `90+N'`, typographic apostrophes, status
  text, and malformed inputs; a regression proves the parser refuses `last_play` and historical
  event times as the current clock.
- Persistence tests prove one observation per changed clock/period/status, that unchanged polls
  do not flood SQLite, and that every signal outcome carries a complete stamp.
- Migration tests preserve existing SQLite volumes and mark old signals as legacy without
  fabricating minutes.

Evidence: PR 12 commit `d0a02aa` — `Persist match clocks and canonical provider events`. Config
`MATCH_CLOCK_MAX_AGE_MS=2500`. Local suite: 145 tests pass after commit 4.

## 12. Gate paper 88+ sleeve on persisted live clock — REOPENED / BLOCKED BY §17

Plan:

- Introduce a narrow `MatchClockGate` object exposing only period, minute, stoppage, provider
  status, age, and source identifiers — the only match-feed object the price-only path imports.
- Replace expected-expiration admission in `engine._run_price_only` with the clock-only gate;
  keep expected expiration in the UI as a calibration diagnostic only.
- Record distinct decline outcomes: `sleeve_clock_pre_88`, `sleeve_clock_unmapped`,
  `sleeve_clock_missing`, `sleeve_clock_malformed`, `sleeve_clock_stale`, `sleeve_clock_not_live`,
  `sleeve_clock_final`, `sleeve_clock_suspended`, `sleeve_clock_abandoned`,
  `sleeve_clock_first_half`, `sleeve_clock_half_time`, `sleeve_clock_pre_match`,
  `sleeve_clock_period_unusable`.

Acceptance tests:

- Deterministic Al-Hazm 90+5 replay reaches the price classifier instead of
  `sleeve_outside_window`.
- 87 rejects; 88 accepts; 89, 90, and 90+N accept; first half, final, suspended, missing,
  malformed, and stale clocks fail closed and raise a readiness fault.
- Expected-expiration values cannot change the gate result.
- AST/import allowlist test proves the price-only classifier and paper desk cannot import score,
  scorer, goal, penalty, VAR, correction, or narrative fields.

Evidence: PR 12 commit `89d15d0` — `Gate paper 88+ sleeve on persisted live clock`. Gate A
detection, sizing, entry, exit, fee, lockout, and settlement behavior unchanged.

## 13. Record executable trade highs and per-kind latency readiness — PASSED

Plan:

- Add `trades.max_executable_bid`, `trades.max_executable_bid_ts`, and `trades.mfe_c`. Observe
  after entry only; persist atomically only when a new best bid strictly exceeds the stored high
  (equal high keeps the first timestamp); continue through partial exits; restore for open
  positions after restart.
- Reject ask, midpoint, last price, and settlement values from ever updating the high.
- Derive `high_after_entry_s` in the trade API.
- Query latency per canonical kind (`feed_ingress_ms`, `decision_ms`, `paper_entry_ms`,
  `order_arrival_ms`, `paper_exit_ms`, `match_response_ms`, `match_clock_age_ms`,
  `scheduler_lag_ms`) instead of a global `LIMIT 1000`; report n, p50, p95, max, invalid,
  latest_ts, age_s, threshold_ms, and state (`PASS`, `BREACH`, `COLLECTING`, `STALE`, `INVALID`).
- Split runtime health from evidence readiness: `/api/status.health.ok` becomes false when K4 is
  `BREACH` or `INVALID`; add `banner` = `all_systems_good` / `evidence_not_ready` /
  `latency_breach` / `attention_required` and `banner_text`. K4 threshold stays at 250 ms; minimum
  readiness sample count stays at 20.

Acceptance tests:

- Rising and falling bid tests; repeated equal high keeps first time; held-side conversion for
  YES and NO; ask/mid/last/settlement cannot update the high; partial exit tracks until final
  fill; restart recovery continues from the stored high.
- Per-kind sampling test proves K4 rows cannot be crowded out by high-volume feed rows; monotonic
  scheduler-lag test; negative and non-finite quarantine.
- Health-banner test proves the API can not report `all_systems_good` while K4 is `BREACH`.

Evidence: PR 12 commit `f669841` — `Record executable trade highs and latency readiness`. Local
suite: 136 tests pass at commit 3.

## 14. Split reliable audit and raw exports — REOPENED / BLOCKED BY §17

Plan:

- `POST /api/export/prepare?scope=audit|full` returns HTTP 202 with a pollable `job_id`. `audit`
  is the browser default: SQLite snapshot + schema + normalized tables + fills + latency +
  match-clock and event tables + manifest + hashes + raw inventory only. `full` adds raw
  recorder segments (ZIP64 `STORED`) copied once through a SHA-256 pass with per-segment
  progress and cancellation.
- `GET /api/export/jobs/{id}` returns processed/total bytes and segments, status, and error code.
- `POST /api/export/jobs/{id}/cancel` sets `cancel_requested`; the worker raises
  `exporter.ExportCancelled`.
- `GET /api/export/jobs/{id}/download` uses `FileResponse` with HTTP Range support and either the
  admin header or a job-scoped HttpOnly cookie set on `prepare`.
- `GET /api/export/raw` lists immutable recorder segments and sets a scoped HttpOnly cookie;
  `GET /api/export/raw/{name}` streams one segment natively with Range and traversal-safe path
  resolution (`safe_raw_segment_path`).
- One `full` job at a time (second `full` prepare returns the active job); many concurrent
  `audit` jobs are permitted; TTL cleanup respects active leases so a served file is not deleted.

Acceptance tests:

- Authorized `audit` prepare/status/download ZIP validation and manifest/table reconciliation.
- Authorized `full` progress/cancel/download and per-segment range requests.
- Ready-file lease prevents premature deletion; unauthorized, wrong-cookie, expired, missing, and
  path-traversal requests fail closed; secrets never appear.
- Event loop and `/api/status` remain responsive while a `full` bundle is preparing (a direct
  test proves the worker does not block asyncio).

Evidence: PR 12 commit `d828357` — `Split reliable audit and raw exports`. Local suite: 145 tests
pass at commit 4.

## 15. Expose clock, high, event, latency, and export audit UI — REOPENED / BLOCKED BY §17

Plan:

- Fetch `/api/match-clocks` on refresh and populate `state.clocks`.
- Consume `status.health.banner` and `banner_text` in `renderHealth`; keep the literal
  `ALL SYSTEMS GOOD` in the healthy case and never say all-good when K4 is `BREACH`, `COLLECTING`,
  or `STALE`. Show per-check p95, threshold, and sample count inline.
- Extend the closed trade card with human match/contract, sleeve, match time, persisted clock
  stamp (age, precision, provider status, 88-gate chip), trigger and exit reason, entry/exit/qty/
  gross/fees/net, max executable bid, MFE, UTC high time, seconds after entry, and nearby event
  or an explicit "No nearby same-match event"; losing trades render an
  entry → executable high → exit `.loss-path` row.
- Extend the signal card with the immutable clock stamp and the exact 88-gate outcome chip.
- Render every `CANONICAL_LATENCY_KINDS` entry in `renderLatency` — including `COLLECTING` — in
  both the bar chart and a full per-kind table with n / p50 / p95 / max / age / threshold / state.
- Add `renderClockCoverage` for the system tab (`watched`, `mapped`, `clock_present`,
  `clock_fresh`, `clock_gate_candidate_misses`) plus a per-event fault list.
- Rewrite `downloadExport` to take `scope=audit|full`, POST `?scope=`, poll `queued` **and**
  `preparing`, show processed bytes/segments progress, use an `AbortController` with per-request
  timeouts, expose a `cancel` button that POSTs `/jobs/{id}/cancel`, and clear the admin token
  only on 401 (transient 5xx and network errors no longer force a reprompt). Downloads use a
  native `<a>` click relying on the job-scoped HttpOnly cookie.
- Add `refreshRawSegments` to list `/api/export/raw` with individual gzip Range downloads.
- Add a `gate` filter select (`accepted`, `declined`, per-outcome) wired through `filterMarkup`,
  `passesFilters`, and the reset button.

Acceptance tests (`tests/test_frontend_contract.py`, and see 15.1 below):

- All new element ids are present in the HTML (`clock-coverage-panel`, `clock-coverage`,
  `clock-faults`, `latency-table`, `export-panel`, `export-audit-button`, `export-full-button`,
  `export-cancel-button`, `export-progress`, `export-error`, `raw-segment-list`).
- Both `data-export-scope="audit"` and `data-export-scope="full"` are present in the HTML.
- The three backend banner keys (`all_systems_good`, `evidence_not_ready`, `latency_breach`)
  appear verbatim in the JS.
- The prepare URL carries `?scope=`, the poll accepts both `queued` and `preparing`, and a
  `/jobs/{id}/cancel` path exists.
- Card helpers `clockStampBlock`, `tradeHighBlock`, `lossPath`, `gateOutcome`, and
  `CANONICAL_LATENCY_KINDS` are referenced.
- Filter fields include `gate` alongside `query`, `strategy`, `match`, `result`, `association`,
  and `period`.
- The 360 px phone breakpoint is present in the CSS and `text-overflow: ellipsis` is not.
- `await response.blob()` still does not appear (native anchor + cookie for downloads).

Evidence: PR 12 commit `e551e6d` — `Expose clock, high, event, latency, and export audit UI`.
Local suite: 146 tests pass (baseline 145 + one new test).

## 16. Production evidence and rollback (final review) — BLOCKED

Plan:

- Machine-readable evidence must be attached to the implementation PR before final review can
  merge: full suite, `compileall`, `ruff check --select E9,F63,F7,F82 app tests`, `node --check
  static/app.js`, `git diff --check`, deterministic Al-Hazm replay output, production API
  snapshots after deploy (`/api/status`, `/api/stats`, `/api/latency`, `/api/match-clocks`,
  `/api/signals`, `/api/trades`, `/api/export/*`), one authorized audit bundle ready in under
  10 seconds with a valid ZIP and reconciled manifest, one protected raw-segment range download,
  and rendered desktop plus 360 px mobile screenshots for the trade, signal, system, and download
  states.
- Confirm the live service remains paper-only and that the unrelated Railway service `kalchi-kill`
  was untouched.
- Rollback: additive schema means application rollback is a single revert of the merge commit.
  Old code tolerates the added tables and columns. Do not drop clock, event, or high data. Cancel
  active export workers before reverting. If containment is needed, disable the price-only clock
  gate through the `PRICE_ONLY_SLEEVE_MODE` flag; Gate A stays unchanged.

Acceptance tests:

- All items above are recorded in `docs/DUAL_SLEEVE_CHANGELOG.md` under the deploy identifier.
- Final reviewer checklist (specification § 12) passes every item; a single failed item blocks
  merge.

Blocker: needs an authorized deploy of PR 12 and access to a live `ADMIN_TOKEN` to collect the
production evidence and rendered acceptance items. Local checks and the deterministic Al-Hazm
replay unit test are already green.

Post-merge: record commit SHA, CI run, deployment ID, exact validation outputs, known
limitations, and the exact rollback command in `docs/DUAL_SLEEVE_CHANGELOG.md`.

## 17. Resolve independent-review blockers at PR 12 head `cd4d36e` — BLOCKED

Binding plan and acceptance contract:

- `docs/PR12_BLOCKER_RESOLUTION_HANDOFF.md`

The handoff pins the reviewed head/tree, exact implementation sequence, forbidden shortcuts,
behavioral regression tests, mode/migration rules, export concurrency threshold, path failure and
gap semantics, browser checks, evidence file names, production/restart verification, rollback, and
the final reviewer decision vocabulary.

Current blocking groups:

- [x] `BR-CLOCK`: no decision-visible clock before successful persistence; positive database id is
  mandatory; failed identical inserts retry; confirmation uncertainty is non-negative.
  Commit `4fbb79d`. Baseline red and mutation proof recorded.
- [x] `BR-HEALTH`: pre-match waiting is not an error, current faults recover, cumulative misses do
  not permanently poison health, and id-less state can never appear fresh.
  Commit `4fbb79d`. Seven section 4.3 cases pass.
- [x] `BR-MODE`: live/demo/legacy rows are preserved and isolated across all study tables and
  restarts; startup deletes no observations.
  Commit `f3d3de0`. Export manifest per-mode counts and the API mode selector remain open.
- [x] `BR-EVENT`: normalized provider occurrence supports real raw shapes and correction lineage
  survives restart without cross-match links.
  Commit `f3d3de0`. Source-string assertions replaced by runtime tests.
- [x] `BR-EXPORT`: a real SQLite backup cannot block event-loop store/status work; legacy export
  delegates to the job flow; every task exception is retrieved.
  Commit `00d41f8`. Contention measured under the 250ms bound against a real WAL database.
- [ ] `BR-PATH`: **PARTIAL — BLOCKED.** Commit `9f831c6` delivers the cap including the terminal
  row, gap semantics with no bridging, the section 8.3 fixture result exactly, and a real
  headless-browser click test. Section 8.2's transactional final-close ownership is **not**
  implemented: a failed final write still does not retain the position or the signal watch, there
  is no `forward_path_finalized` flag, no `signal_path_persistence_failed` fault, and no startup
  rebuild. Four section 8.5 tests are unwritten.
- [ ] `BR-LOCAL` / `BR-AUDIT`: **PARTIAL.** Local gate passes — 260 tests twice, static checks
  clean, zero tracebacks, zero unretrieved task exceptions; production-schema copy migrates twice
  with no loss (`BR-04`); section 10 documentation-routing test added. Section 9 API reconciliation
  assertions and the rendered league/download/mobile contracts are not yet written.
- [ ] `BR-PROD` / `BR-REVIEW`: authorized review deployment, machine-readable and rendered
  evidence, restart proof, paper-only proof, `kalchi-kill` untouched proof, and independent signoff.

Earlier section-level `PASSED` entries are historical implementation notes, not current acceptance.
Sections 11, 12, 14, and 15 are explicitly reopened. Section 16 remains blocked. A green unit suite
does not close this section.

Status at candidate `9f831c6`: **BLOCKED.** Five of eight `BR-*` items are satisfied with baseline
red, behavioral tests and mutation proof. `BR-PATH` is partial, `BR-LOCAL`/`BR-AUDIT` is partial,
and `BR-PROD`/`BR-REVIEW` cannot start without an authorised deployment target. Evidence index:
`docs/evidence/pr12/9f831c6/EVIDENCE_INDEX.md`. PR 12 stays draft.
