# Production Integrity Implementation Specification

> ## ⚠ STOP — read before revising this specification
>
> Parts of this document, as originally written, produced defects in
> production-shaped data. Corrections, deviations, and deliberate extensions are
> recorded in **[`docs/SPEC_CORRECTIONS_AND_DEVIATIONS.md`](SPEC_CORRECTIONS_AND_DEVIATIONS.md)**.
>
> Re-issuing the original wording of **§3.4, §4.1, §4.2, §4.3, §5, §6.2, §7.1, or
> §9** without reading that file **will reintroduce a known defect**. The most
> serious is §4.1: the clock-parsing precedence names `status_text` as a source
> but gives no rule for identifying the clock inside it, and the obvious reading
> parses `"2nd Half 90+5'"` as minute 2.
>
> `tests/test_spec_corrections.py` fails if this banner is removed.


- Status: **PLAN ONLY — implementation must not merge before independent final review**
- Owner: next implementation agent
- Final reviewer: Codex review requested by the repository owner
- Scope: paper trading only; no live orders and no promise of profit

## 1. Confirmed production defects and evidence

This specification starts from observed production evidence, not an assumed design:

1. **The 88+ sleeve is not producing a valid study.** Production has 86
   `sleeve_outside_window` records and zero price-only classified/fill samples. The Al-Hazm vs
   Al-Shabab 90+5 equalizer signal was rejected because `expected_expiration_time` was 3,820.188
   seconds after the signal. Expected expiration is not the match clock.
2. **Download never reaches the download route.** Railway HTTP logs show accepted prepare requests
   and repeated status polling, including status requests blocked for 32.4 and 93.6 seconds, but no
   `/download` request. The production process was using about 2.54 GB memory during review. A full
   raw archive cannot be the only browser export path.
3. **Latency evidence is breached while health says all is good.** `/api/stats` reports total
   order-arrival p95 `3,642.1875 ms` and K4 `BREACH`; `/api/status` reports `health.ok=true`.
   `/api/latency` samples only the newest 1,000 rows across all kinds, so frequent feed-lag rows
   crowd out order-arrival rows and misleadingly expose only four recent order samples.
4. **The event table is sparse by construction.** It stores score-signature changes, not a match
   clock timeline or all provider match events. Most signals therefore cannot receive a match-minute
   stamp and legitimately cannot be associated with a stored event.
5. **Closed trades do not retain maximum favorable executable price.** Entry, exit, and MAE do not
   answer what highest bid was available or how many seconds after entry it occurred.

## 2. Non-negotiable invariants

- Gate A detection, entry, exit, sizing, fees, lockout, and settlement behavior must not change.
- The price-only sleeve may use a **clock-only gate** (`period`, `minute`, `stoppage`, status,
  freshness) to establish minute 88+. It must not read score, scorer, goal, penalty, VAR,
  correction, narrative, or canonical event fields.
- A price-only paper trade without a fresh, persisted 88+ clock stamp is invalid and must fail
  closed with an auditable reason. Do not substitute expected expiration or UTC wall time.
- Every new signal persists the exact match-clock snapshot available at decision time. Every trade
  API record inherits that immutable snapshot through its `signal_id`.
- Provider event proximity is an audit association, not proof of causality. The strategy trigger and
  the nearest match event must be displayed as two distinct facts.
- High-price evidence uses executable best bid on the held side. Never use midpoint, last trade,
  ask, settlement value, or a reconstructed value.
- Historical fields that were not recorded remain `null` with a reason; never backfill invented
  match minutes or highs.
- All data exports remain admin-protected, native-streamed, and secret-free.
- One feature branch, one independently revertible PR, several reviewable commits. Do not touch the
  unrelated `kalchi-kill` Railway service.

## 3. Target data model

### 3.1 Match-clock observations

Add an append-only `match_clock_observations` table:

