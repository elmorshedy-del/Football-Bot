# Engineering Change Log

Reverse-chronological. Newest day first, newest change first within a day.

The required entry format, and the rule that every code change appends here, is
in [`AGENTS.md`](../AGENTS.md#engineering-change-log). Do not rewrite past
entries; correct them with a dated follow-up entry instead.

---

## 2026-09-05

**Branch:** `claude/football-bot-analysis-rxz1vz`
**Base commit:** `c398635` (merge of PR #18)
**Commits:** `6cffcf8`, `c59010b`, `cddbaee`, plus the documentation commit that adds
this section.
**Diff totals:** 22 files, +1,952 / -119 (this change-log section itself
excluded): `app/` +886 / -111 across 10 files, `tests/` +1,022 / -7 across
9 files, `.env.example` +8, `README.md` +36 / -1.
**Suite:** 394 tests OK before (32.0 s), 425 tests OK after (40.1 s), under
`python -X dev -W error::RuntimeWarning -m unittest discover -s tests`.
**Deployment status:** NOT DEPLOYED. Nothing here has run in production; every
number quoted as production evidence was measured on the deployed 2026-09-04/05
build, not on this one.

This is the platform pass of the 2026-09-05 data-capture plan (plan items B1,
B2, B8a, B8b, B8d, B8e). B3-B7 — signal/entry/exit context, path thinning,
market results and score-observer correctness — are a separate later pass and
are deliberately untouched here.

**Configuration identity.** No strategy parameter changed: `strategy_params()`
is byte-identical before and after. But `engine.py`, `detector.py` and
`paper.py` are strategy sources, so `config.CODE_FINGERPRINT` moves
(`5d1849550f08` -> `ab5b7c51459b` in this environment) and with it `config_id`
(`5401a4a5ddd85724` -> `f6b3ffc7be33c1b9`). Rows written after this deploys
therefore carry a new `config_id` and will not pool with earlier rows in a
current-configuration aggregate. That is the intended behaviour of the
provenance stamp and not a defect, but it is the reason the fingerprint moved
without any decision changing.

**Whole-feed regression check.** The two real production raw segments
(`feed-20260904-20/21`, 1,897,688 frames, 114,937 trades, 54 tickers in 18
events) were replayed through `Engine.handle_ws` on the unmodified tree and on
this one. The signal funnel is identical on both: 4 `filled`, 17
`rejected_cap`, 8 `rejected_floor`, 1 `strategy_lockout`, 48 `unconfirmed`, 287
`subthreshold`, and 4 closed trades. That is the strongest available evidence
that Gate A detection, confirmation, sizing, entry, exit, fee, lockout and
settlement are unchanged.

Throughput on that same replay: **17,763 frames/s** (56.3 us/frame) before,
**19,416-19,987 frames/s** (50.0-51.5 us/frame) after, across two runs of the
identical harness. Read the per-type split rather than the headline, because
the two sides are not doing the same amount of work: `trade` frames fell from
374.6 us to 65.0-67.3 us (n=114,937) while `orderbook_delta` frames ROSE from
35.7 us to 49.0-50.4 us (n=1,782,137). That regression is not slower book
handling: it is 220,000 signal forward-path rows the old code never made
durable at all (16,000 rows before, 236,000 after -- see CHG-2026-09-05-004),
written inline here because the harness is synchronous and has no event loop
for `asyncio.to_thread` to dispatch to. In production those rows go to a worker
thread and the loop does not pay for them, so the measured in-process gain is a
conservative lower bound on the relief the event loop actually gets.

### CHG-2026-09-05-006 — Bound the detector's per-trade scan to its own windows

**Commit:** `c59010b`
**Components:** `app/detector.py`, `tests/test_detector_scan.py`

**Observed / original behaviour.** `Detector.on_trade` filtered the entire
300-second trade deque twice on every trade: once for the 150 ms burst window
and once for the 2,100-2,150 ms reference window. Measured on 2026-09-04, this
cost 33 us/trade in a quiet market and 813 us/trade with a 9,000-trade deque —
the state of exactly the hot late-game markets this strategy exists to trade.
Replaying the bundled real tape (32,149 trades across the three legs of
Espanyol vs Real Madrid, deepest deque 3,581) costs 7.330 s, 228.0 us/trade.

**Root cause.** Design gap, not a defect. Both windows are suffixes of a deque
that is, in practice, ordered by exchange timestamp, but they were read with a
full-deque comprehension, so the per-trade cost grew with retention rather than
with window size.

**Why necessary.** At the measured 2026-09-04 peak of 64.7k frames/min
(1,078/s, 20:47-21:05, three hot matches) the event loop could not answer
WebSocket pings; the Kalshi socket dropped 8 times in 15 minutes with
`ConnectionClosedError` and 18 sequence gaps occurred, several of 30-120
frames. CPU was 0.06 vCPU average and 0.51 vCPU peak against a limit of 8, so
the loop was blocked, not saturated. Trade handling was the largest single
in-loop cost.

**Exact change.** `_burst_window` and `_reference_window` walk `st.trades` in
reverse and stop at the window edge. `MarketState.ordered` tracks whether every
appended trade has had a `ts_ms` at least as large as its predecessor; a single
out-of-order print permanently reverts that market to the original exhaustive
filter, so the bounded read is equivalent by construction rather than by
assumption. `on_trade` also gained an opaque `context` argument (see
CHG-2026-09-05-001); the detector never reads it.

**Before / after.** Same bundled tape, same process: 7.330 s / 228.0 us per
trade before, 0.555 s / 17.3 us per trade after — 13.2x. On the two production
raw segments, mean `Engine.handle_ws` time for a `trade` frame fell from
374.6 us to 65.0 us (n=114,937).

**Reasoning and trade-offs.** The plan proposed a second deque holding the
reference window. A reverse walk was chosen instead because it needs no
additional state to keep consistent with `evict()`, and because the fallback
flag makes non-equivalence impossible rather than merely unlikely. The option
not taken was to trust ordering unconditionally: a single out-of-order print
would then silently change a threshold decision, which §2 forbids.

**Validation.** `tests/test_detector_scan.py` replays the bundled real tape
(32,149 trades) through a verbatim copy of the previous implementation and
through the current one and requires identical candidate sequences, identical
`big_bursts` contents after every single trade, and an identical near-miss
inventory. A second case shuffles adjacent trades to force the fallback and
requires the same identity, and a third asserts the fallback latches. Full
suite 425 tests OK.

**Risks / limitations.** The fallback is per market and sticky for the life of
the process, so one out-of-order print costs that market the optimisation until
restart. This does not reduce the number of trades retained, so memory is
unchanged; only the scan is bounded.

**Follow-up.** None.

### CHG-2026-09-05-005 — Batch the paper trade-high write instead of committing per quote

**Commit:** `cddbaee`
**Components:** `app/paper.py`, `tests/test_trade_highs.py`,
`tests/test_bid_path.py`

**Observed / original behaviour.** `PaperDesk._observe_executable_high` called
`store.update_trade_high` — a SELECT plus an UPDATE plus a COMMIT under
`store._lock` — on every new executable high of every open position, from
inside `on_book`, on the event loop, inside the WebSocket handler. On a rising
market that is one fsync per quote change.

**Root cause.** Defect of the same class as CHG-2026-09-05-003 and -004, and a
direct violation of the constraint in `docs/SPEC_CORRECTIONS_AND_DEVIATIONS.md`
**H2** ("collection must never add a synchronous commit to the hot path"),
which the path samples honour and this write did not.

**Why necessary.** It is one of the writes that made the loop unable to drain
the socket at the measured 2026-09-04 peak, and it is on the busiest possible
trigger: a new high of an open position during a post-goal repricing.

**Exact change.** The high is now authoritative in memory
(`pos.max_executable_bid` / `_ts` / `mfe_c`) and marked `pos.high_dirty`. It is
persisted by `_persist_trade_high` from exactly three places: the next
`_flush_exec_path` (every `BID_PATH_FLUSH_EVERY` = 250 path rows), the
close/settle paths immediately before their closing transaction, and at most
once per `TRADE_HIGH_PERSIST_S` = 5 s per position from `check_timeouts` so an
open position's API-visible column has bounded staleness. A failed write leaves
the row dirty, and the retry timestamp is stamped on the attempt so a failing
write retries on the same bounded cadence rather than on every tick.

**Before / after.** Four rising quotes on an open position: four
SELECT+UPDATE+COMMIT round trips before, zero before the next flush and one
afterwards now. The stored value is the same: strictly-greater-only,
held-side-executable-bid only, equal high keeps the first timestamp, settlement
cannot update the high. `store.update_trade_high` keeps its own
strictly-greater guard, so a replayed high is a no-op.

**Reasoning and trade-offs.** The rejected alternative was to keep writing
inline but only every Nth high, which would have made *which* high is durable
depend on quote arrival pattern. Deferring the write while keeping the value in
memory loses no information, because the persisted path rows already carry
every quote and the closing transaction carries the final high.

**Risks / limitations.** Between flushes the `trades.max_executable_bid` column
lags the in-memory high by up to 5 s for an open position; the API and UI read
that column, so a live trade card can show a high up to 5 s old. A hard process
kill inside that window loses at most that interval of high, and
`restore_open_positions` then restarts from the last persisted value — which is
what it did before for any high whose write had failed. Closed rows are exact.

**Follow-up.** None.

### CHG-2026-09-05-004 — Move signal forward-path persistence off the event loop

**Commit:** `c59010b`
**Components:** `app/engine.py`, `tests/test_loop_write_offload.py`

**Observed / original behaviour.** `Engine._finalize_signal_path` inserted up
to `BID_PATH_MAX_SAMPLES` = 4,000 `bid_path_samples` rows plus a summary plus a
commit, synchronously, on the event loop, reached from `on_book` ->
`_expire_signal_paths` -> `_release_finalized` inside the WebSocket handler.
The incremental `_flush_signal_path` already existed and had **no caller at
all**, so nothing was written until the 300 s window expired. Replaying the two
production raw segments left 78 open watches with 0 durable decline rows
between them.

**Root cause.** Defect: the incremental flush was written for exactly this
purpose and never wired up, leaving the whole window to land in one synchronous
transaction on the feed path.

**Why necessary.** Per `docs/.../H2`, a collection feature must not put a
commit on the hot path. A 4,000-row insert there stalls the socket for the
duration; at the measured peak this is one of the reasons local receipt lagged
the exchange `ts_ms` by 5-28 s p50 per minute (max 33 s).

**Exact change.** `_record_signal_paths` calls `_flush_signal_path(watch)` once
a watch has accumulated `paper.BID_PATH_FLUSH_EVERY` = 250 unflushed rows. Both
the incremental flush and `_finalize_signal_path` now run their SQLite work
through `asyncio.to_thread` when an event loop is running, via
`_dispatch_path_write`. The ownership contract is preserved exactly: the watch
stays owned while the write is in flight (`watch["in_flight"]`), the completion
callback runs on the loop thread and is the single place a watch is removed, a
failure keeps the rows and latches `signal_path_persistence_failed` against
that specific owner, and `_expire_signal_paths` / `_evict_signal_paths` return
early on an in-flight watch so no watch is ever finalized twice. Rows appended
while a flush is in flight are preserved by slicing rather than clearing the
buffer, and `_record_signal_paths` skips a watch whose finalization is in
flight. With no running loop — the synchronous replay harness, and the existing
ownership tests — the write happens inline exactly as before.
`rebuild_signal_paths` deliberately keeps the inline write (`sync=True`): it
runs once at startup, before the feed, and must report how many watches it
actually resolved.

**Before / after.** Same 1.9 M-frame replay: 16,000 durable `bid_path_samples`
rows before (trade paths only; every signal watch lost its buffer), 236,000
after, with at most 250 samples at risk per watch instead of the whole window.
A finalization that used to run on the loop thread now provably does not.

**Reasoning and trade-offs.** A dedicated writer thread with a queue for all
`store.ex()` traffic (`docs/.../H3`) would be the general fix and is explicitly
out of scope for this pass; `asyncio.to_thread` per write keeps the change
inside the ownership contract the existing tests pin. The option not taken was
to drop the watch on dispatch and reconcile later, which would have
reintroduced exactly the lost-owner defect the ownership tests exist to
prevent.

**Validation.** `tests/test_loop_write_offload.py` patches
`store.finalize_signal_path` to record `threading.get_ident()` and asserts the
write ran on a different thread from the loop that triggered it, that the watch
stays owned and in flight until the future resolves, that five overlapping
expiry/eviction passes produce exactly one finalization, that a failed
off-loop write keeps the watch and latches the fault and that the same owner
then recovers, and that a 260-sample watch has 250 rows durable and 10
buffered. All eight existing tests in `tests/test_signal_path_ownership.py`
still pass unchanged.

**Risks / limitations.** Under a running loop the durability of a watch is now
asynchronous, so a crash between dispatch and completion loses that batch — the
same exposure the buffer always had, moved a few milliseconds later. The
in-flight flag is per watch and is cleared on the loop thread, so it cannot
leak; but a permanently failing write now keeps a watch owned indefinitely,
which is the intended fail-closed behaviour and is visible as
`signal_path_persistence_failed` in `/api/status`.

**Follow-up.** The writer-thread refactor for all `store.ex()` traffic (H3)
remains open and is recorded in the plan as the next platform PR.

### CHG-2026-09-05-003 — Stop the per-event fsyncs on the feed-lag sampler

**Commit:** `c59010b`
**Components:** `app/engine.py`, `app/store.py`, `tests/test_feed_arrival.py`

**Observed / original behaviour.** `Engine.handle_ws` called
`store.add_latency("feed_lag", lag)` on every 20th trade — a synchronous INSERT
plus COMMIT on the event loop. At the measured 2026-09-04 peak of 1,078
frames/s this is 4-5 fsyncs per second on the WebSocket path, for a metric that
is only ever read as a percentile.

**Root cause.** Defect: a sampling rate expressed per event rather than per
unit of time, so the write rate scales with exactly the load that makes writing
expensive.

**Why necessary.** Same class as -004 and -005: these are the writes that stop
the loop draining the socket, which is what makes every local timestamp wrong
during a burst.

**Exact change.** `handle_ws` appends to the existing in-memory `feed_lag` ring
and to a per-tick list. `Engine._flush_feed_latency`, called from the 5 s stats
tick in `periodic_task`, writes exactly one `feed_lag` sample (the p50 of the
interval) and one `backlog_frames` sample (the deepest arrival queue seen in
the interval). `backlog_frames` was added to `store.LATENCY_KIND_ALIASES` as
its own canonical kind. The per-signal `feed_lag_ms` in `signals.context` (see
CHG-2026-09-05-001) is the row-level evidence that replaces the old sampled
series.

**Before / after.** 60 trade frames: 3 commits before, 0 during the frames and
2 on the next tick now — and the tick's rate is fixed at 2 writes per 5 s
regardless of load, instead of 1 per 20 trades.

**Reasoning and trade-offs.** The p50 of the interval was chosen over the mean
because the distribution is heavily skewed by reconnects; `backlog_frames`
records the max rather than the median because the question the series answers
is "how far behind did it get", not "how far behind was it typically".

**Validation.** `tests/test_feed_arrival.py` feeds 60 trade frames with
distinct arrival stamps and asserts `store.add_latency` is not called at all
during them, that the tick then writes exactly `feed_lag` and `backlog_frames`,
that the lag is computed from the ARRIVAL stamp (not the processing stamp), and
that `backlog_frames` carries the interval maximum.

**Checked and NOT changed.** `store.add_latency("match_clock_age_ms", age)` in
`Engine.record_signal` is still one commit per signal on the loop. It is left
as it is on purpose: it is per signal, not per frame, so its rate is bounded by
the signal rate (about 365 rows in the whole 1.9 M-frame replay) rather than by
feed volume, and it is the only evidence of clock freshness at the moment a
decision was made. It is recorded here so the next person does not
re-investigate it.

**Risks / limitations.** The `feed_ingress_ms` latency series is now one sample
per 5 s instead of one per 20 trades, so its `n` grows far more slowly and
`latency_kind_summary` will take longer to leave `COLLECTING` for that kind.
That is acceptable because the per-signal `feed_lag_ms` is strictly better
evidence, but any analysis that counted `feed_ingress_ms` rows as a proxy for
trade volume will now be wrong.

**Follow-up.** None.

### CHG-2026-09-05-002 — Stop rewriting every already-seen provider event on every poll

**Commit:** `cddbaee`
**Components:** `app/goal_latency.py`, `app/store.py`, `app/config.py`,
`.env.example`, `tests/test_loop_write_offload.py`

**Observed / original behaviour.** `GoalLatencyObserver._record_provider_events`
called `store.upsert_provider_event` for **every already-seen significant-event
fingerprint on every poll**, at a target poll period of 250 ms. Each call is a
SELECT, an UPDATE and a COMMIT under `store._lock`, dispatched from a worker
thread via `asyncio.to_thread`; with five mapped matches carrying dozens of
events each, that is O(events) fsyncs per poll and O(events) acquisitions of
the writer lock that every event-loop `store.ex()` and `store.q()` then blocks
on. The measured effect: the poll period, reconstructed from
`match_clock_observations.observed_ts - previous_poll_ts`, was 5.7 s p50 with a
30 s maximum against a 250 ms target.

**Root cause.** Defect. A repeat sighting of a known event carries no new
information except *when it was last seen*, but it was written with the same
full read-modify-write as a first sighting.

**Why necessary.** The 88+ clock gate needs a fresh persisted clock stamp; the
sleeve's dominant refusal in production is `clock_stale` (46 of 209
evaluations), and a poll running at 5.7 s instead of 250 ms is a large part of
why. It also starves the loop, which is what corrupts the local timestamps.

**Exact change.** An already-seen fingerprint now updates an in-memory
`pending_refreshes[(event, fingerprint)]` entry holding `observed_ts`,
`poll_started_ts`, `previous_poll_ts` and `response_ms`. New fingerprints still
insert immediately, because the insert carries the observation itself. The
buffer is written by `_flush_provider_refreshes` through one new store
function, `store.refresh_provider_events(rows)` — a single transaction with one
`executemany` UPDATE matched on `(event, fingerprint, COALESCE(mode,...))`, the
same identity as the unique index, so a demo observation can never refresh a
live one. The flush runs from the observer's own loop every
`PROVIDER_EVENT_FLUSH_S` (new knob, default 60 s, documented in `.env.example`,
deliberately NOT in `STRATEGY_PARAM_NAMES` because it cannot change a decision)
and is forced when an event is dropped in `_resolve_new_events`, so leaving the
watch window never loses buffered observation times. A failed flush keeps the
buffer and reports through `observer.last_error`.

**Before / after.** One known event over 40 consecutive polls: 40
SELECT+UPDATE+COMMIT round trips before, 0 during the polls and 1 batched
UPDATE at the flush now. The stored row is the same: `first_observed_ts` and
the raw payload are untouched, `last_observed_ts` carries the newest sighting.

**Reasoning and trade-offs.** `store.upsert_provider_event` is deliberately
unchanged, with its original semantics, because
`tests/test_provider_event_audit.py::test_duplicate_refresh_preserves_original_occurrence`
and `tests/test_evidence_modes.py` pin them and it is still the single-row
path. The rejected alternative was to drop the refresh entirely: `last_seen`
is the only evidence of how long the provider kept advertising an event, which
matters for the VAR/correction cases (trade 93 was a disallowal the feed
recorded 2.7 minutes later).

**Before / after on freshness.** Worst case, `last_observed_ts` is now up to
`PROVIDER_EVENT_FLUSH_S` (60 s) behind the last actual sighting, against a
previous lag of one poll period. That is the deliberate trade: the field is an
analysis-time "still being advertised at" marker, not a decision input.

**Validation.** `tests/test_loop_write_offload.py` asserts that 40 repeated
polls call `store.upsert_provider_event` zero times and leave the stored
`last_observed_ts` at its original value, that the flush then persists the
newest `observed_ts` and `poll_started_ts` while `first_observed_ts` does not
move, that the interval is respected and a dropped event forces a flush, that a
failed flush keeps the buffer for the next attempt, and that a live refresh
cannot reach a demo row with the same fingerprint. The existing provider audit
and evidence-mode tests pass unchanged.

**Risks / limitations.** A hard process kill loses up to 60 s of
`last_observed_ts` updates for events already recorded. Nothing else is lost:
the first sighting, the raw payload and the canonical fields were all written
at insert time.

**Follow-up.** None.

### CHG-2026-09-05-001 — Stamp frame arrival, measure the backlog, and record feed health

**Commit:** `c59010b` (`app/store.py`, `app/exporter.py`, `app/main.py` in `6cffcf8`)
**Components:** `app/kalshi.py`, `app/recorder.py`, `app/engine.py`,
`app/detector.py`, `app/store.py`, `app/exporter.py`, `app/main.py`,
`tests/test_feed_arrival.py`, `tests/test_production_migration.py`,
`tests/test_evidence_modes.py`, `tests/test_exporter.py`,
`tests/test_mode_scoped_api.py`

**Observed / original behaviour.** `KalshiWS.run` received and processed each
frame in one `async for` body, so the "local receipt" stamps (`lt`/`lm` in the
raw segments, `local_ts` on every signal) were taken when the consumer got
round to the frame, not when it arrived. During a backlog they are wrong by
seconds and **nothing recorded that a backlog existed**. On 2026-09-04
20:47-21:05 (three hot matches, peak 64.7k frames/min = 1,078/s) the local
receipt lagged the exchange `ts_ms` by 5-28 s p50 per minute, maximum 33 s; the
Kalshi socket dropped 8 times in 15 minutes with `ConnectionClosedError`
(keepalive timeout — the loop could not answer pings); 18 sequence gaps
occurred, several of 30-120 frames; and order-arrival latency on trades 91-95
was 2.4, 8.7, 16.1, 10.9 and 26.2 s. None of those disconnects, gaps or
recoveries left a queryable record, so a hole in the study could not be
distinguished from a quiet market.

**Root cause.** Design gap. One coroutine did receipt and processing, so there
was only one timestamp to take and it could only be the later one. Feed
discontinuities were logged as free text in `eventlog` at best.

**Why necessary.** Every analysis in the plan's Part A rests on differences
between local timestamps — forward paths, order-arrival latency, the
signal-to-goal window. If the local stamp is a processing time, those
differences silently absorb backlog, and the same tape can look like a fast
market or a slow one depending on how loaded the process was.

**Exact change.**
1. `KalshiWS.run` is split into `_read` (does nothing but `recv`, stamps
   `time.time()` and `time.monotonic()`, puts `(raw, wall, mono)` on an
   `asyncio.Queue`) and `_consume` (parses JSON, runs the unchanged
   `subscribed` / sequence / recovery logic in `_handle_raw`, and dispatches).
   The queue is unbounded on purpose — the point is to measure the backlog, not
   to drop frames — and its depth is exposed as `KalshiWS.backlog`. The
   consumer yields every `CONSUMER_YIELD_EVERY` = 16 frames so the reader and
   the websockets keepalive task still run under load. `ping_interval=10`,
   `ping_timeout=20` and `max_size=2**23` are preserved exactly. On disconnect
   the queue is drained, the discarded count recorded, and the connection
   rebuilt after the unchanged 3 s delay.
2. Callback compatibility: `_backlog_call_style` inspects the `on_message`
   signature once at construction. A three-argument callback is still called
   with three arguments, a `*args` callback (`tests/test_sequence.py`) with
   four positional arguments, and the engine — which declares
   `backlog` — by keyword.
3. `Engine.handle_ws(msg, wall, mono, backlog=0)` treats `wall`/`mono` as
   ARRIVAL, takes its own `proc_wall`/`proc_mono`, and passes both to the
   recorder. `RawRecorder.write` gained `arrival_wall`, `arrival_mono` and
   `backlog`, written as `at`/`am`/`bl` and **omitted when None**, so existing
   three-argument callers and existing segments are unaffected.
   `process_trade` and `_record_market_observation` use the arrival stamps.
4. New `signals.context` TEXT column (additive, idempotent `ALTER TABLE` like
   the others), written by `store.insert_signal` from `s.get("context")`, with
   `feed_lag_ms` (arrival x1000 minus the candidate `ts_ms`), `proc_lag_ms`
   (processing minus arrival) and `backlog`, on **every** signal row including
   `subthreshold` and `unconfirmed`. The frame capture is attached to the
   candidate and to the held near miss by `Detector.on_trade(..., context=)`,
   so a near miss flushed by a later trade reports the frame it actually
   happened on. The detector never reads it.
5. New `feed_events(id, ts, mono, kind, detail, mode)` table plus
   `store.insert_feed_event`, added to `exporter.TABLES` and readable at
   `GET /api/feed-events?limit=&mode=` following the existing mode-scoped
   pattern. Kinds: `connected`, `disconnected` (with the exception type and the
   number of frames discarded), `subscribed`, `resubscribed`, `gap` (sid,
   expected, received, markets invalidated, backlog), `snapshot_requested`,
   `snapshot_complete`, `market_added`, `market_dropped` (diffed in
   `discovery_task`) and `recorder_rotate`. The same events are also written
   into the raw stream by `RawRecorder.write_marker` as
   `{"type": "recorder_marker", "kind": ..., "detail": ...}` frames so a
   segment is self-describing for replay. Emission never blocks or breaks the
   feed: the SQLite insert is dispatched to a worker thread when a loop is
   running, and every failure is counted and reported through
   `Engine._record_error` rather than swallowed.
6. `Engine.status()` reports `feed_backlog`, `feed_backlog_max` and
   `feed_event_failures` alongside the existing `feed_lag_p50` / `p95`.

**Before / after.** Five frames received while the handler is busy: before,
all five carried the stamp of the moment they were processed and the queue
depth was unrecorded. After, each carries its own receipt time and a backlog of
4, 3, 2, 1, 0, and `lt - at` on the last frame is the full processing delay. A
sequence gap that previously produced one `eventlog` line now produces a
`feed_events` row with sid, expected, received, markets invalidated and the
backlog at the time, a matching `snapshot_requested` row, and a
`recorder_marker` frame in the segment itself.

**Reasoning and trade-offs.** The queue is unbounded, which trades memory for
measurement: a sustained backlog will grow it rather than shed frames. That is
deliberate for this pass — the first thing needed is a number for how far
behind the process gets, and dropping frames would both destroy the study data
and hide the problem. `lt`/`lm` deliberately keep their old meaning
(processing) rather than being redefined as arrival, so every existing reader
of the recorded segments stays correct and the two stamps can be differenced.
The rejected alternative for the callback was to change every call site to four
arguments, which would have broken `KalshiWS(lambda *args: ...)` in
`tests/test_sequence.py` and any other three-argument consumer.

**Deliberate omission.** `KalshiWS.request_snapshot`, which fires once per
rejected book delta while a book is being rebuilt, does **not** emit a ledger
event. It is unbounded in a bad book period, and the ledger's job is to explain
discontinuities, which the `gap` -> `snapshot_requested` -> `snapshot_complete`
recovery chain already does.

**Behaviour change to note.** A cleanly-ended stream (the `async for` finishing
without an exception) previously fell out of the `async with` and reconnected
immediately, with no state change and no record. It now raises
`ConnectionError("websocket stream ended")`, so it reports `disconnected`,
writes a ledger row, and waits the same 3 s as any other disconnect.

**Validation.** `tests/test_feed_arrival.py` (17 cases) asserts the reader
stamps receipt while the consumer is blocked and reports the descending
backlog; that a three-argument callback still works and that call-style
detection covers every signature shape; that a frame records `at`/`am`/`bl`
with `lt` strictly later, and that a three-argument `write` still produces the
old three-key layout; that a marker is self-describing in the stream and does
not inflate the exchange-frame count; that a ledger failure never fails the
recorder; that every signal outcome and a subthreshold row record their
context; that a real `KalshiWS` gap and disconnect land in `feed_events` with
the right details through the engine; that a ledger write failure is reported
rather than swallowed; and that the ledger insert does not run on the event-loop
thread. `tests/test_production_migration.py` migrates a production-shaped
database twice and additionally requires that `feed_events` is created empty,
that legacy signals keep a NULL `context`, and that remigration does not
rewrite the ledger. `tests/test_evidence_modes.py` and
`tests/test_mode_scoped_api.py` include `feed_events` in the study tables and
in the mode-scoping checks; `tests/test_exporter.py` requires it in the bundle.
Demo mode was smoke-tested for 20 s at `DEMO_SPEED=200`: `/api/status` and
`/api/feed-events` both respond and signals carry `context`, with the same
health banner as the unmodified tree.

**Risks / limitations.** In demo mode `feed_lag_ms` is the offset between the
recorded tape's original timestamps and replay wall time, not a live
measurement; it is honest arithmetic on a replay and must not be pooled with
live rows. The arrival stamp is taken after the websockets library has already
decoded the frame, so it excludes kernel and library buffering — it is an upper
bound on how early this process could have known. `feed_events` is a ledger of
what this process observed; a disconnect that kills the process leaves no
`disconnected` row, and the gap must be inferred from the absence of frames.
The backlog number is the depth of *this* queue only and says nothing about
queueing upstream of the socket.

**Follow-up.** B3-B7 of the plan extend the same `signals.context` column with
sibling evidence, book state and fill counterfactuals; this entry deliberately
keeps the JSON small so that pass can add to it.

---

## 2026-09-04

**Branch:** `claude/strategy-optimization-backtest-wd2j7z` (restarted from `main`
after PR #17 merged as `1089af7`)
**Deployment:** PR #17 live since 2026-09-03 12:01Z, `config_id`
`630d7b0f702f23b1`, 23 h uptime at time of check.

Entries `-001` to `-003` are observation only: a post-deploy verification and a
research freeze, recorded because both carry findings that must not be lost.
They were written before any code changed today and are left below in the order
they were written. Entries `-004` onward, which follow immediately and do change
code, are ordered newest first per `AGENTS.md`.

### CHG-2026-09-04-007 — Lower the sleeve minute floor to 80

**Commit:** `015aecd`
**Components:** `app/config.py`, `.env.example`,
`tests/test_price_floor_and_clock.py`, `tests/test_match_clock.py`,
`tests/test_engine_signal.py`

**Corrects CHG-2026-09-04-006**, which made `SLEEVE_MIN_MINUTE` configurable
and stated that the default was deliberately left at 88 because the timing
study was not precise enough to name a replacement. That reasoning was
incomplete. It treated the threshold as an estimate of the optimum, when its
actual job is to bound which minutes are ever observed.

**Observed / original behaviour.** With the default at 88, the sleeve can only
ever fire at minute 88 or later, so no data is generated below 88 and the
threshold can never be fitted from this venue's own clock. On the most recent
500 provider observations (484 `live`), eligible coverage by floor:

| Floor | Eligible observations | vs. 88 |
|---|---|---|
| 88 | 61 | — |
| 85 | 85 | +39% |
| 82 | 109 | +79% |
| **80** | **125** | **+105%** |
| 75 | 164 | +169% |

**Root cause.** Design gap in how the threshold was reasoned about, not a
defect. The Polymarket study put the shock inflection at minutes 86-90 on an
inferred clock measured to run about 5 minutes fast, back-calibrating to
roughly 81-85 with several minutes of uncertainty either side. Setting the
floor *inside* that band censors the sample exactly where the answer lies: at
85, an optimum of 82 could never be observed, because nothing below 85 fires.
A floor is a sampling bound; it should sit below the lowest plausible optimum,
not at the best point estimate of it.

**Why necessary.** Without it the number stays frozen at a value derived from a
cross-venue inference on an unreliable clock, and no forward evidence can ever
contradict it. The precondition named in CHG-2026-09-04-006's follow-up — a
forward study on Kalshi's own provider clock — cannot run while the gate
prevents the observations it needs.

**Exact change.** `SLEEVE_MIN_MINUTE` default 88 -> 80 in `config.py` and
`.env.example`. No gate logic changed; the parameter was already read from
config and already in `STRATEGY_PARAM_NAMES`, so the change produces a new
`config_id` and the 88-era and 80-era rows cannot pool.

Three existing tests asserted the 87/88 boundary against the default and would
have silently tracked whatever the default became. They are now pinned with
`patch.object(config, "SLEEVE_MIN_MINUTE", 88)` so they keep testing the
property they were written for — a below-threshold minute refuses and
short-circuits before the classifier — independent of what ships.

**Before / after.** Minute 82 in the second half: refused as `clock_pre_88`
before, accepted as `clock_88_plus` after. Minute 79 still refuses. Roughly
double the clock observations become gate-eligible, per the table above.

**Reasoning and trade-offs.** 85 was rejected because it sits inside the
estimated band and censors the answer. 75 was rejected because it doubles the
exposure again for margin there is no evidence is needed, and the whole thesis
is convexity near the whistle — minutes far from it are expected to be worse,
not merely unmeasured. 80 sits about one minute below the bottom of the
calibrated band, which covers the study's uncertainty without paying for
margin beyond it.

The admitted cost is explicit: this fires trades in a regime the study suggests
is worse than the latest minutes. That is the point — every fired trade records
its `provider_minute` and forward path, so the minute becomes a measured
variable rather than a guess. `PRICE_FLOOR` (CHG-2026-09-04-004) bounds what
the sample can cost, since entry price and not minute was the dominant loss
driver: the counterfactual on the 68-trade history was -$843.60 -> +$85.38 from
the price bound alone, with no minute change at all.

Also verified, and worth recording so it is not re-investigated: the
expiration-time window (`SLEEVE_START_BEFORE_EXPIRY_MIN` /
`SLEEVE_AFTER_EXPIRY_MIN`) is **not** a second gate that would blunt this.
`audit.schedule_window` is read only by `main.py` for per-signal display; the
dead gate that once used it was removed in CHG-2026-09-03-006. The clock stamp
at `engine.py:636` is the only minute constraint on the sleeve path.

**Validation.** 2 new tests: the shipped default gates where it claims to
(accepts at the default, refuses one below) — the only test that exercises the
deployed value, since every other minute test now pins its own; and the default
is asserted to sit at or below 81 and at or above 70, which fails loudly if
someone later moves it into the estimated band. Full suite 394 passing, lint
and compile clean. Sizing computed against 500 live production observations.

**Risks / limitations.** This is a deliberate widening of the trading window on
the strength of a study whose clock this log has already recorded as unreliable
(about 5 minutes fast, 9-11 minute inter-match IQR). If the convexity thesis is
right, minutes 80-87 will be measurably worse than 88+ and the floor should
come back up — that is the expected finding, not a failure. Loss accrual per
unit time should be expected to rise, bounded by `PRICE_FLOOR` and by the
sleeve's other admission gates, which remain unvalidated bootstrap numbers that
have still never been exercised against live data. K2 currently reads FAIL with
`[-31.57, +6.11]` at n=325; widening the window does not improve that and will
mix two minute regimes within the new `config_id`.

**Follow-up.** Once a forward sample accumulates, bucket sleeve outcomes by
`provider_minute` and look for the inflection on Kalshi's own clock. That is
the study that should set this number permanently, and it is now possible to
run. Until it does, 80 is a sampling floor and not a claim about the optimum.

### CHG-2026-09-04-006 — Make the sleeve minute configurable

**Commit:** `635cff0`
**Components:** `app/config.py`, `app/match_clock.py`, `.env.example`,
`tests/test_price_floor_and_clock.py`

**Observed / original behaviour.** `evaluate_clock_gate` refused any candidate
with `parsed.provider_minute < 88`, written as a literal. The number could not
be changed without editing the gate, and because it was not a configuration
parameter it was absent from `STRATEGY_PARAM_NAMES`, so two runs with different
thresholds would have carried the same `config_id` and pooled in every summary.

**Root cause.** Design gap, not a defect. 88 was one of the educated guesses the
README already flags; it was written as a constant before there was any data to
argue with it.

**Why necessary.** The Polymarket timing study (CHG-2026-09-04-003) is the first
evidence bearing on this number, and its conclusion is that 88 is *probably* a
few minutes late. That is not enough to move the default, but the number has to
become movable and fingerprinted before it can ever be moved and measured.
Without the fingerprint, an era at 88 and an era at 85 would silently average.

**Exact change.** `SLEEVE_MIN_MINUTE = _i("SLEEVE_MIN_MINUTE", 88)` added to
`config.py` and to `STRATEGY_PARAM_NAMES`; the literal in `evaluate_clock_gate`
replaced by `config.SLEEVE_MIN_MINUTE`. The returned outcome labels
(`clock_pre_88`, `clock_88_plus`) keep their `88` wording. Documented in
`.env.example` with a pointer to the research log.

**Before / after.** Behaviour at the default is byte-identical: minute 87
refuses, 88 accepts. With `SLEEVE_MIN_MINUTE=85`, minute 85 now accepts where it
previously refused, and `config_id` changes, so the two eras never pool.

**Reasoning and trade-offs.** Renaming the outcome labels to track the threshold
was considered and rejected: `clock_pre_88` is already written across every
recorded row and the sleeve funnel keys off it, so renaming would break
comparability with all history for a cosmetic gain. The labels are identifiers,
not descriptions; the threshold that produced them is recoverable from the
`config_id` on the row. **The default was deliberately not changed.** The study
puts the shock inflection near minute 80-85 on a clock measured to run about
5 minutes fast with a 9-11 minute inter-match IQR, which is enough to say "88 is
probably late" and not enough to name a replacement.

**Validation.** 3 tests: the threshold is honoured at 85 and at 88 with the
boundary checked on both sides; outcome labels stay stable when the threshold
moves; a threshold change produces a different `config_id`. Full suite 392
passing.

**Risks / limitations.** The label wording is now potentially misleading to a
reader who does not check the `config_id` — `clock_pre_88` at a threshold of 85
means "before 85". This is documented in the code comment and is the accepted
cost of comparability. Nothing here makes 88 more or less correct.

**Follow-up.** The Kalshi provider clock, not Polymarket's inferred one, should
settle the number. `sleeve_clock_pre_88` rejections now record
`provider_minute`, and signals record forward paths, so a forward study of
"what did the price do after minute M" can be run on this venue's own clock
without changing the threshold at all. That study is the precondition for
moving the default.

### CHG-2026-09-04-005 — Stop the mapping loop starving the match clock

**Commit:** `635cff0`
**Components:** `app/goal_latency.py`, `app/engine.py`, `app/config.py`,
`.env.example`, `tests/test_price_floor_and_clock.py`,
`tests/test_match_clock.py`

**Observed / original behaviour.** After the status fix in CHG-2026-09-03-001
the sleeve reached the clock gate for the first time and was then refused on
freshness: `sleeve_clock_stale` was the largest new rejection bucket (12), and
the sleeve still had **zero trades ever**. Production `match_clock_age_ms` ran
at **p50 6099 ms** against a `MATCH_CLOCK_MAX_AGE_MS` of 2500 ms, on a poll
loop configured at 250 ms.

**Root cause.** Two causes, both real.

First, `GoalLatencyObserver.run` called `await self._resolve_new_events()` at
the top of every poll iteration. That method makes one sequential REST call per
unmapped event, and leagues for which Kalshi publishes no milestone feed never
resolve, so those calls were retried for every such event on every pass,
forever. The poll loop's effective period was therefore set by mapping latency,
not by `GOAL_LATENCY_POLL_MS`.

Second, 2500 ms was itself the wrong bound. It was derived as ten poll
intervals — a property of the code's own cadence, not of the thing being
measured. The signal is a provider match minute, which changes once per 60 s.

**Why necessary.** Without it the sleeve cannot admit a single candidate. Every
sleeve threshold in the config is an unvalidated bootstrap number and none of
them can begin to be measured while the gate upstream of them never opens.

**Exact change.** `_resolve_new_events` removed from `run()` and moved into a
new `mapping_task()` coroutine that loops on
`CLOCK_MAPPING_INTERVAL_S` (default 15 s) with its own exception capture;
`engine.py` starts it as a separate task alongside `run()`.
`MATCH_CLOCK_MAX_AGE_MS` default raised 2500 -> 10000 ms.

**Before / after.** The exact production shape — a clock confirmed 6099 ms ago
at minute 90 in the second half — returned `clock_stale` before and returns
`clock_88_plus` after. A clock 45 s old still returns `clock_stale`. The poll
loop's period is now bounded by `GOAL_LATENCY_POLL_MS` rather than by the
number of unmappable events in the current window.

**Reasoning and trade-offs.** Raising the bound without splitting the loop was
rejected: it would have hidden a starved poll loop behind a looser threshold,
and the tail (p95 was far worse than p50) would still have refused candidates
intermittently and unpredictably. Splitting without raising the bound was also
rejected: even a healthy 250 ms loop plus feed transport does not reliably stay
under 2500 ms, as the K4 investigation already established a 228 ms feed
transport floor.

Ten seconds rather than sixty is the deliberate choice. Staleness is
*directionally safe* for this gate — match minute only increases, so a stale
reading of minute M implies a true minute >= M, and a `minute >= threshold` test
can therefore only refuse an eligible candidate, never admit an ineligible one.
The one risk staleness does carry is the opposite edge: entering just after a
final whistle that a stale clock has not yet reflected. Ten seconds bounds that
exposure while sitting comfortably above the observed p50.

**Validation.** 4 tests: the measured 6099 ms staleness now passes; a 45 s clock
still fails closed; the bound is asserted to sit between the observed p50 and a
provider minute; and `run()` is asserted by source inspection to no longer
resolve mappings while `mapping_task` does. `tests/test_match_clock.py`
B2b was rewritten to derive its stale timestamp from
`config.MATCH_CLOCK_MAX_AGE_MS` rather than the old 2500 ms literal, so it
asserts that coverage and the gate agree about the bound rather than what the
bound is. Full suite 392 passing.

**Risks / limitations.** This does not prove the sleeve will now trade; it
removes the freshness refusal and hands the decision to the sleeve's own
unvalidated admission thresholds, which have still never been exercised. The
final-whistle edge above is bounded, not eliminated. `mapping_task` swallows
exceptions into `last_error` exactly as `run()` does, so a permanently failing
mapping endpoint degrades silently rather than crashing — same behaviour as
before, now on a separate task. Mappings for a newly discovered event are
resolved up to 15 s later than before.

**Follow-up.** Re-measure `match_clock_age_ms` after deploy. If p95 still
exceeds 10 s, the residual cause is transport rather than loop scheduling and
should be investigated as part of K4 rather than by loosening the bound again.

### CHG-2026-09-04-004 — Refuse sub-floor entries, keep the evidence

**Commit:** `635cff0`
**Components:** `app/config.py`, `app/paper.py`, `app/store.py`,
`app/engine.py`, `static/app.js`, `.env.example`,
`tests/test_price_floor_and_clock.py`

**Observed / original behaviour.** Entry price is the dominant driver of the
loss, established from trades 1-61 on 2026-09-03 and confirmed out of sample on
trades 83-89 (CHG-2026-09-04-002). Over all 68 closed trades:

| Bucket | n | Net | Losers | Contracts |
|---|---|---|---|---|
| All, as traded | 68 | **-$843.60** | 41 (60%) | 21,977 |
| Entry >= 35c | 41 | **+$85.38** | 19 (46%) | 5,964 |
| Entry < 35c | 27 | **-$928.98** | 22 (81%) | 16,012 |

The cheap bucket is 40% of trades and **73% of all contract exposure**. Of the
cheap trades with a recorded MFE, none ever traded above entry even once.
`PRICE_CAP` bounded the top of the range at 58c; nothing bounded the bottom.

**Root cause.** The sizing rule. `NOTIONAL_USD` is a fixed dollar amount, so
contract count scales as 1/price: $100 buys ~727 contracts at 13.8c against
~176 at 57c. The strategy therefore takes its largest positions, by a factor of
four, on exactly the outcome the market has just marked down hardest — and pays
a quadratic fee on every one of those contracts. This is a design gap in the
interaction between sizing and entry, not a bug in either.

**Why necessary.** The cheap bucket is not a tail: it is the loss. Its 80%+
loss frequency reproduces independently in both build eras, so it is not an
artefact of a fixed bug. Continuing to take those entries spends capital to
re-confirm something already confirmed twice.

**Exact change.** `PRICE_FLOOR = _f("PRICE_FLOOR", 35.0)` added to `config.py`
and to `STRATEGY_PARAM_NAMES`. Both entry paths check it: the V2 adapter
(`_execute_entry`) after the fill VWAP is computed, returning `rejected_floor`
through `_finalize_entry_outcome` so the arrival book and fill levels are still
persisted; and the legacy `try_enter` path against `entry_px`, so the two cannot
disagree about eligibility. `rejected_floor` added to `confirmed_outcomes` in
`store._strategy_summary`, to the engine's event icon map, and to the dashboard
outcome labels. `PRICE_FLOOR=0` disables the bound.

**Before / after.** Same 68 trades: **net -$843.60 as traded, +$85.38 with the
floor applied**, keeping 41 of 68 trades (60%) and 27% of contract exposure. A
sub-floor candidate that previously opened a trade now records a signal with
outcome `rejected_floor`, no trade row, and a forward path — the same treatment
`rejected_cap` already gives the upper bound.

**Reasoning and trade-offs.** Three alternatives were considered and rejected.
*Fixed contract count instead of fixed notional* addresses the same mechanism
but changes the sizing of every trade including the profitable band, which is a
larger and less reversible change than bounding the range. *Doing nothing and
collecting more data* was the standing position and was reconsidered honestly:
it was correct while the finding was in-sample only, and the out-of-sample
confirmation is what changed it. *Refusing the signal outright* rather than the
fill was rejected because it would destroy the evidence needed to ever revisit
the floor.

The floor is deliberately implemented as a refusal at the execution stage, not
a filter at detection, so the counterfactual stays measurable: every refused
episode still records its signal and forward path, and whether the floor was
right can be re-decided from the database rather than re-argued from memory.

It must be said plainly that 35c was chosen after seeing the data. Every
confidence interval on this still spans zero, and K2 currently reads FAIL with
`[-31.57, +6.11]` at n=325. This is a pre-registered challenger being tested
forward, not a validated parameter. It enters the config fingerprint, so the
before and after eras cannot pool.

**Validation.** 4 tests: a floor change produces a new `config_id`; a
`rejected_floor` signal persists with no trade row; `rejected_floor` counts as a
confirmed signal so the floor cannot silently shrink the K2 denominator; and a
zero floor disables the bound. Counterfactual computed against the real 68-trade
production history, table above. Full suite 392 passing.

**Risks / limitations.** The floor is fitted to 68 trades with wide intervals;
it may be wrong in level or in kind. It removes 40% of the sample, so K2 will
accumulate evidence more slowly from here. Trades that were genuinely mispriced
cheap outcomes will now be refused along with the bad ones, and the recorded
forward paths are what will show whether that cost anything. Nothing here fixes
the sizing rule itself, which remains 1/price within the surviving band.

**Follow-up.** After a forward sample accumulates, compare realised outcomes on
`rejected_floor` signals against filled ones in the band just above the floor.
If refused episodes systematically ran favourably, the floor is too high and
should move; if they ran as the history suggests, the next question is whether
the fixed-notional sizing rule should be replaced outright.

### CHG-2026-09-04-001 — Post-deploy verification of PR #17

**Components:** none changed. Observation only, against the live service.

**Observed.** All four shipped fixes are confirmed working in production after a
full day of live capture.

| Fix | Before deploy | Now |
|---|---|---|
| Sub-threshold capture | absent | **314 observations** |
| K1 fill integrity | FAIL (trades 39, 64) | **PASS, n=64, zero failures** |
| Late-confirmation recording | did not exist | **2 `confirmed_late`** |
| 88-gate status matcher | 32 `sleeve_clock_not_live` | **still exactly 32** |

That last row is the proof for the status fix. `sleeve_clock_not_live` has not
incremented once since deploy, while three *new* rejection reasons appeared that
could never be reached before: `sleeve_clock_stale` (12),
`sleeve_clock_pre_88` (3), `sleeve_clock_half_time` (2). Candidates now pass the
status check and are refused on legitimate grounds.

**New blocker identified, not fixed.** The sleeve still has zero trades. Its
binding constraint has moved from status to **clock freshness**:
`sleeve_clock_stale` is the largest new bucket. `MATCH_CLOCK_MAX_AGE_MS` is
2500 ms against an observed `match_clock_age_ms` p50 of roughly 6000 ms. Until
the clock poll keeps up, or that bound is reviewed, the sleeve will keep
refusing candidates it now correctly reaches.

**Risks / limitations.** Only 17 new sleeve evaluations, so the reason mix is
provisional. K4 reads STALE rather than BREACH because latency samples reset
with the process.

**Follow-up.** Investigate `match_clock_age_ms`. It is now the single thing
standing between the sleeve and its first observation.

### CHG-2026-09-04-002 — Price-floor hypothesis confirmed out of sample

**Components:** none changed. Recorded so the evidence is not re-derived.

**Observed.** Seven trades closed since the previous analysis. The price-floor
hypothesis was stated on 2026-09-03 from trades 1-61; these seven are new data
and reproduce it exactly.

| Band | Trades | Net | Losers | Contracts |
|---|---|---|---|---|
| Below 35¢ | 2 | **-$148.92** | 2 of 2 | 1,020 |
| 35¢ and above | 5 | **+$46.68** | 2 of 5 | 531 |

Both cheap trades recorded `mfe_c` of exactly 0.0: they never traded above entry
once. That is now **6 of 6** measured cheap trades with zero favourable
excursion. Study-wide the sub-35¢ bucket is 27 trades and 22 losers (81.5%).

Without the two cheap trades the day would have been **+$46.68** instead of
-$102.24.

**Why this changes the "keep collecting" answer.** The argument is not that the
paper account is down. It is that the cheap bucket is **contaminating the
evidence being collected**. It holds 41% of trades and 73% of contract exposure
in a band that loses four times in five, so it dominates the variance of the K2
interval, which still spans zero at [-31.57, +6.11] on n=325. Continuing to
trade it adds noise, not signal, to a question already answered.

**Deliberately NOT changed.** No parameter was touched. The recommended design
is a `PRICE_FLOOR` that refuses the entry but still records the signal and its
forward path, mirroring how `rejected_cap` already handles the upper bound. That
keeps the evidence accumulating while removing it from the P&L, and gets a new
`config_id` so the two eras stay separable.

**Follow-up.** Operator decision. See
`RESEARCH_LOG_2026-09-04_POLYMARKET_TIMING.md` §6 for the full challenger queue.

### CHG-2026-09-04-003 — Polymarket cross-venue study frozen

**Components:** `docs/RESEARCH_LOG_2026-09-04_POLYMARKET_TIMING.md` (new).

**Why.** A 462-match, 1.13 M-trade external study was run to answer questions the
live Kalshi sample is too small to settle. It produced one confirmed finding,
one withdrawn claim, and two challengers. Frozen mid-study at the operator's
request so it can be resumed cold.

**Headlines.** Late repricing is genuinely larger and safer, confirmed under
every clock mapping. The precise optimal minute is **not** determined: the naive
clock ran ~5 min late, verified independently by halftime-density detection and
by aligning real goal minutes to price jumps, with several minutes of residual
per-match spread. Sibling coherence is a weak filter that is structurally blind
to VAR reversals and missed penalties. The 2¢ reversal stop fires on 60% of
shocks and looks actively harmful.

**Risks / limitations.** Polymarket charges no fees (a Kalshi fee model is
applied throughout), has no historical order book (all returns are upper
bounds), and stamps trades to the second (so nothing here speaks to Gate A's
±50 ms window). Nothing is promotable without forward testing on Kalshi.

**Follow-up.** The document carries a resume section and an explicit
falsification section. The highest-value next step is the replay engine over the
recorded Kalshi feed, not more Polymarket work.

---

## 2026-09-03

**Branch:** `claude/strategy-optimization-backtest-wd2j7z`
**Base:** `5494025` (main, "Merge pull request #16")
**Commits:** `836c08a`, `2bee855`, `1b1016f`, `6daccf9`, `3904d54`, `21a750f`
**Diff:** 18 files, +1204 / -70
**Suite:** 323 tests passing at base → 363 passing after (3 skipped in both;
the skips are the browser-acceptance tests, unchanged). `python -m compileall`
and `ruff check --select E9,F63,F7,F82` clean.
**Deployment status:** NOT DEPLOYED. The live Railway service
(`football-bot-production-78f7`) still runs the pre-change build, so none of
these changes are in effect in production yet.

### Evidence baseline for the day

All findings below came from the live study pulled from the running service via
the admin study export, not from synthetic data. State at time of analysis:

| Measure | Value |
|---|---|
| Signals | 1,470 |
| Closed trades | 61 |
| Net | -$741.36 |
| Capture span | Aug 25 – Sep 3 (7.9 days at first pull) |
| Price-only sleeve trades | 0 |
| K1 fill integrity | FAIL (trades 39, 64) |
| K2 event-clustered CI | [-30.7, +9.66], n=308 |
| K4 order arrival p95 | 7,018 ms against a 250 ms threshold (BREACH) |

Two structural facts drove most of the work. First, the reported net pooled at
least two different code builds: 27 trades at -$630.13 written before Aug 30 and
34 at -$111.23 after, which no aggregate could separate. Second, the price-only
sleeve had never admitted a single candidate in its entire operating life.

---

### CHG-2026-09-03-006 — Make sleeve refusals re-decidable; drop a dead gate

**Commit:** `21a750f`
**Components:** `app/late_score_sleeve.py`, `app/engine.py`, `README.md`,
`tests/test_late_score_sleeve.py`

**Observed / original behaviour.** Early refusals in
`PriceOnlyLateScoreSleeve.classify` returned a bare reason string. A
`wide_spread` row recorded that the book was too wide but never how wide;
`incomplete_book` never named the offending leg; `no_baseline` and
`stale_baseline` recorded neither how much history existed nor the age of the
best candidate. The later refusals (`insufficient_triplet_shift`,
`weak_post_state`, `incoherent_sibling_rise`, `weak_triplet_coherence`) already
carried full triplet features because `detail.update()` ran before them.

**Root cause.** `_snapshot` returned `(None, reason)` from several points before
any measurement was written into the decision detail, and the baseline checks in
`classify` returned before their own `detail.update()`.

**Why necessary.** A rejection with no measurements cannot be re-decided. There
was no way to ask what `SLEEVE_MAX_SPREAD_C = 12` or a different
`SLEEVE_MAX_BASELINE_AGE_MS` would have admitted, short of replaying the raw
feed, which defeats the purpose of recording the rejection at all.

**Exact change.**
- `_snapshot` accepts an `evidence` dict and populates it before failing closed.
- Spreads are now measured for all three legs before any leg is judged, so a
  refusal shows the whole triplet rather than stopping at the first offender,
  and records `widest_leg`, `widest_spread_c` and `max_spread_c_limit`.
- `incomplete_book` records `missing_leg` and the observed bid/ask or `book.ok`.
- Baseline refusals record `baseline_rows`, `baseline_eligible`,
  `baseline_lag_ms`, `max_baseline_age_ms`, `oldest_row_age_ms`, and
  `baseline_age_ms` where a baseline was found.
- `Engine.is_sleeve_window` deleted (see trade-offs).
- README section corrected: it described the expected-expiration window as the
  live sleeve gate, which is false.

**Before / after.** Before: `{"decision": "wide_spread"}`. After: the same
decision plus every leg's spread, which leg was widest, and the limit it was
judged against.

**Reasoning and trade-offs.** Measuring all three spreads before judging costs
two extra comparisons per evaluation and makes the row far more useful; a
first-offender short circuit would have hidden whether the other legs were also
marginal. `is_sleeve_window` was removed rather than wired up: it approximated
minute 88 from `expected_expiration_time` and had no caller, because admission
is gated on the persisted provider clock. Leaving it implied expiry time
admitted trades. `SLEEVE_START_BEFORE_EXPIRY_MIN` / `SLEEVE_AFTER_EXPIRY_MIN`
were deliberately kept, because `audit.py` still uses them for the per-signal
schedule-proxy diagnostic; deleting them would have removed a live diagnostic to
tidy up dead code.

**Validation.** 4 new tests in `tests/test_late_score_sleeve.py` covering
wide-spread evidence, missing-leg naming, baseline history recording, and leg
count on `not_triplet`. Full suite 363 passing.

**Risks / limitations.** Rejection details are larger, so `signals.detail` grows;
the rows are small and rate-limited by candidate frequency, so this is not a
storage concern at observed volumes. Historical rejections are unchanged and
remain un-re-decidable.

**Follow-up.** None for this change.

**Checked and NOT changed** (recorded so they are not re-investigated):
- `outside_minute_88_window` does not exist in this codebase. The 119 rows
  carrying that label are historical, written by the older build still deployed.
- `store._strategy_key` and `audit.signal_strategy` both already fold
  `price_only_late_score_v1` into `price_only_late_score`. The split label seen
  during analysis was an artifact of the throwaway analysis script, not of the
  product. No product defect existed.

---

### CHG-2026-09-03-005 — Anchor the episode cooldown; measure late confirmations

**Commit:** `3904d54`
**Components:** `app/detector.py`, `app/engine.py`, `app/config.py`,
`.env.example`, `tests/test_confirmation_window.py`

**Observed / original behaviour.** Two mechanisms shaped the recorded episode
inventory by trade arrival pattern rather than by any configured rule.
`Detector.on_trade` advanced `st.last_candidate_ms` on the *suppression* branch,
re-arming the cooldown on every suppressed candidate. Separately, an unconfirmed
candidate was held for a hard-coded `time.time() + 0.2` before being recorded
`unconfirmed`; 76% of Gate A signals ended in that bucket, and it could not be
decomposed.

**Root cause.** For the cooldown, the anchor was the last *evaluated* candidate
rather than the last *emitted* one. For confirmation, a wall-clock transport
deadline of 200 ms was applied against an observed feed lag p95 of 888–1,137 ms,
even though `Detector.confirm` judges coherence purely on exchange timestamps
(`CONF_MS`), so late frame arrival and true incoherence were indistinguishable.

**Why necessary.** A market printing sweeps faster than `EPISODE_COOLDOWN_S`
could be silenced indefinitely, so the episode inventory was not the inventory
the configuration described. And a large, undifferentiated `unconfirmed` bucket
hid whether the sibling rule was rejecting incoherent pairs or merely slow ones.

**Exact change.**
- The cooldown branch no longer assigns `st.last_candidate_ms`.
- New `CONF_WAIT_S` (default 2.0) replaces the hard-coded 0.2 s hold.
- New `CONF_TRADE_MAX_AGE_S` (default 0.2) bounds how old a candidate may be at
  confirmation time and still trade. A later confirmation is recorded as
  `confirmed_late` and is **not** traded.
- `pending` entries carry `queued_at` to measure that age.

**Before / after.** Cooldown, verified by executing the pre-fix source directly:
with a 5 s interval and sweeps at t=100.0 s, 103.0 s and 106.0 s, the pre-fix
detector emitted only the first (the 106 s sweep was suppressed because the
anchor had moved to ~103.007 s); the fixed detector emits the first and the
third. Confirmation: previously a sibling frame arriving at 400 ms produced
`unconfirmed`; it now produces `confirmed_late` with its exchange-clock lag
preserved, and still does not trade.

**Reasoning and trade-offs.** The obvious change was to raise the deadline and
trade whatever confirmed. That was rejected. Controlling for entry price, fills
in the tradeable band are worth about +7.2¢/contract when fast and
-7.6¢/contract when slow, so entering on a two-second-old confirmation would
deepen the study's largest execution problem. Setting `CONF_TRADE_MAX_AGE_S` to
exactly the previous 0.2 s makes the longer wait purely additive evidence and
leaves trading behaviour byte-identical. The bound is a separate knob so it can
be raised deliberately once the `confirmed_late` population says whether those
signals are worth taking.

**Validation.** New `tests/test_confirmation_window.py`, 5 tests: cooldown
anchoring, fresh confirmation still trades, late confirmation recorded but never
traded, the tradeable bound preserves prior behaviour, and non-confirming
candidates still expire. The cooldown regression was additionally proven by
loading the pre-fix `detector.py` from git and running the same scenario against
it. Full suite 363 passing.

**Risks / limitations.** Pending candidates are now held up to 2 s instead of
0.2 s, so `self.pending` holds more entries; it is bounded by candidate rate and
is a list scan per trade, which is unchanged in complexity. `confirmed_late` is
not in the K2 confirmed-outcome set, so it cannot inflate a kill-condition count
— this was checked, not assumed. The true confirmation rate is not yet known;
this change only makes it measurable.

**Follow-up.** After the next capture window, compare `confirmed_late` forward
paths against `filled` outcomes to decide whether `CONF_TRADE_MAX_AGE_S` should
rise. Do not raise it before that evidence exists.

---

### CHG-2026-09-03-004 — Stop reporting truncated fill evidence as a failed fill

**Commit:** `6daccf9`
**Components:** `app/execution.py`, `app/paper.py`, `app/store.py`,
`tests/test_store_execution.py`

**Observed / original behaviour.** K1 (fill integrity) read `FAIL` on the live
study, naming trades 39 and 64. K1 is one of the pre-registered kill conditions
gating any move to real money.

**Root cause.** `ShadowBook.snapshot_dict` truncated the persisted arrival book
to 8 levels per side, while `store._paper_fill_integrity` validated the entire
fill walk against that snapshot. Trades 39 and 64 walked 15 and 14 levels — the
study's only two walks past the cap — so their deeper levels had no
corresponding evidence and the check reported them as bad fills. Both fills were
in fact consistent: their first eight levels matched the recorded book exactly
and no level exceeded available depth.

**Why necessary.** The gate certifying fill realism was failing hardest on the
deepest walks, which are exactly the fills whose realism is least certain and
most worth verifying. A false `FAIL` here is worse than no check, because it
would either block a legitimate promotion or train the operator to ignore K1.

**Exact change.**
- `ShadowBook.SNAPSHOT_DEPTH` introduced (8, unchanged default);
  `snapshot_dict(depth=None)` now also records `depth` and `truncated`.
- `PaperDesk._execute_entry` computes the fill first, then snapshots with
  `depth=max(SNAPSHOT_DEPTH, len(fill.levels))`, so a walk is always
  re-verifiable. Safe because `buy(..., consume=False)` does not mutate the
  shadow book, so the post-fill snapshot is the identical arrival book.
- `_paper_fill_integrity` returns `None` (unverifiable) for a level beyond the
  deepest recorded price, and still returns `False` for a level *inside* the
  recorded range that the book does not support.

**Before / after.** Re-running the check over the live export: before, 59
checked with 2 failures (K1 = FAIL); after, 57 verified, **0 genuine failures**,
4 unverifiable (the 2 truncations plus 2 rows that never had fill levels).
K1 moves FAIL → PASS on n=57.

**Reasoning and trade-offs.** Simply raising the fixed depth to some larger
number was rejected: it moves the cliff rather than removing it. Deriving depth
from the walk removes it by construction. Treating truncation as unverifiable
rather than as a pass was deliberate — the check must never claim to have
verified something it could not see. The "inside the recorded range" carve-out
exists specifically so the tolerance does not become a hole through which a
fabricated fill could pass.

**Validation.** 3 new tests: a walk past recorded depth is `None` not `False`; a
level inside the recorded range with no depth is still `False`; snapshot depth
covers the walk and reports truncation correctly. Re-ran the real integrity
check over all 61 exported trades. Full suite 363 passing.

**Risks / limitations.** `book_at_entry` rows grow for deep walks; the observed
maximum is 15 levels, so this is negligible. Historical truncated rows stay
unverifiable forever — the evidence was never recorded and cannot be
reconstructed. K1's denominator therefore drops from 59 to 57.

**Follow-up.** None. K1 should be re-read after redeploy against fresh fills.

---

### CHG-2026-09-03-003 — Record sub-threshold bursts as research observations

**Commit:** `1b1016f`
**Components:** `app/detector.py`, `app/engine.py`, `app/config.py`,
`app/store.py`, `README.md`, `.env.example`,
`tests/test_subthreshold_capture.py`

**Observed / original behaviour.** `Detector.on_trade` returned `None` for any
burst below `DL_MIN` / `LEVELS_MIN` / `SIZE_MIN`, leaving no row of any kind.

**Root cause.** Not a defect; a design gap. The detector thresholds did double
duty as both the trading gate and the recording gate.

**Why necessary.** Accepted sweeps pile hard against every floor: `dl` p10 0.818
against a 0.8 minimum, `levels` p10 5 against 5, `size` p10 218 against 200
(re-measured on a later pull: 0.820 / 5 / 219, i.e. stable). The study therefore
only ever saw the surviving side of a hard-binding cut, and `DL_MIN`,
`LEVELS_MIN` and `SIZE_MIN` could only be re-fitted by replaying the raw feed.

**Exact change.**
- `Detector(subthreshold_sink=...)`; an unwired detector behaves exactly as
  before.
- Bursts clearing a looser research floor (`SUBTHRESHOLD_DL_MIN` 0.3,
  `_LEVELS_MIN` 3, `_SIZE_MIN` 50) are reported with their displacement, level,
  size, reference and extreme features plus which floors they missed.
- Own per-market cooldown (`SUBTHRESHOLD_COOLDOWN_S`, 5 s) that is **not**
  advanced on suppression, deliberately not repeating CHG-005's defect.
- `Engine.record_subthreshold` writes outcome `subthreshold` with no sibling
  confirmation, no sleeve dispatch, no dashboard broadcast, no forward-path
  watch and no clock-gate miss accounting.
- `store._compute_stats` excludes these rows from both sleeve funnels and
  reports them separately as `subthreshold_observations`.
- The new config knobs are excluded from `STRATEGY_PARAM_NAMES`: capturing an
  observation cannot change a decision.

**Before / after.** Before: a near miss left no trace. After, replaying the
bundled Espanyol–Real Madrid tape (32,149 trades): 8 tradeable candidates and 41
observations, a 5.1× ratio, in a band between the research and trading floors
(near-miss `dl` p10 0.356, p50 0.429, p90 0.736).

**Reasoning and trade-offs.** A sweep sits below the floor part-way up: prices
40→47 read `levels=3` two milliseconds before the same burst becomes a tradeable
`levels=8` candidate. Recording that instant would have filled the inventory
with pre-echoes of sweeps that actually traded, and a threshold fitted on that
inventory would be fitted to an artifact of tick arrival. Observations are
therefore held, upgraded while the burst grows, dropped if the burst clears the
trading floor, and emitted only once the burst window closes; the periodic task
flushes markets that go quiet. Forward paths were deliberately **not** attached:
these rows are numerous by design and each watch costs a tracking slot and up to
`BID_PATH_MAX_SAMPLES` rows.

**Validation.** New `tests/test_subthreshold_capture.py`, 14 tests across
detector, engine and store: near miss reported and not traded, tradeable sweep
never reported, capture switchable off, research floor bounds recording, held
observation is the burst's best, rate limit does not roll forward, failing sink
cannot break trading, unwired detector unchanged, engine writes the right
outcome without moving clock health counters, write failure contained, rows stay
out of sleeve funnels and kill gates, features present for a re-fit. Full suite
363 passing.

**Risks / limitations.** Row volume rises roughly 5× the candidate rate, bounded
hard by the per-market cooldown. These rows carry no outcome label, so they
support re-fitting the threshold *distribution* but not directly the
profitability of a lower threshold; that still needs forward paths or replay.

**Follow-up.** After a capture window, compare the near-miss distribution
against the accepted one to decide whether `DL_MIN` should move. Consider
attaching forward paths to a sampled subset if outcome labels prove necessary.

---

### CHG-2026-09-03-002 — Stamp a configuration identity on every signal and trade

**Commit:** `2bee855`
**Components:** `app/config.py`, `app/store.py`, `app/exporter.py`,
`app/main.py`, `tests/test_config_identity.py`

**Observed / original behaviour.** The study reported a single net of -$609.02
over 56 closed trades (later -$741.36 over 61). `insert_signal` and the three
trade-insert paths recorded no configuration identity, and the export manifest
recorded configuration only once, at export time.

**Root cause.** No provenance column existed, so rows from different builds and
different environment settings pooled into one aggregate, and a mid-study change
would silently relabel history.

**Why necessary.** The single reported net was two configurations: 27 trades at
-$630.13 before Aug 30 and 34 at -$111.23 after, with gross per contract moving
from -3.98¢ to +2.39¢. No aggregate over that pool answered any question about
either, and any threshold tuned on it would have been fitted to a mixture. This
blocks the entire optimisation programme, not just one analysis.

**Exact change.**
- `config.STRATEGY_PARAM_NAMES`, `strategy_params()`, `config_id()`,
  `config_record()`, and `CODE_FINGERPRINT`.
- `config_id` is a SHA-256 over the sorted strategy parameters **and** the
  contents of the strategy-critical source files (`books.py`, `config.py`,
  `detector.py`, `engine.py`, `execution.py`, `late_score_sleeve.py`,
  `match_clock.py`, `paper.py`).
- Additive migration adds `config_id TEXT` to `signals` and `trades`; new
  `config_versions` table resolves an id to its parameters and fingerprint.
- Stamped in `insert_signal`, `insert_trade` and `open_paper_trade`.
- `exporter.non_secret_config()` now derives its strategy half from the same
  name list, and the manifest carries `configuration_identity`.
- `/api/config` exposes `config_id` and `code_fingerprint`.

**Before / after.** Before: two builds indistinguishable in the database. After:
each carries a distinct 16-hex identity resolvable to its exact parameters and
code fingerprint.

**Reasoning and trade-offs.** Hashing code as well as parameters was essential
rather than thorough: the two eras above ran *identical* environment variables
and different code, so a parameters-only hash would have missed the exact case
that motivated the work. File hashing keeps this self-contained, needing no git
metadata in the image. `SOCCER_SERIES` is included because the traded universe
is part of the configuration; read-only observability settings are excluded
because they cannot change a trading decision. Registration is idempotent so a
restart does not look like a new configuration, and a registry write failure is
swallowed so provenance can never stop collection.

**Validation.** New `tests/test_config_identity.py`, 12 tests: stability and
content-addressing, parameter change produces a new id, code change produces a
new id, fingerprint covers every strategy source, observability settings do not
change the id, manifest and identity share one list, stamping on signals and
trades, self-describing registry, idempotent restart, two configurations
separable in one database, legacy rows keep NULL, collection survives a registry
failure. Additionally migrated a **copy of the real production database**: 1,386
signals and 56 trades migrated intact, all retained NULL `config_id`, and
`stats()` still reported -609.02 unchanged. Full suite 363 passing.

**Risks / limitations.** Rows written before this keep `NULL config_id` forever
— unknown provenance is preserved as unknown, deliberately not backfilled to the
current identity. Analysis must treat those 1,470 signals and 61 trades as one
unknown-provenance bucket. `CODE_FINGERPRINT` is computed once at import;
sources cannot change under a running process, so this is safe, but it means a
hot-reload workflow (not used here) would report a stale fingerprint.

**Follow-up.** Never pool rows with different `config_id` values into one
result. This is stated in the README and should be enforced in any future
analysis tooling.

---

### CHG-2026-09-03-001 — Accept in-play halves at the 88-gate

**Commit:** `836c08a`
**Components:** `app/match_clock.py`, `tests/test_match_clock.py`

**Observed / original behaviour.** The price-only late-score sleeve had traded
exactly zero times in its entire operating life. Its rejection mix was 119
`sleeve_outside_window`, 32 `sleeve_clock_not_live`, 3 `sleeve_clock_missing`.
Of signals carrying a clock stamp, 75 recorded `unusable_reason: status_2nd_half`
and 2 `status_1st_half`. Nine candidates reached minute 88 or later and every one
was rejected.

**Root cause.** Kalshi reports the running period in its status field as
`2nd_half`. `_compact` reduces that to `2ndhalf`, which matched no entry in
`_STATUS_LIVE`, `_STATUS_SUSPENDED`, `_STATUS_ABANDONED`, `_STATUS_FINAL`,
`_STATUS_PRE` or `_PERIOD_HALF_TIME`. `normalize_status` therefore returned it
verbatim, and `evaluate_clock_gate` refused any status that is not exactly
`"live"` as `clock_not_live`. Second half is the only period in which a
minute-88 sleeve can ever fire, so the sleeve was structurally incapable of
admitting anything.

**Why necessary.** Every threshold in the sleeve had zero observations behind
it. No amount of further paper trading would have produced any, and the
parameters could not be studied, tuned, or falsified.

**Exact change.**
- New `_STATUS_LIVE_PERIOD` set containing the period-shaped live statuses
  (`1sthalf`, `2ndhalf`, `firsthalf`, `secondhalf`, `1h`/`2h`, `h1`/`h2`,
  `fh`/`sh`, `period1`/`period2`, `stoppage`, `addedtime`, `extratime*`, `et`).
  Half-time and full time are deliberately absent because they name a stoppage,
  not play; bare `"1"` / `"2"` are absent because a lone digit in a status field
  establishes nothing.
- `1sthalf` / `2ndhalf` added to `_PERIOD_FIRST` / `_PERIOD_SECOND` so a
  status-shaped period field also resolves.
- `evaluate_clock_gate` now calls `normalize_status(parsed.provider_status)` at
  the decision boundary.

**Before / after.** Replaying all 1,827 stored provider observations through the
gate: **before, 0 accepted and 1,720 `clock_not_live`; after, 157
`clock_88_plus` accepted and 3 `clock_not_live`.** The 3 remaining are
`penalties` / `awaiting_penalties`, which a minute-88 soccer gate should refuse.

**Reasoning and trade-offs.** Normalisation was placed at the decision boundary
rather than in the stamp so the stamp and the persisted observation keep the
provider's own wording for audit, which this codebase treats as an invariant.
`normalize_status` is idempotent, so canonical values pass through unchanged. A
named set was used rather than adding two strings to `_STATUS_LIVE`, so the
intent (a period name in a status field means play is underway) is explicit and
extra-time variants are covered uniformly.

**Validation.** 2 new tests: `normalize_status` maps the period-shaped statuses
to `live` while half-time, full time and suspended keep their own labels; and
the exact production shape (minute 88, period `2nd`, status `2nd_half`) reaches
`clock_88_plus` while minute 87, first half, half-time and final all still fail
closed with their correct outcomes. Validated against real data by replaying all
1,827 production clock observations, before and after. Full suite 363 passing.

**Risks / limitations.** More candidates now reach the sleeve classifier, which
is the point, but the sleeve's own admission thresholds remain unvalidated
bootstrap numbers and have still never been exercised against real data. The
first live window after deploy will be the first time they are.
`MatchClockGate.evaluate` short-circuits on a stamp that already carries a
declared refusal, so historical rejections are not retroactively repaired — this
is intentional (recorded verdicts stay as recorded) and was verified.

**Follow-up.** After redeploy, watch the sleeve's rejection mix. If candidates
now reach `classify` and are refused on triplet thresholds, those thresholds
become the next thing to study — they are the deliberately-guessed bootstrap
values the README already flags.

---

### Outstanding at end of day

| Item | Status |
|---|---|
| Redeploy so any of this takes effect | **Blocked on operator.** Highest value action available. |
| K4 latency BREACH (arrival p50 981 ms, p95 7,018 ms) | Open, root cause not established. `paper_entry_ms` also runs at 391 ms against a configured 150 ms. |
| `PRICE_FLOOR` as a pre-registered challenger | Open decision. See analysis note below. |
| K2 CI still spans zero | Expected; needs sample, not code. |

**Analysis note not yet acted on.** Entry price is the dominant driver of the
loss and it is not the build era. Below 35¢: 25 trades, 20 losers (80%), median
-$49.66. At or above 35¢: 36 trades, 17 losers (47%), median +$2.23. The loss
frequency is 80% in the cheap band in *both* eras independently. The mechanism
is the sizing rule: `NOTIONAL_USD` is a fixed dollar amount so contract count
scales as 1/price, and 41% of trades carry 73% of all contract exposure (mean
600 contracts below 35¢ against 151 above). Of trades with recorded MFE, 4 of 4
cheap ones never traded above entry even once. Separately, controlling for entry
price reverses the naive latency reading: within the ≥35¢ band, fast fills are
+7.21¢/contract and slow fills -7.55¢/contract, where the uncontrolled split
suggested fast was worse (Simpson's paradox via the cheap bucket).

This was found by slicing after seeing the data and every confidence interval
still spans zero. It must be pre-registered as a challenger configuration and
tested forward, not retrofitted. No parameter was changed.
