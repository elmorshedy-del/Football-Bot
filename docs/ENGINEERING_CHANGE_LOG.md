# Engineering Change Log

Reverse-chronological. Newest day first, newest change first within a day.

The required entry format, and the rule that every code change appends here, is
in [`AGENTS.md`](../AGENTS.md#engineering-change-log). Do not rewrite past
entries; correct them with a dated follow-up entry instead.

---

## 2026-09-04

**Branch:** `claude/strategy-optimization-backtest-wd2j7z` (restarted from `main`
after PR #17 merged as `1089af7`)
**Deployment:** PR #17 live since 2026-09-03 12:01Z, `config_id`
`630d7b0f702f23b1`, 23 h uptime at time of check.

Entries `-001` to `-003` are observation only: a post-deploy verification and a
research freeze, recorded because both carry findings that must not be lost.
They were written before any code changed today and are left below in the order
they were written. Entries `-004` to `-006`, which follow immediately and do
change code, are ordered newest first per `AGENTS.md`.

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
