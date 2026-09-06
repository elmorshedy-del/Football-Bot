# Investigation: what the Football-Bot is actually doing (2026-09-04 to 2026-09-06)

An open-ended investigation of the bot as it exists, from everything it has
collected: what it is really doing, whether each trade and signal carries enough
recorded context to be analysed honestly later, whether the dataset itself can be
trusted, what works or fails and why -- separating the trading *idea* from the
*thresholds*, from *execution*, from the *feed*, from plain *bugs* -- and which
simpler strategies are worth testing.

**How to read this.** Every number here was measured, from the live deployment's
read API, its raw WebSocket recording, the Railway metrics and deploy history,
and the code in this repository. Where a finding is directional rather than
established, it says so; where a sample is too small to carry a conclusion, it
says that too. Several sections exist specifically to record that an earlier
conclusion in this same document was wrong (A5c retracts P&L figures in A1-A3;
A7 retracts A4's attribution of the latency). That is the point of keeping it.

**Status.** The data-capture and platform work this investigation called for is
implemented and deployed; each change is recorded individually in
[`ENGINEERING_CHANGE_LOG.md`](ENGINEERING_CHANGE_LOG.md), which carries the
production measurements, the invariants preserved, and the verification for
each. This document keeps the findings and the forward agenda. The
implementation plan itself has been dropped from it, having been executed.

**What is not settled.** The strategy questions. Nothing here establishes that
Gate A makes or loses money: A5c shows the same 29 replayed trades pricing
anywhere from -$862.96 to +$550.33 on the entry model alone. The point of the
capture work was to make the next answer trustworthy, not to supply one now.

---

**A note on the `B*` references below.** They name items of the data-capture
plan this investigation produced, all of which are now implemented: B1 arrival
stamping and backlog measurement, B2 the feed-health ledger, B3 signal context,
B4 entry/exit context and book age per fill, B5 time-based path thinning, B6
market settlement results, B7 corrected score classification, B8 the four
commits moved off the event loop, B9 documentation. Each has its own entry in
[`ENGINEERING_CHANGE_LOG.md`](ENGINEERING_CHANGE_LOG.md).

## Part A. Findings (evidence first)

### A1. What the bot is actually doing

- Only Gate A has ever traded: 75 closed trades, net **-$836.90** (gross -$400, fees **$437** = half the loss), 40% win, 66/75 exits are the 180 s timeout, 7 targets, 2 settlements. The price-only sleeve has **never** filled (0 trades) after 209 evaluations.
- Gate A funnel (all time): 1,150 unconfirmed, 361 confirmed (75 filled, 223 `rejected_cap`, 9 `rejected_floor`, 26 `no_book`, 25 `unsupported_fee`, 3 killed, 7 lockout, 3 `confirmed_late`). 76% of qualifying sweeps never confirm.
- Sleeve refusals: 119 `outside_window` (old code), 32 `clock_not_live` (old code), **46 `clock_stale`** (still the dominant refusal after the 10 s bound), 4 `pre_88`, 3 missing, 2 half-time, 1 first-half, 1 incomplete book, 1 not-rising. The sleeve's own triplet thresholds have still never been exercised on live data.
- The candidate is usually the **second** leg to move: sibling confirmation lags are mostly negative (-1 to -41 ms).
- Sub-threshold capture is working (870 rows); `dl` is the binding floor (331 of 385 near misses fail on `dl`), and accepted sweeps sit right at the floor (unconfirmed `dl` p50 0.87 vs 0.8 minimum).

### A2. The underlying idea: where the edge actually is (supported by evidence, small n)

Since the score observer went live (Aug 30), every trade can be labelled by whether a real goal was observed by the Kalshi score feed within -30..+90 s of entry:

| Bucket (trades 52-96) | n | net | wins |
|---|---|---|---|
| goal observed within -30..+90 s | 28 | **+$378** | 16 (57%) |
| goal 90-300 s after | 3 | +$143 | 3 |
| goal exists in match but far away | 14 | **-$615** | 1 (7%) |
| pre-observer trades (22-51, unlabelable) | 30 | -$742 | 10 |

On the newest 500 signals the same split appears in the forward paths (held-side best bid 5 min after the signal): near a goal, last-first **+14c p50** (unconfirmed, n=16), **+20c** (stale-sleeve, n=4); with no goal, **0c** (n=54, n=14). Sibling confirmation does **not** enrich for goals: 19-27% of sweeps are goal-related in every outcome bucket (subthreshold 19%, unconfirmed 23%, confirmed 27%).

Interpretation (plausible, not proven): the tradeable phenomenon is **post-goal continuation over minutes**, not a hundreds-of-milliseconds microstructure gap. Trades that entered 8-26 s late on real goals still won (91: +$43, 92: +$93). The 75-80% of sweeps that are not goals (arb hits, thin end-of-match books, position squaring) have zero drift and just pay spread + fees. The Betis-Madrid equalizer (trade 93) was a VAR disallowal recorded by the feed 2.7 min later; trades 94/95 were a second false equalizer wave the score feed never recorded at all.

By provider minute (newest 500): minute 80-89 signals: 36% goal-related, +11c fwd; **minute 90+: 16% goal-related, -2c fwd** (settlement-convergence sweeps). The late-game thesis holds for 80-89 and fails for 90+.

Time-resolved forward paths (93 non-duplicate signals with paths, newest window) show *where* the edge is in time. Held-side bid change from the first post-signal sample, p50 (share ≥ +5c):

| horizon | goal-related (n=20) | not goal-related (n=73) |
|---|---|---|
| +5 s | +1c (25%) | 0c (7%) |
| +15 s | +3c (45%) | 0c (10%) |
| +30 s | +9c (55%) | 0c (17%) |
| +60 s | +12c (65%) | 0c (18%) |
| +120 s | +16c (64%, n=14) | 0c (22%, n=64) |

- A **persistence check** separates them cheaply: signals still ≥ +6c at +15 s are 64% goal-related (9/14) against a 22% base rate, and still drift +5c p50 over the following 105 s; ≥ +3c at 15 s gives 45% (10/22).
- **Score-feed-follow is not viable**: from the feed's own goal observation (12-79 s after the sweep) the bid moves only +3c p50 in the next 60-120 s (n=17), about spread plus fees. The market completes most of the repricing within 15-60 s of the sweep.
- By provider minute, goal signals at 80-89 drift +16..+38c by 120 s (n=11); at 90-94 only +4c (n=7), while non-goal 90-94 signals drift -3c (n=35).
- Coverage caveat: 40 of 114 paths hit the 4,000-sample cap and 58 span less than 250 s, so horizons beyond 120 s are thinly observed exactly in the hot markets (see B5).

Nothing in the current signature separates goal sweeps from the rest (newest 500, all recorded bursts grouped into 329 episodes within 3 s exchange time):
- legs moved in the episode: 1 leg 13% goal-related (n=264), 2 legs 27% (n=45), 3 legs 25% (n=20). Gate A confirms mostly 2-3 leg episodes, so confirmation buys a 2x enrichment at most, not a goal detector.
- episode max `dl`: flat 11-16% goal-related from 0.25 to 1.0; the high-`dl` bins are dominated by 1c-5c longshot legs (log-odds exaggerates cheap legs).
- widening the sibling window to ±250 ms / ±1 s / ±3 s / ±10 s exchange time confirms 5 / 12 / 22 / 35 of the 71 unconfirmed sweeps, with **no** goal enrichment in the confirmed subset (0/5, 1/12, 4/22, 8/35 vs 14/71 overall). `CONF_MS` is not the reason 76% are unconfirmed; most sweeps are genuinely single-leg.
- The Betis-Madrid tape (minute 87-90) shows repeated coherent three-leg "goal-shaped" waves with no goal behind them (VAR check / second false equalizer). The market prints goal-shaped moves roughly 4x more often than goals occur.

### A3. Rules and thresholds

- `PRICE_FLOOR=35` (deployed Sep 4) is consistent with the ledger (sub-35c bucket -$929 on 27 trades); fixed dollar notional was the mechanism. Unchanged conclusion.
- Exit rule: with the 27 trades that have execution paths, exiting at 30 s would have cut -$163 to -$44 and at 60 s to -$96; a +8c target would have been worse (-$360). Peak comes at 43 s p50 but 8/27 never trade above entry (post-sweep spread). Sample too small to tune; entry selection is the bigger lever.
- `EVENT_MATCH_WINDOW_S=20` is too short: the score feed lands 10-40 s after the market moves (goal obs minus entry: +12..+50 s typical), so most real-goal trades show `no_nearby_same_match_event`.
- `late` flag / expected expiration is ~60 min after minute 90 for every match seen (p10/50/90 = 56/61/68 min). It is a useless timing feature; `LATE_ONLY` would never fire in play.

### A4. Execution and latency (bugs, not strategy)

> **Partly retracted by A7.** The symptoms below were measured correctly. The
> *attribution* -- that synchronous `store.ex()` commits on the event loop were
> the cause -- was not tested and turns out to be wrong: the frame path issues
> 0.0008 SQLite statements per frame. See A7 for what the constraint actually
> was.

- **Feed lag at signal** (local receipt minus exchange `ts_ms`, derivable from every signal row): calm 100-800 ms; during the Sep 4 evening (three hot matches) **p50 16 s, max 33 s** (20:55-21:05). Order-arrival on trades 91-95: 2.4, 8.7, 16.1, 10.9, **26.2 s**.
- The Kalshi WebSocket dropped **8 times in 15 minutes** (20:48-21:04) with `ConnectionClosedError` = keepalive timeout: the event loop could not answer pings for 20 s. Each drop is a blind window in the raw feed and the book.
- Stepwise degradation by deploy: arrival p50 0.5-0.8 s (Aug 27-29) → 2.6 s (Aug 30 dashboard) → 4.2 s (Sep 2 PR13) → 10.9 s (Sep 4). The score-poll HTTP time went from **35 ms p50 (Aug 30-Sep 1) to 1-22 s (Sep 2 onward)**: the instrumentation added since Aug 30 is what saturated the process.
- Concrete causes found in code: (1) `GoalLatencyObserver._record_provider_events` upserts **every already-seen significant event on every 250 ms poll** (SELECT+UPDATE+COMMIT each, under the writer lock, on worker threads) — O(events) fsyncs per poll; (2) `Engine._finalize_signal_path` inserts up to 4,000 rows + commit **synchronously on the event loop** inside the WebSocket handler (`_flush_signal_path` exists but is never called incrementally); (3) `store.ex()` commit-per-statement on the loop for every signal/latency/eventlog write (known, H3); (4) dashboard reads cost 1-2.5 s CPU each (`/api/signals` decorates 500 rows × all observations) and are polled every ~10 s per client, competing for the GIL; (5) `Detector.on_trade` rescans the 300 s trade deque on every print.
- **Capacity, measured from the raw feed** (Sep 4 20:00-22:00, 1.9 M frames: 94% book deltas, 6% trades): the process keeps up at ≤ ~30k frames/min (~500/s, lag 0.1-0.6 s p50) and falls behind at every minute above ~40k/min; the peak minute (20:54, 64.7k frames = 1,078/s, three hot matches) carries 4-28 s of backlog. The lag tracks the frame rate minute by minute and clears within a minute of the rate dropping, so this is throughput, not the network. Eight reconnects (`orderbook_snapshot seq 1` after 4-12 s silences) and 18 sequence gaps, several of 30-120 frames, sit inside the same minutes — Kalshi appears to drop frames for a slow consumer, and each gap invalidates every book and triggers a snapshot storm (up to 45 snapshots/min), adding load.
- **It is waiting, not computing.** Railway metrics over 48 h show CPU at 0.06 vCPU average and **0.51 vCPU maximum** (the worst minute, with an 8-vCPU limit) while the backlog reached 28 s. A CPU-bound loop would show a full core. The loop is blocked on synchronous I/O: `store.ex()` commits (fsync on network block storage) on the loop for every signal write, every 20th trade's `feed_lag` sample (4-5 fsyncs/s at peak), every new executable high on an open position, and every forward-path finalisation; plus `store._lock` (a blocking `threading.Lock`) held by worker threads doing the observer's per-poll upserts. Local micro-benchmarks confirm the pure-CPU costs are secondary: gzip level-9 recording 11.5 µs/frame, `Detector.on_trade` 33 µs/trade with a quiet market and 813 µs/trade with a 9,000-trade 300 s window (a 20x cheap win, but not the bottleneck).
- **Paper fills are taken from the delayed book.** The desk fills against the shadow book as of the last *processed* frame; with a 16 s backlog, trade 93's TIE fill at 55.9 (local 20:54:15) is the price the exchange showed at ~20:53:59, while the live TIE was already 63-67. During backlog the ledger is therefore *optimistic*, not conservative, and `order_arrival_ms` does not describe the book the fill used. Entry (and exit) records need the exchange time of the book they were filled against (B4).
- Consequences: the clock poll runs at **5.7 s p50 / 30 s max** instead of 250 ms, so `MATCH_CLOCK_MAX_AGE_MS=10 s` still refuses most sleeve candidates (`clock_stale` ages 12-143 s during La Liga); event-log lines are written 8-22 s after their observation; `paper_entry_ms` p50 730 ms against a configured 150 ms; scheduler lag up to 1.3 s. Note the stamp's `age_ms` is measured against the signal's *processing* time, which was itself 16-33 s late that evening, so a good part of the "stale clock" refusals are the feed backlog wearing a clock label. Twenty-three stamps even have negative age (`clock_future`): the clock row was written after the sweep's delayed processing time.

### A5. Data feed and dataset trustworthiness

- **Local timestamps are processing times, not arrival times.** During backlog they drift by seconds; `lt`/`lm` in the raw feed and `local_ts` on signals inherit that. Trades carry exchange `ts_ms` (reliable); Kalshi book deltas carry an optional `ts_ms` (docs confirmed) which the recorder keeps but nothing uses; snapshots carry none. A replay ordered on `lm` reproduces the bot's delayed view, not the market.
- **Score observer mislabels second-half kickoffs as goals**: `classify_score_change` diffs the whole numeric signature, so a new `period_scores.1.*` key reads as +2 → "goal". 199 of 476 "goal" rows have `side=unknown` and an unchanged score. `status.goals` and any goal-rate statistic are inflated ~2x unless filtered to `side in (home, away)`.
- Score feed latency: provider `occurence_ts` is 10-40 s before first observation (p50 15 s); goal observations arrive 12-50 s after the bot's own entry; polls up to 30 s apart. It missed the second Betis-Madrid false-equalizer entirely. Fine as a label, useless as a trigger.
- Forward/execution paths hit the **4,000-sample cap in hot markets** (samples=3999 for every La Liga trade/signal), so the 300 s window collapses to ~60-130 s exactly where activity is highest. Truncation is flagged, but the tail is gone.
- Book at entry is persisted (K1 passes, 71/71); book at **exit** is not (only fills). No market **settlement result** is persisted anywhere queryable (positions are settled, the market outcome is not stored).
- 148 sequence gaps logged (Aug 30-31 alone); each invalidates **all** books until re-snapshot → 26 `no_book` and sleeve `incomplete_book` refusals. 69,122 "foreign" frames dropped (never recorded; source unexplained).
- **Raw feed inventory**: 176 hourly segments, 2.94 GB, Aug 25 18:00 → Sep 5. The three largest hours (Aug 27 17-19, ~100-118 MB each, vs 8-30 MB for a busy soccer hour) are the all-market firehose era before the Aug 27 22:14 filter fix; those segments contain every Kalshi market and must be excluded or filtered by ticker in any replay. Signals from that era were already purged.
- Provenance: `config_id` is NULL on trades 22-82 (pre Sep 3), `630d7b0f` on 83-90, `8286c2bb` on 91-96; trade 22 has `signal_id=0`; trades before 70 have no `mfe_c`; `feed_lag` samples are every 20th trade (biased to calm periods). Three eras cannot be pooled; the change log already says so.
- Event-log timestamps are unreliable by 8-22 s under load; `eventlog` was purged on Aug 30 (starts 08-30 22:38).
- **Duplicated episodes**: in `parallel` mode every confirmed episode writes two signal rows (one `gate_a`, one `price_only_late_score`) with different ids, identical trigger fields and *two* forward watches. Nothing keys them together; a naive count of "confirmed signals" or of forward paths double-counts, and the watch cost is doubled. (22 gate_a + 22 sleeve rows for 22 episodes in the newest 500.)
- **K1 verifies internal consistency, not realism**: the "arrival book" is the shadow book at processing time + paper latency; when processing is already 16 s behind the exchange, the fill is checked against a book 16 s after the sweep. K1 = PASS says the fill math is right, not that the fill was available when the signal happened.

### A5b. Operational risk found on the way: the volume is full

> **Resolved 2026-09-06.** Every sealed segment is uploaded to Cloudflare R2
> and verified there before anything is deleted locally; retention was then
> released (48 h local, 512 MB free floor). 164 segments pruned, volume 4.98 GB
> -> 2.31 GB, 3.54 GB of raw feed still held remotely. Nothing was deleted that
> had not been re-read back from R2 first.

An audit-scope export requested during this investigation failed with `study_export: database or disk is full` (SQLite `SQLITE_FULL` while writing the snapshot copy). Railway reports **4.00 GB used on the volume** (max 4.08 GB in the last 48 h; the volume's size is not exposed to this session). Raw segments account for 2.94 GB, so the database is roughly 1 GB, growing with up to 4,000 path rows per signal and ~12 KB of JSON per goal observation; a snapshot copy no longer fits. The raw recorder still reports healthy writes, so the volume is not completely full yet, but at the current ~300 MB/day the next stage is silent write failures on the study database within days. This needs an operator action before anything else: check the volume size in Railway, enlarge it or delete the firehose-era segments (Aug 25 18:00 - Aug 27 22:00, ~330 MB) and any stale `exports/` leftovers. B5 (path thinning) is the main brake on database growth.

### A5c. Raw L2 replay of the Sep 4 evening: the recording cannot price a fill (added after the study)

The two recorded hours were replayed properly: books rebuilt from snapshots and deltas in exchange time, the detector re-run at the deployed thresholds, entries and exits priced by walking real depth with Kalshi fees both sides. 78 Gate-A candidates, of which only 29 tradeable (42 refused by the 58c cap). Full write-up in the scratchpad at `study/FINDINGS.md`.

**The decisive test.** A print with `taker_side=yes` at P proves a resting NO bid at 100-P. Against the cheapest same-side aggressive print in the next 2 s, the reconstructed best ask was worse for **29 of 29 candidates, median +12c, max +36c**. One book held a single YES bid level with 86,956 contracts; another said the cheapest YES ask was 34c while takers bought YES at 13-16c in the same millisecond. Cause: books are long delta chains from snapshots up to 500 s old, and this window has 18 book gaps, 17 trade gaps and 8 reconnects, with frames lost during disconnects simply absent and unmarked.

**Consequence.** The entry assumption dominates the study: the same 29 trades and the same exit are -$862.96 at $100-notional ladder walk, -$228.72 at best-ask/100 contracts, and **+$550.33** priced at the post-sweep executed print VWAP. No absolute claim about Gate A's profitability can be made from this window, in either direction. This retrospectively weakens every P&L figure in A1-A3 that came from paper fills taken during backlog.

**What survives, because both arms carry the same bias:**
- **Sizing, not entry price, is what makes cheap legs lethal.** Same trades, only the sizing rule changing: below 35c, -$726.07 at fixed dollars versus **-$9.90 at fixed 100 contracts**; above 35c, -$136.88 versus -$209.65. Median position under fixed dollars is 303 contracts (max 1,192). `PRICE_FLOOR=35` is treating a sizing defect as a price defect, and pays for it by discarding half the sample.
- **Sibling confirmation selects the worse half**: confirmed n=8 mean -$50.54 at 12% wins, unconfirmed n=21 mean -$21.84 at 29%. Independent of the live-data finding that confirmation does not enrich for goals, and pointing the same way.
- **The loss is broad**: removing Betis-Madrid makes the baseline worse (-$639 from 12 trades elsewhere, Peru alone -$370 from 5).
- **The persistence gate does not survive its own cost**: charged the 15 s of entry price it needs, kept trades are -$29.67 mean (n=4) against +$8.11 if you pretend to enter at the sweep, and the gate is largely a price filter in disguise (kept median entry 50.1c vs discarded 14.1c). n=2-4, so not a verdict, but hypothesis C1 is not confirmed.
- **Not credible yet**: fading the sweep near market determination shows +$1,090 (n=19, 58% wins). It is the largest positive in the study and the arm most flattered by a too-expensive ask. Test it properly on a clean recording; do not act on it.

### A5d. "Liga MX and MLS have no clock" was wrong (2026-09-06, corrected same day)

34% of the newest 400 signals refused with `clock_unmapped`, and all of them
were Liga MX (79) and MLS (57) -- 100% of both leagues, while every other
competition mapped cleanly. Read as a provider-coverage gap in those two
competitions. It is not.

Kalshi returns a milestone for every one of those events. The Liga MX fixture's
milestone carries `start_date 2026-09-06T03:00:00Z` and `last_updated_ts
2026-09-06T05:27:27Z` -- the provider was actively updating it inside the window
the signals were refused in.

What the 136 signals actually share is a 30-minute window, **05:16-05:45 UTC**,
across three events. Every league that mapped traded later in the day. That
window is on the pre-fix build, before the arrival queue was bounded: the
morning of `order_arrival_ms` p95 at 38 minutes. The mapping task -- one
sequential REST call per unmapped event -- did not run. Liga MX and MLS are the
competitions whose kick-offs land in the early-UTC hours, which is when the bot
was at its worst.

**The lesson is about the instrument, not the leagues.** `if not choices:
continue` recorded nothing, so "never attempted", "attempted, provider had
nothing" and "attempted, task never ran" were indistinguishable, and the
dashboard's "Mapped to live clock: 0" invited the league-shaped reading.
Counters now separate the three cases (CHG-2026-09-06-008). The falsifiable
prediction: both leagues map normally the next time they play on the fixed
build.

### A6. Signature completeness per record type (what is missing to analyse honestly)

| Record | Present | Missing |
|---|---|---|
| Signal (all outcomes) | dl/levels/size/ref/ext/dir, conf lag, clock stamp, config_id, forward path (5 min, capped), sleeve triplet features (sleeve rows only) | full 3-leg book state at decision (best bid/ask/size), candidate spread, fillable depth to cap (VWAP/qty), **feed lag and queue backlog at that moment**, sibling burst evidence (which sibling, dl, lag, sign; also for `unconfirmed`), pre-signal price history, load state (open watches/positions) |
| Trade entry | arrival book + fill levels + fees, latency fields in `detail` | exchange ts of the candidate and backlog at fill; spread at fill |
| Trade exit | fills, reason, path summary | **book at exit decision and at fill (both sides)**, trigger values behind the reason (elapsed, bid vs target, etc.) |
| Blocks (`rejected_cap/floor`, `no_book`, lockout) | outcome + forward path | same signal context gaps; for `no_book`: why the book was invalid (gap id, time since invalidation) |
| Feed health | eventlog text lines only | structured connect/disconnect/gap/resubscribe/snapshot-complete markers in DB **and in the raw stream** |
| Market outcome | none | settlement result, settled ts, final prices |
| Goal/score | raw payload retained | correct goal classification; per-poll sequence |

---

### A7. What actually limited the feed (measured 2026-09-06, after deploying B1-B9)

A4 said the process "is waiting, not computing" and attributed it to synchronous
`store.ex()` commits on the loop. Deploying the platform work made the real
answer measurable, and **A4's attribution was wrong**.

**The unbounded queue was the first defect, and it was mine.** B1 specified the
arrival queue as unbounded so its depth would measure the backlog. That removed
the socket's own backpressure -- the thing that had been converting a slow
consumer into a loud failure (Kalshi hangs up on a backed-up socket). Result on
2026-09-05: backlog 896,017 frames, `order_arrival_ms` p95 38 minutes, 3.77 GB
held at 0.12 of 8 vCPU, and trades 110-114 "entering" against book state 41-79
minutes old on finished matches, then booking the settlement queued behind them.
About +$281 of fabricated paper profit. Bounded on 2026-09-06 (drop-oldest,
every discard ledgered, holed books invalidated through the sequence-gap path).

**The consumer was capped at 16 frames per event-loop turn.**
`CONSUMER_YIELD_EVERY = 16` yielded on a frame count, so throughput was 16
frames per loop turn however cheap a frame was and however idle the CPU. At the
measured scheduler lag (p50 19 ms, p95 165 ms) that is 97-800 frames/s. Replaced
with a 5 ms time slice. Verified against the old code: it measures exactly
`[16, 16, 16, 12]` frames per turn.

**Recovery was the largest single load on the process it was recovering.** The
bound's own drops read as unknown sequence gaps; each gap re-requested every
market's snapshot, and `request_snapshot` fired once per rejected delta,
unbounded. 161 gaps and 220 snapshot requests in eight minutes, 36 markets each,
feeding the queue whose overflow was manufacturing the gaps. Now: one request
per market per episode, and a gap already being recovered is recorded without
re-requesting.

Effect: 676,093 dropped frames and 55 forced reconnects became 1,463 and 0; a
14,666-frame backlog cleared to 188 in 46 s.

**The frame path costs 2.6% of wall clock.** Per-stage timing on the live
process, 97,257 frames over 372 s: `record` 47.1 us, `on_book` 31.9 us,
`process_trade` 200.8 us, `book_apply` 12.2 us, `observe` 2.6 us -- **101 us per
frame, 9.6 s of work in 372 s**, against 66 us/frame measured locally on the
bot's own recorded frames. The consumer is starved, not slow.

**H3 is not the throughput constraint.** Driving 120,000 real recorded frames
through `handle_ws` issues **96 SQLite statements in total -- 0.0008 per frame,
0.8% of the run**. Commit-per-statement traffic on the loop is a *tail* problem,
not a throughput one: `process_trade`'s 118 ms max is a `record_signal` write on
the network volume, and a stall like that costs the consumer a whole turn.

**First readings after the fixes (2026-09-06 evening, 33 markets).** `status()`
fell from 104 ms to ~0.5 ms per call, confirming the snapshot. `periodic` totals
0.26% of wall clock. Sub-staging the periodic tick names the owner of its tail:
`periodic.subthreshold` reached a **295 ms max** and `periodic.timeouts` 104 ms,
both phases that write to SQLite on the loop. Their totals are trivial (460 ms
and 333 ms over 298 s) so this is a tail, not a throughput cost -- but a 295 ms
stall is 295 ms in which the process cannot see the market at all, which is the
same harm the queue backlog does by a different route. Not yet fixed.

**The consumer is starved, measured over a window that qualifies.** A cumulative
`consume_share` since boot cannot answer this -- it blends the idle warm-up with
the busy period, and a deep queue at one sampling instant does not mean the queue
was deep across the averaging window. Taken as a delta instead, over 56 s with
queue depth 1,761 at the start and 1,944 at the end, so backlogged throughout:
the consumer worked **2,575 ms of 56,000 -- 4.6%** -- while processing 343
frames/s, with Railway CPU at **0.246 of 8 vCPU**. Its own frame stages account
for essentially all of that 4.6%. So the coroutine is not doing something
expensive and is not competing for CPU; it is not being run. What the loop does
instead is the open question, and `consume_slices` (slices per second, and work
per slice) is the instrument that distinguishes "scheduled rarely" from "queue
oscillating to empty between samples".

**Starved, not saturated — settled 2026-09-06 20:12 UTC.** 61 s window, 54
markets, queue growing 10 -> 4,585, 376 frames/s:

| | |
|---|---|
| consumer working time | 3,221 ms = **5.28%** of wall clock |
| its own frame stages | 2,507 ms = 4.11% |
| everything else measured (`periodic` + `status`) | 40 ms = **0.07%** |
| slices | 667 = 10.9/s |
| **work per slice** | **4.83 ms against a 5 ms budget** |

The per-slice figure is what decides it. In the quiet window an hour earlier it
was 1.91 ms: slices ended early because the queue emptied. Under load they run
to the full budget, so the consumer is doing everything it is permitted to do
and is handed the loop only 10.9 times a second -- a turnaround of ~92 ms.
10.9 x 4.83 ms = 5.26%, which is the measured share. It is budget-limited, not
work-limited.

**And the loop is not visibly doing anything else.** `periodic` cost 28.8 ms
over the window (0.05%) and `status` 11.5 ms (0.02%); their alarming maxima --
`periodic.subthreshold` 374 ms, `status` 558 ms -- are cumulative since boot,
from the earlier overload, and do not recur in this window. No dashboard was
open: `/api/perf` shows 2 requests in 180 s totalling 10 ms, so response
handling and serialisation are excluded. Railway CPU was 0.246 of 8 vCPU.

So ~95% of wall clock is in nothing instrumented, while ~0.2 of a core is being
burned somewhere. The untimed work on the loop is the **reader** -- `_read`'s
`async for raw in ws`, which decrypts and decodes every one of those 22,910
frames -- and `_handle_raw`'s `json.loads` and sequence gate, which sit inside
`consume_ns` and so cannot explain a *low* share. Instrumenting the reader the
same way is the next measurement. It has not been done, and no fix should be
attempted before it is: the three wrong diagnoses in this document were each
produced by reasoning one step past the last measurement.

**Open.** What owns the remaining loop time. `Engine.status()` was one
identified consumer -- 18 queries on the loop, 104 ms per call, from the 5 s
broadcast and every `/api/status` poll and WebSocket hello -- now moved to a
snapshot refreshed through `store.read`. `periodic_task` and `status()` are
instrumented as stages so the next reading names its own share. Do not attribute
the remainder without that reading; this section exists because the last
attribution was made without one.

## Part C. Strategy hypotheses worth testing (not conclusions)

1. **Goal-follow with a persistence gate** — *downgraded by the L2 study (A5c)*: the gate looked strong on live forward paths (22% goal base rate becoming 45-64%) but did not survive being charged the 15 s of entry price it costs, and behaves largely as a price filter. Keep it as a hypothesis, test it on a clean recording, and do not build on it yet.
2. **Score-feed-follow**: enter when the Kalshi score feed reports a goal. **Already falsified on the live sample** (+3c p50 left after the feed report, n=17); keep the feed as a label only.
3. **Minute filter**: drop minute 90+ (16% goal-related, negative drift); keep 75-89. Needs the clock to be fresh, which needs B8.
4. **Drop sibling confirmation as an admission rule**; use it only as a feature. It costs 76% of episodes and does not select goals.
5. **Sizing: fixed contracts, not fixed dollars — now the best-supported change in the study.** The L2 replay isolates it from entry price (A5c): the cheap bucket goes from -$726 to -$9.90 under fixed contracts with everything else held constant. This also means `PRICE_FLOOR=35` should be re-examined rather than kept, since it discards half the sample to avoid a loss the sizing rule causes.
6. **Fade the late sweep** (new, from A5c, not credible yet): sweeps near market determination revert. Largest positive in the study and the one most exposed to the study's entry bias. Test on a clean recording before believing it.
7. **VAR/second-wave defence**: after entry, a reversal of more than X% of the sweep within 60-120 s exits (93/94/95 all gave clear reversal signatures before the timeout).
8. **Fade minute-90+ sweeps** (speculative): thin books converging to settlement; needs settlement results (B6) to evaluate.

## Part D. Prioritised experiments, now that the capture work is deployed
1. Re-measure latency and disconnects on one busy evening (kills or keeps the whole platform).
2. Goal-conditioned forward-return study on all signals with corrected goal labels and contexts (idea validation).
3. Sleeve threshold study on fresh clocks (first ever live observations).
4. Counterfactual exit/sizing study on execution paths with exit contexts.
5. **Replay harness, in-repo, reusing the live code** (next PR after B). It must carry the book-correctness test from A5c as a **gate, not a diagnostic**: for every candidate, compare the reconstructed best ask against the cheapest same-side aggressive print in the following seconds, reject the candidate when the book is worse, and refuse to report P&L at all for any window where that test fails at a material rate. Without it a replay produces confident numbers from corrupted books, which is exactly what the first study did. Design: feed recorded `feed-*.jsonl.gz` frames into `Engine.handle_ws` ordered by exchange `ts_ms` (deltas and trades carry it; snapshots inherit the previous frame's), with an injectable clock replacing `time.time()`/`time.monotonic()` in engine/paper/sleeve, an in-memory store, and the clock/score tables replayed from the export. The paper desk's IOC depth walk, quadratic fees and configurable latency are reused unchanged, so every variant tested is deployable as-is and live/backtest parity is by construction. Add a sweep runner over frozen episode inventories with the existing event-clustered CI. Excluded from replay: the firehose-era segments and any window inside a recorded disconnect (B2 markers).

### External frameworks and data: borrow, do not migrate (decision, with what was verified on 2026-09-05)
- **NautilusTrader**: mature event engine with L2 replay, latency models and backtest/live parity, but no Kalshi adapter. Kolberg's `prediction-market-backtesting` (1.2k stars, v4.1-alpha) states: *"Kalshi support depends on access to L2 historical book data. Current Kalshi components are research and fee-modeling plumbing, not a public runnable backtest path."* Its inputs are vendor Parquet (PMXT/Telonex) and Polymarket ledgers; ingesting a user's own WebSocket recording is not a documented path.
- **PredictionMarketBench** (Oddpool, 27 stars, 6 commits): Kalshi order-book snapshots + prints from Parquet/CSV, market/IOC/GTC/post-only orders, pro-rata queue position for resting orders, Kalshi fee schedule (7% taker, 1.75% maker); the agent is polled every 5 s by default and there is no latency model. Its maker queue model is the one idea worth borrowing when the maker-entry hypothesis is tested.
- **PMXT archive**: "free hourly snapshots" of order books (one snapshot per hour is not microstructure), Kalshi index currently empty and last reported May 2026; **Telonex**: Polymarket only. No third-party Kalshi L2 history exists for this study's period; the bot's own recording is the only L2 source, which is why B1/B2/B5 matter more than any engine choice. Kalshi's REST trade history (months, all soccer markets, no books) remains the free extra sample for price-path questions with conservative fill assumptions.
- **VectorBT PRO**: vectorised parameter sweeps, not an order-book simulator; irrelevant until there are thousands of episodes.
- Decision: no migration of live trading (the live defects are fixable in place; a Kalshi adapter would re-implement the WebSocket/book/sequence code that already exists). Build item 5 first. Re-open the Nautilus + Kolberg option only if (a) the strategy is to run on Polymarket too, where Telonex L2 history gives sample size, or (b) queue-position modelling and large sweeps become the binding need. The architectural lesson to adopt now is Nautilus's separation of data ingestion, strategy, execution simulation and persistence into separate components, which is exactly what B8 and the next platform PR move toward.

## Part E. Other observations, hypotheses and open questions found on the way

- **The observer perturbs the observed.** Every instrumentation layer added since Aug 30 (dashboard, clocks, provider ledger, paths) ran in the same single-threaded process as the trading loop and is measurably what pushed arrival latency from sub-second to tens of seconds. Any conclusion about "execution latency" drawn from Sep 2 onward is about the platform, not about Kalshi or the strategy.
- **Fees are half the loss** ($437 of $837): taker both ways at 3.5c/contract/side near 50c. A maker-style entry (resting bid on the beneficiary leg after a goal) would change the economics entirely; the fee model already supports `quadratic_with_maker_fees` series metadata but V2 never simulates resting orders. Worth a shadow "maker" ledger later.
- **Sequence gaps**: 148 single-message gaps logged (about one per 15 min on Aug 30-31). Each invalidates all ~80 books. Unknown whether Kalshi drops frames or increments `seq` on message types the client filters; the raw feed (with the B2 markers) can answer it. 69,122 "foreign" frames dropped in 6.5 h are also unexplained (possibly lifecycle frames for unsubscribed markets).
- **Second-mover bias**: confirmation lags are almost all negative, so Gate A buys the leg that moved second. Whether the second mover is the informed leg or the arbitrageur's leg is unknown and testable with B3's sibling evidence.
- **The 2,377-event backtest** claimed sibling confirmation lags of ~1 ms median and a +$1.28..+$5.76 net-per-fill CI; live confirmation lags are 1-41 ms and net-per-fill is -$11. Either the historical tape differed (older, simpler markets) or its fill assumptions did; the recorded arrival books now make the fill part checkable, the price-path part needs the replay engine.
- **K2 (event-clustered CI [-28.45, +6.35], n=361)** pools three configurations and two latency regimes; it is not a valid statement about any one of them.
- **Score feed as a label, not a trigger, is the right call** (10-40 s late, holes, false goals); but as a *research label* it is currently unusable until B7 fixes the classification and the association window grows to about 90 s.
- Small-n warning: every strategy-level number above comes from 45 labelled trades and ~115 labelled signals over four days; treat them as hypotheses with direction, not effect sizes.

