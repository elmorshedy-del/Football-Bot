# PR 12 Blocker-Resolution Handoff

> **BINDING — PR 12 MUST STAY DRAFT AND MUST NOT MERGE OR DEPLOY UNTIL EVERY
> `BR-*` ITEM BELOW HAS EVIDENCE AND THE FINAL REVIEWER SIGNS OFF.**

This is the implementation contract for the next pass on pull request 12. It
supersedes any `PASSED` label in checklist sections 11, 12, 14, or 15 and any PR
comment that says the affected behavior is complete. It does not replace
`SPEC_CORRECTIONS_AND_DEVIATIONS.md` or
`PRODUCTION_INTEGRITY_IMPLEMENTATION_SPEC.md`; read those first and apply this
document where the independent review found that the implementation did not
meet them.

## 0. Fixed scope and baseline

| Item | Required value |
|---|---|
| Repository | `elmorshedy-del/Football-Bot` |
| Pull request | `#12` |
| Existing head branch | `cursor/production-integrity-clock-export-aaf8` |
| Reviewed head | `cd4d36e1adeb01d63381fce79b58d6311cfc7b2d` |
| Reviewed tree | `70b550d8f620db82e8ca22ee58c4ea294eb5d925` |
| Base branch/head at review | `main` / `8b6a8a8e736f8eb59cec7383a51b38c857947816` |
| Reviewed CI run | `33450135700` (`success`, but missing the regressions in this handoff) |
| Delivery shape | Continue the same draft PR; do not open a replacement PR |
| Runtime mode | Paper only; no live-order code or endpoint |
| Forbidden service | Never modify or deploy Railway service `kalchi-kill` |

Before editing code, fetch PR 12 again and record its current head. If it is not
the reviewed head above, compare the new commits against this document and
record the new starting SHA in the changelog. Do not overwrite another agent's
work and do not force-push.

This pass fixes collection and audit integrity. It **must not** tune entry,
momentum, profit-taking, scratch, stop, oscillation, or league parameters. It
must not change Gate A behavior. It must not claim that the strategy is a money
printer, guaranteed profitable, or lossless.

## 1. Definition of done

The implementer may hand this back for final review only when all of the
following are true:

- [ ] `BR-00` Each regression named below was first run against the reviewed
  head and failed for the stated reason. The command and failure excerpt are in
  `docs/evidence/pr12/<candidate-sha>/baseline-red-tests.txt`.
- [ ] `BR-01` Every code contract and forbidden shortcut below is satisfied.
- [ ] `BR-02` Every named regression test is behavioral. Reading source text and
  checking that a function name/string exists is not proof of behavior.
- [ ] `BR-03` The full suite and static checks in section 10 pass without an
  unhandled task exception, `ResourceWarning`, or warning hidden after an exit
  code of zero.
- [ ] `BR-04` A production-schema copy migrates twice without data loss,
  duplicate columns/indexes, or changed historical provenance.
- [ ] `BR-05` The review deployment and rendered checks in section 11 pass. A
  local unit-suite result alone is not delivery.
- [ ] `BR-06` The checklist and changelog contain exact commit SHAs, files,
  commands, outputs, deployment evidence, limitations, and rollback.
- [ ] `BR-07` An independent reviewer reruns the checks and explicitly records
  `APPROVED`. The implementer may not self-approve.

If any item cannot be completed, leave PR 12 draft, mark the exact item
`BLOCKED`, and return the evidence. Do not relabel a blocker as a limitation.

## 2. Required implementation order and commits

Keep these independently reviewable commits in this order. A work package may
use more than one commit only when the changelog explains why.

1. `Fix persisted clock publication and current clock health` — sections 3–4.
2. `Preserve evidence modes and provider-event lineage` — sections 5–6.
3. `Keep export preparation off collection locks` — section 7.
4. `Make path finalization durable and gap-aware` — section 8, including UI.
5. `Record validation and production evidence` — sections 9–12; documentation
   and evidence only.

Do not mix formatting, dependency upgrades, strategy tuning, or unrelated
refactors into these commits.

## 3. Persisted clock publication and 88+ gate (`BR-CLOCK`)

### 3.1 Reproduced defects

At the reviewed head:

- `GoalLatencyObserver._record_clock()` calls
  `MatchClockTracker.observe()`, which publishes a new row to `latest`, then
  awaits `store.insert_match_clock()`. The trading loop can consume that
  unpersisted row during the await.
