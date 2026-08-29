# EPL fee compatibility remediation

Date: 2026-08-29  
Scope: realistic paper execution (`PAPER_EXECUTION_V2=true`) only

## Incident

Kalshi reports the Premier League series `KXEPLGAME` with fee type
`quadratic_with_maker_fees`. The V2 fee adapter accepted only the literal value
`quadratic`, so an otherwise executable EPL entry was finalized as
`unsupported_fee`. This blocked all EPL candidates that reached the V2 fee
check, including Everton YES signal 700 during Bournemouth–Everton after the
90+1 equalizer.

## Root cause and correction

V2 entries are simulated immediately executable orders against displayed live
depth; they never rest on the book. V2 exits also use the taker calculation when
`FEE_EXIT_TAKER=true`. Kalshi's published schedule applies the general/taker
quadratic formula to taker orders in maker-enabled markets and the lower maker
formula only to resting orders.

The adapter now recognizes both of these series metadata values for taker fills:

- `quadratic`
- `quadratic_with_maker_fees`

For both, fees continue to be rounded up per executed price level using:

`ceil(0.07 * fee_multiplier * contracts * price * (1 - price) * 100) / 100`

No maker-order simulation was added. `flat`, missing, null, or any other fee
schedule remains fail-closed as `unsupported_fee`.

## Deliberate non-changes

- No detector, confirmation, league, score, or goal-event logic changed.
- No price cap, notional, timing, sizing, target, stop, or timeout changed.
- No live-order capability was added.
- No historical trade or signal row was rewritten.

## Cross-check

Regression coverage proves that the maker-enabled schedule produces the same
taker fee as the standard quadratic schedule, that a `KXEPLGAME` entry can fill
and persist its live fee metadata, and that an unrelated schedule still fails
closed. Run:

```bash
.venv/bin/python -X dev -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app tests
.venv/bin/ruff check --select E9,F63,F7,F82 app tests
git diff --check
```

Rollback is a normal revert of the pull request; there is no data migration.

## Sources

- Kalshi fee schedule: <https://kalshi.com/docs/kalshi-fee-schedule.pdf>
- Kalshi fee explanation: <https://help.kalshi.com/en/articles/13823805-fees>
