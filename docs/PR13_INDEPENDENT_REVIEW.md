# PR 13 Independent Review — BLOCKED

Reviewed target:

| Field | Value |
|---|---|
| Repository | `elmorshedy-del/Football-Bot` |
| Pull request | #13 |
| Reviewed head | `d69f5a4c512fddf55d30cb79079a69e0fa6a1651` |
| Base | `main` at `8b6a8a8e736f8eb59cec7383a51b38c857947816` |
| GitHub Actions | Run `33561327860`: success, but all three browser tests skipped |
| Reviewer decision | **BLOCKED** |

This review applies `docs/PR12_BLOCKER_RESOLUTION_HANDOFF.md` to the implementation stack opened as PR #13. It was performed from a clean detached checkout. No deployment, merge, strategy tuning, Gate A change, live-order path, or `kalchi-kill` change is authorized by this document.

The remaining work is a major implementation pass, not a small final patch. It spans transaction ownership and restart recovery, mode-scoped application programming interfaces and exports, behavioral browser coverage, continuous integration, and production evidence.

## 1. Independently reproduced failures

### 1.1 Final trade path is orphaned after a failed write

Forced failure: `app.paper.store.insert_bid_path -> OSError("disk full")` during a simple final close.

Observed result:

```json
{
  "db_trade": {
    "status": "closed",
    "exit_reason": "target"
  },
  "position_still_owned": false,
  "buffered_rows_left": 1,
  "errors": [
    ["bid_path", "disk full"]
  ]
}
```

The database closes the trade before the terminal/path transaction. `PaperDesk.close()` then removes the position even though the final path failed. The remaining row has no retry owner. The realistic path has the same split: `record_paper_exit()` commits the final fill and close before `_complete_realistic()` flushes the path.

Required implementation:

1. For realistic exit, simple close, and settlement, use one SQLite transaction containing:
   - any remaining buffered rows;
   - exactly one terminal row;
   - the complete path summary;
   - final fill/progress, when applicable;
   - final closed-trade fields.
2. On any failure, roll back all five parts, restore the shadow book where applicable, retain the position and pending exit, and do not broadcast/log a close.
3. Retry the same sequence keys. Pop the position and pending exit only after commit.
4. A failed/uncommitted close must restore as open after restart and remain eligible for retry.

### 1.2 Restored positions silently discard new path observations

Fixture: an open trade with durable path sequences 1 and 2, then process restart and one new 60c held-side quote.

Observed result:

```json
{
  "restored_exec_path_total_before_new": 0,
  "attempted_new_sample_seq": 1,
  "buffer_cleared": true,
  "durable_sample_seqs": [1, 2],
  "durable_bids": [51.0, 52.0]
}
```

`restore_open_positions()` does not restore durable path sequence/cap state. The first new sample reuses sequence 1. The partial unique index ignores it, `insert_bid_path()` returns the input length rather than proving the row is durable, and the buffer is cleared.

Required implementation:

1. Load durable `MAX(sample_seq)` and required cap/truncation state with each open position.
2. Resume at max + 1 and preserve the 4,000-row cap, including the terminal row, across restarts.
3. Make the insert result distinguish a known idempotent retry from an unexpected collision. Never clear a buffer merely because `executemany INSERT OR IGNORE` returned without raising.
4. Restore enough last-observation state to avoid silently relabeling or duplicating the first post-restart quote.

### 1.3 Failed signal finalization removes its owner

Forced failure: `app.engine.store.insert_bid_path -> OSError("disk full")` during expiration.

Observed result:

```json
{
  "watch_still_owned": false,
  "unpersisted_rows": 1,
  "durable_rows": 0,
  "errors": [
    ["signal_path", "disk full"]
  ]
}
```

Both expiry and max-tracked eviction call `popleft()` before final persistence. Additional defects in the same contract:

- decline rows omit `sample_seq`, so the new unique index cannot make retries exactly-once;
- decline watches skip no-ladder observations instead of recording one gap;
- rows and summary are separate commits;
- no durable `forward_path_finalized` flag/timestamp exists;
- no `path_incomplete_reason` exists;
- no startup rebuild exists;
- no current `signal_path_persistence_failed` fault remains latched until recovery.

Required implementation:

