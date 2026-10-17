# Original 0.8 versus proposed 0.5: completed limited screen

Original repository threshold 0.8 remains a serious candidate. The script now
always includes it explicitly and reports it beside the previously proposed 0.5.
This compares the log signal with sibling confirmation and price gates removed;
it does not compare complete production strategies.

## Search actually run

Recovered the existing independent state-change audit and ran the preparation
adapter and all 37 thresholds from 0.20 through 2.00. Inputs: 37 match groups,
38,116 cached features, 98 original leg cases, 42 complete price windows, and 21
stable large-move legs with independently reconstructible price onset. The other
cases were not silently labeled negative. Training has 18 labeled moves; the
chronological holdout has only three, far below the recommendation requirement.

| Prespecified threshold | Training moves caught | Training unclassified alerts | Holdout moves caught | Holdout unclassified alerts |
|---|---:|---:|---:|---:|
| Original 0.8 | 13/18 | 99 | 2/3 | 9 |
| Proposed 0.5 | 14/18 | 123 | 2/3 | 10 |

The lower threshold catches one additional training move and adds 24
unclassified training alerts. These alerts are **not proven false positives**:
the source lacks complete ordinary-period negative labels and fresh as-of clocks.
There are zero *certified* false alerts in these rows because certified negative
exposure is zero, not because either threshold has demonstrated perfect precision.

Of the 13 training moves caught by both thresholds, the median receipt-time
advantage of 0.5 is zero; the largest recorded difference is about 0.188 ms.
This sub-millisecond comparison does not establish an achievable infrastructure
or order-arrival advantage.

The provisional training candidate is 0.2 (15/18 training moves, 149 unclassified
alerts; frozen holdout 3/3, 11 unclassified). **Recommended threshold is null.**
Incomplete ordinary periods, negative labels, clock coverage, feature capture,
control exposure, sample support and unclassified alerts block a recommendation.
Neither 0.2 nor 0.5 is established as optimal.

## Actual original-detector smoke test

Separately reran the repository's actual `Detector.on_trade` and paper execution
classes at 0.8 on the previously selected five shocks / ten legs / 14,088 public
prints. All ten first signals had the expected direction. The same one-contract,
100 ms modeled order delay and 30-second exit produced the same entries and net
$1.86 as the prior 0.5 replay: nine profitable legs and one break-even.

This selected-shock result is not an independent holdout or a false-positive
estimate. Public prints are exchange ordered; recorded receipts are matched from
the private cache. Fresh clock operation, full depth, queue priority and actual
network/order delays remain unverified. Only two best-price entries covered a
full $100 request; do not scale the one-contract result.

## Reproducibility and status

Use the commands in `THRESHOLD_OPTIMIZER.md`. The recovered data and detailed
results remain outside the public repository. The local workspace is available
again; the previous failure was not a GitHub permissions or checks block. Original
main `1132c4b`, research branch `codex/log-threshold-optimizer-20261017`, draft
pull request 19. No application, production setting or deployment was changed.
