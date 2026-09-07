# Systematic audit plan

## Why this exists

Every defect found between 2026-09-04 and 2026-09-07 was found **incidentally, while looking for
something else**:

| Found | While looking for |
|---|---|
| Unbounded arrival queue fabricating $281 of profit | why the Inter–Napoli trades looked odd |
| The settlement card reading backwards | a question about Miami vs Atlanta |
| The consumer capped at 16 frames per event-loop turn | why the queue bound was dropping frames |
| Recovery amplifying its own failure | the same |
| The dashboard saying 88 when the gate fires at 80 | a question about the clock |
| `if not choices: continue` hiding the mapping state | a wrong claim about Liga MX coverage |
| Position size being a leftover, not a rule | a question about why sizes differ |
| Two MLS winners unreachable under today's config | a remark that MLS looked good |

That is not thoroughness. It is luck with a large surface area, and it means **the count of
undiscovered defects is unknown** — every probe so far has returned something, which is the
signature of a population nowhere near exhausted.

Three facts set the scale of the problem:

- **47 parameters in `STRATEGY_PARAM_NAMES`, 100 closed trades.** Roughly one trade per two
  tuneable knobs. Nothing fitted on that sample can be trusted, individually or collectively.
- **37 `except: … pass` and 66 bare `continue`** in `app/`. Each is a place the system can decline
  to tell you something. Two of today's findings came from exactly this construct.
- **2,163 Gate A signals produced 100 trades (4.6%); 365 sleeve signals produced 0.** Most of the
  system's behaviour has never been observed in the only mode that matters.

---

## Rules of engagement

These exist because they are the specific ways this investigation went wrong. They are not general
advice; each one has a corpse behind it.

1. **Write the falsifier before the measurement.** State what result would prove the hypothesis
   wrong, then measure. Three wrong diagnoses this session (SQLite as the latency cause; Liga MX as
   a coverage gap; "widen the spread limit") came from reasoning one step past the last measurement.
2. **Deltas, never cumulative means.** Cumulative counters are polluted by startup and by past
   incidents. A cumulative reading made `status()` look like a 13 ms regression when the windowed
   value was 859 µs.
3. **"Not measured" is not "measured and fine".** They must be different buckets in every output.
   The mapping bug was invisible precisely because these were conflated.
4. **A non-discriminating reading changes nothing.** If the conditions the test needs were not met,
   record that and stop. Do not act on a reading that both hypotheses predict.
5. **Record what was checked and found clean.** A findings list alone cannot distinguish thorough
   coverage from a lucky probe. The coverage ledger is a required deliverable, not a nicety.
6. **Separate the four verdicts.** Every finding is exactly one of: *idea is wrong*, *threshold is
   wrong*, *implementation is wrong*, *instrument is wrong*. Conflating these is how a sizing defect
   became a price filter.
7. **No fixes during the audit.** Findings only. A fix mid-audit changes the thing being measured
   and invalidates every subsequent workstream. Batch them afterwards.

---

## Workstreams

Eight, each targeting a defect class the evidence has already demonstrated. They are independent
unless the dependency section says otherwise.

### W1 · Decision reconstructability
**Question.** For every outcome the system can record, can a decision be fully reconstructed from
stored fields alone — without the code, without inference?

**Method.** Enumerate the outcome vocabulary from `static/app.js` (`outcomeLabels`,
`clockGateLabels`) and `app/match_clock.py`. For each, find a real stored row. Attempt to answer,
from the row alone: what were the inputs, what was the threshold, why did this outcome and not a
neighbouring one, and what would have had to differ to flip it.

**Deliverable.** A table: outcome → example row id → reconstructable yes/no → missing fields.

**Acceptance.** Every outcome word either has a worked reconstruction or a named missing field.
Outcomes with **zero** stored examples are themselves a finding — dead code or an unreachable branch.

**Known trap.** Several outcomes have never fired. Absence of a row is a finding, not a gap in the
audit.

---

### W2 · Label ↔ reality sweep
**Question.** Does every number and word shown to a human match what the code actually does?

**Method.** Mechanical, exhaustive. Extract every user-facing string in `static/` and every log
message in `app/`. For each that names a number, a threshold, a direction or a unit, locate the code
it describes and compare. Include: axis labels, chip text, exit-reason wording, event-log lines,
tooltip copy, panel headings.

