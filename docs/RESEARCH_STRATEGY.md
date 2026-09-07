# What we are doing, and how we will know when we are done

This is the document the audit runs inside. The audit asks *is the machine sound*; this asks
*what is the machine for, and what would finishing look like*.

---

## 1. The goal problem

The bot currently serves two goals that pull in opposite directions, and has never been told which
one it is.

| | A profit bot wants | A study bot wants |
|---|---|---|
| Refusals | as many as possible — refusing is free | as few as possible — refusing costs information |
| Position size | as large as conviction allows | as small as measurement allows |
| Gates | tight, to protect capital | wide, to observe the population |
| Success | money | a number you can act on |

**It is built like the first and judged like the second.** That is the root of the recurring
confusion: every gate was added with profit logic (protect the capital), and every complaint about
it is study logic (why won't it trade). Both are reasonable; they are just answers to different
questions, and nobody has picked the question.

**Decision required, and it is yours.** My recommendation, stated plainly:

> **This is a study bot until it has an edge estimate with a confidence interval that excludes zero.
> Profit is a later mode, switched on deliberately, not drifted into.**

Everything below assumes that answer. If you choose differently, the plan changes shape and I will
say how.

---

## 2. Can the current setup ever decide? No.

Measured on the 95 clean closed trades (the 5 stale-book fills excluded):

```
mean net per trade   -$12.15
standard deviation    $61.32
95% interval         [-$24.48, +$0.19]
```

The interval contains zero. It also contains −$24. After 12 days of live trading the honest
statement is **"we do not know, in either direction"**.

**How much more would settle it:**

| true edge per trade | trades needed | at ~100 trades/month |
|---|---:|---:|
| $2 | 14,759 | 147 months |
| $5 | 2,362 | **23.6 months** |
| $10 | 590 | 5.9 months |
| $20 | 148 | 1.5 months |

*(80% power, 95% two-sided, at the observed σ = $61.32.)*

An edge worth having is probably in the $2–$10 range after fees. **At the current variance and
trade rate, that is one to twelve years.** The project cannot reach a conclusion this way.

### The variance is self-inflicted

σ = $61.32 on a $100 stake is enormous, and it is not the market's fault. It is the fixed-dollar
sizing rule: $100 buys 1,164 contracts at 8.6¢ and 173 at 57.9¢, so the payoff distribution is six
times wider at the cheap end. Sizing in **fixed contracts** compresses that distribution directly.

Required sample scales with σ². **Halving the standard deviation cuts the required number of trades
by four.** So the sizing change is not primarily a returns improvement — it is what makes the
experiment finishable at all. That is why it sits first on every change list in these documents.

---

## 3. Why not just backtest? Mostly, yes — and here is exactly how far it goes

### What Kalshi actually gives you, free, today

Verified against the live API on 2026-09-07:

```
1,776 settled markets across 10 of ~22 soccer series   = ~592 matches
extrapolated across all series                          ≈ 1,000+ matches
history reaches back to                                  2026-07-17 (7 weeks)

per trade: created_time (µs), yes_price_dollars, no_price_dollars,
           count_fp, taker_side, taker_outcome_side, trade_id
```

Complete tape. Cursor-paginated. No authentication problems.

### The decisive fact: the detector consumes trades, not books

`Detector.on_trade` takes prints. Its three admission tests are all computable from the tape:

| Gate | Needs | Available historically? |
|---|---|---|
| `DL_MIN` — log-odds displacement | prices in the burst | **yes** |
| `LEVELS_MIN` — distinct price levels | prices in the burst | **yes** |
| `SIZE_MIN` — contracts | `count_fp` | **yes** |
| `CONF_MS` / `CONF_SIGN` — sibling confirmation | sibling prints, timestamps | **yes** |
| Post-event drift — *the candidate edge itself* | later prints | **yes** |

**The entire detection and confirmation path replays exactly on historical data.** Not approximated
— the same inputs the live code sees.

### What the tape cannot give you

| Missing | Consequence |
|---|---|
| Order-book depth | No ladder-walk fills; no `no_book`; no depth-limited sizing |
| Bid/ask spread | The sleeve's `SLEEVE_MAX_SPREAD_C` gate cannot be evaluated |
| Book mid-prices | The sleeve's triplet inference is approximate at best |
| Book age | `stale_book` and freshness gates are untestable |

So: **Gate A is ~90% backtestable. The price-only sleeve is largely not.**

### The fill problem, and why it is smaller than it looks

The obvious objection is "you cannot know what price you would have got". A5c already settled this
in the opposite direction from intuition: reconstructing fills from the bot's *own recorded books*
was **worse than useless** — the reconstructed ask was worse than a real contemporaneous executed
print in **29 of 29 cases, median +12¢**. A print at price P is proof that a trade happened at P.

The honest fill model for a backtest is therefore: **price entries from executed prints in the
seconds after the signal, and report the answer as a range across assumptions** — because the same
29 trades priced at −$863, −$229 or +$550 depending only on that choice. A backtest that reports one
number is lying; one that reports the range is informative.

### So: can you one-shot the bot from a backtest?

**No — and yes to the useful half.** You cannot get a deployable configuration from prints alone,
because execution reality (fills, spread, latency, book depth) is exactly what prints omit, and that
is where a large part of the loss lives. What you *can* do is compress what would be **one to twelve
years of live discovery into a few days of computation**, and arrive at the live phase already
knowing which questions are worth spending real fills on.

**The division of labour that follows:**

| | Backtest (~1,000 matches, free, now) | Live bot (slow, expensive, real) |
|---|---|---|
| Does post-event drift exist? | **yes, answer here** | confirm out of sample |
| At what horizon, and how big? | **yes** | confirm |
| Does the detector select for it? | **yes** | confirm |
| Do the 47 parameters matter? | **yes — sweep them** | fix the survivors |
| What do fills actually cost? | no | **only here** |
| What does latency cost? | no | **only here** |
| Do spreads permit entry after a goal? | no | **only here** |

The live bot stops being the instrument of discovery and becomes the instrument of **confirmation
and execution realism**. That is what it is actually good at, and it is a much smaller job.

---

## 4. The plan

### Stage 0 · Backtest harness — build it, then answer the idea question
**Blocked by nothing. Start here.**

Pull every settled soccer market's trade tape (~1,000 matches, cursor-paginated, cache to disk).
Feed prints into the *existing* `Detector` — the real class, not a reimplementation, so what the
backtest tests is what production runs. For every candidate, measure forward price movement at
+5 s, +15 s, +30 s, +60 s, +120 s, +300 s from executed prints.

**Answers:** Does post-event drift exist across 1,000 matches? How large, at what horizon, and with
what dispersion? Does sibling confirmation select for the drifting cases, or not? Which of the 47
parameters change the answer, and which are noise?

**Report as a range across fill assumptions, never a single number.**

### Stage 1 · Re-derive parameters from the backtest
Every threshold currently set on tens of trades gets re-set on ~1,000 matches, or is deleted for
lack of an effect. Expect deletions: 47 parameters will not all survive contact with real power.

### Stage 2 · Live confirmation mode
Reconfigure the live bot as a **measurement instrument**, not a trader:

- **Fixed contracts, small** — cuts σ, makes the sample finishable, removes the floor's reason to exist
- **Gates wide** — anything the backtest did not justify comes off
- **Every episode recorded**, whether traded or not

**Purpose:** confirm the backtest out of sample, and measure the four things prints cannot show —
fill quality, latency cost, spread reality after a goal, and fee load in practice.

### Stage 3 · Profit mode
Only after Stage 2's interval excludes zero *and* the fee model closes. Deliberate switch, with the
sizing rule and the exposure limit chosen on purpose.

---

## 5. Pre-registered decision gates

Written before the data, so the answer cannot be negotiated afterwards.

| Result at Stage 0 | Conclusion | Action |
|---|---|---|
| Drift exists at ≥ 2× round-trip fee, across ≥ 3 leagues, robust to fill assumption | The idea is sound | Go to Stage 1 |
| Drift exists but < round-trip fee | Idea real, economics dead | Attack fees (maker entry) or stop |
| Drift only under the most favourable fill assumption | Not established | Do not deploy; return to method |
| No drift at any horizon across 1,000 matches | **The idea is wrong** | Stop. Do not tune gates |
| Confirmation does not select for drift | Confirmation is not an admission rule | Demote to a recorded label |

And the one that matters most:

> **If Stage 0 says the idea is wrong, that is a successful outcome.** It costs days instead of
> years, and it is the single highest-value result available right now. The failure mode to avoid is
> spending another twelve months collecting live data to learn the same thing.

---

## 6. How this relates to the audit

They run in parallel and answer different questions.

- **The audit (`AUDIT_PLAN.md`)** asks: *is the machine honest?* Does it record what it did, does it
  say what it means, does it fail loudly.
- **This document** asks: *is the machine pointed at anything real?*

A sound machine aimed at nothing is worthless; a real edge measured by a dishonest machine is
undetectable. Neither is sufficient alone.

**One dependency runs between them:** audit workstream **W7 (data trustworthiness manifest)** must
complete before any live-data claim in Stage 2, because it defines which recorded windows can
support a statistical statement. Stage 0 is unaffected — it uses Kalshi's own historical tape, which
none of the bot's recording defects touch.

---

## 7. What I would do first, if it were one thing

Build the Stage 0 harness and run the drift question across 1,000 matches.

It is free, it takes days rather than years, it needs no live trading, none of the recording defects
touch it, and it can return the most valuable answer available — *the idea does not work* — before
another parameter is tuned.

Everything else on every list, including the audit, is worth more once that number exists.
