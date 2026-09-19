# Research log: repricing-episode exit pilot (single event)

**Date:** 2026-09-19
**Status:** Complete as far as available data permits. Nothing applied to any parameter.
**Credits spent:** 25 of the 25,000 monthly allowance on `pmxt.dev`.

---

## 0. The one thing to read first

The study was specified against the `pmxt.dev` API. **That API cannot supply
the data the study needs**, and the reason is capability, not budget — 25 of
25,000 credits were spent establishing it. Section 1 records exactly what was
probed so nobody repeats it.

The analysis was therefore run against the only real high-resolution football
tape in this environment: `demo_data/KXLALIGAGAME-26AUG22ESPRMA-TIE.jsonl`,
6,739 recorded Kalshi trades on the Tie leg of Espanyol vs Real Madrid,
2026-08-22, with microsecond stamps, sizes and taker sides.

Two consequences, both load-bearing:

1. **There is no order book anywhere in this pilot.** Section 4C of the
   specification — favourable-side depth disappearing, opposing-side depth
   building, imbalance reversing — is **untested and untestable here**. What is
   testable is the spread and the executable bid, because both can be
   reconstructed from executed prints (§2).
2. **The event is a tie being broken, not an equalizer.** The Tie leg
   *collapses* (0.83 → 0.01), so the side whose executable price rises is NO.
   The position is long NO on the Tie market. The requested "Tie YES series" is
   the series analysed; the sign of the tradeable side is the opposite one.

`n = 1`. Nothing here is promotable. It generates challengers, as the
2026-09-04 Polymarket log did.

---

## 1. Why `pmxt.dev` could not be used

Every documented route to historical trades or historical L2 was tried. The key
is on the Free plan (25,000 credits/month, 60/min).

| Route | Result |
|---|---|
| `POST /v0/sql` (ClickHouse; the documented bulk route) | **403** `sql_access_denied` — "SQL query access requires an Enterprise plan" |
| `fetchOrderBook` + `since`/`until`, Kalshi ticker | **502** `Historical order book query failed` |
| … Kalshi market UUID; `since` only; UUID + `outcome` alias | **502**, all three shapes |
| … Polymarket token id; conditionId + `outcome`; GET form | **502**, all three shapes |
| `fetchOrderBook` live, settled Polymarket market | 404 (expected: market closed) |
| `fetchTrades` with `start`/`end` | **200, but the window is ignored** |
| `fetchTrades`, Kalshi soccer | `[]` |
| `fetchOHLCV`, Kalshi | 404 · Polymarket | `[]` |
| `archive.pmxt.dev` (bulk Parquet the docs point to) | unreachable through the environment's network policy (connection reset) |

The `fetchTrades` result is the one worth dwelling on, because it returns
HTTP 200 and looks like data. Asked for 2026-06-14, it returned 1,000 rows
**all stamped within the last 90 seconds of wall clock**, carrying prices of
0.25, 0.51 and 0.72 *inside the same second* on a single outcome. That is not a
single-market tape. A caller who trusted the status code would have analysed a
synthetic live stream and called it match history.

The catalog itself is static: no market in it is open — every row is
`finalized`, `determined`, or null-status with zero price.

So T0/T1 identification, the L2 window, and the price-aware exit analysis are
all blocked at the data layer for this API. The optional fifth request was
never reached and no L2 snapshot was ever retrieved; **the entire requested L2
window is an unobserved gap.**

---

## 2. Reconstructing executable prices from executed prints

Kalshi reports every print as a YES-equivalent price plus the taker's side,
which is enough to name the side of the resting order that was consumed:

    taker_side == "no"    a taker bought NO, eating a resting YES bid
                          -> NO ask = 1 - print price        (what entry pays)
    taker_side == "yes"   a taker bought YES, eating a resting NO bid
                          -> NO bid = 1 - print price        (what exit receives)

Every price in §4 is therefore a trade that really happened, on the correct
side, at that instant. No midpoint, no last price, no settlement value is used,
which is the repository's standing invariant. The executed spread is the gap
between a print and the most recent print on the other side; 1,976 of the 4,231
exit observations have a paired ask fresh within 1s.