**Deliverable.** Table: string → file:line → what it claims → what the code does → match/drift.

**Acceptance.** Every string with a claim in it is checked. Drift is reported even when harmless.

**Known trap.** The two found so far were both *stale identifiers used as labels*: `clock_88_plus`
rendered as "88+" while the gate fires at 80, and a contract name printed beside a payout with the
side omitted. Look hardest where a stored identifier is displayed directly.

---

### W3 · Silent-failure sweep
**Question.** Where can the system decline to tell you something?

**Method.** Enumerate all 37 `except … pass`, all 66 bare `continue`, every `.get(k, default)` and
`or 0` on a decision path, and every unbounded collection (list, dict, deque, queue) that grows from
external input. For each: what is swallowed, is it counted, is it recoverable after the fact.

**Deliverable.** Table: site → what it swallows → counted? → ledgered? → verdict (justified /
should count / should fail loudly).

**Acceptance.** Every site classified. Every unbounded collection either bounded, or documented with
the argument for why it cannot grow without limit.

**Known trap.** The unbounded arrival queue was *deliberate and documented* and still catastrophic.
"There is a comment explaining it" is not a passing verdict — the question is what happens at the
limit.

---

### W4 · Parameter provenance
**Question.** For each of the 47 strategy parameters: what evidence set this value, how large was
the sample, and does that evidence still exist?

**Method.** For each name in `STRATEGY_PARAM_NAMES`, find its introducing change-log entry. Record
the value, the justification, the sample size, and whether the analysis is reproducible from data
still on disk. Flag any parameter whose justification cites a sample smaller than 30, or cites data
from a superseded `config_id` era, or cannot be traced at all.

**Deliverable.** One row per parameter: value → origin → sample n → reproducible? → verdict
(evidence-backed / underpowered / untraceable / inherited default).

**Acceptance.** All 47 classified. Expect most to land in "underpowered" — that is the finding, and
its size is the point.

**Known trap.** `PRICE_FLOOR` was set on 27 trades and its own comment correctly identifies the
mechanism as *sizing* while the parameter controls *price*. Check whether the stated mechanism and
the controlled variable are the same variable. This is likely not the only case.

---

### W5 · Derived-not-designed behaviour
**Question.** What does the bot do that nobody decided?

**Method.** Walk the trade lifecycle and, at each step, ask whether the behaviour is *configured* or
*emergent from the interaction of two other rules*. Position size is the known instance: it is
whatever `$100` buys after walking a ladder to a price cap, so it is jointly determined by
`NOTIONAL_USD`, `PRICE_CAP` and the book's depth, and is not itself a parameter.

**Deliverable.** List of emergent behaviours, with the rules that produce each, and whether the
result was ever stated as an intent anywhere.

**Acceptance.** Entry size, exit size, hold time, re-entry behaviour, per-market exposure and
concurrent-position count each classified as designed or emergent.

**Known trap.** Emergent behaviour is invisible in config review, because there is no knob to read.
It only appears when you ask "what determines this number?" of an *observed* value.

---

### W6 · Measurement validity
**Question.** Is the instrumentation telling the truth, and what does it cost?

**Method.** For every number on `/api/status`, `/api/stats`, `/api/latency` and the dashboard:
what window does it cover, is it cumulative or windowed, what does it exclude, and what does
computing it cost the event loop. Re-derive a sample of them independently from the raw tables.

**Deliverable.** Per metric: definition → window → known exclusions → independently reproduced?
→ cost to compute.

**Acceptance.** Every displayed metric has a stated window. Any metric whose displayed value cannot
be reproduced from stored data is a finding.

**Known trap.** Part E of the investigation: every instrumentation layer added since 2026-08-30 ran
on the trading loop and *became* the latency it was added to explain. Measure the cost of each
measurement, not only its value.

---

### W7 · Data trustworthiness manifest
**Question.** Which time windows are usable for which claims?

**Method.** Build a calendar of the recorded history marking: `config_id` era boundaries, known
incidents (the 05:16–05:45 mapping window, the stale-book window, the firehose era before
2026-08-27 22:14), feed gaps and reconnects from `feed_events`, and stale-book fills from
`entry_context`. Then state, per window, which analyses it can and cannot support.

**Deliverable.** A manifest table other workstreams cite before making any statistical claim.