- If the insert raises, the id-less row remains in `latest`; the next identical
  poll is treated as unchanged and never retries the insert.
- `MatchClockGate.evaluate()` accepts an otherwise fresh minute-88 row when
  `observation_id` is null. The reproduced result was
  `{"observation_id":null,"accepted":true,"outcome":"clock_88_plus","usable":true}`.
- Reconfirmation overwrites `previous_poll_ts` on the cached original row while
  retaining its original `observed_ts`. After receipts at 10.00, 10.25, and
  10.50, the current code reports `poll_uncertainty_ms=-250`.

### 3.2 Required state model

Implement two-phase publication. Use these semantics even if method names
differ:

1. **Parse/candidate phase:** build a candidate row without changing any
   decision-visible value in `MatchClockTracker.latest`, `last_identity`,
   `clock_present`, `last_confirmed`, or current health.
2. **Persist phase for a new identity:** call `store.insert_match_clock()` in a
   worker thread. Until it returns a positive integer row id, the previous
   persisted observation remains the only decision-visible observation.
3. **Promotion phase:** on the event loop, atomically publish the complete row
   with its positive `id`, update identity/presence/confirmation, and clear the
   current persistence fault for that event.
4. **Insert failure:** retain a retryable pending candidate or leave identity
   uncommitted so the next identical poll retries. Keep the previous persisted
   observation visible, set a current `clock_persistence_failed` fault, and
   report the exception. Never promote an id-less candidate.
5. **Unchanged identity:** it may refresh the already-persisted row's
   `confirmed_ts`, `confirmation_previous_poll_ts`, and response metadata. It
   must preserve the original `id`, `observed_ts`, identity, and source.
6. **Event removal/restart:** clear pending/current in-memory state. A new
   process must receive a new provider confirmation before the clock becomes
   fresh; a database row alone is not a fresh live confirmation.

The immutable signal stamp must carry both concepts:

| Field | Meaning |
|---|---|
| `observation_id` | Positive id of the persisted row that established the clock identity |
| `observed_ts` | Receipt time stored on that row |
| `confirmed_ts` | Most recent provider receipt confirming the same identity before the signal |
| `confirmation_previous_poll_ts` | Receipt time immediately before `confirmed_ts` |
| `age_ms` | `(signal_local_ts - confirmed_ts) * 1000` |
| `established_age_ms` | `(signal_local_ts - observed_ts) * 1000` |
| `poll_uncertainty_ms` | `(confirmed_ts - confirmation_previous_poll_ts) * 1000` |

`poll_uncertainty_ms` is null when either endpoint is unavailable. It is never
negative. Do not calculate current confirmation uncertainty using the original
`observed_ts`.

Add `source TEXT` idempotently to `match_clock_observations`. Every new row must
store the exact clock source used by its stamp; legacy null source remains
`legacy_unknown`, not silently relabeled as the current provider. Confirmation
receipt fields belong to the immutable signal snapshot because they describe
the confirmation selected for that decision; the persisted observation row
anchors the clock identity and original receipt.

### 3.3 Gate invariant

Both `stamp_from_observation()` and `MatchClockGate.evaluate()` must fail closed
before applying minute/status logic when `observation_id` is not a positive
integer:

```json
{
  "accepted": false,
  "outcome": "clock_unpersisted",
  "usable_for_88_gate": false,
  "unusable_reason": "unpersisted"
}
```

Reject a confirmation or observation later than the signal receipt; use
`clock_future` / `future_timestamp` and do not coerce the age to zero. Preserve
the existing terminal-status ordering and 88+ semantics.

For every accepted price-only signal, a query by `observation_id` must return
exactly one `match_clock_observations` row whose `event`, provider period,
minute, stoppage, rendered clock, status, and source identity equal the stamp.
This database relationship is an acceptance invariant, not a UI convention.

### 3.4 Required behavioral tests

Add or rewrite tests in `tests/test_match_clock.py` and
`tests/test_goal_latency.py` with these exact cases:

- [ ] `test_signal_during_blocked_clock_insert_rejects_unpersisted_clock`:
  block `insert_match_clock` with threading events, let a signal run while the
  await is pending, and assert `clock_unpersisted` or the prior persisted stamp;
  never `clock_88_plus` on the candidate.
- [ ] `test_failed_clock_insert_retries_identical_next_poll`: first insert
  raises, second identical poll succeeds, and no acceptance occurs before the
  successful id is published.
