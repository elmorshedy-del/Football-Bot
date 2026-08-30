# Price-Only Late-Score Backtest Handoff

This is an architecture and validation contract, not a backtest implementation. The live
application remains paper-only. No parameter set or trading method is guaranteed to make money.

## 1. Research question

Test whether a confirmed market sweep after the scheduled minute-88 boundary, followed by a
coherent normalized reallocation across the three mutually exclusive match-result contracts,
has positive net expectancy after realistic arrival delay, depth consumption, fees, partial
fills, and exits.

The match-event feed must not be an entry feature. It is reserved for post-trade attribution,
latency measurement, correction/reversal labeling, and stratified analysis.

## 2. Export inputs

Start with `manifest.json`. Validate its schema, row counts, file hashes, and configuration before
using any observation.

| Input | Role |
|---|---|
| `database/footballbot-snapshot.db` | Consistent relational snapshot and authoritative joins |
| `tables/*.csv` and `tables/*.jsonl` | Portable table-level review and non-SQL workflows |
| `raw/feed-*.jsonl.gz` | Exchange WebSocket replay with local wall/monotonic receipt stamps |
| `goal_latency_observations` | Diagnostic provider observations; never entry inputs |
| `manifest.json` | Frozen study configuration, counts, hashes, and semantics |

Reject an archive if a listed gzip file does not decompress, a hash differs, a table count does
not reconcile, or a signal/trade references a missing parent record.

## 3. Replay clock and market state

1. Order raw frames by local monotonic receipt time, using file order only to break exact ties.
2. Reconstruct each subscribed order book from snapshots and sequence-validated deltas.
3. Invalidate a market on any sequence gap and prohibit fills until a new snapshot restores it.
4. Recreate trades, detector bursts, sibling confirmation, and triplet snapshots using only data
   available by that replay timestamp.
5. Apply configured decision and order-arrival delays before walking executable shadow depth.
6. Give Gate A and price-only one independent shadow book each. Neither may consume the other's
   counterfactual liquidity or mutate the reconstructed live book.
7. Preserve exchange timestamp, local receipt, decision, arrival, fill, exit, and settlement as
   separate boundaries.

## 4. Episode construction

An episode begins with the first threshold-crossing sweep after the cooldown and includes the
configured sibling-confirmation window. Key the episode by match, target contract, and start time.
All parameter configurations must evaluate the same frozen episode inventory; a configuration may
reject an episode but may not redefine history to create a more favorable sample.

Group related signals from the same match and shock into one statistical event. Multiple contracts
or both sleeves reacting to one underlying match development are not independent observations.

## 5. Candidate configuration registry

Every run must have a content-addressed configuration record containing:

- detector displacement, price-level, size, cooldown, confirmation, and sign rules;
- scheduled window boundaries and their clock source;
- triplet baseline/freshness, maximum spread, target gain, post-state, sibling-rise, and explained-
  flow thresholds;
- notional, cap, entry/exit delay, fee schedule, partial-fill rules, and lockout;
- scratch arm/buffer, profit-trail arm/minimum/fraction, reversal, oscillation, timeout, target,
  stop, settlement, and kill behavior;
- source archive hash, code commit, replay-engine version, random seed, and split IDs.

Never overwrite a run. A new threshold or code path creates a new immutable configuration ID.

## 6. Time-safe model selection

Use chronological, match-grouped walk-forward splits. All episodes from one match stay in one split.

1. **Development:** earliest block; broad configuration search and implementation debugging.
2. **Validation:** later block; choose one champion and a small, predeclared challenger set.
3. **Untouched test:** latest block; opened once after the selection rule is frozen.
4. **Forward paper:** all new live episodes after selection; no retroactive threshold edits.

Normalization baselines, calibration, fee assumptions, and any learned parameter must be fitted
inside each training fold. Do not use future market depth, settlement, provider events, or full-
sample quantiles in an earlier decision.

## 7. How new data chooses configuration

Use a champion/challenger registry, not continuous manual tuning.

- Score configurations on event-clustered net return after fees, with one match/shock as the
  resampling unit.
- Primary selection statistic: lower bound of the 95% event-clustered confidence interval for net
  per accepted episode.
- Constraints: minimum accepted events, minimum executable-fill rate, maximum drawdown, maximum
  conditional loss in the worst 5%, and no fill-integrity failures.
- Penalize broad searches for multiple comparisons using a predeclared reality-check or false-
  discovery procedure. Report the number of tried configurations.
- Promote a challenger only at scheduled review boundaries when it beats the champion by the
  predeclared margin on validation and remains eligible on all constraints.
- Never promote from the untouched test. After the test is opened, it becomes historical evidence
  and a new future period must serve as the next untouched test.

Default minimum before any statistical promotion decision: 100 independent match/shock events and
at least 50 executable accepted entries per sleeve. Larger samples are required when acceptance is
concentrated in one league, price range, or event type.

## 8. Economics and risk outputs

Report combined and per sleeve:

- candidate, rejection, queued, filled, partial, and closed counts;
- gross, entry fees, exit fees, net, net per accepted episode, and net per filled contract;
- executable fill rate, depth walked, arrival slippage, time in trade, and remaining exposure;
- win rate with sample count, event-clustered confidence interval, maximum drawdown, worst loss,
  conditional loss in the worst 5%, and longest losing sequence;
- results by league, target price, spread, arrival-latency bucket, inferred state, exit reason,
  and provider-event diagnostic label;
- sensitivity surfaces around every selected parameter, not only the winning point.

Mark open unrealized values separately. Do not add indicative midpoint profit to realized net.

## 9. Adversarial and falsification tests

- Add delay and slippage stress beyond the observed 95th and 99th percentiles.
- Remove the best match, best day, and best league; the conclusion must not depend on one cluster.
- Replay corrections/reversals, missing siblings, wide books, gaps, stale triplet legs, partial
  exits, service restarts, and settlement near entry.
- Run placebo times and direction/sign permutations. A purported edge that survives only its exact
  hand-selected sample but also appears in placebos is not credible.
- Compare against simpler baselines: confirmed Gate A only, target-leg displacement only, and a
  no-trade policy after costs.
- Verify both sleeves see identical source frames and independent shadow depth.

## 10. Required output artifact

Each run should produce:

- immutable run manifest and configuration ID;
- input archive and code hashes;
- split inventory with match/episode IDs;
- episode ledger containing every admission/rejection reason and timing boundary;
- fill and exit ledger with arrival books, walked levels, fees, and counterfactual depth state;
- per-sleeve and combined metrics, uncertainty, stress tests, and sensitivity plots;
- data-quality report, failed invariants, and a plain-language limitations section;
- explicit decision: reject, continue collecting, keep champion, or promote challenger.

## 11. Acceptance tests for the external engine

- Replaying the same archive/configuration twice produces byte-identical ledgers.
- Shuffling file discovery order does not change results after monotonic sorting.
- No decision accesses a frame or provider observation received after its timestamp.
- Strategy totals reconcile exactly to combined totals.
- Independent sleeves can fill the same source depth without cross-consumption.
- Delay, cap, fees, partial fills, exits, sequence gaps, restarts, and settlement match hand-built
  deterministic fixtures.
- Every plotted point traces to an episode, fill, or event row and immutable source identifier.