1. Add nullable, idempotent signal finalization/incompleteness columns.
2. Set sequence, availability, and finalization metadata on every new decline row.
3. Record one gap on the first no-ladder observation, suppress repeated gaps, and start a new segment when a quote resumes.
4. Peek the watch, then transactionally persist its remaining rows, summary, and durable finalized marker. Pop only after commit.
5. On failure, retain the watch and expose a current health fault until successful retry.
6. Rebuild unfinalized watches on startup from durable signal/path state. If an abnormal stop lost an in-memory tail, label the path incomplete instead of presenting it as complete.

### 1.4 Live APIs and exports mix evidence modes

API reproduction with active store mode `live`:

```json
{
  "active_mode": "live",
  "api_signal_modes": ["live", "demo"],
  "events": ["LIVE", "DEMO"]
}
```

Export reproduction from `build_study_bundle(mode="live", scope="audit")`:

```json
{
  "manifest_mode": "live",
  "manifest_signal_rows": 2,
  "exported_signal_modes": ["demo", "live"],
  "per_mode_counts_present": false
}
```

The included SQLite snapshot is also all-mode while the manifest labels the bundle live. Unscoped reads remain in signals, trades, equity, latency histograms, goal latency, clocks, provider events, paths, nested signal/trade lookup, and event-association lookup. Legacy null modes are not presented as `legacy_unknown`.

Required implementation:

1. Add one validated selector: `live`, `demo`, `legacy_unknown`, or `all`; default to active mode.
2. Apply it to every study endpoint and every nested query used to decorate the response.
3. Mode-scope path access through the parent signal/trade so a caller cannot fetch another mode by id.
4. Present null provenance as `legacy_unknown`.
5. Scope normal audit/full CSV, JSONL, and SQLite snapshot content to requested mode. Only an explicit archival all-mode request may include all modes.
6. Manifest must record requested modes and counts by mode for every applicable table.
7. Reconcile same-mode trades and fills with no orphan fill.

### 1.5 Browser acceptance is not an actual click test

`tests/test_frontend_path_browser.py` does not load the shipped dashboard. It extracts helper functions into synthetic HTML, injects samples with `page.evaluate()`, and calls `pathSparkline()` directly. It never:

- clicks **Show path**;
- performs or waits for the path application programming interface request;
- exercises loading or visible error state;
- renders the complete dashboard at 360px;
- tests filters, league analytics, health, or download interaction.

GitHub Actions run `33561327860` was green with:

```text
Ran 260 tests
OK (skipped=3)
```

All three skipped tests reported `chromium is not available`.

Required implementation:

1. Serve/load the actual application and shipped JavaScript.
2. Intercept or fixture the real path endpoint.
3. Click the real control and assert loading to visible scalable vector graphic, then an independent visible-error case.
4. For a gapped response, require multiple path `M` subpaths and no cross-gap line.
5. Run full rendered 360px assertions for dates, match clock, reason, high timing, errors, filters, league view, and downloads with no page overflow or truncation.
6. Provision Chromium in continuous integration and fail acceptance if the browser cannot run.

### 1.6 Current head fails its stated validation/evidence contract

Independent local result:

- 260 unit tests pass; 3 browser tests skip.
- `compileall`, Ruff fatal-error selection, and JavaScript syntax pass.
- `git diff --check main...d69f5a4` fails on trailing whitespace in:
  - `docs/evidence/pr12/9f831c6/local-validation-gate.txt`;
  - `docs/evidence/pr12/baseline-cd4d36e/baseline-red-tests.txt`.

Evidence/checklist/changelog cite nonexistent commit `4fbb79d`. The actual clock commit is:

```text
c9f490ac1b3f8a694c65df088f74fae6a7bc8d90 Fix persisted clock publication and current clock health
```

Therefore the documented rollback command is invalid. Evidence also labels PR #12/candidate `9f831c6`, while the implementation under review is PR #13 at `d69f5a4`. Continuous integration omits `node --check`, `git diff --check`, strict RuntimeWarning handling, and runnable browser setup.

## 2. Mandatory behavioral tests before hand-back

The implementer must add and pass all of these. Source-text assertions do not count as behavior.

### Transaction and restart

- `test_final_trade_path_failure_keeps_position_then_retries_exactly_once`
- `test_final_signal_path_failure_keeps_watch_then_retries_exactly_once`
- `test_uncommitted_final_close_restores_open_trade_after_restart`
- restored open path continues at durable max sequence + 1 and persists the first post-restart quote
- realistic exit, simple close, and settlement each roll back path/summary/fill/close as one unit
- retry produces one terminal row, one final fill set, one summary, and one closed transition

### Path behavior and query cost