- [ ] `test_every_accepted_clock_stamp_resolves_to_matching_database_row`:
  generate accepted stamps, query SQLite, and compare every lineage field.
- [ ] `test_unchanged_poll_preserves_id_and_uses_latest_confirmation_interval`:
  receipts 10.00/10.25/10.50 retain one id, set `confirmed_ts=10.50`, and report
  `poll_uncertainty_ms=250`, never `-250`.
- [ ] `test_restart_requires_new_provider_confirmation_for_freshness`.
- [ ] `test_future_observation_and_confirmation_fail_closed`.
- [ ] Mutation proof: temporarily remove/bypass the positive-id check and show
  that at least one committed test fails. Record the mutation and failing test
  in the changelog; do not commit the mutation.

## 4. Current clock health, recovery, and all-good banner (`BR-HEALTH`)

### 4.1 Reproduced defects

- A mapped pre-match game with no minute is reported unhealthy because
  `_clock_coverage_check()` treats `mapped > clock_present` as a fault.
- `clock_gate_candidate_misses` is cumulative but is treated as a permanent
  current fault, so one historical miss prevents recovery forever.
- An id-less pending clock can increase presence/freshness and produce a false
  green state.

### 4.2 Required model and API

Separate **current state** from **cumulative evidence**. Coverage must expose an
`events` array (or an equivalently explicit per-event object) containing:

`event`, `mapped`, `provider_status`, `observation_id`, `clock_present`,
`clock_fresh`, `last_confirmed_ts`, `candidate_active`, `current_fault`, and
`state` (`waiting`, `observing`, `fault`).

Apply these rules:

- Mapped + pre-match/not-yet-live + no minute = `waiting`, healthy.
- Provider status live + missing/malformed/stale persisted clock = current
  fault.
- An active price-only candidate + missing/malformed/stale/unpersisted clock =
  current fault even if provider status is unknown.
- Presence/freshness count only a positive-id, decision-visible observation.
- A successful persisted fresh confirmation clears the event's current clock
  fault. Historical misses remain in
  `clock_gate_candidate_misses_total` for audit but do not block health.
- Staleness is derived from current time on every status request; no cached
  `fresh=true` survives decay.
- Mapping, clock, database, recorder, WebSocket, or recent backend fault makes
  the top banner non-green. After the current fault clears, all-good is
  reachable again if all other checks pass.

Do not use count arithmetic alone to decide health; evaluate the per-event
current states and return their reasons to the frontend.

### 4.3 Required tests

In `tests/test_health.py` add:

- [ ] `test_mapped_pre_match_without_clock_is_healthy_waiting`.
- [ ] `test_live_provider_without_persisted_clock_is_fault`.
- [ ] `test_active_candidate_with_missing_or_stale_clock_is_fault`.
- [ ] `test_persisted_reconfirmation_clears_current_fault_but_keeps_total_miss_count`.
- [ ] `test_pending_idless_clock_never_counts_present_or_fresh`.
- [ ] `test_clock_freshness_decays_at_status_time`.
- [ ] `test_all_good_is_impossible_during_current_clock_fault_and_returns_after_recovery`.

## 5. Evidence-mode isolation without deleting history (`BR-MODE`)

### 5.1 Reproduced defects

- Schema migration added `mode` to goal-latency and provider-event tables, but
  `insert_goal_latency()` and `upsert_provider_event()` do not write it.
- A live startup calls `purge_non_live()`, which deletes those newly inserted
  null-mode rows on the next restart.
- Demo `paper_fills` and demo `latency` rows survive into live statistics;
  `latency` has no mode column.
- Deleting null-mode rows destroys legacy evidence and falsely treats unknown
  provenance as disposable.

### 5.2 Required schema and query contract

The following study tables must carry active capture mode on every new row:

`signals`, `trades`, `paper_fills`, `latency`, `bid_path_samples`,
`match_clock_observations`, `provider_match_events`, and
`goal_latency_observations`.

Requirements:

1. Add `latency.mode TEXT` idempotently. Keep existing null values unchanged;
   expose them as `legacy_unknown` at the API/export semantic layer.
2. Every insert and transactional insert path writes the current `store._mode`,
   including `add_latency()`, `open_paper_trade()`, `record_paper_exit()`, goal
   latency, provider events, clocks, signals, trades, fills, and paths.
3. Remove startup deletion as an isolation mechanism. `purge_non_live()` must
   no longer delete study observations. If retained for compatibility, make it
   non-destructive and rename/document the behavior. Do not delete null/demo
   history during `init()`, startup, restart, or export.
