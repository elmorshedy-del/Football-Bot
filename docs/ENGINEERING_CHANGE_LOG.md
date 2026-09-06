# Engineering Change Log

Reverse-chronological. Newest day first, newest change first within a day.

The required entry format, and the rule that every code change appends here, is
in [`AGENTS.md`](../AGENTS.md#engineering-change-log). Do not rewrite past
entries; correct them with a dated follow-up entry instead.

---

## 2026-09-06

**Branch:** `claude/football-bot-analysis-rxz1vz`
**Base commit:** `eb9c88a` (merge of the bounded-queue and settlement-wording
work into `main`).
**Deployment status:** the 2026-09-05 work IS now deployed; this section's
entries are not, unless an entry says otherwise.

### CHG-2026-09-06-001 — Show every measured number against the bound it is supposed to respect

**Commit:** this change
**Components:** `app/store.py` (`expectations`, `_expectation_specs`,
`_expectation_verdict`), `app/engine.py` (`status()`), `static/index.html`,
`static/app.js` (`renderExpectations`), `static/style.css`,
`tests/test_expectations.py`, `tests/test_frontend_contract.py`,
`tests/test_dashboard_browser.py`, `tests/test_pr13_browser_followup.py`

**Observed / original behaviour.** The operator asked to be able to see, without
reading a ledger, whether latency and the other numbers the bot reports are
"following intended or exceeding". They could not. The dashboard's latency table
prints n / p50 / p95 / max / age and a `Threshold` column that is populated for
exactly one kind — `order_arrival_ms`, from kill condition K4 — because
`latency_kind_summary` only ever attaches that one threshold. Every other row
showed a number with nothing to judge it against. `match_clock_age_ms` sat at a
p50 of 18.4 s beside a configured `MATCH_CLOCK_MAX_AGE_MS` of 10 s, which is the
knob that refuses sleeve candidates, and nothing on the page connected the two.

**Root cause.** No layer paired a measurement with its declared bound. The
obvious fix — passing more thresholds into `latency_kind_summary` — would have
been wrong: `Engine.status` reads that function's `state` for K4, so a bound
added for reporting would silently have become one that can stop the bot
trading. A slow score feed would have raised BREACH, which no kill condition
ever said it should.

**Change.** A separate `store.expectations()` that pairs each number with its
bound and returns a verdict, and a dashboard panel that renders it. It is
observation only: it consumes the readiness dict `status()` has already
computed, performs no write, and feeds no kill condition, health check or
trading decision.

A bound appears only where something already declares one, resolved from the
running configuration on each call rather than copied:

| number | bound | declared by |
|---|---|---|
| `order_arrival_ms` p95 | 250 ms | kill condition K4 |
| `paper_entry_ms` p50 | `PAPER_ENTRY_LATENCY_MS` | the delay the desk simulates |
| `match_clock_age_ms` p50 | `MATCH_CLOCK_MAX_AGE_MS` | above it the sleeve refuses |
| `match_response_ms` p95 | `GOAL_LATENCY_POLL_MS` | the cadence it must sustain |
| `backlog_frames` p95 | `ws_queue_stall_depth()` | where the stall guard reconnects |
| `queue_dropped_total`, `feed_event_failures`, `archive_failures`, `recorder_failures` | 0 | a non-zero value is lost data, not slow data |

`feed_ingress_ms`, `decision_ms`, `paper_exit_ms` and `scheduler_lag_ms` have no
declared bound anywhere in the system. They are reported as `UNBOUNDED` and
listed under `unbounded`, rather than being given an invented number that would
read as design intent. Which numbers nobody ever declared an intent for is
itself worth seeing.

A measurement that is stale or still collecting reads `STALE` / `NO DATA`, never
`WITHIN`: the 2026-09-05 stall left `order_arrival_ms` at a p95 of 38 minutes
and then stale, and reporting that as passing would be worse than reporting
nothing.

**Verification.** 13 tests in `tests/test_expectations.py`, including that the
bounds track the config rather than duplicating it, that a stale or collecting
measurement never reads as passing, and — the invariant that makes this safe —
that `latency_kind_summary` still attaches a threshold to `order_arrival_ms`
alone, so K4 is unchanged. Two real-browser tests assert the panel names what is
exceeding (worst first) and fits a 360 px viewport, and the contract test asserts
the browser never hardcodes a bound of its own. Full gate: 604 tests OK,
`compileall`, `ruff`, `node --check`, `git diff --check`.

---

## 2026-09-05

**Branch:** `claude/football-bot-analysis-rxz1vz`
**Base commit:** `c398635` (merge of PR #18)
**Commits:** platform pass `6cffcf8`, `c59010b`, `cddbaee`, plus the
documentation commit that adds this section; R2 archive pass `aac510f`,
`88dcf17`, plus the documentation commit that adds CHG-2026-09-05-007/008.
**Diff totals:** platform pass 22 files, +1,952 / -119 (this change-log section
itself excluded): `app/` +886 / -111 across 10 files, `tests/` +1,022 / -7
across 9 files, `.env.example` +8, `README.md` +36 / -1. R2 archive pass
13 files, +2,362 / -20: `app/` +1,245 / -19 across 7 files (of which
`app/archive.py` is 837 new lines), `tests/` +916 / -1 across 3 files (of which
`tests/test_raw_archive.py` is 852 new lines), `scripts/r2_probe.py` +104,
`.env.example` +31, `README.md` +66.
**Suite:** 394 tests OK before the platform pass (32.0 s), 425 after (40.1 s),
459 after the R2 archive pass (52.1 s), 504 after the capture pass, under
`python -X dev -W error::RuntimeWarning -m unittest discover -s tests`.
**Deployment status:** NOT DEPLOYED. Nothing here has run in production; every
number quoted as production evidence was measured on the deployed 2026-09-04/05
build, not on this one.

The day carries three passes of the 2026-09-05 data-capture plan, developed
concurrently and merged here:

- **platform pass** (plan B1, B2, B8a/b/d/e), entries `-001` to `-006`, commits
  `6cffcf8`, `c59010b`, `cddbaee`, `b7fe0fc`;
- **R2 archive pass** (raw-feed continuity storage), entries `-007` and `-008`,
  commits `aac510f`, `88dcf17`, `11e0fbc`;
- **capture pass** (plan B3-B7, B9), entries `-009` to `-020`, commits
  `b59cece`, `b5e0470`, `d743a17`, `a900e6d`, `e6f4c93`.

**Added after the merge: the incident pass**, entries `-021` and `-022`,
commits `0ef21dd` and `45d1b08`, on the same branch with base commit `17ed463`
(the merge of the three passes above). It is not part of the data-capture plan:
it fixes two defects the deployed 2026-09-05 build demonstrated in production
on the day. Diff totals 13 files, +1,431 / -27 excluding this section:
`app/` +466 / -22 across 5 files, `static/` +84 / -5 across 2 files,
`tests/` +875 across 5 files (of which `tests/test_ws_queue_bounds.py` is 556
new lines), `.env.example` +33. Suite 538 tests OK before this pass, 562 after
`0ef21dd`, 580 after `45d1b08`, under the same command as above. Still NOT
DEPLOYED.

**Configuration identity, incident pass.** `strategy_params()` is byte-identical
before and after: the JSON of the whole parameter set compares equal, and the
new `WS_QUEUE_*` settings are deliberately not in `STRATEGY_PARAM_NAMES`. But
`config.py` and `engine.py` are strategy sources, so `CODE_FINGERPRINT` moves
(`5bfd89de8bf6` -> `e2d93ec7f4df` in this environment) and with it `config_id`
(`04284893a0ab0dcf` -> `af5bdf160217cea5`). Rows written after this deploys will
not pool with earlier rows in a current-configuration aggregate. That is the
provenance stamp working as designed, not a decision change.

**Numbering correction, recorded rather than hidden.** The archive and capture
passes were written in parallel against the same base and both claimed
`-007`/`-008`. The archive pass had already been pushed and its entry numbers
cited, so the capture entries were renumbered `+2` (`-007`…`-018` became
`-009`…`-020`) when the two were merged. Commit `e6f4c93`'s message therefore
names the pre-merge range `-007` through `-018`; the entries it added are the
ones now numbered `-009` through `-020`. No entry was rewritten, only renumbered.

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

### CHG-2026-09-05-023 — Disclose an overflowing market once, not once per frame

**Commit:** this change
**Components:** `app/kalshi.py` (`_note_dropped`, `_recover_dropped_books`),
`tests/test_ws_queue_bounds.py`

**Observed / original behaviour.** Found in review of CHG-2026-09-05-021 before
that change was deployed; never ran in production.

The bounded queue rate-limits its overflow summary to one per
`WS_QUEUE_OVERFLOW_REPORT_S`, with one deliberate exception: a market seen for
the first time in an overflow episode is disclosed **immediately**, because its
book has to be invalidated before the consumer can fill from it. "Seen for the
first time" was implemented as "not in `_overflow_markets`", and
`_overflow_markets` was only updated with the markets that were actually acted
on — the intersection of the dropped frames' markets with `_subscribed`.

Two kinds of frame are therefore never remembered, because there is nothing to
do about either:

1. an order-book frame for a market this process is not subscribed to (the feed
   delivers these in volume — 69,122 in 6.5 h on 2026-08-30/31);
2. an order-book frame whose `market_ticker` cannot be read, which is treated as
   though it could have been any market and blanket-invalidates all of them.

Every repeat of either read as newly affected, so each one forced its own
immediate disclosure: a `queue_overflow` ledger row, a raw-stream marker and a
dispatched `orderbook_gap` (which writes an event-log row) — **per dropped
frame**, synchronously, while the consumer is by definition already too slow.
Measured on the regression test: 39 emissions for 39 dropped frames where 2 are
correct. The bound would have converted a silent stall into a write storm timed
to arrive at the exact moment the process could least afford it.

**Root cause.** Conflating "newly affected" with "newly acted on". The set is a
seen-set, so it has to record what was seen.

**Change.** `_recover_dropped_books` records every market the episode was
observed to hole, not only the subscribed subset it could re-snapshot; and an
unreadable book frame counts as newly affecting only while some subscribed
market is still outside `_overflow_markets` — once everything is invalidated
there is nothing left for the next such frame to disclose.

**Invariants preserved.** A market with a book to invalidate is still disclosed
immediately and still re-snapshotted through the sequence-gap path; recovery
still clears a market from `_overflow_markets` when its fresh snapshot lands, so
a market holed again after recovering is invalidated again; the reader's
teardown flush still discloses the final window; no strategy parameter and no
Gate A behaviour is touched.

**Verification.** Three tests added to `tests/test_ws_queue_bounds.py`:
40 dropped frames for an unsubscribed market produce one immediate disclosure
plus the teardown summary (was 39 emissions, asserted against the pre-fix code);
40 unreadable book frames blanket-invalidate once, not 39 times; and a market
holed, re-snapshotted, then holed again is invalidated both times. Full gate:
583 tests OK, `compileall`, `ruff check --select E9,F63,F7,F82`,
`node --check static/app.js`, `git diff --check` all clean.

### CHG-2026-09-05-022 — Say which way round every trade was, and why a settlement paid

**Commit:** `45d1b08`
**Components:** `static/app.js`, `static/style.css`, `app/main.py`
(`/api/trades`, new `_market_settlements`), `tests/test_settlement_display.py`,
`tests/test_dashboard_browser.py`, `tests/test_frontend_contract.py`,
`tests/test_pr13_browser_followup.py`

**Observed / original behaviour.** Production trades 112, 113 and 114 of
2026-09-05 are all **correct**, and nothing in this entry changes settlement.
The match finished a draw. Trade 112 held YES on the Draw leg; trades 113 and
114 held NO on "Miami wins"; a draw resolves "Miami wins" as NO. All three were
therefore paid 100, for +$107.87, +$72.42 and +$96.14 net.

The card rendered the market name "Miami wins" beside an exit price of 100,
with the side held present only as a bare `yes`/`no` token in the raw-JSON
`<details>`. Read left to right it says "Miami wins ... 100". The operator who
designed this bot read his own ledger that way and concluded Miami had won a
match that was a draw. For an audit dashboard, a card that the author himself
misreads is a defect in the card.

**Root cause.** Two gaps, one presentational and one in the API.
Presentationally, `tradeCard` never stated the position in words: it printed
`display_contract` (which is always phrased as the YES claim, "X wins" or
"Draw") and left the reader to combine it mentally with a token they could not
see. In the API, `/api/trades` selected only from `trades`; `markets.result`
and `markets.settled_ts` exist (CHG-2026-09-05-016) but were never joined, so
the dashboard had nothing to explain a payout with even if it had wanted to.
This is a design gap, not a regression: the card never showed this.

**Why necessary.** The dashboard is the audit surface. A settlement row whose
plain reading inverts the fact it records is worse than no row, because it is
read confidently. It already produced one wrong conclusion by the person best
placed to catch it, on trades that were themselves correct.

**Exact change.** `/api/trades` now runs one extra query per page,
`_market_settlements`, and stamps `market_result`, `market_settled_ts` and
`market_status` from the parent `markets` row onto every trade. It is
deliberately not mode-scoped, because `markets` carries no capture mode: a
market is one exchange object observed in whatever mode was running. A ticker
with no row, or no stored result, yields nulls.

In `app.js`, `positionWording`/`positionCallout` render on every trade card and
open position: a short `.tag` chip carrying the side token, a
`.position-sentence` element carrying "Betting AGAINST: Inter Miami wins" or
"Betting ON: Draw", and the payout condition, "Pays 100 only if Inter Miami did
not win." The sentence is its own element precisely so the uppercase chip
styling can never be the only thing stating the direction.
`settlementBlock` renders only for `exit_reason == "settle"` and states
"Market resolved NO — Inter Miami did not win — NO pays 100." followed by "This
position held NO, so it was paid 100 per contract." `resolutionMeaning` turns a
`yes`/`no` result into a claim about the match, handling the Draw leg
separately ("the match was a draw" / "the match was not a draw"). A row whose
`market_result` is absent renders "Market resolution not recorded" and says the
payout is not explained rather than inferring a resolution from the side that
was paid. The position sentence and `resolved <result>` also join the card's
searchable text, so "against" is a usable filter. Cards carry `data-trade-id`.
CSS reuses the existing `.tag` palette; the sentence chips opt out of `.tag`'s
`white-space: nowrap` so they wrap rather than overflow at 360 px.

**Before / after.** Trade 113 (`KXMIA-MIA`, side `no`, exit 100, market resolved
`no`). Before: "Inter Miami wins · Major League Soccer · <date>", "$72.42",
"Market settlement", exit price 100 — nothing on the card contradicts "Miami
won". After, rendered in Chromium against the shipped page and the live server:
"Betting AGAINST: Inter Miami wins", "Pays 100 only if Inter Miami did not
win.", "Market resolved NO — Inter Miami did not win — NO pays 100.", "This
position held NO, so it was paid 100 per contract." The string "Inter Miami
won" does not occur anywhere on the card. Trade 112 (`KXMIA-TIE`, side `yes`,
resolved `yes`) reads "Betting ON: Draw" and "Market resolved YES — the match
was a draw — YES pays 100." A legacy row with no stored result reads "Market
resolution not recorded".

**Reasoning and trade-offs.** Rewriting `display_contract` per side (rendering
the NO leg as "Miami does not win") was considered and rejected: the contract
name is the exchange's, it is what the raw identifiers and the export show, and
silently renaming it in one place would break the correspondence an audit
depends on. Deriving the resolution from the payout (exit 100 implies the held
side won) was rejected outright — it is exactly the inference that must never
be made, and it would have printed a confident resolution for legacy rows that
have none. Adding the side to the compact featured story as a chip was replaced
by a plain sentence for the same reason the callout uses one: `.tag` is
uppercase-transformed and a styled token is not a statement.

**Validation.** `tests/test_settlement_display.py` (7 tests) asserts the API
exposes `market_result`/`market_settled_ts`/`market_status` for the real
draw-shaped fixture, that a legacy row reports null rather than an inference,
that a deleted `markets` row does not break the listing, and that the lookup is
ONE query for the whole page rather than one per trade. `test_frontend_contract`
gains three tests pinning the new literals, ids and CSS rules; its existing
`ALL SYSTEMS GOOD` assertions still pass. Four Playwright Chromium tests drive
the shipped page: a NO settled trade renders the "against" wording and the
resolution sentence and nowhere reads as the team having won; a YES-settled
Draw trade reads as betting on the draw; an unknown-result row says so; and the
settled cards produce 0 px of horizontal overflow at 360 px with no clipped leaf
element. Chromium runs in this environment
(`/opt/pw-browsers/chromium-1194/chrome-linux/chrome`, 141.0.7390.37), so the
browser assertion was added rather than skipped. Suite 580 tests OK. Also
verified against a running server on port 8731 in demo mode with a seeded
settled row: `/api/trades` returned `market_result: "no"` and the rendered card
showed the wording above with 0 page errors.

**Risks / limitations.** The resolution is only as good as `markets.result`, so
a settled trade whose market result was never captured shows "not recorded" —
correct, but it means older rows explain less than new ones, and this change
does not backfill them (nor should it). The wording assumes the YES leg is
phrased as a claim, which `_display_names` guarantees for soccer game markets
("X wins" / "Draw"); a series with a differently-phrased leg would still read
correctly as "Betting AGAINST: <leg>" but the negation ("X did not win") would
be less idiomatic. Nothing here validates that the stored result is right; it
reports what was stored.

**Follow-up.** None. Signals are not settled positions and were deliberately
left alone.

### CHG-2026-09-05-021 — Bound the arrival queue, and make what it discards explicit

**Commit:** `0ef21dd`
**Components:** `app/kalshi.py` (`KalshiWS`), `app/config.py` (`WS_QUEUE_*`),
`app/engine.py` (`handle_ws` gap branch, `status()`), `app/exporter.py`
(`_OBSERVABILITY_NAMES`), `.env.example`, `tests/test_ws_queue_bounds.py`

**Observed / original behaviour.** CHG-2026-09-05-001 split `KalshiWS.run` into
a reader that stamps arrival and pushes onto an `asyncio.Queue`, and a consumer
that parses and dispatches. That queue was unbounded on purpose, so its depth
would measure the backlog. In production on 2026-09-05 it measured a backlog
nothing then bounded.

The reader kept up perfectly: feed lag p50 63-148 ms throughout. The consumer
fell behind and never recovered. Backlog reached **896,017 frames** and
stabilised near **556,000**. `order_arrival_ms` reached **p95 2,280,666 ms (38
minutes)**, max **2,895,826 ms**. Memory peaked at **3.77 GB of an 8 GB limit**.
CPU averaged **0.12 of 8 vCPU**, so this was not compute saturation: the
consumer was I/O- or lock-bound.

The consequence is worse than lateness. Five real closed trades, with
`book_age_ms` (CHG-2026-09-05-012) proving it:

| trade | arrival delay | held | net |
|---|---|---|---|
| 112 (MIA/TIE yes) | 76.2 min | 25 s | +$107.87 |
| 113 (MIA no) | 76.2 min | 24 s | +$72.42 |
| 114 (MIA no) | 79.2 min | 9 s | +$96.14 |
| 111 (Villarreal TIE no) | 42.1 min | 24 s | +$108.48 |
| 110 (Villarreal TIE yes) | 41.3 min | 17 s | -$103.73 |

Those matches had already finished. The bot processed frames about 77 minutes
old, "entered" at pre-settlement prices, and the settlement frame — queued
behind them — paid out 100 seconds later. About **+$281 of fabricated paper
profit**. Before the reader/consumer split the same slowness produced a VISIBLE
failure: the socket backed up and Kalshi dropped the connection. After it, the
same slowness is silent.

`PAPER_MAX_BOOK_AGE_MS=5000` was set in production as the immediate mitigation
before this fix existed. It refuses a fill against a stale book
(CHG-2026-09-05-014); it does not bound the queue, does not stop the process
holding 3.77 GB of stale frames, and does not stop the bot operating minutes
behind the exchange. It is a fill guard, not a transport guard.

**Root cause.** A design gap, and mine to own: the unbounded queue was
specified. Removing the socket's own backpressure removed the only thing that
had been converting a slow consumer into a loud failure. An unbounded buffer in
front of a real-time decision does not preserve the data — it preserves frames
whose informational value expired, and spends memory to do it. The consumer's
underlying slowness is a separate defect and is NOT addressed here.

**Why necessary.** Without a bound, a consumer stall is unobservable except as a
number on a status panel nobody was watching, and its output is not "late
trades" but *trades against a past the market has already resolved*, booked as
profit. Disclosure without a bound is not enough either: the frames were all
there, and the study would still have replayed a bot that had traded 77 minutes
in the past.

**Exact change.** `WS_QUEUE_MAX` (default 20000, 0 = unbounded) bounds the
queue; `WS_QUEUE_DROP_POLICY` selects the policy. Only `oldest` is implemented —
for trading the newest market state is the only state worth having — and any
other value normalises to `oldest` rather than failing open to unbounded.
Enforcement is in `_read`: while `qsize() >= limit`, pop from the head.

Every discard is counted in `queue_dropped_total` and accumulated into a
summary carrying the count, the queue depth, the bound, the policy, how many
were order-book frames, how many were unparseable, and the **span of exchange
timestamps discarded** (`exchange_ts_min_ms`/`max_ms`/`span_ms`, read from the
dropped frames themselves; a frame with no provider stamp contributes nothing
rather than contributing the local clock). The summary is emitted through the
existing `_emit`, which writes both a `feed_events` row of the new kind
`queue_overflow` and a `recorder_marker` frame into the raw stream, so a later
replay sees the hole instead of bridging silently across it. Emission is rate
limited to one summary per `WS_QUEUE_OVERFLOW_REPORT_S` (default 1 s), and
whatever the limiter is still holding is flushed synchronously when the reader
stops, so the last window of an episode is never the one nobody hears about.

Dropping an order-book delta corrupts book state exactly as a sequence gap
does, so it goes through the same path: `_recover_dropped_books` dispatches an
`orderbook_gap` frame — with `reason: "queue_overflow"` — straight to
`Engine.handle_ws`, whose existing branch calls `desk.invalidate_books` and
clears `book.ok`; the affected tickers are added to `_recovering_orderbooks`, so
every further delta for them is refused until their snapshot lands; and a
`get_snapshot` request goes out immediately. This is deliberately NOT rate
limited by time: a newly affected market is disclosed and invalidated on the
drop that first touches it, because the consumer must not be able to fill from
it in the interim. Repeat drops for a market already invalidated are counted
only, so the cost is bounded by the number of distinct markets per recovery
cycle (54 tickers in the incident), not by the frame rate. A dropped book frame
whose market cannot be read invalidates every subscribed market.

The stall guard is a third coroutine beside the reader and consumer. When the
depth stays at or above `WS_QUEUE_STALL_DEPTH` (0 derives half of
`WS_QUEUE_MAX`, and leaves the guard off while the queue is unbounded) for
longer than `WS_QUEUE_STALL_S` (default 60), it logs the fault through
`on_state`, emits a `queue_stall_reconnect` feed event, drains the queue into
`frames_discarded`, and closes the socket, which completes one of the tasks
`run()` waits on and rebuilds the connection — re-subscribing, which yields
fresh snapshots. `WS_RECONNECT_MIN_INTERVAL_S` (default 120) floors the interval
between two forced reconnects, and a suppressed attempt is counted rather than
queued. The decision is a pure function, `_stall_reconnect_due(depth, now)`, so
it is testable without waiting on wall-clock time.

`Engine.status()` and `KalshiWS.status()` report `queue_depth`, `queue_max`,
`queue_drop_policy`, `queue_dropped_total`, `queue_overflow_events` and
`queue_forced_reconnects` beside the existing `feed_backlog`.

**Before / after.** Same input: 6 frames arriving into a queue bounded at 4
while the consumer is blocked. Before: 6 frames queued, depth 6, nothing
recorded, the consumer eventually processes frames 1 and 2 and trades on book
state that is two frames stale — and at production scale, 556,000 frames and 38
minutes stale. After: frames 1 and 2 are discarded from the head, the 4 newest
survive, `queue_dropped_total` is 2, one `queue_overflow` ledger row and one raw
marker record 2 dropped, depth 4, bound 4, and the exchange-timestamp span
1,000-1,100 ms that was thrown away. Same input again with frame 1 an
`orderbook_delta` for market A: additionally, an `orderbook_gap` with
`reason: "queue_overflow"` reaches the engine on the drop, `books["A"].ok`
becomes False, the desk's shadow book for A is invalidated, a `get_snapshot`
for A goes out, and every further delta for A is refused until that snapshot
arrives. A holed book cannot serve a fill: `book.ok` gates all six fill paths
(`paper.py` 392, 570, 1009 and `engine.py` 477, 910, 940) and only
`Book.apply_snapshot` sets it back to True — a queued delta applied afterwards
does not, which is asserted directly.

**Reasoning and trade-offs.** *Dropping the newest instead of the oldest* was
rejected: it preserves the past and discards the present, which is the wrong way
round for a live book, and it would have kept exactly the 77-minute-old frames
that produced the fabricated trades. *A bounded `asyncio.Queue` with `maxsize`
and a blocking `put`* was rejected: it restores backpressure onto `recv`, which
is the old visible failure but reached slowly and without a record of what was
lost; the explicit head-drop makes the loss countable. *Fixing the consumer's
slowness in this change* was rejected as out of scope and separately reviewable
— bounding and disclosure are the priority, and a faster consumer with an
unbounded queue is still one bad hour from the same silent failure. *Reconnect
only, without a bound* was rejected: a reconnect after 60 s of stall still
leaves up to 60 s of stale processing, and the guard cannot fire more often than
`WS_RECONNECT_MIN_INTERVAL_S`. *Failing closed on an unknown drop policy* was
rejected because failing closed here means an unbounded queue, which is the
defect.

`WS_QUEUE_*` are transport knobs and stay OUT of `STRATEGY_PARAM_NAMES`: they
decide which frames the process sees, not what it does with a frame it has seen,
so they must not move `config_id` and re-partition the study. They ARE added to
`exporter._OBSERVABILITY_NAMES`, because a bundle captured under a bound may
contain deliberate holes and a replay has to be able to read the bound off the
manifest.

**Validation.** `tests/test_ws_queue_bounds.py`, 24 tests. The bound and the
policy: overflow discards the oldest and the newest survive, with the ledger
detail including the discarded exchange-timestamp span; `WS_QUEUE_MAX=0`
restores the unbounded queue exactly (50 frames queued, 0 dropped, 0 events), so
existing tests are unaffected; an unparseable discard is still counted; an
unknown policy normalises to `oldest`, never to unbounded. Book safety: a
dropped delta invalidates its market, dispatches `orderbook_gap` with
`reason: "queue_overflow"`, sends `get_snapshot`, and causes the NEXT delta for
that market to be refused while the following snapshot is accepted; a dropped
trade alone invalidates nothing; a book frame with no readable market
invalidates every subscribed market. Rate limiting: 59 discards inside one
window produce exactly ONE `queue_overflow` event that still accounts for all
59, while a newly affected market is disclosed without waiting. Stall guard:
`_stall_reconnect_due` fires exactly once after the hold time, disarms when the
queue drains, refuses a second reconnect inside the minimum interval (counting
the suppression) and allows one after it, and is off while the queue is
unbounded; `_watch_queue` drains 5 queued frames into `frames_discarded` and
closes the socket; and driving `run()` end to end with a deliberately slow
consumer (1 ms/frame) and a patched `websockets.connect` produces
`connections >= 2` with `queue_forced_reconnects == 1` — the stall really does
rebuild the connection, and the minimum interval really does stop the second
one. Engine-level: an overflow gap clears `book.ok` for the affected market only,
a queued delta does not revive it, a fresh snapshot does, the eventlog line reads
"arrival queue overflow ... dropped=400" while a real sequence gap still reads
"sequence gap sid=7 expected=11 received=40", and `status()` surfaces all six
counters (and reports zeroes with no socket). Suite 562 tests OK after this
commit, from 538. Smoke run on port 8731 in demo mode: `/api/status` reports
`queue_depth 0`, `queue_max 20000`, `queue_drop_policy oldest`,
`queue_dropped_total 0`, `queue_overflow_events 0`,
`queue_forced_reconnects 0`, and the dashboard serves.

**Risks / limitations.** This does not make the consumer faster, and with
`WS_QUEUE_MAX=20000` a consumer as slow as the one measured will now DROP
frames rather than queue them — that is the intended trade, but it means the raw
archive will contain real holes under load, which is why every hole is
ledgered and markered. 20,000 frames is roughly 18 s of the measured peak
(64.7k frames/min); a stall shorter than that costs nothing, a longer one costs
data. The stall guard's reconnect discards the queue, so up to `WS_QUEUE_MAX`
frames are lost per forced reconnect, counted in `frames_discarded`. Recovery
still depends on the snapshot arriving through the same queue, so under a deep
backlog an invalidated book can stay invalid for a long time — correct (no fills
from it) but it means missed opportunities, and the stall guard is what
eventually resolves it. The `orderbook_gap` dispatch writes one `eventlog` row
per newly affected market on the event loop, through `store.ex`, which commits;
that is bounded per recovery cycle and matches the pre-existing sequence-gap
path, but it is an fsync on the loop during exactly the condition that produced
the stall. Finally, the fabricated +$281 across trades 110-114 stays in the
ledger; this change stops it recurring and does not rewrite history.

**Follow-up.** The consumer's slowness is unfixed and deliberately out of scope.
Two specific blocking calls on the consumer coroutine were identified while
working here and NOT changed, recorded so the next person does not have to find
them again:

1. `Engine.handle_ws` line 437 calls `self.recorder.write(...)` inline for every
   `orderbook_snapshot`, `orderbook_delta`, `trade` and `market_lifecycle_v2`
   frame. That is a synchronous `gzip` write — deflate compression plus buffered
   file I/O, with an explicit `flush()` every 200 frames — on the event loop, at
   1,782,137 `orderbook_delta` frames per two hours in the measured replay. This
   is the strongest single candidate for an I/O-bound consumer.
2. `store.ex` takes a process-wide `threading.Lock` and `commit()`s (an fsync) on
   every call. `record_signal` reaches it on the consumer coroutine via
   `store.insert_signal` and `store.add_latency`, and the `orderbook_gap` branch
   via `store.log_event`. Because work dispatched with `asyncio.to_thread` writes
   through the SAME lock, an event-loop caller can block waiting on a lock held
   by a worker thread mid-fsync — which is precisely the "I/O- or lock-bound at
   0.12 of 8 vCPU" signature the incident showed.

Neither was measured under load in this pass; both are hypotheses with a
mechanism, and each needs its own change with its own evidence.

### CHG-2026-09-05-020 — Record that `PRICE_FLOOR`'s rationale is now in doubt

**Commit:** none. Observation only; no code changed.
**Components:** none. This entry exists so the evidence is not lost.

**Observed / original behaviour.** `PRICE_FLOOR` (CHG-2026-09-04-004) was
introduced on the hypothesis that cheap legs lose money because they are cheap:
sub-35c entries were 41% of trades and 73% of contract exposure, that bucket
lost 22 of 27, and the counterfactual on the 68-trade history was
-$843.60 -> +$85.38 from the price bound alone.

A raw L2 replay of 2026-09-04 20:00-22:00 (1.9 M frames, 78 Gate-A candidates)
re-ran the same trades under two sizing models with the entry model held fixed.
Below 35c: **-$726 at $100 fixed notional, -$9.90 at a fixed 100 contracts.**
Same trades, same entry model, same prices. The loss is a function of position
size, not of entry price: a fixed dollar notional buys contracts as 1/price, so
$100 is ~727 contracts at 13.8c against ~176 at 57c.

**Root cause.** Design gap in the earlier reasoning, not a defect in the code.
The 2026-09-04 counterfactual varied price and size together — refusing a cheap
entry also refuses a large one — so it could not separate the two, and
attributed the whole effect to price.

**Why necessary.** Without this recorded, the next person reads
CHG-2026-09-04-004 as settled and either leaves a possibly-unnecessary floor in
place or removes it without knowing what actually has to change with it.

**Exact change.** None. Nothing in this pass changes sizing, and `PRICE_FLOOR`
keeps its default of 35.

**Before / after.** No behavioural difference.

**Reasoning and trade-offs.** Changing `NOTIONAL_USD` to a contract count was
considered and rejected for this pass: it is a strategy change with its own
economics, it needs its own reviewed change, and the capture work in this pass
is exactly what would let it be judged on forward evidence rather than on
another replay. Removing `PRICE_FLOOR` now was also rejected — the replay says
the floor is not the *mechanism*, not that removing it is free.

**Validation.** None; nothing was changed. The replay numbers above are the
evidence, n=29 trades in the priced window, n=78 Gate-A candidates overall.

**Risks / limitations.** The floor is currently doing work whose stated reason
is wrong. It is still bounding exposure, so it is not harmful, but any claim
that "the price bound fixed the cheap-leg loss" is unsupported and should not
be repeated.

**Follow-up.** Decide sizing explicitly — fixed contracts, or a notional that
scales with price — in its own change, using the forward evidence this pass
starts collecting.

### CHG-2026-09-05-019 — Set the event-association window from measurement, 20 s -> 90 s

**Commit:** `a900e6d`
**Components:** `app/config.py`, `.env.example`, `README.md`,
`tests/test_score_classification.py`

**Observed / original behaviour.** `EVENT_MATCH_WINDOW_S = 20.0` is the ±window
`audit.match_signal_event` uses to associate a signal with the nearest
same-match provider event. SPEC_CORRECTIONS C7 already records it as
"inherited", and C6 records that the documented Al-Shabab case had the provider
observation arriving **18.635 s after the signal** — about 1.4 s of margin
against a window that was guessed, not measured.

Measured since: provider `occurence_ts` to first observation is **p50 15 s**,
and goal observation minus bot entry is typically **+12..+50 s**, because the
Kalshi score feed lands 10-40 s after the market moves. At 20 s, most genuinely
goal-driven trades therefore recorded `no_nearby_same_match_event`, which reads
as "no goal was near this trade" and is wrong.

**Root cause.** The window was set from an unmeasured guess about feed timing,
and the feed is structurally slower than the guess.

**Why necessary.** The association is the only ground-truth label the study has
for "was this candidate actually goal-driven". A window narrower than the
measured lag makes that label systematically false-negative, and every
precision/recall statement built on it is wrong in the same direction.

**Exact change.** Default 20.0 -> 90.0, with the measurement recorded in the
config comment and in `.env.example`. No code path changed: the value was
already read from config by `audit.signal_event_window` and `main`.

**Before / after.** A signal whose goal observation lands 35 s later reads
`no_nearby_same_match_event` before and `state_consistent` /
`temporally_associated` after. A signal with no same-match event within 90 s
still reads `no_nearby_same_match_event`.

**Reasoning and trade-offs.** 60 s was considered and rejected: it sits inside
the measured +12..+50 s band's upper tail with no margin, which is the same
mistake being corrected. 90 s covers the tail with room. The cost is a looser
label — a wider window admits more coincidental matches — and that is the
correct direction here, because C6 already establishes that proximity can never
*explain* an individual trade, only label it, and the label is reported
alongside `state_consistent` / `state_mismatch`, which is what separates a real
match from a coincidence.

**Validation.** `tests/test_score_classification.py::AuditWindowTests` pins the
new default, that `EVENT_MATCH_WINDOW_S` stays out of `STRATEGY_PARAM_NAMES`,
that patching it does not move `config_id`, and that the export manifest still
reports it under `_OBSERVABILITY_NAMES`. **Confirmed: this is an audit-window
default and is excluded from the strategy identity, so widening it does not
re-partition the study.** Suite 504 OK.

**Risks / limitations.** Historical rows keep the association computed at
whatever window was active when they were read; the window is applied at read
time, so re-reading old signals now applies 90 s. That is a presentation change
on old rows, not a rewrite of stored data, but a saved screenshot from before
this will disagree with the dashboard.

**Follow-up.** None.

### CHG-2026-09-05-018 — Stamp a poll sequence on every clock and score observation

**Commit:** `a900e6d`
**Components:** `app/goal_latency.py`, `app/match_clock.py`, `app/store.py`,
`tests/test_score_classification.py`, `tests/test_production_migration.py`

**Observed / original behaviour.** Each observation carries `observed_ts`,
`poll_started_ts`, `previous_poll_ts` and `response_ms`, but nothing says which
poll produced it. Two observations from the same `/live_data/batch` response are
indistinguishable from two observations one poll apart, and a gap in the series
cannot be told from a poll that returned nothing for that event.

**Root cause.** Design gap. The observer already counted its polls
(`GoalLatencyObserver.polls`); the counter was reported in `status()` and never
written to a row.

**Why necessary.** Poll cadence is the denominator for every latency claim the
observer makes. Without it, "the clock was 6 s stale" cannot be separated into
"the poll was late" and "the provider did not move".

**Exact change.** `goal_latency_observations.poll_seq` and
`match_clock_observations.poll_seq` (additive INTEGER columns).
`GoalLatencyObserver._poll` increments `self.polls` before building `timing` and
carries the value in `timing["poll_seq"]`; `MatchClockTracker.observe` and
`_record_change` pass it through; both inserts write it. It is the same counter
`status()["polls"]` already reports, so no second source of truth is created.

**Before / after.** Before: two clock rows 6 s apart, no way to say whether one
poll or twenty-four happened between them. After: `poll_seq` 41 and 42 says one.

**Reasoning and trade-offs.** A UUID per poll was rejected: a monotonic integer
sorts, subtracts and compresses, and the run boundary is already visible from
the process restart. The counter restarts at 1 on a new process — that is what
"per observer run" means, and it is deliberately not made globally monotonic,
because a globally monotonic counter would need durable state that the observer
does not otherwise keep.

**Validation.** New tests assert the column is written and read back monotonic
for both tables, and that a row written without one keeps NULL. The migration
test asserts the columns appear, do not duplicate on remigration, and that
**historical rows stay NULL — they are not backfilled**, because the counter did
not exist when they were written and any value assigned now would be invented.
Suite 504 OK.

**Risks / limitations.** `poll_seq` is only comparable within one process run.
Joining across a restart needs `observed_ts`.

**Follow-up.** None.

### CHG-2026-09-05-017 — Stop reading a period boundary as a goal

**Commit:** `a900e6d`
**Components:** `app/goal_latency.py`, `app/match_events.py`,
`tests/test_score_classification.py`

**Observed / original behaviour.** `classify_score_change` diffed the **whole**
numeric signature with `before.get(key, 0.0)` as the default for an absent key,
so any newly appearing numeric key read as a positive delta and returned
`"goal"`. `score_signature` collects every numeric field at or below a key
containing `score`, and Kalshi's `details.period_scores` is a list of
`{away_score, home_score, number, type}` — so `period_scores` matches, and a
second-half kickoff appending `period_scores[1]` introduces
`period_scores.1.number = 2`, a period **ordinal**, and the row was labelled a
goal.

In the deployed study, **199 of 476 rows labelled `goal` carried
`side=unknown` and an unchanged score**. Any goal-rate statistic computed from
that table is inflated by roughly 2x.

**Root cause.** A schema-flexible key matcher (correct, and deliberately so)
feeding a value comparison that assumed every collected key was a score.

**Why necessary.** Goal labelling is the ground truth for the entire
price-only thesis. A label that fires on a period boundary makes the goal rate,
the association rate and any precision claim built on them wrong by about a
factor of two, and it fires exactly at half-time and second-half kickoff — which
is inside the late-game window the strategy trades in.

**Exact change.** `match_events.score_values()` filters a signature to
score-valued keys only: `*_same_game_score`, `*_aggregate_score`,
`period_scores.N.home_score` / `away_score` (matched on the last path segment
with underscores stripped, so snake_case and camelCase resolve identically, and
`score_home` / `score_away` are accepted for parity with `_primary_score`).
`classify_score_change` diffs only that subset, with three rules and the
original correction-beats-goal precedence:

* a key present on both sides is a goal when it rises, a correction when it
  falls;
* a key that **appears** is a goal only when it appears **above zero**, so a new
  period starting 0-0 is a `score_schema_change`;
* a key that **disappears** is a `score_schema_change` — the provider stopped
  reporting a field, it did not un-score a goal.

**Before / after.** Same real-shaped payload, `home_same_game_score=1`,
`away_same_game_score=0`, `period_scores` growing from one entry to two at the
second-half kickoff: `"goal"` before, `"score_schema_change"` after, normalizing
to `score_schema_change.unknown` with `side="unknown"` instead of
`goal_observed.unknown`. A genuine 1-0 -> 1-1 goal is still `"goal"`, a 2-0 ->
1-0 revision is still `"score_correction"`, and a goal in a newly appearing
period (`period_scores[1].away_score` appearing as 1) is still `"goal"`.

**Reasoning and trade-offs.** Narrowing `score_signature` itself was rejected:
it is the raw evidence, the observation rows store it, and shrinking it would
lose fields that a later re-analysis may want. Filtering at classification time
keeps the recorded signature complete and makes the interpretation explicit.
An allowlist of exact key paths was rejected as too brittle for a provider that
documents `details` as flexible JSON; matching the final path segment keeps the
schema tolerance the module was written for.

**Validation.** `tests/test_score_classification.py` builds the fixture from the
real payload shape — `home_same_game_score`, `away_same_game_score`,
`period_scores` as a list of `{away_score, home_score, number, type}`, `half`,
`status`, `status_text`, `time`, `last_play` — and asserts the structural key
`period_scores.0.number` is present in the raw signature and absent from the
filtered one; that a second-half kickoff is `score_schema_change` and not a
goal; that a genuine goal, a genuine correction, correction-over-goal
precedence, a goal in a new period, a disappearing key and a pure structural
change all classify correctly. Suite 504 OK.

**Risks / limitations.** **The 199 mislabelled historical rows are NOT
rewritten.** They stay exactly as recorded, with their raw `score_before` /
`score_after` intact, so anyone re-deriving the label gets the corrected answer
from stored evidence. Any aggregate over `change_kind` that spans this deploy
mixes two labelling rules and must be split at it. A provider that reports a
score under a key whose last segment is none of the accepted forms would now be
ignored where it was previously (accidentally) counted; the raw signature still
records it, so that shows up as `score_schema_change` rather than silence.

**Follow-up.** Re-derive the historical labels from the stored signatures when
the goal-rate statistic is next quoted, and say which rule each row was written
under.

### CHG-2026-09-05-016 — Persist the settlement result of every watched market

**Commit:** `d743a17`
**Components:** `app/engine.py`, `app/store.py`, `tests/test_market_results.py`,
`tests/test_production_migration.py`

**Observed / original behaviour.** No market settlement result was persisted
anywhere queryable. `handle_ws` read `settled_result` off a
`market_lifecycle_v2` frame and passed it straight to `PaperDesk.settle_market`,
which used it to close positions and then discarded it. `settle_poll_task`
polled `/markets/{ticker}` only for tickers with an **open paper position**, so a
market the bot declined never had its result observed at all.

**Root cause.** Design gap. Settlement was treated as an input to closing a
position rather than as an observation about a market.

**Why necessary.** Most of the funnel is declined — 1,150 `unconfirmed` and
several hundred sleeve refusals against 75 closed trades. Without a result for
those markets the declined population has no outcome label, so "was declining
right" is unanswerable from the database and can only be recovered by replaying
raw tape.

**Exact change.** Additive `markets.result`, `markets.settled_ts`,
`markets.last_yes_bid`, `markets.last_yes_ask`.
`store.record_market_result()` writes them with first-write-wins on
`settled_ts` and `COALESCE` on the quotes, so a lifecycle frame and a later REST
poll cannot degrade each other, and a result for an unregistered ticker writes
nothing and returns 0 rather than inventing a market row.
`Engine._record_market_result` captures the last YES bid/ask from the live book
and dispatches the write to a worker thread when a loop is running — the
platform pass removed the SQLite writes from `handle_ws` and this does not
reintroduce one. `settle_poll_task` now polls open-position markets **plus**
every watched market whose expected expiration has passed, capped at
`SETTLE_POLL_MAX = 50` per 30 s cycle with open positions first, and skips
markets already recorded via `_settled_markets`.

**Before / after.** A declined market that settles YES: no row anywhere before;
`markets.result='yes'` with `settled_ts` and the last observed quote after. A
market with an open position is polled exactly as before, and is still polled
after its result is recorded, so a settlement that failed to close a position
keeps retrying.

**Reasoning and trade-offs.** Writing `markets.status='settled'` was considered
and rejected: discovery re-upserts `status` from `/markets`, so the two would
fight, and the settlement fact belongs in its own column. Polling every watched
market unconditionally was rejected on rate-limit grounds; markets leave
`_watched_markets` `DROP_AFTER_CLOSE_MIN` (20 min) past expiration, which bounds
the set on its own, and the explicit per-cycle cap bounds it again.

**Validation.** New tests cover: a settled market with **no position** records
its result and does not call `settle_market`; the lifecycle frame records the
result and settles the desk; a market with no book records null quotes rather
than a guess; first settlement time and a known quote are never degraded by a
later null; a non-binary result writes nothing; an unregistered ticker invents
nothing; the poll covers expired declined markets, skips already-settled ones,
still covers open positions after recording, and is bounded per cycle; and the
socket-side write is dispatched off the event loop rather than called inline.
Suite 504 OK.

**Risks / limitations.** Markets that expired and settled while the process was
down are never observed — they drop out of `_watched_markets` before the poll
sees them. Historical markets keep NULL results and are not backfilled.
The widened poll adds up to 50 REST calls per 30 s in a busy window, against a
previous bound of the open-position count.

**Follow-up.** Reconcile results for markets that settled during downtime from
the settlement endpoint, if a bulk one is available.

### CHG-2026-09-05-015 — Spend the path budget on time, not on arrival order

**Commit:** `d743a17`
**Components:** `app/config.py`, `app/engine.py`, `app/paper.py`,
`tests/test_path_thinning.py`, `tests/test_bid_path.py`

**Observed / original behaviour.** Both path recorders — the forward watch in
`engine._record_signal_paths` and the position path in
`paper._record_exec_path` — recorded every change until a flat
`store.BID_PATH_MAX_SAMPLES = 4000` cap, then dropped everything after it. A
flat cap is a budget consumed fastest by the busiest markets: in the deployed
study **every La Liga trade and signal recorded `samples=3999`**, so the
intended 300 s forward window collapsed to 60-130 s of coverage exactly where
activity — and therefore the answer — was highest.

**Root cause.** Design gap. The cap bounds storage, which it should, but it was
also acting as the sampling policy, which it should not: nothing said which
4,000 of the available observations were the useful ones.

**Why necessary.** The path exists to answer "what could this position have
exited at, and when" (SPEC_CORRECTIONS C4/E1). A truncated path answers that for
the first two minutes of a five-minute window and is silent afterwards, in
precisely the markets where the exit rule matters most.

**Exact change.** New knobs `PATH_THIN_AFTER_S` (10) and
`PATH_THIN_INTERVAL_MS` (250), and a shared `paper.path_thins()` used by both
recorders. Every change is recorded for the first `PATH_THIN_AFTER_S` after the
anchor — the window the reaction happens in — then at most one row per
`PATH_THIN_INTERVAL_MS`. Four exemptions, in order: an unpriced observation is
an availability change and is always recorded; a new peak or trough is always
recorded; nothing inside the early window is thinned; and an outage resets the
interval so the quote that resumes availability is always kept.
`BID_PATH_MAX_SAMPLES` remains the hard backstop, the reserved terminal slot is
unchanged, and thinned rows are counted separately (`exec_path_thinned` /
`watch["thinned"]`) so `truncated` and `dropped_samples` keep meaning exactly
"the cap bit". `store.bid_path_summary` is unchanged.

A thinned observation deliberately does **not** advance the dedupe signature,
which now tracks what is durable rather than what was last seen, so the row
written at the next interval carries the then-current quote instead of being
deduplicated against one that was never persisted.

**Before / after.** On the bundled real tape: 2,255 persisted path rows before,
1,906 after (-15.5%), across the same 9 path owners, with the per-owner
`(min bid, max bid)` **identical for all 9** — rows were removed, extremes were
not. In a hot market the shape of the saving is much larger: 200 chop
observations at 10 ms inside an already-seen range reach at most one row per
250 ms.

**Reasoning and trade-offs.** Raising `BID_PATH_MAX_SAMPLES` was rejected: it
moves the wall without changing the policy, and multiplies storage on exactly
the busiest markets. Reservoir sampling was rejected because it does not
guarantee the extremes, and the extremes are the measurement. Thinning by price
change size was rejected because it destroys time-at-price, which is what
distinguishes a 90c quote resting 200 ms in size 1 from one resting 12 s in size
500 (C4). These knobs are **not** strategy parameters: nothing in the trading
path reads a persisted path, so changing them cannot change a decision.

**Validation.** `tests/test_path_thinning.py` pins the rule directly (early
window, interval, new peak, new trough, unpriced) and behaviourally on both
recorders: every change kept inside the first 10 s; the rate bounded afterwards
while a later peak and trough still survive; thinning never counted as
truncation; the 4,000 backstop still bites with exactly one terminal row; the
reserved terminal slot survives thinning; a quote resuming after an outage
always recorded; and `bid_path_summary` on a thinned path still returns the true
peak, trough, first and last bid with `truncated=False`. Plus the whole-tape
extremes check above. Suite 504 OK.

**Risks / limitations.** Between two recorded rows more than 250 ms apart, the
in-between quotes are gone. Time-at-price is therefore quantised to 250 ms
outside the first 10 s; `ms_at_peak` is still bounded by real recorded rows on
both sides of the peak, because the peak itself is always recorded, but its
resolution is coarser than before. Paths already persisted are untouched and
were collected under the old policy, so a comparison of `samples` across this
deploy is meaningless.

**Follow-up.** None.

### CHG-2026-09-05-014 — Add `PAPER_MAX_BOOK_AGE_MS`, default 0 (record only)

**Commit:** `b5e0470`
**Components:** `app/config.py`, `app/paper.py`, `app/store.py`,
`.env.example`, `README.md`, `tests/test_book_age_context.py`

**Observed / original behaviour.** A paper entry filled against whatever book
was in memory, however old. The raw L2 replay of 2026-09-04 20:00-22:00 measured
the process **5.6 s median behind the exchange** across 18 sequence gaps and 8
reconnects, so "whatever book was in memory" was routinely several seconds
stale, and there was no way to refuse on that basis or to measure what refusing
would have cost.

**Root cause.** Design gap, not a defect: staleness was never a modelled input
to the entry decision.

**Why necessary.** Every future P&L claim inherits the ambiguity of a fill taken
from an unknown-age book. The bound has to exist before it can be studied, and
it has to default to off so that studying it is a deliberate act.

**Exact change.** `PAPER_MAX_BOOK_AGE_MS`, default **0.0 = record only**. At 0
no entry is ever refused and behaviour is byte-identical. Above zero, an entry
whose book age is known and exceeds the bound is finalised as a new outcome
`stale_book` instead of filling, in **both** paper paths (`_execute_entry` and
`try_enter`) so the two cannot disagree about eligibility, with the measured
`entry_context` attached to the signal detail. `stale_book` is added to
`store._strategy_summary`'s `confirmed_outcomes` alongside `no_book`, so raising
the bound shows up as refusals rather than silently shrinking the K2
denominator. **It is added to `config.STRATEGY_PARAM_NAMES`: it changes which
entries are taken, so it changes `config_id`, and that is correct.**

An **unknown** age never refuses. Unknown is not evidence of staleness, and
refusing on it would turn a missing provider timestamp into a silent change in
which entries are taken; the unknown is recorded as
`book_age_unknown: "no_arrival_stamp"` instead.

**Before / after.** At the default 0, a book 600 s old still fills, exactly as
today. At 5,000, a 16 s-old book is refused as `stale_book` with
`book_age_ms: 16000.0` in the signal detail, and a 0.5 s-old book still fills.

**Reasoning and trade-offs.** Defaulting it to a live bound was rejected
outright: this pass must not change which entries are taken. Refusing on unknown
age was rejected for the reason above. Making it an observability knob was
rejected because above zero it demonstrably refuses trades, and a knob that
changes admissions belongs in the configuration identity even when its default
is inert.

**Validation.** New tests assert that 0 disables the bound entirely (a 600 s
book still fills, signal outcome `filled`); that above the bound the entry is
refused as `stale_book` with the measured age in the detail; that below the
bound it still fills; that an unknown age never refuses; that both paper paths
agree; that `stale_book` is counted in `k2_ci.n_signals` exactly like `no_book`
and never like `unconfirmed`; and that the knob is in `STRATEGY_PARAM_NAMES` and
moves `config_id`. The whole-tape regression above confirms no behaviour change
at the default. Suite 504 OK.

**Risks / limitations.** Adding the knob moves `config_id` even though its
default is inert, so rows from before and after this deploy do not pool. That is
the provenance stamp working as designed, but it is a real discontinuity in
every current-configuration aggregate.

**Follow-up.** Fit the bound from the `book_age_ms` distribution once forward
data exists, in its own reviewed change.

### CHG-2026-09-05-013 — Record the numbers behind every exit label, and the book at the exit fill

**Commit:** `b5e0470`, plus the documentation commit that adds this entry (the
exit-peak correction below).
**Components:** `app/paper.py`, `app/store.py`, `app/main.py`,
`tests/test_book_age_context.py`

**Observed / original behaviour.** 75 closed trades, net -$836.90, and **66 of
75 exits were the 180 s timeout**. The record carried the label and nothing
else: not the bid the desk saw when it decided, not how long the position had
been held, not how far it had ever run, and — for the four sleeve exits that
SPEC_CORRECTIONS H1 calls historically unfalsifiable — not the computed scratch
level that separated `sleeve_scratch` from `sleeve_profit_lock`.

**Root cause.** Design gap. The exit reason is a *label* produced from four
inputs that were all discarded at the moment they were evaluated.

**Why necessary.** An exit rule cannot be re-tuned from labels. With 88% of
exits carrying one label and no inputs, the dominant behaviour of the strategy
is the least explicable part of the record.

**Exact change.** Additive `trades.exit_context` JSON.
`PaperDesk._exit_trigger` captures the reason, the observed bid, `elapsed_s`,
the peak, the entry price and the remaining size at the moment the exit is
decided, plus `scratch_c` (recomputed with the same
`fee_aware_scratch_price`/`fee_dollars` the rule used) and `anchor_bid` for a
sleeve position. It is stored on `PendingExit.trigger` — **`Position.bid_path`
keeps its exact shape and length (H1); no new state is put on it.**
`_execute_exit`, `close` and `settle_market` add `book_side_depth()`: the
held-side and opposite-side **top-8** at the fill, plus the book-age fields and
the feed lag/backlog. Where an exit is reached without a book in hand — timeout,
flatten, settlement — the desk falls back to the last live book seen for that
market (kept by reference in `PaperDesk._last_book`, updated in `on_book`) and
labels it `book_source: "last_seen"` rather than claiming it is the fill book;
with no book at all it records `book_age_unknown: "no_book_at_exit"`.
`main.py` decodes `entry_context`/`exit_context` on `/api/trades` so the API
serves objects rather than JSON text.

**Correction made during validation.** The first implementation reported
`Position.peak_bid` as the trigger's `peak_bid`. That field only advances inside
`sleeve_exit_reason`, so on a Gate-A position it never leaves the entry price:
the demo smoke run recorded `peak_bid: 43.0` on a trade whose bid reached 90c.
It now reports `pos.max_executable_bid` (with `peak_bid_ts`), which
`_observe_executable_high` maintains for **every** position, and reports
`Position.peak_bid` separately as `sleeve_peak_bid` on sleeve positions, because
that is the value three sleeve exits actually read.

**Before / after.** A timeout exit before: `exit_reason='timeout'`. After:
`{reason: "timeout", observed_bid: 50.0, elapsed_s: 180.0, peak_bid: 61.0,
peak_bid_ts: ..., entry_px: 45.0, remaining: 100.0}` with the held-side and
opposite-side top-8 at the fill, the book's age, and whether that book was the
fill book or the last seen.

**Reasoning and trade-offs.** Reconstructing the trigger at analysis time from
`bid_path_samples` was rejected: the scratch level depends on the fee schedule
and the remaining size at that instant and is not recoverable from the path, and
the whole point of H1 is that the four sleeve exits were unfalsifiable precisely
because their inputs were discarded. Extending `Position.bid_path` was rejected
outright — H1 forbids it and four exit decisions read it. Recording the full
ladder was rejected as unbounded; top-8 matches the existing entry-side
`ShadowBook.SNAPSHOT_DEPTH` convention.

**Validation.** New tests assert the trigger values on a close; the held/opposite
top-8 at the fill (8 of 10 offered levels on each side, correct sides for a YES
position); the computed scratch level and anchor on a sleeve exit; the
`last_seen` fallback and its label on a timeout reached with no book in hand; a
settlement recording its own context; that a Gate-A exit reports the executable
high and not the entry price as the peak; and that `Position.bid_path` is still
a `deque(maxlen=240)`. The whole-tape regression above confirms exit behaviour
is unchanged. Suite 504 OK.

**Risks / limitations.** For an exit reached without a book, the recorded book
is the last one seen, which may be older than the fill; the row says so, but a
consumer that ignores `book_source` will over-trust it. Closed trades from
before this deploy keep NULL `exit_context` and are not backfilled — the inputs
were never recorded and any value produced now would be invented.

**Follow-up.** None.

### CHG-2026-09-05-012 — Record the age of the book every paper fill used

**Commit:** `b5e0470`
**Components:** `app/books.py`, `app/execution.py`, `app/paper.py`,
`app/engine.py`, `app/store.py`, `app/replay.py`,
`tests/test_book_age_context.py`

**Observed / original behaviour.** A paper fill recorded the arrival book it
walked (`trades.book_at_entry`) but nothing about **when** that book was true.
The raw L2 replay of 2026-09-04 20:00-22:00 (1.9 M frames, 78 Gate-A candidates)
found the reconstructed book's best ask **worse than a real same-side executed
price for 29 of 29 candidates, median +12c**, with the process running **5.6 s
median behind the exchange** across 18 sequence gaps and 8 reconnects. On the
same 29 trades, the entry assumption alone moved the total between
**-$862.96, -$228.72 and +$550.33**.

**Root cause.** Design gap. `Book` tracked `ts_ms` from deltas and used it
nowhere; nothing recorded when a frame arrived in this process, so the two
distinct quantities — how long the depth had been sitting here, and how far
behind the exchange it was — could not be separated or even measured.

**Why necessary.** This is the item the rest of the pass depends on. A fill
price taken from a 16 s-old book is a different claim from one taken at the
touch, and until each fill is labelled with which it was, **every** P&L
statement the study makes inherits the same unresolvable ambiguity.

**Exact change.** `Book` gains `last_exchange_ts_ms` (from the delta's `ts_ms`,
which Kalshi supplies) and `last_arrival_wall`, stamped in `apply_snapshot` and
`apply_delta` from the reader's arrival stamp that the platform pass already
threads into `handle_ws`. A frame with no `ts_ms` leaves the exchange stamp
untouched: a missing provider timestamp stays missing. `ShadowBook` mirrors both
through `reset` and `apply_delta`, because the shadow is the book the fill
actually walks. `paper.book_age_context()` derives `book_exchange_ts_ms`,
`book_age_ms` (fill wall minus book arrival wall) and `book_exchange_lag_ms`
(fill wall minus exchange stamp) for a fill, with an explicit
`book_age_unknown` reason instead of a substituted value. Every entry persists
these on a new additive `trades.entry_context` JSON column, together with the
`feed_lag_ms` and `backlog` at the fill, supplied by an optional
`PaperDesk(feed_state=...)` callable the engine passes.
`replay.synth_book` stamps both fields so demo mode produces usable values.

**Before / after.** Before: `entry_px 45.0` with no way to date the book behind
it. After, from the demo smoke run: `book_exchange_ts_ms: 1787433675896.0,
book_age_ms: 2.809, book_exchange_lag_ms: 1198154470.447` — the last being the
replay offset, which is the same caveat `feed_lag_ms` already carries in demo
and is documented as such.

**Reasoning and trade-offs.** Reusing the existing `Book.ts_ms` alone was
rejected: it is the exchange's stamp only, and the two lags answer different
questions — one measures this process, the other measures the exchange link.
Deriving the age at read time from `bid_path_samples` was rejected because the
path is anchored at entry and does not carry the book's own provenance. Passing
the arrival stamp explicitly (rather than defaulting to `time.time()` inside the
book) keeps the reader's stamp authoritative in the live path while leaving
isolated callers usable.

**Validation.** New tests assert the exchange stamp and arrival wall on snapshot
and delta; that a frame with no `ts_ms` leaves the stamp alone; that
`ShadowBook` mirrors both through `reset` and `apply_delta`; that the realistic
entry records age, exchange lag, feed lag and backlog; that the original paper
path records the same fields; and that an unstamped book reports the age as
unknown rather than as zero. The whole-tape regression above confirms entry
behaviour is unchanged. Suite 504 OK.

**Risks / limitations.** `book_age_ms` measures arrival into this process, not
the exchange's own book time; a frame delayed on the wire before arrival is
invisible to it, which is exactly why `book_exchange_lag_ms` is recorded beside
it. Snapshot frames may not carry `ts_ms`, in which case the exchange stamp
stays at whatever the last delta set, or NULL. Trades from before this deploy
keep NULL `entry_context`.

**Follow-up.** Compare `book_age_ms` at fill against `feed_lag_ms` over a live
window to decide whether `PAPER_MAX_BOOK_AGE_MS` should ever be armed.

### CHG-2026-09-05-011 — Key every row of one episode with `episode_id`

**Commit:** `b59cece`
**Components:** `app/engine.py`, `app/store.py`,
`tests/test_signal_capture_context.py`, `tests/test_production_migration.py`

**Observed / original behaviour.** In `parallel` mode one confirmed episode
writes **two** signal rows — one `gate_a`, one `price_only_late_score` — with
different ids, identical trigger fields and two independent forward watches, and
nothing keyed them together. Joining a Gate-A row to its price-only twin meant
matching on `(market, ts_ms, dl, levels)` and hoping.

**Root cause.** Design gap introduced when the sleeves were made to run
independently: the dispatch copies the candidate per strategy and the copies
lost their shared identity at the row level.

**Why necessary.** The whole point of `parallel` mode is to compare the two
sleeves on the *same* episodes. Without a key that comparison is a heuristic
join, and any episode where the heuristic is wrong silently biases the
comparison.

**Exact change.** Additive `signals.episode_id TEXT` plus an index on it.
`Engine.episode_id(cand)` returns `<market>:<int(candidate ts_ms)>` — the
exchange timestamp and market **are** the episode — and `record_signal` writes it
on **every** row it produces: `gate_a`, `price_only`, `unconfirmed`,
`confirmed_late`, `strategy_lockout`, every `sleeve_*` refusal and every paper
outcome. `record_subthreshold` deliberately does not: a near miss is not an
episode, and it stays NULL.

**Before / after.** Two rows from one episode before: ids 811 and 812 with no
shared field that is guaranteed unique. After: both carry
`episode_id='KXLALIGAGAME-...-TIE:1787433761420'`.

**Reasoning and trade-offs.** A generated UUID per episode was rejected: it is
not derivable from the candidate, so it cannot be recomputed at analysis time or
recovered if a row is written by a different path. Using the first row's signal
id was rejected because it makes the second row depend on the first having been
written. The chosen key is content-addressed and stable — re-deriving it from
the same candidate gives the same string.

**Validation.** New tests assert the parallel pair shares one key, that the key
is stable under re-derivation, that every episode outcome carries it, that a
sub-threshold row does not, and — explicitly — that `store.stats()` funnel counts
are **unchanged**: they key on outcome per strategy, and the test asserts the
combined and both per-sleeve `unconfirmed` counts across an episode pair. The
migration test asserts legacy rows keep NULL. Suite 504 OK.

**Risks / limitations.** Two genuinely distinct episodes on the same market with
the same exchange millisecond would collide. `EPISODE_COOLDOWN_S` (5 s) makes
that impossible for candidates from the same detector, and the key is scoped by
market, so the residual case is a duplicate exchange timestamp, which would also
break the existing lockout. Historical rows keep NULL.

**Follow-up.** None.

### CHG-2026-09-05-010 — Return and persist the evidence behind every confirmation decision

**Commit:** `b59cece`
**Components:** `app/detector.py`, `app/engine.py`,
`tests/test_signal_capture_context.py`, `tests/test_confirmation_window.py`,
`tests/test_detector_scan.py`

**Observed / original behaviour.** `Detector.confirm` returned
`(confirmed, lag_ms)`. **`unconfirmed` is 76% of the Gate-A funnel — 1,150 of
1,511 rows — and carried no explanation whatsoever.** The three questions that
decide whether the confirmation rule is right (was there a sibling burst at all;
how far away in time; did it move the right way) could only be answered by
replaying raw tape.

**Root cause.** Design gap: the scan already computed everything needed and
returned only its verdict.

**Why necessary.** `CONF_MS` and `CONF_SIGN` are frozen Gate-A parameters. They
cannot be re-fitted from a table whose dominant row type says only "no".

**Exact change.** `confirm` returns `(confirmed, lag_ms, evidence)`. The
decision is unchanged: same scan, same window, same sign rule, same nearest-lag
tie-break. `evidence` carries `window_ms`, `sign_required`, `siblings`,
`siblings_with_tape`, `bursts_scanned`, and `bursts`: for each sibling burst
within `4 x CONF_MS` of the candidate, `{sibling_ticker, lag_ms, signed_dl,
levels, in_window, opposite_sign}`. `MarketState.big_bursts` becomes
`(ts_ms, signed_dl, levels)` so the burst's shape travels with it; the rule reads
`[0]` and `[1]` only. The list is sorted by `|lag|` and capped at 12
(`bursts_near` records how many were in range), so a hot market cannot grow the
row. `Engine._attach_confirmation` puts it on the candidate — which
`_strategy_candidate` copies, so one attachment reaches every row of the episode
— and adds `attempts`, the number of times the scan actually ran, which is what
separates "no sibling ever printed" from "the wait window expired before its
frame arrived". `record_signal` persists it under `signals.context.confirmation`
for **confirmed and unconfirmed rows alike**.

**Before / after.** An `unconfirmed` row before: `conf_lag_ms=NULL`, nothing
else. After, from the demo smoke run: one row with
`bursts_scanned=17, bursts=12, siblings=2, attempts=1` (siblings printed, all
rejected) and another with `bursts_scanned=0, siblings=2, attempts=1903` (the
siblings never printed a big burst at all, across 1,903 re-checks). Those are
two completely different failures that were previously the same row.

**Reasoning and trade-offs.** Recording every retained burst was rejected: the
5 s retention window can hold hundreds in a hot market and `confirm` is called
once per sibling trade while a candidate is pending, so the row and the cost
would scale with trade rate. Bounding capture to `4 x CONF_MS` keeps the
plausible confirmations and the near misses — a burst 60 ms outside a 50 ms
window is the interesting case, one 3 s away is not. A separate
`confirm_evidence()` method was rejected because it would scan twice.

**Validation.** New tests assert the evidence on a real unconfirmed scan
(in-window same-sign burst and out-of-window opposite-sign burst both recorded
with the right flags and level counts); that a confirmation still decides
exactly as before; that the list is bounded to the 12 nearest in a hot market
with `bursts_near` and `bursts_scanned` showing the truncation; and that the
engine persists it on the row. `tests/test_confirmation_window.py` still pins
confirmed / late-confirmed / unconfirmed semantics and was updated only for the
new tuple width. `tests/test_detector_scan.py` compares `big_bursts` on the
fields the confirmation rule reads, so the verbatim reference implementation is
**not** back-dated; the bundled-tape equivalence it exists to prove is
unaffected. The whole-tape regression above confirms the funnel is identical.
Suite 504 OK.

**Risks / limitations.** `attempts` counts scans, not distinct siblings, so a
market printing 1,903 trades during the wait window reports 1,903 — that is the
measurement, but it is not a count of opportunities. Only bursts within
`4 x CONF_MS` are described, so a sibling that printed 2 s late leaves no trace
beyond `bursts_scanned`. Historical `unconfirmed` rows stay unexplained; nothing
can recover their sibling tape except a raw replay.

**Follow-up.** Re-fit `CONF_MS` and `CONF_WAIT_S` from the recorded lag
distribution once forward data exists.

### CHG-2026-09-05-009 — Record the book, the fill it refused, and the load behind every signal

**Commit:** `b59cece`
**Components:** `app/engine.py`, `app/store.py`,
`tests/test_signal_capture_context.py`, `tests/test_subthreshold_capture.py`

**Observed / original behaviour.** A signal row recorded the burst (`dl`,
`levels`, `size`, `ref`, `ext`) and, since the platform pass, the frame it
arrived on (`feed_lag_ms`, `proc_lag_ms`, `backlog`). It recorded nothing about
the market. **A declined signal therefore had no counterfactual at all**: 1,150
`unconfirmed` rows, hundreds of sleeve refusals and every `rejected_cap` /
`rejected_floor` row said what was refused but never what it would have filled
at. The raw L2 replay showed why that matters: the same 29 trades price at
-$862.96, -$228.72 or +$550.33 depending only on how entry is modelled.

**Root cause.** Design gap. The books were in memory at the decision and were
not read.

**Why necessary.** Without the fill a decline would have taken, the declined
population — most of the funnel — cannot be priced, so no threshold that
produces declines can be re-fitted from the database.

**Exact change.** `Engine.record_signal` extends the **existing**
`signals.context` JSON (no second column) with:

* `books`: per leg of the event, `{bid, ask, bid_size, ask_size, last, mid,
  spread_c}` from the live books, or `null` for a leg with no usable book;
* `spread_c`: the candidate's own leg;
* `fillable`: `{vwap, qty, levels, notional_usd, price_cap}` from walking
  `book.ask_ladder(side)` to `PRICE_CAP` for `NOTIONAL_USD` **without consuming
  anything** — deliberately the same walk as `PaperDesk.try_enter`;
* `load`: `{open_watches, open_positions, pending_candidates}`;
* `confirmation`: see CHG-2026-09-05-010.

`record_subthreshold` gets a deliberately cheap subset — the legs' top of book
and the spread, with no depth, no ladder walk and no load state — because those
rows are numerous by design (287 against 78 candidates in a two-hour replay).
Recorded prices are rounded to 3dp, because `best_yes_ask` is `100 - max(no_bids)`
in floating point and an exact 55c NO bid reads back as 44.99999999999999.

**No score, goal or event field is put on a signal.** Goal labelling stays a
join at analysis time, and the AST allowlist test
(`test_engine_price_only_decision_path_reads_no_match_feed_content`) stays
green.

**Before / after.** A `rejected_floor` row before: the refusal and nothing else.
After, from the demo smoke run: `fillable: {vwap: 7.03, qty: 1423.1, levels: 3,
notional_usd: 100.0, price_cap: 58.0}` — 1,423 contracts at 7c, which is exactly
the exposure `PRICE_FLOOR` exists to refuse, now visible on the row that refused
it.

**Reasoning and trade-offs.** A separate `signals.book_context` column was
rejected: the platform pass established `context` as the per-signal capture
column one day earlier and a second column would fragment it. Recording the full
ladder was rejected as unbounded on 1,511 rows; `{vwap, qty, levels}` is what
prices the entry. Giving sub-threshold rows the full context was rejected on
volume. **Capture is additive to collection and must never cost a row**, so the
enrichment is wrapped: a failure records `capture_error` in the row and keeps
the frame context, which is visible in the data rather than swallowed.
No statement and no commit is added to the hot path — the fields ride the
`insert_signal` that already happens (H2).

**Validation.** New tests assert the context on `filled`, `unconfirmed`,
`rejected_cap`, `sleeve_clock_stale` and `strategy_lockout` rows; that a leg
with no usable book is `null` and not invented; that `fillable` **equals what
`PaperDesk.try_enter` actually fills** on a laddered book (same vwap, same
quantity, three levels); that it respects `PRICE_CAP`; that a sub-threshold row
gets top of book and spread and explicitly **not** `fillable` or `load`; and
that a capture failure keeps the row and records the reason. The whole-tape
regression above confirms the funnel is unchanged. Suite 504 OK.

**Risks / limitations.** `fillable` mirrors `try_enter`, which is the non-V2
entry model; the V2 adapter walks a shadow book that may already be depleted by
an earlier fill, so on a market with a very recent fill the two can differ. The
row records what an *unconsumed* book offered, which is the right
counterfactual for a decline but is an upper bound for a second entry in quick
succession. Historical rows keep whatever context they had.

**Follow-up.** None.

### CHG-2026-09-05-008 — Serve and report the raw archive as one timeline

**Commit:** `88dcf17` (with CHG-2026-09-05-007; the two are one deployment)
**Components:** `app/exporter.py`, `app/main.py`, `tests/test_raw_archive.py`,
`tests/test_exporter.py`

**Observed / original behaviour.** `exporter.raw_inventory()` listed
`DATA_DIR/raw` and nothing else, and `GET /api/export/raw/{name}` resolved a
name to a file on that volume or returned 404. Once a segment is pruned to R2
(CHG-2026-09-05-007) both would report it as if it had never existed: an audit
bundle would silently describe 48 hours of feed where the archive holds
11 days, and a caller asking for `feed-20260825-18.jsonl.gz` would get a 404
for a segment that is intact in object storage.

**Root cause.** Design gap. Storage was about to become two places while every
reader still assumed one.

**Why necessary.** The operator's requirement was "make sure it's a continuity
to the Railway volume so it's not inconsistent". Without this half, extending
storage would create exactly the inconsistency it was meant to avoid: an export
whose manifest contradicts the archive, and a download path whose 404 means
either "never recorded" or "moved", with no way to tell which. That ambiguity
is unrecoverable after the fact, because the raw feed is the only record of
what the exchange sent.

**Exact change.** `raw_inventory()` with no argument now returns the union of
the volume and the `raw_segments` ledger as one ordered inventory, each item
carrying `name`, `bytes`, `sha256` and a `location` of `local` (on the volume
only), `both` (verified in R2 and still local) or `r2` (pruned). Passing
explicit paths keeps the old scoped behaviour, enriched from the ledger. The
ledger read is guarded: a missing or uninitialised database degrades to "the
volume is all we know about" rather than breaking a listing that used to work.

`GET /api/export/raw/{name}` is unchanged for a local segment (native file
response, HTTP Range as before). When the name is not on the volume it looks up
the ledger and, for a row with an `r2_key` and a `verified_ts`, streams the
object back under the same admin/cookie authorisation, passing `Range` straight
through to R2 and relaying the `206` with its `Content-Range`. A transport
fault is a 502 with `engine._record_error`, never a crash.

`archive.archive_continuity()` reports total segments, counts by state, local
and remote bytes, the first and last hour, and the list of missing hours
between them. It is built from the union deliberately: a segment sealed seconds
ago that the background task has not registered yet is still part of the
timeline. It is surfaced in `Engine.status()` under `archive` (from a snapshot
the background task refreshes off the loop, with a 30 s TTL when nothing else
does), in a new mode-scoped `GET /api/archive` alongside the ledger rows, and
in every study manifest under `archive`. `raw_segments` is in
`exporter.TABLES`, and a full bundle also appends the segments it could not
copy because they live in R2.

The report separates three facts that a half-configured deployment would
otherwise conflate: `enabled` (the switch), `credentialled` (R2 reachable), and
`active` (both, so the archive is actually running).

**Before / after.** Same input, an archive holding 11 days of segments with the
oldest 9 days pruned to R2. Before: `raw_inventory()` returns 48 entries; an
audit manifest describes 48 hours; `GET /api/export/raw/feed-20260825-18.jsonl.gz`
returns 404. After: the inventory returns all 176 entries, 128 with
`location="r2"`; the manifest additionally carries
`archive.missing_hours` naming the hours the bot was actually down; the same
GET returns the segment, and `Range: bytes=0-9` returns 206 with ten bytes.

**Reasoning and trade-offs.** The alternative was a separate "remote archive"
endpoint and a second inventory, leaving callers to merge two lists. Rejected:
that is precisely the two-stores-that-disagree failure the operator asked to
avoid, and every consumer (dashboard, audit bundle, future replay tooling)
would have to re-implement the merge correctly. Serving remote bytes through
the existing endpoint costs a proxied stream through this process rather than a
presigned redirect; a redirect was rejected because it would hand a URL bearing
archive credentials' authority to the browser, and the authorisation model here
is a single admin token.

`missing_hours` is capped at 720 entries with an explicit
`missing_hours_truncated` flag, so one stray old file cannot turn the status
payload into a million rows.

**Validation.** `tests/test_raw_archive.py` (32 tests, all against a local stub
S3 endpoint; no network) covers: a pruned segment is listed with
`location="r2"`, its recorded sha256, and is served whole and by range through
`/api/export/raw/{name}` with the gzip member intact end to end (40 lines
decompressed after the round trip); a local segment is still served from the
volume and no GET reaches R2; an unknown name is still 404; `/api/archive`
reports continuity and the ledger and contains neither credential; missing
hours between the first and last are reported exactly
(`20260901-12`, `20260901-13` for segments at 10, 11 and 14); local and remote
bytes are counted separately after a prune; an unregistered file is still part
of the timeline; and the manifest block equals the `Engine.status()` block key
for key. `tests/test_exporter.py` asserts an audit bundle still does not hash
bodies it did not copy (`sha256` is null, `location` stated) and that every
table in `TABLES`, now including `raw_segments`, is exported. Suite: 459 tests
OK in 52.1 s.

**Risks / limitations.** The continuity block in a bundle is computed from the
live ledger while the exported `raw_segments` table comes from the SQLite
snapshot, so an upload completing between the two edges can make them differ by
one row; the capture-boundary note already covers that class of drift, but it
is not zero. Serving a pruned segment streams it through this process, so a
large ranged read competes for the same uplink as the live WebSocket; it is
bounded by being admin-only and by Range support. `/api/archive` is readable
without the admin token, like the other observation endpoints: it exposes
segment names, sizes and states, and no credential, but it does reveal when the
bot was down.

**Follow-up.** The dashboard has no archive panel; continuity is available at
`/api/archive` and inside `/api/status` but is not rendered. `static/` is
untouched by this pass.

### CHG-2026-09-05-007 — Archive the raw feed to Cloudflare R2 under a verified continuity contract

**Commit:** `aac510f`, `88dcf17`
**Components:** `app/archive.py` (new), `app/config.py`, `app/store.py`,
`app/recorder.py`, `app/engine.py`, `scripts/r2_probe.py` (new),
`tests/test_raw_archive.py` (new), `tests/test_production_migration.py`

**Observed / original behaviour.** Measured in production on 2026-09-05: the
Railway persistent volume at `/srv/data` holds 4.00 GB of a 4.08 GB maximum
(peak over 48 h). Raw hourly segments are 2.94 GB of that — 176 files,
`feed-YYYYMMDD-HH.jsonl.gz`, from Aug 25 18:00 to Sep 5 — and the SQLite study
database is roughly the remaining 1 GB. An audit export already fails with
`study_export: database or disk is full` because the snapshot copy no longer
fits. New segments accumulate at ~300 MB/day. Nothing deleted anything, and
nothing could: the recorder only ever appends.

**Root cause.** Design gap, not a defect. The recorder was built on the
assumption that the volume is large enough, and there was no second tier of
storage and no record of what the archive contains. The next failure mode after
the export failure is worse than a failed export: SQLite on a full volume loses
writes silently, so the study database — the thing every measurement in this
log is computed from — degrades without an error anyone sees.

**Why necessary.** Without it the volume fills within days and live collection
starts losing observations with no signal. Deleting old segments to make room
is not an option: the raw feed is the only record of what the exchange actually
sent, and it is what will replace the print-constrained fill model. The
operator's requirement was explicit — extend storage to Cloudflare, "but make
sure it's a continuity to the Railway volume so it's not inconsistent" — which
rules out any design where the volume and the remote store can disagree about
what exists.

**Exact change.** A new `raw_segments` table (additive, idempotent migration in
`store.init()`) is the source of truth for what the archive contains. Every
segment is in exactly one state, recorded durably and never inferred from a
directory listing: `local`, `uploaded` (verified in R2, still on the volume),
`pruned` (verified in R2, removed from the volume). There is no state in which
a segment is neither on the volume nor verified remotely.

`app/archive.py` holds `SigV4Signer`, `R2Client` and `RawArchive`.
`RawRecorder` gained one hook: `_rotate` and `checkpoint_for_export` call
`on_sealed(path)` for a segment that will never be appended to again. The hook
does one set insertion and never touches SQLite, because `_rotate` runs on the
WebSocket path; the archive's own pass reconciles the directory anyway, so a
dropped hook costs promptness, never correctness. Startup reconcile registers
any `feed-*.jsonl.gz` that is not the active hour and not in the table as
`local`.

Uploads are one PUT (R2 allows 5 GB; the largest segment here is 118 MB), the
body streamed from the file handle so 118 MB is never held in memory, with
`x-amz-content-sha256` computed in a separate streaming pass. Verification is
two independent checks: the returned `ETag` must equal the md5 computed while
reading the file, then a `HEAD` must report the same content length as the
local file. Only then does the row become `uploaded` with `verified_ts`. A
mismatch leaves it `local`, records `last_error`, counts an attempt and retries
with exponential backoff up to `RAW_ARCHIVE_MAX_ATTEMPTS`; a segment that
exhausts its attempts is kept on the volume, never deleted.

Pruning is bounded and deliberate: a verified segment older than
`RAW_LOCAL_RETENTION_HOURS` (48), or, when free space on `DATA_DIR` is below
`RAW_ARCHIVE_MIN_FREE_MB` (512, via `shutil.disk_usage`), oldest first until the
floor is cleared. `RawArchive._prune_one` is the only place in the codebase
that deletes a recorded segment (`app/archive.py:750`), and every guard is
re-checked there — against SQLite and against R2 — at the moment of deletion
rather than when the candidate list was built: the row still exists and is
`uploaded`; it carries `verified_ts` and an `r2_key`; it is not the hour the
recorder is appending to; the file on disk is still exactly the size that was
verified; a live `HEAD` confirms the remote object is still there at that size;
and the ledger flip `uploaded -> pruned` succeeded, itself a conditional UPDATE
requiring `verified_ts IS NOT NULL`. Every prune, upload, verification and
failure is written to the feed-health ledger as `archive_uploaded`,
`archive_verified`, `archive_pruned` or `archive_error`.

"Sealed" is deliberately stricter than "not the current hour": after a quiet
hour boundary the recorder's gzip handle is still open on the PREVIOUS hour's
file, so the active set is the current wall-clock hour AND the recorder's open
hour. Uploading the latter would archive a truncated gzip member.

SigV4 is implemented with stdlib `hmac`/`hashlib` over the existing `httpx` —
no boto3, no aiobotocore — against `https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
path-style, region `auto`, service `s3`. Everything runs off the event loop:
one background task awaits `tick()` inside `asyncio.to_thread`, with backoff,
and a failure records `last_error`, a feed event and `engine._record_error`
without ever reaching the recorder, the WebSocket path or the paper desk. The
first pass registers the existing backlog and drains it oldest-first, one
upload at a time with a pause between, so 2.94 GB moves without saturating the
uplink the live socket shares.

`scripts/r2_probe.py` is an operator tool, not part of the suite: it performs
PUT/HEAD/GET/Range/DELETE against the real bucket with one small object under
`probe/` and deletes it again, so connectivity can be confirmed after deploy
without touching `raw/`.

**Before / after.** Same input, the production volume as measured. Before:
2.94 GB of segments on a 4.08 GB volume, growing 300 MB/day, audit export
failing with `database or disk is full`, and no record of what the archive
contains. After, with `RAW_ARCHIVE_ENABLED=true` and credentials: the 176-file
backlog uploads oldest-first and is verified; everything older than 48 h is
removed from the volume only after that verification, leaving roughly 600 MB of
recent segments locally; the ledger names every segment and its state; and both
the deleted and the retained segments remain downloadable. With the switch off
or credentials absent the process behaves exactly as before: no task is
created, no row is written, no byte is deleted.

**Reasoning and trade-offs.** Options rejected:

- *Delete old segments outright.* Fastest fix, permanent data loss. The raw
  feed is the study's irreplaceable input.
- *A bigger volume.* Buys weeks at 300 MB/day and moves the same failure later,
  without making the archive describable.
- *boto3/aiobotocore.* Would have made the S3 half trivial, but
  `requirements.txt`/`.lock` are pinned and minimal and this runs beside a live
  trading loop; ~50 lines of verified SigV4 is a smaller risk surface than a
  large transitive dependency tree. The signer is therefore validated against
  the published AWS test vectors rather than trusted.
- *Multipart upload.* Unnecessary below 5 GB, and it would have cost the
  strongest verification available: for a single PUT the ETag IS the md5 of the
  body, which a multipart ETag is not.
- *Prune on the strength of the upload response alone.* Rejected. The prune
  path re-HEADs the object immediately before deleting, costing one request per
  prune. If R2 is unreachable the prune is refused and the volume stays fuller
  for another minute — the correct direction to fail.
- *Presigned redirects for reads.* Rejected; see CHG-2026-09-05-008.

**Validation.** `tests/test_raw_archive.py`, 32 tests, entirely against a local
stub S3 endpoint started in a thread — no network. The stub recomputes the
SigV4 signature of every request it receives and refuses a mismatch, so each
upload test is also an end-to-end test of the signer.

The signer is validated offline against the published AWS Signature Version 4
test vectors: `get-vanilla` (full Authorization header compared byte for byte,
signature `5fa00fa3...fbf31`), `post-vanilla-query`
(`28038455...f7f11`), and the two S3 worked examples — GET Object with a signed
`Range` (`f0e8bdb8...bdb41`) and PUT Object with a non-empty payload
(`98ad7217...108bd`, body hash `44ce7dd6...8b072`). All four match exactly.

Behaviour covered: a sealed segment uploads, verifies and flips state with its
sha256, md5, ETag and byte count recorded, the stub's bytes equal the file's,
and the local copy is kept; an ETag mismatch leaves the row `local` with
`attempts=1`, does NOT prune even with retention forced to zero, and succeeds
on retry; a HEAD length mismatch does the same; exhausted attempts keep the
segment; the active hour is never registered, uploaded or pruned, and neither
is the recorder's open hour; retention prunes oldest-first and keeps a segment
inside the window; the low-disk floor prunes exactly as many as the floor
requires, oldest first; a segment appended to after verification is refused; a
segment missing from R2 is refused; a five-file backlog drains oldest-first two
per pass; the recorder's rotation hook seals only the previous hour; a failing
tick never crashes the task; and `tick` runs on a worker thread, not the main
thread. With the feature disabled: nothing registered, no request made, no file
touched, no event, no error, and `R2Client.from_config()` is None.

Secret hygiene: a test asserts none of `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY` or `R2_ACCOUNT_ID` appears anywhere in a full study
bundle or in `exporter.non_secret_config()`, and that none of the storage knobs
is in `config.STRATEGY_PARAM_NAMES` — changing retention, the free-space floor
or the switch leaves `config_id()` unchanged.

`tests/test_production_migration.py` migrates a production-shaped database
twice and asserts the ledger appears empty, keeps a verified segment's stamps
across remigration, and refuses to mark an unverified segment pruned.

Trading behaviour: `strategy_params()` is byte-identical before and after
(diffed as sorted JSON). No detection, confirmation, sizing, entry, exit, fee,
lockout or settlement code was touched; the `engine.py` diff is construction,
the status block and task startup only. The price-only sleeve's AST allowlist
test stays green. Because `config.py` and `engine.py` are strategy sources,
`CODE_FINGERPRINT` moves (`f74a5bed98d2` -> `444a612f5f8e` in this
environment) and with it `config_id` (`01ed0f686eabf351` ->
`788061d1dfc317bc`); that is the provenance stamp working as designed, not a
decision change.

Demo-mode smoke run (`MODE=demo DATA_DIR=/tmp/fbarch uvicorn app.main:app
--port 8098`): `/api/status` reports `archive.enabled=false`,
`active=false`, `failures=0`, `last_tick_ts=null`, `health.ok=true`;
`/api/archive` returns an empty timeline; `raw_segments` has 0 rows and the
feed-health ledger has 0 `archive_*` events; no traceback in the server log. A
second run with `RAW_ARCHIVE_ENABLED=true` and no credentials is equally inert
(`enabled=true`, `credentialled=false`, `active=false`, `last_tick_ts=null`,
0 rows, 0 tracebacks) — the fail-closed path.

Full gate: 459 tests OK (52.1 s) under
`python -X dev -W error::RuntimeWarning`, `compileall` clean,
`ruff check --select E9,F63,F7,F82 app tests scripts` clean, `node --check
static/app.js` clean, `git diff --check` clean.

**Risks / limitations.** Not exercised against the real R2 endpoint from this
branch: every archive test uses a local stub. `scripts/r2_probe.py` exists
precisely because that gap can only be closed with credentials on the
deployment. The retry backoff between attempts is in memory, so a restart
retries immediately; the durable cap is `attempts` in SQLite, which is what
bounds it. Deleting an object in R2 by hand while its row says `pruned` would
leave a segment recorded as archived that no longer exists — the prune-time
HEAD prevents this process from creating that state, but nothing outside this
process is guarded. R2 egress and storage cost is not modelled anywhere. The
archive does not verify a previously pruned segment periodically; verification
happens at upload and again immediately before the delete, and not after.
Nothing here reduces the ~1 GB the SQLite database itself occupies, so the
volume pressure returns eventually from that side.

**Follow-up.** Deploy needs `RAW_ARCHIVE_ENABLED=true`, `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET` set on the Railway
service, then `scripts/r2_probe.py` run once to confirm connectivity, then the
first backlog drain watched through `/api/archive` (`by_state`) and the
feed-health ledger. A study-database retention pass is a separate change.
**Capture pass (plan items B3-B7), same day, same branch.** The platform-pass
preamble above is left exactly as written; it describes that pass and stays
true of it. What follows is the second pass of the 2026-09-05 plan.

**Commits:** `b59cece`, `b5e0470`, `d743a17`, `a900e6d`, plus the documentation
commit that adds these entries.
**Base:** `25edb0e` (head of the platform pass above).
**Diff totals:** 24 files, +2,558 / -86 (this section excluded): `app/`
+916 / -72 across 12 files, `tests/` +1,580 / -9 across 10 files,
`README.md` + `.env.example` +62 / -5.
**Suite:** 425 tests OK before (39.2 s), 504 tests OK after (50.3 s), under
`python -X dev -W error::RuntimeWarning -m unittest discover -s tests`.
**Deployment status:** NOT DEPLOYED. Nothing in this pass has run in
production. Every number quoted below as production evidence was measured on
the deployed 2026-09-04/05 build or on raw tape recorded by it, never on this
one.

**Configuration identity.** One strategy parameter is added
(`PAPER_MAX_BOOK_AGE_MS`, default 0), and six strategy sources change
(`books.py`, `config.py`, `detector.py`, `engine.py`, `execution.py`,
`paper.py`), so both halves of the identity move: `CODE_FINGERPRINT`
`f74a5bed98d2` -> `34df105ef152` and `config_id` `01ed0f686eabf351` ->
`b504892fe63910bb` in this environment. `STRATEGY_PARAM_NAMES` goes from 48 to
49 entries. Rows written after this deploys will not pool with earlier rows in
a current-configuration aggregate. That is the intended behaviour of the
provenance stamp. `EVENT_MATCH_WINDOW_S` and the two `PATH_THIN_*` knobs are
deliberately NOT in that list — see CHG-2026-09-05-019 and -013.

**Whole-tape regression check.** The bundled real tape (all three legs of
Espanyol vs Real Madrid, 26,845 prints merged and time-ordered) was replayed
through a full `Engine` at default settings on `25edb0e` and on this tree, with
`time.time` driven off the tape so the two runs are comparable. The traces are
**identical row for row**: 49 signal rows with identical
`(ts_ms, market, dir, dl, levels, size, ref, ext, conf_lag_ms, late, outcome)`
— 41 `subthreshold`, 5 `unconfirmed`, 2 `rejected_floor`, 1 `filled` — and one
closed trade with identical `entry_px`, `size`, `exit_px`, `exit_reason`,
`gross`, `fees`, `net`, `mae` (net +$103.83, exit `target`). `strategy_params()`
is byte-identical once the new knob is removed. That is the evidence that Gate A
detection, confirmation, sizing, entry, exit, fee, lockout and settlement are
unchanged.

The one intended difference on the same tape is path volume: 2,255 persisted
path rows before, 1,906 after (-15.5%), across the same 9 path owners, with the
per-owner `(min bid, max bid)` identical for **all 9** — thinning removed rows,
not extremes. See CHG-2026-09-05-015.

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