**An early version of this harness carried the last quote forward for up to 3
seconds.** That is fatal in precisely the moment that matters: during the sweep
the last NO bid seen is tens of cents stale, and three exit rules "filled" at
0.38 when no such bid existed. The harness now uses executed prints only. This
is the same defect the change log records as pricing against a stale book.

Fees use the live quadratic taker schedule, charged both sides:
`ceil(0.07 · m · n · p(1−p) · 100)/100`.

---

## 3. The repricing episode

Thresholds are calibrated on the first 300s of the tape — a stretch certainly
quiet and strictly before anything traded. `--cal-secs` is the only hand-set
number in the detector.

| Quantity, measured on the quiet stretch | Value |
|---|---|
| update rate | 1.27 prints/s (median gap 431 ms) |
| spread | median 1.00¢, p90 1.00¢ |
| worst single-print dislocation | 2¢, across 2 price levels |
| largest 1s move of the executable bid | 2¢ |

**Onset needs two tests, because either alone is fooled.** A lone off-level
print clears a displacement test by itself and this tape contains several, so a
sweep must also eat ≥3 distinct price levels inside one 150 ms burst. And a
burst that dislocates then snaps back is a blip, so the executable bid must
still be ≥10¢ away 500 ms later.

**20 bursts before the event cleared the burst test and failed the hold test** —
4–11¢ dislocations across 3–8 levels at 21:03, 21:12, 21:17, each holding
+0.0¢ to −10.0¢. Without the hold test this tape produces 20 false entries
before the real one. That is the single most useful entry-side result here.

| | |
|---|---|
| **T0** | 21:21:15.897 — 21¢ across 3 levels, then 55¢ across 11 levels 95 ms later |
| **T1** | 21:21:25.793 |
| **duration** | **9.9 s** |
| **total move** | **72¢** on the executable NO bid (0.17 → 0.89) |
| continuation waves | 4, separated by 3 pauses (median 1.6 s, max 2.4 s) |

T1 is the last instant the tape moved ≥3¢ in a second — the rate the quiet
market never reached. It is *not* the end of all progress: the bid drifted a
further 10¢ to 0.99 over the next four minutes, at ordinary velocity. That
distinction matters for §4, because the best exits harvest part of that drift.

The intra-episode pauses (up to 2.4 s) are longer than several of the fast exit
rules' entire holding periods, which is why those rules fire inside the move.

Match minute is unavailable: this tape carries no provider clock, so the
2026-09-04 log's Finding B (the minute axis is not trustworthy) is neither
confirmed nor addressed.

---

## 4. Entry, and what the confirmation costs

The sweep's prints share a timestamp, so their order is not recoverable and **no
fill inside the burst can be honestly justified**. Entry is the first executed
NO ask strictly after confirmation: **0.73, at 21:21:16.716, 819 ms after the
sweep.**

The bot is therefore buying the *tail*: 56¢ of the 72¢ episode is gone before it
can act, and 26¢ of executable continuation remains.

| confirm delay | entry ask | continuation left | false onsets rejected |
|---|---|---|---|
| 0 ms | *no confirmed onset* | — | — |
| 150 ms | 0.73 | 26.0¢ | 50 |
| **500 ms** | **0.73** | **26.0¢** | **20** |
| 1000 ms | 0.75 | 24.0¢ | 20 |

**Waiting was almost free, and waiting zero is impossible.** At 0 ms the bid has
not re-quoted yet — the first NO bid after the sweep is 265 ms later — so there
is nothing to confirm against. Between 150 ms and 500 ms the fill is identical
because the sweep overshot and the market retraced a tick, while the false-onset
count drops from 50 to 20. On this event, patience cost nothing and bought a
2.5× reduction in false triggers.

---

## 5. Candidate exits

Priced on the executable NO bid, 100 contracts, taker fees both sides.
`capture` is the specified ratio: `(exit − entry) / (best executable bid reached
after entry − entry)`, the denominator being 0.99.