4. Default stats, latency readiness, signals, trades, paths, clocks, provider
   events, goal latency, and equity queries to the active mode. Provide an
   explicit safe selector for `live`, `demo`, `legacy_unknown`, or `all` where
   audit endpoints need it. `legacy_unknown` maps to SQL `mode IS NULL`; it is
   not silently included in live.
5. Export manifest records requested modes and per-table counts by mode. The
   normal audit/full bundle defaults to the active mode; an explicit all-mode
   archival export may include all modes but must label them.
6. Provider-event duplicate identity is mode-scoped. Replace the existing
   `(event,fingerprint)` unique index with an idempotent, data-preserving unique
   index over `(event,fingerprint,COALESCE(mode,'legacy_unknown'))`; no row may
   be deleted or have its original mode rewritten. Upsert never changes the
   first row's mode or raw payload.
7. Every PnL aggregate reconciles to same-mode trades and fills. K4/order
   arrival readiness reads only active live latency when the process is live.

Changing an index is permitted only for the mode-scoping requirement above;
the migration must be transactional and must not rewrite/delete observations.

### 5.3 Required tests

Create `tests/test_evidence_modes.py` and extend exporter/store tests:

- [ ] `test_every_fresh_live_insert_writes_live_mode` covers all eight tables.
- [ ] `test_demo_then_live_restart_preserves_both_but_live_apis_exclude_demo`.
- [ ] `test_legacy_null_rows_survive_init_startup_and_two_restarts` and API
  presentation labels them `legacy_unknown`.
- [ ] `test_provider_and_goal_rows_survive_two_live_restarts`.
- [ ] `test_same_provider_fingerprint_can_exist_once_per_mode_without_overwrite`.
- [ ] `test_live_latency_readiness_excludes_demo_and_legacy_samples`.
- [ ] `test_export_manifest_and_rows_reconcile_by_mode_with_no_orphan_fill`.
- [ ] `test_mode_migration_is_idempotent_and_preserves_row_hashes` (hash the
  historical value columns before and after two migrations; exclude only new
  metadata/index definitions from the comparison).

## 6. Canonical provider-event occurrence and revision lineage (`BR-EVENT`)

### 6.1 Reproduced defects

- Significant events store the individual provider row as `raw_payload`, while
  `audit.match_signal_event()` only searches
  `raw_payload.details.last_play.occurence_ts`. A real row with root
  `occurence_ts` therefore reports a null occurrence time.
- Only the provider's misspelled key is recognized; root/nested and corrected
  `occurrence_ts` variants are not handled deterministically.
- The correction chain depends on in-memory
  `last_substantive_fingerprint`; restart loses it and event removal does not
  clear it. Existing tests merely search source text.

### 6.2 Required normalized fields

Add these columns idempotently to `provider_match_events`:

| Column | Contract |
|---|---|
| `provider_occurrence_ts REAL` | Finite numeric provider occurrence timestamp, never receipt time |
| `provider_occurrence_source TEXT` | Exact source path, e.g. `raw.occurence_ts` |
| `provider_occurrence_unavailable_reason TEXT` | `provider_field_absent` or `provider_field_invalid` when null |

At canonicalization, use this fixed precedence:

1. individual row `occurence_ts`;
2. individual row `occurrence_ts`;
3. `details.last_play.occurence_ts` in a full payload;
4. `details.last_play.occurrence_ts` in a full payload.

Accept finite `int`/`float` values only; reject booleans, NaN, infinities, text,
and negative values. Do not substitute `first_observed_ts`, `observed_ts`,
exchange signal time, match minute, or wall time. Preserve the raw provider
object without deleting, renaming, or humanizing its keys.

`match_signal_event()` must consume the normalized database field. A
deterministic legacy fallback may derive the same field from preserved raw JSON
using the same precedence, labeled `legacy_raw_derived`; absence remains null
with the explicit reason. Keep provider occurrence, first receipt, prior poll,
response duration, and match clock as separate fields in the API and UI.

### 6.3 Revision-chain contract

- For a correction/reversal, resolve `previous_fingerprint` from the in-memory
  same-event state first. If absent (including restart), query the newest
  persisted **substantive** event for the same event and same evidence mode.
- Never link across event ids, modes, or to another correction.
- On event removal, clear `seen_fingerprints`, lifecycle state, pending clock
  state, and `last_substantive_fingerprint` for that event.
