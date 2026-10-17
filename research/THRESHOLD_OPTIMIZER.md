# Offline log-move threshold optimizer

The tool searches for **early detection with few false alerts per correct alert**.
It does not assume 0.5 is optimal, and it does not tune production or place trades.

## Selection rule

- Keep burst=150ms, reference=150–2100ms, distinct prices>=5 and own-market volume>=200 fixed.
- Replay a separate causal actionable-alert stream for each threshold, including per-market
  lockout (120 seconds by default). A weak early alert can therefore preempt a later true signal.
- Default cached-data grid: log move **0.20 through 2.00, step 0.05** (37 thresholds).
  Raw trades permit searching below 0.2. A truncated cache never does.
- A correct detection matches an independently labeled completed move, in the correct market
  and direction. Each move contributes at most one true positive.
- Certified unmatched alerts and redundant duplicate alerts count as false alerts;
  duplicates are separately reported. Uncertain label periods remain UNKNOWN.
- False alerts per correct alert = FP / TP; undefined when TP=0, never a rewarded zero.
- Precision = TP / (TP+FP). Recall = correctly detected moves / all labeled target moves.
- Timing score = [sum of detected delays capped at 30s + 30s for each missed move] /
  all target moves. This prevents a mostly silent detector from winning on apparent speed.
- Among thresholds with precision>=90% and recall>=80%, prefer the smallest timing score,
  then fewer false alerts per correct alert, then lower median quoted entry cost.
- Also output the Pareto frontier: the nondominated faster-versus-cleaner trade-offs.
  The 90%/80% targets are configurable policy choices, not discovered facts.

All legs from a physical match stay in the same chronological split. Whole UTC days stay
together. The training winner is frozen before its single held-out evaluation; a holdout grid
is intentionally not emitted. Uncertainty resamples whole matches, preserving leg dependence.

## Cached independent audit: limited screening

From the repository root, point at the *extracted* earlier state-change audit bundle:

~~~bash
python -m research.prepare_threshold_data \
  --audit-directory /path/to/Football_State_Change_Audit_2026-10-05 \
  --out /tmp/threshold-data.json

python -m research.threshold_optimizer \
  --input /tmp/threshold-data.json --out /tmp/threshold-results
~~~

The existing audit has 98 leg cases and 42 complete price windows; missing windows stay
unknown. This adapter independently reconstructs a fixed +10c/100ms market-price onset,
instead of interpreting a provider processing timestamp as physical goal time.

Case labels are from the independently reconstructed score and price audit, not the bot's
trade/gate decisions. Only positive single-goal tie-to-lead or lead-to-tie cases are accepted.
A target move is at least 15c, stable and complete through the audit window, without a known
score reversal. That defines "correct" for this search; changing the definition changes the
question and requires a new versioned dataset.

The cache contains features only above 0.2 and recordings centered on known goal events.
Its ordinary-period, negative-event and fresh-clock coverage are NOT certified. Thus it can
produce a provisional training comparison but **cannot issue a recommended threshold**.
Pre-case control signals without a complete negative event ledger remain unknown, not false.

Quoted entry cost is diagnostic: the adapter reads a reconstructed exchange quote at original
receipt plus a modeled 100ms allowance. It is not a fee-adjusted profit or fill simulation.
This program optimizes detection, not order sizing or exit rules.

## Full-data input

Canonical UTF-8 JSON shape:

~~~json
{
  "metadata": {
    "feature_floor_log": 0.0,
    "continuous_ordinary_periods_complete": true,
    "negative_labels_complete": true,
    "clock_coverage_complete": true,
    "feature_capture_complete": true,
    "synthetic": false
  },
  "groups": [{"id": "same-physical-match", "start_ts": 1790000000}],
  "features": [{
    "market": "market-A", "group": "same-physical-match",
    "received_ts": 1790000100.12, "exchange_ts": 1790000100.0,
    "log_move": 0.6, "levels": 6, "volume": 250,
    "side": "yes", "eligible": true, "entry_ask_c": 42
  }],
  "truths": [{
    "id": "goal-1:leg-A", "event_id": "goal-1", "group": "same-physical-match",
    "market": "market-A", "side": "yes",
    "onset_ts": 1790000100.0, "start_ts": 1790000099.0, "end_ts": 1790000130.0,
    "baseline_c": 20, "terminal_c": 80
  }],
  "coverage": [{
    "group": "same-physical-match", "market": "market-A",
    "start_ts": 1790000000.0, "end_ts": 1790000600.0,
    "negative_labels_complete": true
  }]
}
~~~

The example illustrates fields, not sufficient sample size. Real selection needs multiple
dates. Certification requires at least 20 training positive matches and 10 held-out positive
matches, complete continuous late-match ordinary periods, complete negative labeling, verified
clock coverage, no unknown alerts, and held-out point precision/recall meeting the targets.
Manifest booleans must be supported by an audited coverage ledger; setting them cannot create
missing data. Confidence intervals remain descriptive and are not a profitability guarantee.

Labels are outcomes used only for scoring. They must be constructed independently of the
candidate threshold. Preserve own-leg price/volume, revisions, disallowed goals, outages,
stoppage time and explicit final status. Both 90 and 90+N are eligible while live; a future
clock observation must never qualify a past signal.

Optional clock observations:

~~~json
{"group":"same-physical-match","id":7,"observed_ts":1790000100,
 "minute":90,"added_time":8,"period":2,"status":"live"}
~~~

Put these in a top-level "clock_observations" array. They are joined as-of receipt;
pre-80, non-second-half, final, missing or older-than-10-second stamps fail the gate.
Historical missing-clock periods remain a coverage limitation.

## Replay original raw recordings

With independently prepared groups/truths/coverage/clocks in metadata.json:

~~~bash
python -m research.threshold_optimizer \
  --input metadata.json --raw-trades feed-1.jsonl.gz feed-2.jsonl.gz \
  --start 0.05 --stop 2 --step 0.025 --out /tmp/threshold-results
~~~

Supply files in original receipt order. The reader supports the original recorder
frame format and canonical JSONL trades with exchange_ts, received_ts, yes_price_c,
size, taker_side, market and group. Feature extraction streams the files and never
uses future/unreceived prints. Compact retained features are then evaluated repeatedly.
Raw WS frames need the separate as-of clock observations to establish late eligibility.

## Outputs

- result.json: policy, frozen training choice, its one held-out result, blockers and audit rows.
- training_grid.csv: counts, precision, recall, false/correct ratio, timing penalty and entry cost.
- training_frontier.json: nondominated early/clean/missed-move trade-offs.

recommended_threshold is null unless all certification requirements pass. A provisional
frozen_training_candidate is not a production recommendation or a global mathematical optimum.

## Tests

~~~bash
python -m unittest discover -s tests -p test_threshold_optimizer.py -v
~~~

Standard library only. No credentials, HTTP requests, order endpoints, production defaults,
database changes, or deployments. Research branch remains unmerged.