**Acceptance.** Every closed trade falls in exactly one window with a stated usability verdict.

**Known trap.** The L2 replay showed the same 29 trades pricing from −$863 to +$550 on the entry
model alone. A window being *recorded* does not make it *usable*; the book-correctness test from
A5c is the gate.

**This workstream blocks W8 and any statistical claim in any other workstream.**

---

### W8 · Economic model
**Question.** Independent of signal quality, do the mechanics permit profit?

**Method.** Decompose the 100 closed trades into signal edge, fee cost, spread cost and sizing
effect. Fees are $548.74 against a gross trading loss of $323.91 — establish what gross edge per
trade would be required to overcome the current fee load, and whether any observed subset achieves
it. Model the same trades under: fixed-contract sizing, maker-side entry, and shorter holds.

**Deliverable.** A break-even statement — the edge required, in cents per contract, for the current
fee and sizing model to be viable — and whether anything in the record clears it.

**Acceptance.** A number an operator can hold every future strategy proposal against.

**Known trap.** This is the workstream most likely to conclude that no gate change matters, because
the fee load dominates. That is a legitimate and important result. Do not soften it.

---

## Sequencing

```
W7 (data manifest) ──┬──> W8 (economics)
                     └──> any statistical claim in W1–W6

W2, W3, W4, W5 ──> independent, run in parallel, no data dependency
W6 ──> before re-measuring anything performance-related
```

Run **W7 first**. Run **W2, W3, W4, W5 in parallel** — they are code-reading work needing no live
data. **W1 and W6** need a running system. **W8 last**, because it depends on W7's verdict and
W5's sizing analysis.

---

## Running it with Fable

**One workstream per agent session.** They share no state; a shared session pollutes each with the
others' priors, which is exactly how the incidental-finding pattern started.

**Effort calibration:**

| Workstream | Effort | Why |
|---|---|---|
| W3, W4, W5 | high | Judgement-heavy; the finding *is* the reasoning |
| W1, W2, W6 | medium | Largely enumerable; volume over subtlety |
| W7 | high | Every downstream claim rests on it |
| W8 | high | Modelling choices dominate the answer (see A5c) |

**Fresh agents, deliberately.** I have already formed views on the price floor, sibling confirmation
and the sizing rule. For W4 and W5 in particular, an agent with no exposure to this conversation is
worth more than one briefed on my conclusions — if it reaches the same findings independently, that
is evidence; if it is told them, that is nothing.

**Required output schema.** Every finding, in every workstream:

```
id:            W4-011
class:         idea | threshold | implementation | instrument
claim:         one sentence, falsifiable
evidence:      file:line, or query + result, or measurement + window
falsifier:     what would have shown this to be wrong
confidence:    established | supported | directional | speculative
cost if true:  what it breaks or what it costs, concretely
fix:           proposed change, or "none — record only"
```

**Required coverage ledger.** Separate file, per workstream:

```
checked:       what was examined
clean:         examined and found correct  ← the part that makes the audit worth anything
findings:      ids raised
not reached:   what was in scope and not examined, and why
```

Without the ledger you cannot distinguish *"we checked 47 parameters and 40 are fine"* from
*"we found 7 problems"*. Only the first tells you whether to keep auditing.

---

## What not to do

- **Do not fix anything during the audit.** Findings only. A fix changes the system other
  workstreams are measuring.
- **Do not pool data across `config_id` eras** without W7's explicit clearance.
- **Do not accept "there is a comment explaining it"** as a passing verdict in W3.
- **Do not report a cumulative mean** as a current rate anywhere.
- **Do not let an agent conclude from a non-discriminating measurement.** If the conditions were not
  met, the finding is "not measured", and that is a legitimate deliverable.
- **Do not skip the coverage ledger** because the findings list looks productive.

---

## What this audit will probably conclude

Stated in advance, so the audit can contradict it rather than confirm it:

1. Most of the 47 parameters are underpowered — fitted on samples of tens.
2. The fee load requires an edge larger than anything in the record.
3. Several more label/reality drifts exist, in the same places: identifiers used as labels.
4. More silent-failure sites exist on paths that matter.
5. The sleeve's entry conditions are jointly unsatisfiable near a goal, not merely strict.

If the audit returns these five and nothing else, it has under-delivered — the whole premise is that
the unknown population is larger than the known one.