- `test_path_cap_is_4000_including_one_terminal_row`
- `test_gap_summary_never_bridges_unavailable_quotes`
- position and decline watches each record one no-ladder gap and a resumed segment
- signal rows use non-null sequence values and retry without duplication
- `test_trade_and_signal_list_endpoints_do_not_query_paths_per_row` with runtime query counting, not source inspection

### Mode and export

- every study endpoint defaults to active mode and accepts all four safe selectors
- nested event/signal/trade decoration cannot cross modes
- legacy null rows are presented as `legacy_unknown`
- live export excludes demo/legacy from CSV, JSONL, and SQLite snapshot
- manifest per-mode counts reconcile with every exported table
- no same-mode fill is orphaned from its trade/signal

### Shipped frontend

- real click triggers a real/intercepted request and visible scalable vector graphic
- request failure remains visible
- gapped path has multiple subpaths and no cross-gap line
- real tabs/filters/league/health/download flows render at desktop and 360px
- 360px viewport has no horizontal page overflow and no required-field truncation

## 3. Final validation sequence

1. Run the complete section 10 gate twice: fresh database, then migrated production-schema fixture.
2. Continuous integration must run the same strict command set, including JavaScript, diff whitespace, and a non-skipped browser.
3. Correct PR number, head/tree, work-package SHAs, rollback command, CI URL, and evidence hashes.
4. Keep the PR draft.
5. Do not deploy until an authorized target and live `ADMIN_TOKEN` are supplied.
6. After deployment, collect every section 11 application programming interface, export, restart, latency, rendered desktop/mobile, paper-only, and untouched-service proof.
7. Return for independent review. The implementer must not self-approve.

## 4. Decision

**BLOCKED.** The current green unit/continuous-integration result is useful but does not satisfy `BR-01`, `BR-02`, `BR-03`, `BR-05`, `BR-06`, or `BR-07`. No merge or deployment is approved.

## 5. Follow-up independent review at `59010d2` — still BLOCKED

This section supersedes the implementation-status conclusions above, but not the
requirements. It reviews the implementer's remediation of sections 1.1–1.6.

| Field | Value |
|---|---|
| Reviewed head | `59010d28a0c7dbb1d8110c29c90a915cc3a61e52` |
| Reviewed tree | `670e833bfbf546774a396a7f69cb692020ba28e0` |
| Base | `main` at `8b6a8a8e736f8eb59cec7383a51b38c857947816` |
| Current CI | Run `33577130477`, job `100083455260`, success |
| PR state | Draft, open, unmerged |
| Reviewer decision | **BLOCKED** |

The current CI result is genuine: Chromium launched, all 298 tests ran, no
browser test skipped, and compile, Ruff, JavaScript, and diff-whitespace checks
passed. Two independent local strict runs passed the 292 non-browser tests; the
browser class skipped locally only because this review host could not download
Chromium. The current branch also fixes the original forced-write rollback cases
for ordinary simple close, realistic final exit, settlement, and signal expiry.

Those passes do not close `BR-PATH`, `BR-MODE`, `BR-AUDIT`, or `BR-LOCAL`. The
suite misses the behavioral failures below, and the checked-in evidence does not
reconcile to the current candidate.

### 5.1 A sequence-key collision still discards evidence and falsely closes/finalizes

`store._persist_path_in_transaction()` uses `INSERT OR IGNORE` and never verifies
that an ignored row is byte-for-byte the already-durable retry. Both final-close
paths then commit and release their owner. The incremental trade and signal
flushers report a short write but still slice the entire buffer and mark it
successful.

Independent trade reproduction: durable sequence 1 is `51c`; the buffer contains
a different `99c` observation at sequence 1, followed by close.

```json
{
  "buffer_rows": 0,
  "close_result": true,
  "durable_path": [
    {"bid": 51.0, "sample_seq": 1, "terminal": 0},
    {"bid": 60.0, "sample_seq": 2, "terminal": 1}
  ],
  "errors": [],
  "position_owned": false,
  "trade_status": "closed"
}
```

Independent signal reproduction: the same conflicting sequence is silently
ignored, the watch is popped, the signal is marked finalized, and no health
fault remains.

```json
{
  "buffer_rows": 0,
  "durable_rows": [{"bid": 51.0, "sample_seq": 1}],
  "errors": [],
  "fault": null,
  "watch_owned": false
}
```

Required implementation:

1. Centralize strict path insertion for incremental flushes and final
   transactions. An ignored key is an idempotent retry only when every persisted
   field matches: owner ids, kind, event, market, side, strategy, anchor, offset,
   bid/depth/executable price/quantity, mode, sequence, availability, and
   terminal flag.