- Events remain append-only. A duplicate refresh may update only receipt/poll
  metadata for its same-mode row; it may not overwrite the original raw
  payload, occurrence fields, first receipt, canonical identity, or mode.

### 6.4 Required tests

Replace source-string assertions in `tests/test_provider_event_audit.py` with
runtime tests:

- [ ] root `occurence_ts` real-shaped significant row.
- [ ] root `occurrence_ts` variant.
- [ ] full-payload `details.last_play` with both spellings.
- [ ] absent and invalid timestamp stays null with the exact reason and no
  fabricated delta.
- [ ] restart, then correction-only poll, links the persisted prior substantive
  fingerprint for the same event/mode.
- [ ] drop event A, add event B, then correction cannot link to A.
- [ ] duplicate refresh preserves original raw payload/mode/occurrence.
- [ ] provider ledger API returns raw payload, canonical label, provider match
  clock, occurrence/source/reason, first receipt, previous poll, response,
  uncertainty, and revision link as distinct fields.

## 7. Non-blocking export and captured failures (`BR-EXPORT`)

### 7.1 Reproduced defects

- `store.backup_database()` holds the global store `_lock` for the entire
  SQLite backup. Running it in `asyncio.to_thread()` moves the lock owner but
  event-loop calls such as `database_health()` and `stats()` still block waiting
  on that lock. A 350 ms simulated backup lock produced a 349.3 ms event-loop
  status stall.
- Legacy `GET /api/export` performs recorder checkpoint and database snapshot
  synchronously on the event loop.
- The current heartbeat test sleeps inside a mocked bundle builder and never
  creates real store-lock contention.
- A background export task can raise an exception that is logged as `Task
  exception was never retrieved` while the suite exits zero.

### 7.2 Required architecture

1. The long SQLite online backup must use a dedicated source connection, not
   `_conn`, and must run without holding `store._lock`. Hold `_lock` only long
   enough to validate the configured database path and establish any short
   transaction/capture metadata required. WAL writers and status reads must
   proceed during page copying.
2. Recorder rotation/path enumeration, filesystem stat walks, SQLite snapshot,
   hashing, and ZIP creation stay off the event loop.
3. Preserve an explicit capture boundary. The manifest must record at least
   `raw_checkpoint_ts`, `db_snapshot_started_ts`, `db_snapshot_finished_ts`,
   selected raw segment names/hashes, and the resulting uncertainty interval.
   Never imply simultaneity if the boundaries differ. The snapshot and
   normalized export tables must reconcile to the manifest's captured database
   counts/modes.
4. Make legacy `GET /api/export` call the same asynchronous full-job prepare
   path and return the 202 job descriptor plus scoped cookie and deprecation
   header. It must not build or snapshot inline. Do not leave a second export
   implementation.
5. Keep strong references to background tasks in an `_export_tasks` registry.
   A done callback must retrieve every exception, set the corresponding job to
   `error` with a stable code, record the backend fault, and remove the task.
   Shutdown cancels and `await asyncio.gather(..., return_exceptions=True)` on
   remaining tasks. No unobserved exception is permitted.
6. Preserve auth, path containment, range responses, leases, TTL cleanup,
   single-full-job behavior, progress, and cancellation.

### 7.3 Required concurrency tests

Create `tests/test_export_concurrency.py` using a real temporary WAL SQLite
database, not only mocked `sleep`:

- [ ] Slow the dedicated backup through SQLite's page/progress mechanism while
  concurrently calling `/api/status`, writing a signal, writing latency, and
  advancing a heartbeat/recorder probe. Assert every operation completes and
  status/scheduler maximum delay is below 250 ms in the test environment.
- [ ] Assert a write committed during the backup exists afterward and the
  captured snapshot/manifest follows the documented boundary (no silent lost
  row or false count).
- [ ] Call legacy `GET /api/export`; assert quick 202 response, job id, scoped
  cookie, deprecation header, and no inline snapshot/bundle call.
- [ ] Force failure before and during `_build_export_job`; job reaches `error`,
  the backend fault is visible, and a custom asyncio loop exception handler
  records zero unhandled task exceptions.
- [ ] Re-run cancellation, active lease, TTL, Range, wrong cookie, missing auth,
  traversal, and secret-exclusion tests against the single job flow.

## 8. Durable, bounded, gap-aware quote paths and working UI (`BR-PATH`)

### 8.1 Reproduced defects

- On a final trade flush failure, `_complete_realistic()`/`close()` immediately
  removes the position. The retained buffer has no owner and is never retried.