| Column | Type | Rule |
|---|---:|---|
| `id` | integer PK | Monotonic observation identifier |
| `observed_ts` | real, required | Local receipt wall time |
| `poll_started_ts` | real, required | Local request start |
| `previous_poll_ts` | real | Prior successful receipt for uncertainty |
| `response_ms` | real, required | Monotonic request duration |
| `event` | text, required | Original Kalshi event ticker |
| `milestone_id` | text, required | Original provider identifier |
| `provider_period` | text | Raw-normalized first/second half or match state |
| `provider_minute` | integer | Base match minute, e.g. `90` |
| `provider_stoppage` | integer | Added minute, e.g. `5` for `90+5` |
| `provider_clock` | text | Human rendering, e.g. `90+5′` |
| `provider_status` | text | Live, half-time, final, suspended, etc. |
| `precision` | text, required | `provider_minute_polled`; never imply exact seconds |
| `raw_context` | JSON text, required | Preserved source clock/status fields only |

Create index `(event, observed_ts)`. Insert when clock/period/status changes; do not insert every
250 ms unchanged poll. Preserve the latest normalized clock in memory for decision-time lookup.

### 3.2 Immutable signal clock stamp

Add `signals.match_clock_snapshot TEXT` containing:

```json
{
  "schema": "football.match_clock_stamp.v1",
  "observation_id": 123,
  "event": "original-event-ticker",
  "provider_period": "2nd",
  "provider_minute": 90,
  "provider_stoppage": 5,
  "provider_clock": "90+5′",
  "provider_status": "live",
  "observed_ts": 1788112879.6,
  "signal_local_ts": 1788112879.8,
  "age_ms": 200.0,
  "poll_uncertainty_ms": 250.0,
  "source": "kalshi_live_data_batch",
  "precision": "provider_minute_polled",
  "usable_for_88_gate": true,
  "unusable_reason": null
}
```

Store a stamp for every new signal, including Gate A, price-only declines, lockouts, no-book rows,
and unconfirmed detector rows. If mapping is genuinely broken, persist a complete unusable stamp
with the exact reason and raise an infrastructure health fault; do not omit the field.

Trades do not need a second mutable copy. `/api/trades` must join the immutable signal stamp and
return it as `match_clock`; the export contains both the FK and source table.

### 3.3 Provider match events

Keep score-latency observations, and add/rename an append-only canonical event ledger capable of
retaining more than score changes. Diff provider significant-event arrays and `last_play` using a
stable fingerprint. Preserve raw payload and first/last observed times. Minimum canonical types:

- `goal.observed`, `goal.disallowed`, `score.correction`
- `penalty.awarded`, `penalty.scored`, `penalty.missed`
- `var.review`, `var.overturned`
- `card.red`, `card.yellow`, `substitution`
- `match.started`, `period.started`, `period.ended`, `match.ended`, `match.suspended`
- `provider.unknown`

Do not relabel an unchanged numeric score as a goal. Revisions append a correction linked to the
prior fingerprint; raw history is immutable.

### 3.4 Maximum executable price

Add nullable `trades.max_executable_bid`, `trades.max_executable_bid_ts`, and `trades.mfe_c`.

- Start observing only after entry fill.
- On every valid order-book update for the held side, compare its executable best bid.
- Persist atomically only when `new_bid > stored_bid`; equal highs keep the first timestamp.
- Continue through partial exits until the final exit fill.
- Settlement prices are not executable bids and never update the high.
- Restore all three values for open positions after restart.
- `high_after_entry_s = max_executable_bid_ts - entry_ts` is derived by the API.
- `mfe_c = max(0, max_executable_bid - entry_px)`; keep the actual high even when below entry.

## 4. Clock ingestion and the authoritative 88+ gate

### 4.1 Parsing precedence

Parse current clock from current-state fields in this order:

1. `details.time`
2. `details.match_clock` / `details.game_clock` / `details.clock`
3. the clock portion of `details.status_text`

Do not use a historical significant-event time or `last_play` time as the current match clock.
Normalize `87'`, `88'`, `90'`, `90+5'`, typographic apostrophes, and numeric forms. Preserve raw.

### 4.2 Mapping and coverage

- Resolve a milestone for every watched event before its market is eligible for the price-only
  sleeve. Retry unresolved events with bounded backoff and expose event ticker plus last error.
- Batch-poll mapped milestones. Maintain `watched`, `mapped`, `clock_present`, `clock_fresh`, and
  `clock_gate_candidate_misses` counts.
