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

## 6. Frontend makeover — BLOCKED (RENDER CHECK)

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

Implementation and static/API validation are complete. The cloud browser blocks local and
self-contained preview URLs. Desktop/phone and injected-failure rendering will be checked on the
public deployment in Step 8; this step cannot be marked `PASSED` before that evidence exists.

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

## 8. Full verification and deployment — IN PROGRESS

Plan:

- Run all unit/integration tests, migration tests, static checks, and browser smoke tests.
- Review the pull-request diff and GitHub Continuous Integration results.
- Deploy with both strategies paper-only and the event feed diagnostic-only.

Acceptance tests:

- Local tests and GitHub Continuous Integration are green.
- Railway is healthy and the dashboard reports either `ALL SYSTEMS GOOD` or a specific fault.
- New signals/events persist through a restart on `/srv/data`.

## 9. Independent final review — PENDING

Plan:

- Re-review entry mathematics, simulation independence, execution realism, leakage risk,
  match semantics, failure visibility, mobile usability, and export completeness.
- Fix every material issue before merging; record non-blocking limitations explicitly.

Acceptance tests:

- `docs/DUAL_SLEEVE_CHANGELOG.md` contains a final audit with evidence and remaining limitations.
- Pull request stays separately revertible from the original sleeve change.