- Signal expiry/eviction calls `_flush_signal_path(self._signal_paths.popleft())`;
  a failed write leaves rows on an already-orphaned dictionary.
- `_record_exec_terminal()` increments beyond the 4,000-sample cap, while the
  read query limits to 4,000 and can omit the terminal sample.
- Summary and frontend filter null bids then connect numeric neighbors across a
  quote outage. `[90@0, null@1000, 70@2000]` is rendered as one line and reports
  misleading travel/efficiency; the 90c quote can be counted as held until
  2000 ms although availability ended at 1000 ms.
- `pathCache` stores with a string DOM id but reads with numeric `trade.id`, so
  clicking **Show path** can fetch successfully and still render no chart.

### 8.2 Persistence and ownership contract

Add nullable `sample_seq INTEGER`, `availability TEXT`, and `terminal INTEGER`
to `bid_path_samples` for backward compatibility. New capture rows must set
them. Add idempotent partial unique indexes so new rows are exactly-once by
`(trade_id,kind,sample_seq)` or `(signal_id,kind,sample_seq)`; legacy null
sequence rows remain untouched.

For a trade's final exit, persist these in one store transaction:

1. any remaining buffered path rows;
2. exactly one terminal row;
3. the complete path summary;
4. final fill/progress and closed-trade fields.

`record_paper_exit()` and the simple `close_trade()` path must both use that
transaction for final closure. If it rolls back, the trade stays open and the
position/pending exit remains owned for retry; do not broadcast close or pop it.
Sequence keys make a retry idempotent. On restart, a failed/uncommitted close is
restored as open and may retry.

For signal watches, peek before finalizing. Persist remaining rows plus summary
transactionally, mark a durable `forward_path_finalized` flag/timestamp on the
signal, and only then remove the watch. If the transaction fails, retain the
watch and expose a current `signal_path_persistence_failed` health fault. On
startup, rebuild/finalize unfinalized watches from durable signal/path state;
if an in-memory tail was lost in an abnormal process death, mark explicit
`path_incomplete_reason` rather than presenting a complete path.

### 8.3 Cap and availability contract

- The hard cap is 4,000 rows **including** gaps and the terminal row. Reserve
  one slot: at most 3,999 non-terminal rows may be recorded. `samples_total`
  never exceeds 4,000.
- A quote change writes `availability="quote"`. The first no-ladder observation
  after a quote writes one `availability="gap"`, `bid=null` row. Repeated
  no-ladder observations do not flood rows. A resumed quote starts a new
  segment. The same behavior applies to trade and signal-forward paths.
- The terminal row is `availability="terminal"`, `terminal=1`. Its executed
  price is not relabeled as an observed book quote. Keep `exec_px`/exit fields
  semantically distinct.
- If rows are dropped because the cap is reached, persist
  `truncated=true`, `dropped_samples`, and the capture-version metadata in the
  summary. Do not silently truncate.

`bid_path_summary()` must return:

`samples_total`, `samples_priced`, `segments`, `gap_count`, `gap_duration_ms`,
`unknown_gap_duration_ms`, `first_bid`, `last_bid`, `peak_bid`, `peak_dt_ms`,
`peak_bid_size`, `peak_exec_px`, `ms_at_peak`, `trough_bid`, `trough_dt_ms`,
`path_travelled_c`, `displacement_c`, `path_efficiency`, `span_ms`,
`truncated`, and `dropped_samples`.

Calculations may join only consecutive priced observations in the same segment.
Time from a priced observation to a following gap-start counts as known
availability; nothing beyond the gap-start does. For the fixture
`90@0, gap@1000, 70@2000`, require:

```json
{
  "segments": 2,
  "gap_count": 1,
  "gap_duration_ms": 1000,
  "ms_at_peak": 1000,
  "path_travelled_c": 0,
  "path_efficiency": null
}
```

The trade path endpoint returns the full bounded path (maximum 4,000 rows), its
summary, and truncation metadata. The list endpoints return summaries only and
must perform no per-row path query.

### 8.4 Frontend contract

- Normalize cache keys with `String(tradeId)` in `has`, `get`, and `set`.
- Split samples at every gap. Generate one SVG `M...` subpath per priced
  segment; never draw `L` across a gap. Show gap count/duration and truncation.
- The click must transition through loading, chart, or visible error state. A
  failed fetch cannot disappear silently.