- A pre-match event may be mapped with no live clock without being an error. Once a confirmed price
  candidate occurs or provider status becomes live, missing/stale clock is an infrastructure fault.
- Persist clock stamps at signal receipt, not later by nearest-time reconstruction.

### 4.3 Gate contract

Create a narrow immutable `MatchClockGate` object. It is the only match-feed object that the
price-only path may import. It contains only period/minute/stoppage/status/age/source IDs.

The gate accepts when all are true:

1. mapping exists and the current provider status is live;
2. clock observation age is within a configured maximum backed by measured polling behavior;
3. the period is second half (including stoppage) or a provider-equivalent terminal second-half
   label;
4. base minute is at least 88. Both `90′` and `90+N′` remain eligible while the market is open.

Final, suspended, abandoned, stale, unmapped, malformed, or pre-88 clocks decline with distinct
outcomes. Expected expiration stays in the UI as a calibration diagnostic only and cannot activate
the sleeve.

After the gate accepts, the existing triplet price classifier, paper execution, exits, and
independent shadow books run unchanged. Score/event content is never passed to them.

## 5. Event association semantics

Every signal/trade UI record must show the match-clock stamp even when there is no nearby event.
Association then runs separately:

1. same original event ticker is mandatory;
2. compare signal local receipt with provider event occurrence and first local observation;
3. enforce configured windows and show both deltas;
4. prefer substantive goal/correction/penalty/VAR events over schema refreshes;
5. return `temporally_associated`, `state_consistent`, `state_mismatch`, or
   `no_nearby_same_match_event`—never `caused_by`.

The trade's **reason** remains its strategy trigger and exit reason. A separate **nearby match
event** field explains what provider event, if any, was recorded around it.

## 6. Export architecture

### 6.1 Two explicit products

The browser must offer both:

1. **Audit bundle (default):** SQLite snapshot, schema, all normalized tables, fills, latency,
   match clocks/events, configuration, manifest, hashes, and raw-file inventory. It excludes raw
   WebSocket segment bodies and must normally prepare in under 10 seconds.
2. **Full raw handoff:** the audit bundle plus all raw recorder segments. It is allowed to be large
   and must expose progress, size, segment count, and cancellation.

Also expose each raw segment as a protected native file download with HTTP range support. This
guarantees that all required data remains downloadable even when one multi-gigabyte archive is
impractical.

### 6.2 Job behavior

- `POST /api/export/prepare?scope=audit|full` returns `202` and a job ID.
- Status returns `queued|preparing|ready|error|expired|cancelled`, processed/total bytes, processed/
  total raw segments, output bytes, timestamps, and a public error code.
- Build the full bundle outside the web event loop (dedicated worker process or equivalent). One
  pass copies each already-gzipped raw segment with ZIP64 `STORED` while calculating its SHA-256.
- Only one full job may run; an audit job must remain usable while it runs.
- Downloads use native streaming/FileResponse and the job-scoped HttpOnly cookie or admin header;
  never `response.blob()` for the whole archive.
- Apply TTL cleanup only after active downloads finish. Job replacement must not delete a file
  currently being served.
- UI polling uses timeout/abort, displays progress and precise failure, and must not clear a valid
  admin token because of a transient 5xx/network error.

## 7. Latency and readiness architecture

### 7.1 Metrics and exact definitions

| Metric | Start | End |
|---|---|---|
| `feed_ingress_ms` | exchange message timestamp | local WebSocket receipt |
| `decision_ms` | confirmed candidate local receipt | paper order queued |
| `paper_entry_ms` | paper order queued | simulated fill attempt |
| `order_arrival_ms` | exchange candidate timestamp | simulated fill attempt |
| `paper_exit_ms` | exit decision queued | simulated exit fill |
| `match_response_ms` | match poll monotonic start | response receipt |
| `match_clock_age_ms` | clock observation receipt | signal receipt |
| `scheduler_lag_ms` | scheduled execution tick | actual execution tick |

Reject/quarantine non-finite or impossible negative values. Detect material wall-clock drift by
cross-checking monotonic durations; do not silently clamp a bad clock into a good sample.