2. A same-key/different-payload row raises a stable sequence-conflict error. In
   a final transaction it rolls back rows, summary, fill/progress, and close. In
   every caller it leaves the buffer and owner intact and raises a current health
   fault.
3. Clear a buffer only after every row was newly inserted or proved to be an
   exact durable retry. Logging a collision and then clearing is forbidden.

Required behavioral tests:

- `test_conflicting_trade_sequence_rolls_back_close_and_keeps_position`
- `test_conflicting_signal_sequence_keeps_watch_unfinalized`
- `test_incremental_trade_collision_keeps_buffer`
- `test_incremental_signal_collision_keeps_buffer`
- `test_identical_trade_and_signal_retries_are_idempotent`

The existing `test_an_ignored_collision_does_not_clear_the_buffer` does not test
its name: it calls `store.insert_bid_path()` directly and never asserts that a
desk/watch buffer remains owned.

### 5.2 Restart recovery misses watches with zero durable rows and drops failed rebuilds

Every signal starts an in-memory forward watch, but
`store.unfinalized_signal_paths()` inner-joins `bid_path_samples`. A process that
dies before the first quote row reaches SQLite is therefore invisible at
restart:

```json
{
  "watch_existed_before_crash": 1,
  "durable_rebuild_candidates": [],
  "marker_after_restart_scan": {
    "forward_path_finalized": null,
    "path_incomplete_reason": null
  }
}
```

When a watch does have durable rows, a failed `rebuild_signal_paths()` call
creates only a local dictionary. On failure that dictionary is discarded rather
than retained for retry:

```json
{
  "fault": "signal_path_persistence_failed",
  "rebuilt": 0,
  "retry_owner_count": 0,
  "still_unfinalized": [1]
}
```

Required implementation:

1. Add an additive nullable `forward_path_started_ts` marker and write it in the
   existing signal insert transaction when forward collection is enabled. Do
   not add a second hot-path commit; legacy rows remain null.
2. Rebuild from `started IS NOT NULL AND finalized IS NULL`, including signals
   with zero durable samples. Mark the lost tail explicitly.
3. A failed startup finalization must be placed in the engine's owned retry
   queue. The fault stays latched until all failed owners commit; success for a
   different watch must not clear it.
4. Run rebuild independently of `PAPER_EXECUTION_V2`; signal collection itself
   is not conditional on realistic paper execution.

Required behavioral tests:

- `test_restart_marks_zero_row_watch_incomplete`
- `test_failed_startup_rebuild_retains_retry_owner_then_recovers`
- `test_one_success_cannot_clear_another_failed_watch_fault`
- `test_signal_rebuild_runs_when_paper_execution_v2_is_disabled`

### 5.3 Terminal rows are still treated as executable quotes, and restart can exceed the cap

`PaperDesk._record_exec_terminal()` stores the executed exit price in `bid`.
`store._path_segments()` treats every numeric `bid` as an observed quote, so a
settlement at 100c can become the recorded path peak, alter travel/efficiency,
and inflate `samples_priced` even though no 100c bid existed. Setting only
`bid_size` and `exec_px` to null does not satisfy the contract that execution is
not relabeled as a book quote.

The restart cap also has no final guard. Starting with 4,000 durable
non-terminal rows, restore and close produced:

```json
{
  "bounded_has_terminal": false,
  "bounded_rows": 4000,
  "close_result": true,
  "durable": {"max_seq": 4001, "n": 4001, "terminals": 1}
}
```

Required implementation:

1. New terminal rows have `availability="terminal"`, `terminal=1`, and
   `bid/bid_size/exec_px=null`. The trade's exit price remains on `trades`.
2. Summary and frontend segmentation treat a terminal as the end time of the
   current availability segment, never as a priced observation or a gap.
   Terminal rows do not affect peak, trough, travel, displacement, efficiency,
   or `samples_priced`.
3. Enforce the 4,000-row invariant inside the final transaction using durable
   state, not only the in-memory counter. An impossible pre-existing exhausted
   path must fail closed with an explicit fault/incompleteness reason; it may not
   write row 4,001 and then hide the terminal behind a read limit.

Required behavioral tests:

- `test_settlement_terminal_cannot_become_executable_peak`
- `test_terminal_closes_peak_duration_but_is_not_priced`
- `test_restart_at_3999_rows_closes_with_exactly_4000_including_terminal`
- `test_exhausted_legacy_path_never_writes_row_4001_or_releases_owner`