- Use human labels while preserving ids/raw fields in expandable audit detail.
  Mobile 360 px layout must show full dates, match clocks, reason, high timing,
  and errors without ellipsis or horizontal page overflow.

### 8.5 Required tests

Extend `tests/test_bid_path.py` and frontend coverage:

- [ ] `test_final_trade_path_failure_keeps_position_then_retries_exactly_once`.
- [ ] `test_final_signal_path_failure_keeps_watch_then_retries_exactly_once`.
- [ ] `test_uncommitted_final_close_restores_open_trade_after_restart`.
- [ ] `test_path_cap_is_4000_including_one_terminal_row`.
- [ ] `test_gap_summary_never_bridges_unavailable_quotes` with the exact fixture
  and result above.
- [ ] `test_no_ladder_records_one_gap_and_resume_starts_new_segment` for both
  position and decline watches.
- [ ] `test_trade_and_signal_list_endpoints_do_not_query_paths_per_row`.
- [ ] Add a real DOM/headless-browser test: click **Show path** on a numeric
  trade id, wait for the request, and assert that the card now contains a
  visible SVG. Then load a gapped path and assert its `d` attribute contains at
  least two `M` commands and no cross-gap `L`. A source-string assertion is not
  acceptable.

## 9. Cross-cutting audit and frontend requirements (`BR-AUDIT`)

After the work packages above, verify the existing user-facing contract rather
than redesigning it:

- Two sleeves remain independently filterable and each has reconciled gross,
  fees, and net PnL plus a combined total.
- Every new signal/trade shows full UTC receipt/entry/exit/high times, persisted
  match minute/stoppage, human-readable game/contract, strategy, exact decision
  reason, gate outcome, and matched provider event or an explicit unmatched
  reason.
- Association is labeled temporal/state evidence, not proven causation.
  Provider occurrence, provider receipt, poll uncertainty, response latency,
  exchange signal, local signal, entry, high, and exit remain distinct.
- Canonical and human event labels are additive. Stored provider names, ids,
  fingerprints, and raw payloads are unchanged and downloadable.
- The System tab shows current connectivity/health and cumulative counters
  separately. It says `ALL SYSTEMS GOOD` only when every current required check
  is healthy; errors/disconnections/export/path faults remain visible until
  recovered and are retained in recent history.
- League performance remains present with combined, Gate A, and price-only
  splits, denominators, small-sample warning, and totals that reconcile.
- Audit and full downloads include the normalized tables, raw identifiers,
  event/clock lineage, mode, paths/gaps/terminal metadata, schema, manifest,
  hashes, and a backtest README/data dictionary. No credential or private key
  may appear.

Add API integration assertions for these fields. Use a rendered browser for UI
behavior; do not substitute string searches for clicks, filters, tab changes,
download progress/error, or responsive layout.

## 10. Local validation gate (`BR-LOCAL`)

Use dependencies from `requirements-dev.lock`. Run from a clean checkout of the
candidate head and save complete output, not a paraphrase:

```bash
python -X dev -W error::RuntimeWarning -m unittest discover -s tests -v
python -m compileall -q app tests
ruff check --select E9,F63,F7,F82 app tests
node --check static/app.js
git diff --check
```

If the browser test has a separate command, add it here and to CI. Run the full
suite at least twice: once on a fresh database and once on a migrated copy.
Install/startup/import warnings and `Task exception was never retrieved` fail
the gate even when unittest exits zero.

Extend `tests/test_spec_corrections.py` with a documentation-routing test that
asserts this handoff exists and is listed in `AGENTS.md` after
`SPEC_CORRECTIONS_AND_DEVIATIONS.md` but before
`PRODUCTION_INTEGRITY_IMPLEMENTATION_SPEC.md`. This one source/document check is
allowed only to protect instruction discovery; it is not evidence for any
runtime behavior.

For each work package, record:

| Evidence | Required content |
|---|---|
| Before | failing test name, reviewed SHA, failure excerpt |
| Change | commit SHA and exact files/functions/schema |
| After | targeted command/output and full-suite command/output |
| Mutation | removed invariant and test that caught it |
| Limits | remaining uncertainty; never relabel a failed requirement |
| Rollback | exact revert command and non-destructive data behavior |

## 11. Review-deployment and rendered acceptance (`BR-PROD`)

Do not deploy until the user authorizes the target. Deploy only the PR candidate
to the authorized Football-Bot review/production environment; never touch
`kalchi-kill`. Keep both sleeves paper-only.