| rule | exit | hold s | upd | gross | net | capture | gave back from peak | left after exit |
|---|---|---|---|---|---|---|---|---|
| A continuation-failure, 10 upd | 0.73 | 0.4 | 10 | 0.0¢ | −2.8¢ | 0.0% | 1¢ | 26¢ |
| A continuation-failure, 25 upd | 0.70 | 0.4 | 25 | −3.0¢ | −5.9¢ | −11.5% | 4¢ | 29¢ |
| A continuation-failure, 50 upd | 0.73 | 3.3 | 51 | 0.0¢ | −2.8¢ | 0.0% | 4¢ | 26¢ |
| **A continuation-failure, 100 upd** | **0.94** | **35.2** | **236** | **21.0¢** | **19.2¢** | **80.8%** | **1¢** | **5¢** |
| B reversal, 10 / 20 / 30 / 40% | 0.73 | 3.1 | 45 | 0.0¢ | −2.8¢ | 0.0% | 4¢ | 26¢ |
| C spread blow-out ×2 | 0.69 | 0.4 | 21 | −4.0¢ | −6.9¢ | −15.4% | 5¢ | 30¢ |
| C spread blow-out ×3 | 0.67 | 0.8 | 28 | −6.0¢ | −8.9¢ | −23.1% | 7¢ | 32¢ |
| D velocity regime, h=0.5s | 0.68 | 0.8 | 27 | −5.0¢ | −7.9¢ | −19.2% | 6¢ | 31¢ |
| D velocity regime, h=1.0s | 0.73 | 2.9 | 38 | 0.0¢ | −2.8¢ | 0.0% | 4¢ | 26¢ |
| D velocity regime, h=2.0s | 0.90 | 12.8 | 84 | 17.0¢ | 15.0¢ | 65.4% | 0¢ | 9¢ |
| E plateau, 25 upd no new high | 0.92 | 85.8 | 397 | 19.0¢ | 17.1¢ | 73.1% | 5¢ | 7¢ |
| E plateau, 50 upd | 0.93 | 99.0 | 422 | 20.0¢ | 18.2¢ | 76.9% | 4¢ | 6¢ |
| E plateau, 100 upd | 0.93 | 119.3 | 472 | 20.0¢ | 18.2¢ | 76.9% | 4¢ | 6¢ |
| **E plateau, 200 upd** | **0.95** | **161.4** | **572** | **22.0¢** | **20.3¢** | **84.6%** | **2¢** | **4¢** |
| *T fixed 1s (diagnostic)* | 0.73 | 1.5 | 35 | 0.0¢ | −2.8¢ | 0.0% | 1¢ | 26¢ |
| *T fixed 2s* | 0.77 | 2.9 | 36 | 4.0¢ | 1.4¢ | 15.4% | 0¢ | 22¢ |
| *T fixed 5s* | 0.81 | 5.2 | 54 | 8.0¢ | 5.5¢ | 30.8% | 0¢ | 18¢ |
| *T fixed 10s* | 0.90 | 10.2 | 74 | 17.0¢ | 15.0¢ | 65.4% | 0¢ | 9¢ |
| *T fixed 20s* | 0.94 | 20.1 | 135 | 21.0¢ | 19.2¢ | 80.8% | 0¢ | 5¢ |
| *T fixed 30s* | 0.95 | 30.0 | 209 | 22.0¢ | 20.3¢ | 84.6% | 0¢ | 4¢ |
| *T fixed 60s* | 0.96 | 60.1 | 305 | 23.0¢ | 21.3¢ | 88.5% | 0¢ | 3¢ |
| *T fixed 120s* | 0.93 | 121.2 | 473 | 20.0¢ | 18.2¢ | 76.9% | 4¢ | 6¢ |
| *R hold to end of tape* | 0.99 | 408.9 | 807 | 26.0¢ | 24.6¢ | 100.0% | 0¢ | 0¢ |

`upd` counts executed prints, not book updates; no book update count exists here.

### What this says

**The clean split is fast versus slow, not clever versus simple.** Every rule
that reacts inside the first ~3 seconds — A at 10–50 updates, B at every
fraction tested, C at both multiples, D at 0.5s and 1.0s — exits between 0.67
and 0.73 and **loses money after fees**, leaving 26–32¢ of continuation on the
table. Every rule with a horizon of tens of seconds captures 65–85%.

The reason is in §3: the episode contains internal pauses of up to 2.4 s. A
rule whose memory is shorter than the market's own pauses cannot tell a pause
from an ending, and this event punishes that directly.

