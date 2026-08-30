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
