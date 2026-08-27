# Football-Bot audit remediation ledger

Date: 2026-08-27  
Audited base: `main` at `61eae4f0e370740803bd71614384d2e03cfb41d4`  
Scope: paper trading only; detector, Gate A thresholds, signal confirmation,
price cap, target, stop setting, timeout, and real-order behavior were not
changed.

## Pull request map

| Change set | Branch | Purpose | Rollback |
|---|---|---|---|
| [PR 1](https://github.com/elmorshedy-del/Football-Bot/pull/1) | `codex/sequence-safe-orderbooks` | Subscription-scoped sequence validation and snapshot recovery | Revert PR 1 |
| [PR 2](https://github.com/elmorshedy-del/Football-Bot/pull/2) | `codex/realistic-paper-execution` | Opt-in latency/depth execution, durable fills, recovery, fees, K1/K2/K4 | Revert PR 2 or set `PAPER_EXECUTION_V2=false` |
| [Safety comparison](https://github.com/elmorshedy-del/Football-Bot/compare/main...codex/repository-safety) | `codex/repository-safety` | Recorder, lifecycle, controls, signal IDs, dependencies, CI | Revert the safety PR |
| [Integration comparison](https://github.com/elmorshedy-del/Football-Bot/compare/main...codex/all-audit-fixes) | `codex/all-audit-fixes` | Conflict-resolved, jointly tested combination of all three atomic change sets | Revert the integration PR; do not also merge the atomic PRs |

## Finding-to-fix cross-check

| Confirmed finding | Reproduction/evidence before | Remediation | Verification |
|---|---|---|---|
| Sequence numbers were checked per ticker although Kalshi sequences the whole subscription | Interleaved market frames invalidated healthy books and produced `NO BOOK` | PR 1 tracks sequence by subscription ID | Interleaved, duplicate, forward/backward-gap tests |
| Gap recovery unsubscribed/re-subscribed from a stale market list and could ignore a reused subscription ID | Market list was copied outside the lock; old IDs were permanently ignored | PR 1 keeps the same stream and sends documented `get_snapshot` under the subscription lock | Stale-ID, reused-ID, market-delete, snapshot-before-ack tests |
| A database failure could delete a pending entry and consume shadow depth | Reproduction returned `pending=0`, `shadow_qty=0` after `insert_trade` raised | PR 2 previews fills, rolls back depth on persistence failure, and retries the pending order | Entry and exit database-failure rollback tests |
| Net book reconciliation missed remove/re-add churn between polls | Remove 2 then add 2 appeared as net zero and left shadow availability at zero | PR 2 applies every WebSocket delta to observed and shadow state exactly once | Remove/re-add churn test |
| Partial stop/flatten exits were attempted only once | A partial stop left 1.5 contracts with no pending exit | PR 2 persists the exit intent and retries no-fill/partial attempts until closed or settled | Partial retry, repeated stop, empty-depth flatten tests |
| Fill fees were calculated at aggregate VWAP and assumed one schedule | The quadratic function is nonlinear and Kalshi documents series-specific schedules | PR 2 stores fees per executed level, reads `fee_type`/`fee_multiplier` from the series API, and rejects unknown schedules | Per-level/multiplier and unknown-schedule tests |
| K1 was static text | `k1_fill_note` never evaluated a fill | PR 2 verifies size, VWAP, cap, notional, and level availability against each saved arrival book; 25 fills are required | Valid/invalid arrival-book tests |
| K2 could pass after five signals | Five positive event clusters returned `PASS` despite `needed=50` | PR 2 hard-blocks pass status until 50 confirmed signals and sufficient event clusters | Five-signal positive-confidence-interval regression test |
| K4 measured feed lag, not order arrival | Paper entry latency was recorded but ignored | PR 2 records signal timestamp to simulated arrival and makes it K4's primary source | Latency-source regression test |
| Open database trades became zombie positions after restart | `trades.status='open'` survived, while `PaperDesk.positions` started empty | PR 2 persists position progress and restores open positions before execution tasks start | Partial-position restart and loader tests |
| Raw recording failures were silent | `except Exception: pass` hid disk/write failure | Safety PR exposes recorder health/error/failure count and sends a throttled alert while retrying | Disk-full and successful-write tests |
| Newly discovered markets were not added to the lifecycle stream | Only order-book and trade subscription IDs received dynamic updates | Safety PR tracks, updates, and unsubscribes the lifecycle subscription ID | Add-market, empty-set, and lifecycle-ack tests |
| Legacy trades used `signal_id=0` | `try_enter(0, ...)` ran before the signal row existed | Safety PR persists the signal first and passes its real ID into the paper desk | Signal-ID and execution-error tests |
| Kill and flatten endpoints were public | Any caller could POST to `/api/kill` or `/api/flatten` | Safety PR requires a constant-time checked `X-Admin-Token` and fails closed if not configured | Missing, wrong, and correct-token tests |
| Deployments used unbounded dependency minimums and had no CI | `requirements.txt` used only `>=`; no workflow existed | Safety PR adds compatible upper bounds, a tested exact lock, locked Docker install, and read-only CI permissions | Tests, byte compilation, critical Ruff rules |

## Operational changes

1. Set a long random `ADMIN_TOKEN`; the dashboard requests it only for an admin
   action and keeps it in session storage.
2. Set `PAPER_EXECUTION_V2=true` only when the realistic adapter is wanted. It
   remains off by default so merging PR 2 cannot silently change paper results.
3. Check `/api/status.recorder`. `healthy=false` means the raw research record
   is incomplete even if market processing continues.
4. An `unsupported_fee` result is intentional fail-closed behavior. Verify the
   series metadata instead of substituting the standard fee formula.

## Reproduction and verification commands

```bash
python -m venv .venv
.venv/bin/pip install --requirement requirements-dev.lock
.venv/bin/python -X dev -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app tests
.venv/bin/ruff check --select E9,F63,F7,F82 app tests
git diff --check
```

Expected atomic-branch counts at preparation time:

- PR 1: 11 tests.
- PR 2: 22 tests.
- Safety PR: 10 tests.
- Final integration branch: 43 tests after conflict resolution, plus byte
  compilation, critical Ruff rules, whitespace validation, and API smoke tests
  with execution V2 both disabled and enabled.

## Source checks used

- Kalshi order-book recovery uses the documented `get_snapshot` subscription
  action: <https://docs.kalshi.com/websockets/orderbook-updates>
- Series fee metadata and scheduled changes:
  <https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes>
- Kalshi states that some markets use different fees:
  <https://help.kalshi.com/en/articles/13823805-fees>

## Deliberate non-changes

- No live order placement was added.
- No detector thresholds or league priors were tuned.
- No historical rows were deleted by these migrations.
- Existing paper behavior remains available by disabling V2; reverting each PR
  removes its schema/code additions without requiring a destructive database
  rollback.