**Reversal-from-extreme (B) is degenerate here.** All four fractions exit at the
identical print, even armed so the move must exceed the prevailing spread
before the rule engages. Immediately post-entry the favourable move is small,
so 10% and 40% of it are the same one or two ticks, and an early 4¢ pullback
trips all of them. Measuring reversal as a fraction of a move that is still
forming does not work at this timescale; it needs a floor in ticks or a minimum
elapsed continuation, and this single event cannot fit one.

**Spread-based exit (C) is actively perverse on this event.** The spread is at
its widest *during* the most violent continuation — the very moment to stay in.
Exiting on spread expansion is exiting on the signature of the opportunity.

**The best causal rule is E at 200 updates: 84.6% capture, 20.3¢ net, 2¢
surrendered from the peak, 4¢ left afterwards.** A at 100 updates is close —
80.8%, 19.2¢ net — and reaches its exit in 35 s rather than 161 s, so it
surrenders less time and less reversal exposure for 4 points of capture.

**Did it exit for the right reason?** Partly. E fires because the executable bid
sets no new high for 200 consecutive prints while prints keep arriving — a
genuine, observable plateau, and the plateau is real: T1 was 135 s earlier. But
it fires *long* after the violent phase ended, harvesting the slow drift to
settlement rather than recognising exhaustion. It is a correct observation
arriving late, not a sharp detection of the turn. A at 100 updates has the
better claim: it exits 35 s in, 25 s after T1, having watched 100 updates
produce no net progress.

**The fixed 60s diagnostic captures the most (88.5%) and is not selected**, per
the specification. Its margin over the best causal rule is 3.9 points. That
margin is the honest measure of what the causal machinery costs on this event,
and it is small.

---

## 6. The research question

> Can a bot that sees only prices and the order book recognise that the violent
> repricing has largely exhausted itself, without knowing a goal occurred and
> without relying on an arbitrary timer?

**On this one event, price alone was sufficient to get most of the way, with two
firm caveats.**

- Exhaustion *was* detectable from the tape without a timer and without knowing
  a goal happened. The best causal rule took 84.6% of the executable
  continuation and surrendered 2¢ from the peak, against 88.5% for the best
  fixed timer.
- It was detectable **only on a horizon of tens of seconds**. Nothing that
  reacted within a few seconds of entry was profitable after fees. The
  distinction the specification asks for — a pause inside the episode versus
  the end of it — is not resolvable at the timescale on which the repricing
  itself happens.
- The detection was **late rather than sharp**. Both surviving rules recognise a
  plateau well after T1. Neither identifies the turn; they identify that the
  turn has been over for a while. On this event that was cheap, because the
  price kept drifting favourably. On an event that reverses after T1, the same
  lateness would be expensive, and **this pilot cannot tell the difference from
  one observation.**

The entry-side result is firmer than the exit-side one: requiring a sweep to
*hold* for 500 ms removed 20 false onsets at no measurable cost in fill price.

---

## 7. What would falsify this

- If the depth terms of §4C, once real L2 exists, fire *earlier* than the
  plateau rules and keep their capture, then the exit problem is a book problem
  and the trade-only conclusion above is an artifact of missing data.
- If, across many events, the bid after T1 reverses as often as it drifts on,
  the late-plateau rules lose their advantage and the 3.9-point gap to the fixed
  timer inverts.
- If the 500 ms hold test, applied across many matches, also rejects real goals,
  its 20-to-0 record here is a single-event accident.
- If an equalizer (Tie YES rising) behaves asymmetrically to this tie-break —
  plausible, since the collapsing leg drifts to 0 while a rising draw leg must
  survive further play — none of §5 transfers to the case the specification
  actually asked about.

## 8. How to resume

1. `scripts/repricing_episode_pilot.py <tape.jsonl>` reproduces every number
   above. It takes the recorder's JSONL shape.
2. The L2 half needs a source that actually serves historical books. `pmxt.dev`
   on the Free plan does not; its SQL route requires Enterprise and its archive
   route is unreachable here.
3. The highest-value next step is unchanged from the 2026-09-04 log: the replay
   engine over the recorded Kalshi feed in `PRICE_ONLY_BACKTEST_HANDOFF.md`.
   That feed has real depth and millisecond stamps, which is exactly what §4C
   needs and what no API in this environment provided.
