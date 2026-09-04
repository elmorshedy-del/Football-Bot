# Research freeze: Polymarket cross-venue timing and gate study

**Date:** 2026-09-04
**Status:** FROZEN mid-study. Nothing here has been applied to any parameter.
**Author context:** written so this can be resumed cold, by someone who was not
present for the analysis.

---

## 0. Why this exists, and the one thing to read first

The strategy's numeric parameters were educated guesses. The live Kalshi study
was too small and too contaminated to settle them, so an external dataset was
used to answer the questions that do not need millisecond resolution.

**Read this before trusting anything below:** every finding in this document
comes from **Polymarket**, not Kalshi. Three differences matter and are not
cosmetic:

1. **Polymarket charges no trading fee.** Kalshi's quadratic fee is roughly
   2¢/contract round trip and consumes essentially the whole observed gross
   edge. Every simulation here therefore applies a Kalshi fee model explicitly;
   if you re-run anything, keep that.
2. **No historical order book exists on Polymarket.** Entries are simulated at
   the traded price with no spread, depth or slippage. **Every return in this
   document is an upper bound.**
3. **Trade timestamps are whole seconds.** Nothing here can speak to Gate A's
   150 ms burst window or its ±50 ms sibling window.

Nothing in this document is promotable on its own. It generates challengers to
be tested forward on the Kalshi feed, which is the only venue that is actually
traded.

---

## 1. Dataset

| | |
|---|---|
| Source | Polymarket Gamma API (`/events`) + Data API (`/trades`) |
| Matches | 462 completed 1X2 match-result events |
| Trades | 1,128,728 |
| Leagues | Primeira Liga 97, Champions League 91, Bundesliga 58, Ligue 1 58, La Liga 55, EPL 54, MLS 49 |
| Local file | `pm_matches.jsonl`, ~98 MB, one JSON object per match |

Selection rule for a full-time 1X2 event: exactly three markets, slug matching
`<league>-<home>-<away>-<date>` with no derivative suffix (`-first-to-score`,
`-halftime-result`, `-second-half-result`), one leg titled as a draw, all three
markets closed, and a non-null `gameStartTime`.

`outcomePrices` gives true settlement per leg. `gameStartTime` gives scheduled
kickoff.

**Reproduction scripts** (kept out of the repo; they are throwaway research
tooling, recreate from this spec if needed):

| Script | Purpose |
|---|---|
| `pm_collect.py` | discovery + serial fetch |
| `pm_collect_fast.py` | threaded fetch, appends JSONL, resumable |
| `pm_minute.py` | naive minute mapping, EV-vs-gate curve |
| `pm_shock.py` | shock magnitude / adverse excursion by minute |
| `pm_calib.py` | per-match halftime detection and recalibration |
| `pm_sibling.py` | coherence vs reversal, exit-rule simulation |

Two gotchas that cost time: Polymarket returns **HTTP 403 to the default urllib
User-Agent** (set a browser-like one), and `/book` is current-state only and
404s on a closed market, so there is no historical depth to fetch.

---

## 2. Finding A — late repricing is bigger and safer (CONFIRMED)

The operator's hypothesis, in his words: a goal is the same event at minute 45
and minute 90, but its price impact is not. At 45 the market still prices in
time for the favourite to restore order. At 90 there is no time left, so price
is forced most of the way to settlement. Same news, larger move, less room
afterwards for anything to go against you.

Measured across 3,660 upward shocks (≥3¢ in 3 min, low threshold on purpose so
early small moves are not excluded by construction):

| Minute (uncalibrated) | Median move | Median adverse after | R/R |
|---|---|---|---|
| 70-72 | 0.090 | 0.230 | 0.39 |
| 76-78 | 0.081 | 0.115 | 0.70 |
| 82-84 | 0.090 | 0.120 | 0.75 |
| 84-86 | 0.100 | 0.100 | 1.00 |
| 86-88 | 0.100 | 0.072 | 1.39 |
| 88-90 | 0.100 | 0.035 | 2.83 |
| 90-92 | 0.130 | 0.030 | 4.33 |

Draw leg only (the equalizer case), 1,025 shocks: median move triples from 0.050
(minute 0-30) to 0.160 (90-94) while median adverse falls from 0.200 to 0.010.

**This is the most robust result in the study.** It appears under every clock
mapping tried, and random timing noise can only blur a transition, never create
one.

---

## 3. Finding B — the minute axis is not trustworthy (LIMITATION)

The operator challenged the clock accuracy. He was right, and the error is
larger than the effect being resolved.

The first pass mapped wall clock to match minute with a fixed rule: first half
ends at elapsed 47, halftime 15 min, then `minute = elapsed - 15`. That assumes
a punctual kickoff, fixed first-half stoppage and a fixed break.

Two independent measurements of the error:

**Method 1, halftime density.** Trading goes quiet during the break because no
new information arrives. Aggregate density across 462 matches is flat to elapsed
48, troughs at 56-64, recovers from 66, and peaks at 110-114. Per match
(n=382 with enough trades) the halftime centre is median elapsed 60, IQR 55-64.
The fixed rule implies 54.5. Bias ≈ **+5.8 min**.

**Method 2, real goal minutes.** A sub-agent fetched actual goal minutes for 20
matches from public match reports. Aligning them to price jumps:

- First-half goals: median offset **+1.4 min**. Near-perfect. This validates the
  agent's data, the scheduled kickoff, and the first-half mapping simultaneously.
- Second-half goals, well-aligned cases: offsets cluster at **+17 to +22 min**
  against the assumed +15. Bias ≈ **+5 min**.

**Both methods agree: the first pass ran about 5 minutes late.** An event
labelled "minute 88" was really about minute 83.