### 5.4 Mode scoping is incomplete for open trades, path links, and archival download

`/api/trades?mode=demo` always constructs `open` from the active engine's
in-memory positions. An independently reproduced demo response contained the
active live position:

```json
[{"event":"LIVE","id":99,"signal_id":2,"strategy":"gate_a"}]
```

Also, signal/trade list responses generate path URLs without the selected mode.
A consumer of `?mode=demo` follows the supplied URL and silently falls back to
the active live mode, normally receiving 404. Finally, `all_modes=True` exists
only as a direct Python exporter argument: `/api/export/prepare` and the frontend
offer no explicit archival all-mode product, so the preserved demo/legacy data
is not actually downloadable from the dashboard.

Required implementation:

1. Build open-trade output from the selected database rows. Merge live
   in-memory marks only into matching parent ids in the active mode; never use
   the active engine list as the selector's source of truth.
2. Preserve the validated selector in every returned path URL.
3. Expose one admin-protected explicit archival all-mode export option through
   prepare, job state, manifest, and frontend. Default audit/full behavior stays
   active-mode only. Validate the same four selectors and never accept arbitrary
   SQL-like values.

Required behavioral tests:

- `test_demo_trade_endpoint_never_returns_live_open_position`
- `test_demo_open_trade_is_returned_from_demo_storage`
- `test_mode_scoped_path_urls_are_followable_without_mode_drift`
- `test_frontend_all_mode_archive_reconciles_and_is_labelled`

### 5.5 The required runtime N+1 regression is still a source-text assertion

The handoff explicitly required runtime query counting. Instead,
`test_list_endpoints_do_not_embed_full_paths` reads `app/main.py` as text and
searches for function names. Rename/replace it with the specified behavioral
test: seed at least 25 signals and trades, execute both list endpoints, count
real SQLite queries, assert query count is constant with row count, and assert
that no full path-sample query occurs. Static source inspection is not proof.

Required test name:

- `test_trade_and_signal_list_endpoints_do_not_query_paths_per_row`

### 5.6 Browser acceptance is real but materially narrower than the contract

The six Playwright tests do load the shipped page and the path click cases are
valid. The rest does not prove the claimed full acceptance:

- mobile content asserts only `2026`, `90+5`, and `Arsenal`; it does not assert
  reason, high timing, visible errors, signal fields, league values, or download
  states;
- the signal fixture is empty and the league fixture has no league results;
- only the free-text filter is exercised;
- the download test checks `is_visible()` and never clicks prepare, observes
  queued/preparing/progress/ready, follows the native download, cancels, or
  renders a server/network error;
- health waiting/fault/recovered states are never rendered or asserted.

Extend the real-browser suite with non-empty signal, league, latency, and health
fixtures and actual interaction tests for all filters/reset, health
fault/recovery, audit/full/all-mode prepare and progress, cancel, native
download request, and visible error. Repeat the required content and error
assertions at 360 px and retain the overflow/clipping checks.

### 5.7 Checked-in evidence does not reconcile to the candidate

`docs/evidence/pr13/0cbf651/EVIDENCE_INDEX.md` names candidate `0cbf651` and CI
run `33576969178` at `3f1f8e3`, while the reviewed head is `59010d2` and current
run is `33577130477`. Its claimed validation hash is
`f23dc040...`, but the committed file hashes to `7bc6b04d...`. That validation
file also contains a failing `git diff --check` excerpt while the index describes
the gate as clean. Regenerate the artifact and index only after the final code
head is fixed and CI is green; record the exact candidate head/tree, current
run/job, exact hashes, and commands without contradictory stale output.

### 5.8 One-round hand-back gate

Before returning again, the implementer must:

1. Add every named behavioral test in sections 5.1–5.6 and show each fails on
   `59010d2` for the reproduced reason.
2. Implement the contracts without strategy tuning, Gate A changes, live-order
   code, synchronous per-quote commits, historical rewriting, or deployment.
3. Run the strict suite twice, mandatory Chromium acceptance, migration twice,
   compile, Ruff, JavaScript, and both diff checks from a clean checkout.
4. Regenerate self-consistent evidence at the final candidate and leave PR #13
   draft.
5. Request independent review. Do not mark `BR-PROD` or `BR-REVIEW` passed.

This is a multi-file data-integrity, API, rendered-acceptance, and evidence pass,
not a safe small reviewer patch. After it passes code review, overall merge
approval will still remain blocked until the owner authorizes the section 11
deployment and production proof. No deployment is authorized by this review.