### 7.2 Aggregation and health

- Query the newest N samples **per kind**, never one global LIMIT across kinds.
- Return n, p50, p95, max, invalid count, latest timestamp, age, threshold, and state.
- Preserve the existing K4 order-arrival threshold of 250 ms unless a separately reviewed data
  study changes it. Minimum readiness sample count is 20.
- Readiness states: `PASS`, `BREACH`, `COLLECTING`, `STALE`, `INVALID`.
- Runtime health and evidence readiness are separate. The header may say “All systems good” only
  when runtime checks pass and execution latency is not breached/invalid. While collecting or stale,
  say “Runtime healthy · paper evidence not ready,” not “all good.”
- A large export must not breach scheduler lag or block status responses; include a direct test.

## 8. Frontend acceptance contract

### Trades

Every closed trade card shows, without opening raw JSON:

- human match and contract; sleeve; match date/time;
- persisted match clock (`88′`, `90+5′`) and observation age/precision;
- trigger reason, entry, exit, exit reason, quantities, gross, fees, net;
- maximum executable bid, MFE, UTC time of high, and seconds after entry;
- nearest canonical event plus provider occurrence/receipt deltas, or an explicit “No nearby
  same-match event” message;
- raw identifiers in an expandable audit section.

Losing trades visually prioritize entry → high → exit so missed profit/reversal can be reviewed.

### Signals

Every signal shows the immutable clock stamp and one of the exact 88-gate outcomes. Price-only
accepted/declined counts reconcile with `/api/stats`. Filters cover sleeve, match, 88-gate result,
event association, profitable/loss, and time period.

### System and downloads

- Clock coverage panel: watched/mapped/live/fresh/missed plus per-event faults.
- Latency panel: n/p50/p95/max/freshness/threshold/state for every metric.
- Separate audit/full/raw-segment downloads with progress and visible errors.
- No mobile truncation; raw text wraps; download controls remain usable at 360 px width.

## 9. Required automated tests

### Clock parser and persistence

- table-driven parse: `87'`, `88'`, `90'`, `90+1'`, `90+12’`, numeric values, status text,
  malformed/missing values;
- current clock never comes from historical `last_play` or significant-event times;
- one observation per changed clock/period/status; unchanged polls do not flood SQLite;
- every signal outcome persists a complete stamp; trade API returns the identical stamp;
- schema migration preserves old DBs and marks old signals as legacy without fabrication;
- restart restores latest clock mapping/cache safely from new polls, not stale process memory.

### 88+ sleeve

- 87 rejects; 88 accepts; 89/90/90+N accept; first half/final/suspended reject;
- missing, malformed, future-dated, and stale clocks fail closed and raise readiness faults;
- expected-expiration values cannot change the gate result;
- a deterministic Al-Hazm 90+5 replay reaches the price classifier instead of
  `sleeve_outside_window`;
- accepted price-only and Gate A candidates queue separate orders, lockouts, positions, and P&L;
- an AST/import allowlist proves the price-only classifier and paper desk cannot access raw
  live-data, score, goal, penalty, VAR, correction, scorer, or narrative fields.

### Events and association

- penalty announced, penalty scored, ordinary goal, disallowed goal/VAR, score correction, card,
  substitution, and unknown event fixtures;
- unchanged score-schema refresh is not a goal and carries no stale scorer;
- de-duplication and revision links preserve raw history;
- signal between score events still has a clock stamp but no fake event association;
- semantic event preference and association-window boundaries are deterministic.

### Trade highs

- rising/falling bids; repeated equal high keeps first time; held-side conversion for YES and NO;
- ask/mid/last/settlement cannot update high;
- partial exit tracks until final fill; restart recovery continues from stored high;
- high below entry remains visible while `mfe_c=0`; no observed quote remains null;
- API seconds-to-high and UI loss layout use exact stored timestamps.

### Exports