Create `docs/evidence/pr12/<candidate-sha>/EVIDENCE_INDEX.md` with links/hashes
for the artifacts below. Sanitize secrets before committing. Attach large ZIPs
to the PR rather than Git. Required evidence:

1. GitHub candidate SHA/tree, CI run URL/status, deployment id/service/environment,
   configuration flags with secret values redacted, and rollback command.
2. Timestamped JSON snapshots from `/api/status`, `/api/config`, `/api/stats`,
   `/api/latency`, `/api/match-clocks`, `/api/signals`, `/api/trades`,
   `/api/provider-events`, `/api/goal-latency`, and `/api/export/*`.
3. An authorized audit bundle ready in under 10 seconds, valid ZIP, verified
   SHA-256 files, schema/data dictionary, per-mode counts, parent relationships,
   PnL/fill/path/event/clock reconciliation, and zero secrets.
4. A protected raw-segment Range request with request/response metadata and
   bytes/hash verification.
5. A real full export running concurrently with status reads, a signal/latency
   write, recorder progress, and heartbeat. Record p50/p95/max endpoint latency,
   scheduler lag, export duration/bytes, DB faults, and dropped observations.
6. Restart the service, then prove clocks/events/modes/paths remain, demo rows
   do not enter live aggregates, current health recovers, and collection
   continues.
7. Query accepted price-only signals/trades and prove 100% have a positive
   clock id resolving to a matching persisted 88+ row. Zero accepted rows may
   use expected expiration, UTC wall time, an id-less clock, or
   `sleeve_outside_window` as the match-minute decision.
8. Desktop and 360 px screenshots plus browser console/network logs for:
   overview with two sleeve PnLs; filtered trades; losing-trade high/path;
   clicked gapped path; signal clock/gate/reason; provider raw/revision/timing;
   league performance; waiting/healthy/fault/recovered System states; audit and
   full export progress/cancel/error. At 360 px, document scroll width must not
   exceed client width and no required text may be ellipsized.
9. Source/config/runtime proof that no live-order endpoint/call was added and
   the unrelated service was untouched.

If there are not yet enough live minute-88 candidates to prove a statistical
strategy claim, say so. Infrastructure correctness may pass; profitability and
configuration selection remain a later backtest decision.

## 12. Final reviewer stop/go checklist (`BR-REVIEW`)

The final reviewer must independently answer every line with evidence:

- [ ] Clock candidate is invisible until its database id exists.
- [ ] Insert failure retries; accepted stamps resolve exactly to SQLite.
- [ ] Reconfirmation uncertainty is non-negative and truthful.
- [ ] Pre-match waiting is healthy; live/candidate faults are red; recovery can
  return green without erasing cumulative evidence.
- [ ] Live/demo/legacy evidence is preserved and isolated across two restarts.
- [ ] Provider occurrence and correction lineage survive real payload shapes
  and restart without fabricated causality/time.
- [ ] Real SQLite backup contention stays below the required 250 ms test bound;
  legacy export cannot block; background exceptions are retrieved.
- [ ] Trade/signal paths survive failure, end exactly once, stay within 4,000,
  represent gaps, and render after an actual click.
- [ ] Two sleeve PnLs, league results, audit fields, health, errors, mobile
  layout, and downloads pass rendered checks.
- [ ] All exports reconcile and contain no secret.
- [ ] Full local, migration, CI, deployment, restart, and production evidence is
  present and reproducible.
- [ ] Gate A, paper-only status, raw naming, and `kalchi-kill` are unchanged.

Decision vocabulary is fixed:

- `APPROVED`: every line passes with linked evidence.
- `BLOCKED`: any line lacks proof or fails.
- `NOT OBSERVED`: allowed only for historical/provider facts that genuinely do
  not exist; it never converts a required implementation/verification item into
  a pass.

## 13. Implementer hand-back template

Use this exact structure in the PR comment and changelog:

```text
Candidate head/tree:
Commits by work package:
Baseline red-test artifact:
Targeted test results:
Full local/CI results:
Migration-from-production result:
Review deployment id and service:
Machine-readable evidence index:
Rendered evidence index:
Mode/table reconciliation:
Accepted clock-id reconciliation:
Export concurrency p50/p95/max:
Known limitations:
Exact rollback command:
Paper-only proof:
kalchi-kill untouched proof:
Requested final-review decision: APPROVED or BLOCKED
```

Do not write “done,” “fixed,” “all green,” or “ready to merge” without the
corresponding artifact named above.
