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