- authorized audit prepare/status/download ZIP validation and manifest/table reconciliation;
- authorized full progress/cancel/download and per-segment range requests;
- concurrent audit/full behavior; ready-file lease prevents premature deletion;
- unauthorized, wrong-cookie, expired, missing, and path-traversal requests fail closed;
- secrets never appear; ZIP64 and large synthetic files work; event loop/status remains responsive;
- frontend uses native download and surfaces timeout/network/server errors.

### Latency and health

- per-kind sampling cannot be crowded out by feed rows;
- exact percentile fixtures, sample minimums, stale/invalid/breach transitions;
- monotonic scheduler-lag test and negative/non-finite quarantine;
- health banner cannot say “All systems good” when K4 is breached;
- `/api/status`, `/api/latency`, `/api/stats`, and UI evidence reconcile.

## 10. Implementation sequence and commit boundaries

Use branch `codex/production-integrity-clock-export` and one revertible PR with these commits:

1. `Persist match clocks and canonical provider events` — additive schema/parser/observer/API only;
   no strategy behavior change.
2. `Gate paper 88+ sleeve on persisted live clock` — narrow `MatchClockGate`, explicit outcomes,
   independence tests; Gate A untouched.
3. `Record executable trade highs and latency readiness` — storage, paper desk, health/API.
4. `Split reliable audit and raw exports` — background job, progress, segment downloads, security.
5. `Expose clock, high, event, latency, and export audit UI` — desktop/mobile frontend.
6. `Document production evidence and rollback` — checklist/changelog only.

Each commit must pass the full suite and be individually reviewable. Do not combine formatting or
unrelated refactors.

## 11. Production verification before final review

The implementation agent must attach machine-readable evidence to the PR:

1. Full unit/static suite, migration test from a copy of the persisted schema, JavaScript syntax,
   diff whitespace, and dependency audit.
2. Local deterministic Al-Hazm replay: clock `90+5′`, price-only gate accepted, separate sleeve
   execution, event association shown, no score/event field in the price decision record.
3. Production API snapshot after deploy: status, stats, per-kind latency, clock coverage, signals,
   trades, and export job metadata.
4. Authorized production audit bundle: ready in under 10 seconds, HTTP 200 download, valid ZIP,
   manifest schema/table counts/hashes reconciled.
5. One protected raw-segment range download and, if practical, a full-job progress observation.
6. Rendered desktop and 360 px mobile screenshots for trade, signal, system, and download states.
7. Confirm the live service remains paper-only and the unrelated Railway service was untouched.

Live acceptance is strict for records created after deployment:

- 100% of new signals contain a structured clock stamp (usable or explicit infrastructure fault);
- 100% of new price-only fills have a fresh `usable_for_88_gate=true` stamp at minute 88+;
- no price-only record uses `sleeve_outside_window` based on expected expiration;
- no closed post-deploy trade omits high evidence if at least one executable bid was observed;
- K4 state shown in `/api/stats`, `/api/status`, and the UI is identical;
- runtime requests remain responsive while an export is prepared.

## 12. Final reviewer checklist

The final reviewer must block merge on any failed item:

- Trace data lineage from raw clock payload → normalized observation → immutable signal stamp →
  88 gate → trade API/UI.
- Prove by code search and tests that the price sleeve reads clock-only fields and no event/score.
- Recalculate a sample of p50/p95 values and trade high/time-to-high directly from exported rows.
- Reconcile combined and per-sleeve signal counts, fills, fees, P&L, and league totals.
- Inspect loss cards for entry/high/exit accuracy and legacy-null honesty.
- Exercise both export products, authorization failures, expiry, cancellation, and mobile errors.
- Review migration idempotency, restart recovery, file cleanup leases, and rollback instructions.
- Confirm CI, deployment health, production evidence, and no real-order code path.

Only after this review passes should the PR merge. After merge, record the commit, CI run,
deployment ID, exact validation outputs, known limitations, and rollback command in
`docs/DUAL_SLEEVE_CHANGELOG.md`.

## 13. Rollback

Because schema changes are additive, application rollback is one Git revert of the merge commit.
Old code must tolerate the added tables/columns. Do not drop clock/event/high data during rollback.
Disable only the price-only clock gate through its paper-sleeve feature flag if rapid containment is
needed; Gate A remains unchanged. Export workers must be cancellable before reverting the app.