**Residual error is large.** Per-match halftime IQR is 9 min; final-whistle
peak IQR is 11 min. The goal-alignment failed outright on 7 of 19 matches
(jump count did not match goal count) and produced impossible offsets on
several more. Recalibrating per match made the R/R curve *noisier*, not
cleaner, because the break detector has its own error (median detected break
11 min, p95 30 min, against a real halftime of ~15).

**Consequence:** the inflection is somewhere around **minute 80-85**, not 88,
with several minutes of uncertainty either side. The claim "88 is optimal" is
**withdrawn**. The claim "88 is probably a few minutes late" is supportable.
The claim "set it to 83" is not.

**This is why the minute question belongs on Kalshi.** `match_clock_observations`
carries `provider_minute` straight from the exchange, with no inference at all.
Polymarket buys sample size and pays for it in the exact variable being measured.

---

## 4. Finding C — sibling coherence is a weak gate, blind to the real risk

The operator's concern: false positives from a goal reversed by VAR or offside,
and from a penalty awarded but then missed.

**Structural point, established before testing:** both events produce *genuine,
coherent* three-leg reallocations, because the market really does reprice.
Sibling confirmation tests whether the market believes something happened, not
whether it will stand. It is therefore structurally blind to exactly these two
cases.

Coherence is defined as the sleeve defines it: in a market whose legs sum to ~1,
a leg rising by `d` should be matched by the others falling by a combined `d`.

    coherence = -(delta_other1 + delta_other2) / delta_target

Across 2,038 shocks (≥6¢ in 3 min, all three legs present). "Reverted" means
giving back ≥60% of the move within 6 minutes:

| Coherence band | n | Reverted | Median given back |
|---|---|---|---|
| Incoherent (<0.3) | 991 | 37.4% | 0.27 |
| Partial (0.3-0.7) | 354 | 35.6% | 0.29 |
| Coherent (0.7-1.3) | 478 | 27.6% | 0.15 |
| Over-explained (>1.3) | 215 | 25.1% | 0.19 |

Cost of the gate:

| Gate | Keeps | Reversal rate |
|---|---|---|
| None | 100% | 33.5% |
| coherence ≥0.30 | 51.4% | 29.8% |
| coherence ≥0.70 | 34.0% | 26.8% |
| **coherence ≥0.85 (current `SLEEVE_MIN_EXPLAINED`)** | **26.7%** | **26.1%** |

So the live setting discards **73% of shocks to move reversal by 7.4 points**,
and coherent shocks still revert 27.6% of the time (30.3% late). Move size is a
comparable predictor and free: <10¢ reverts 37.6%, ≥20¢ reverts 25.7%.

**Reversal hazard is flat across the match**: <60 min 32.9%, 60-80 36.2%,
≥80 32.5%. Late events are not more *reliable*, only more *valuable when right*.
Trading later does not reduce VAR/penalty exposure at all.

---

## 5. Finding D — the reversal stop looks actively harmful

If reversal is a constant hazard that entry gates cannot filter, the natural
defence is an exit rule. The sleeve has one: `SLEEVE_REVERSAL_C = 2.0`, a 2¢
give-back from the entry anchor.

Simulated on late shocks (n=580), net per contract in probability units, Kalshi
taker fee both sides:

| Reversal stop | Mean net | Median | Fires on |
|---|---|---|---|
| 2¢ (current) | -0.0234 | -0.0559 | **59.5%** |
| 3¢ | -0.0280 | -0.0640 | 54.3% |
| 5¢ | -0.0324 | -0.0773 | 46.9% |
| 8¢ | -0.0215 | -0.0903 | 35.3% |
| **No stop** | **+0.0149** | **+0.0748** | 0% |

Every stop setting is worse than none. At 2¢ it fires on 60% of shocks: ordinary
post-entry wobble exceeds 2¢, so it stops out winners far more often than it
saves from reversals.

The comparison *between* settings is the robust part, because it is driven by
fire rate, a property of the price path rather than of execution. The absolute
levels are flattered by the no-slippage assumption.

---

## 6. Challenger queue (nothing applied)

Ranked by expected value of testing, to be registered as frozen challengers and
tested forward on Kalshi, never retrofitted:

1. **`SLEEVE_REVERSAL_C`** — strongest signal that a live parameter is harmful.
2. **`SLEEVE_MIN_EXPLAINED`** 0.85 → ~0.5 — roughly doubles opportunity for
   about one point of reversal rate.
3. **`PRICE_FLOOR`** — see the live-study note in the change log; independent of
   this document and with out-of-sample support.
4. **The 88 gate itself** — probably a few minutes late, but only the Kalshi
   provider clock can resolve it.

---

## 7. How to resume

1. The dataset is **not committed** (98 MB). Re-fetch with the spec in §1, or
   ask the operator for the scratchpad copy.
2. The highest-value next step is **not** more Polymarket work. It is the replay
   engine over the recorded Kalshi feed, specified in
   `PRICE_ONLY_BACKTEST_HANDOFF.md`. That feed has real depth, real fees,
   millisecond stamps and an authoritative match clock. As of this freeze it
   holds 152 gzip segments, ~2.7 GB compressed.
3. If you do continue on Polymarket, the open thread is whether the shock
   *shape* (speed and path of the repricing, not just its size) predicts
   reversal better than coherence does. That is measurable and was not attempted.

## 8. What would falsify the main finding

Stated so it is testable rather than merely believed:

- If Kalshi's provider clock shows R/R flat across minutes 70-95, Finding A is
  venue-specific and does not transfer.
- If late shocks on Kalshi, after real depth and fees, show negative expectancy
  held to settlement, Finding D's "no stop" conclusion is an artifact of the
  missing order book.
- If coherent shocks on Kalshi revert materially less than incoherent ones in
  the 88+ window, Finding C over-weights a coarse 1-second coherence measure.
