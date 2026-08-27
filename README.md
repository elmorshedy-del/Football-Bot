# ⚽ Football-Bot — Late-Game Sniper

A Kalshi **paper-trading engine + live mission-control dashboard** for the late-game
soccer microstructure strategy: when a decisive stoppage-time event hits, one leg of a
match's Home/Draw/Away triplet sweeps the book, its sibling confirms within ~1ms, and
the rest of the market takes hundreds of milliseconds to finish repricing. This bot
detects that first sweep, requires sibling confirmation, and paper-buys the remaining
underreaction against the **live recorded order book** — measuring exactly what a real
order would have gotten.

Built from a 2,377-event backtest over the entire history of Kalshi soccer markets
(all 10.5 weeks of it), with a pre-registered out-of-sample holdout pass
(95% CI on net-per-fill [+$1.28, +$5.76], P(no edge)=0.0005). Paper first. Always.

## What it does

- **Discovers** every in-play soccer match across ~46 leagues on Kalshi (REST, every 3 min)
- **Subscribes** to `orderbook_delta` + `trade` + lifecycle WebSocket channels for markets near close
- **Records** the entire raw feed to hourly gzip segments (`DATA_DIR/raw/`) — the research goldmine
  that later replaces backtest fill assumptions with true book-at-arrival replays
- **Detects** the frozen Gate-A signal: ≥0.8 log-odds sweep, ≥5 levels, ≥200 contracts,
  sibling leg confirming within ±50ms with opposite sign
- **Paper-executes** IOC entries against the live book with a hard ~58¢ price cap
  (the isotonic zero-crossing), exits at target/timeout/settlement, verified Kalshi fees
- **Tracks kill conditions** from the research memo: live EV confidence interval,
  fill-model-vs-reality, feed latency p95, per-league rolling edge
- **Dashboard**: live match cards, signal wire, paper desk, equity curve, league edge,
  latency histograms, kill switch — all streaming over WebSocket

**No credentials? DEMO mode** auto-starts: it replays the real Espanyol–Real Madrid
tape (Aug 22, 2026 — the 90'+ winner that started this project) through the exact same
pipeline with synthetic books, so the dashboard runs hot out of the box.

## Deploy on Railway

1. Push this repo to GitHub (`Football-Bot`), then in Railway: **New Project → Deploy from GitHub repo**
2. Railway auto-detects the Dockerfile. Add a **Volume** (e.g. mount at `/srv/data`) and set `DATA_DIR=/srv/data`
3. (Live mode) Set environment variables:
   - `KALSHI_API_KEY_ID` — from kalshi.com → Account & security → API Keys
   - `KALSHI_PRIVATE_KEY` — paste the full PEM (multiline values are supported)
4. Generate a domain (Settings → Networking) and open it. Green LIVE badge = connected.

Leave the credentials empty to run the demo. `MODE=demo` forces demo even with keys.

### Credential security

- The private key is used **only** to sign Kalshi requests (RSA-PSS), read-only usage:
  no order endpoints are called anywhere in this codebase (`grep -r portfolio app/` → nothing)
- Never commit `.env` / `.key` files (gitignored); rotate the key at kalshi.com anytime

## Local run

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8080
# open http://localhost:8080  → demo mode
```

## Configuration

Every strategy parameter is an env var (see `.env.example`). The defaults are the
**frozen Gate-A primary config** — change them consciously; the backtest CI applies
to the defaults. Notable ones:

| Var | Default | Meaning |
|---|---|---|
| `PRICE_CAP` | 58 | max cents paid per contract (isotonic zero-crossing) |
| `NOTIONAL_USD` | 100 | paper size per trade |
| `CONF_MS` / `CONF_SIGN` | 50 / true | sibling confirmation window / opposite-sign requirement |
| `LATE_ONLY` | false | trade only within `LATE_WINDOW_MIN` of scheduled close |
| `USE_STOP` | false | stops off per Gate A forensics (shadow-stop is always recorded) |
| `PAPER_EXECUTION_V2` | false | opt into latency-aware paper arrivals, shadow liquidity, and entry/exit depth walking |

### Realistic paper execution (opt-in)

Set `PAPER_EXECUTION_V2=true` to route confirmed signals through the isolated V2
paper adapter. The detector, Gate-A thresholds, sibling confirmation, price cap,
notional, target, stop, and timeout rules remain unchanged. Only fill mechanics
change: orders reach the latest valid book after configurable latency, consume a
counterfactual shadow book, retain partial exit remainders, and record the actual
volume-weighted average price across every level walked. The live Kalshi book is
never mutated. Turning the flag off restores the original immediate paper desk.

V2 also persists every entry/exit level and fee in `paper_fills`, commits the
signal result and trade atomically, retries database failures without losing
paper depth, resumes partial stop/timeout/flatten exits, and restores open paper
positions after restart. Live fee type/multiplier metadata comes from Kalshi's
`/series/{series_ticker}` endpoint; an unknown or unsupported schedule is
recorded as `unsupported_fee` instead of guessing profitability. K1 verifies
fills against the saved arrival book after 25 fills, K2 cannot pass before 50
confirmed signals, and K4 measures total signal-to-paper-arrival latency.

## Architecture

```
discovery (REST, 3min) ──► subscriptions (WS: orderbook_delta, trade, lifecycle)
                                  │
                     ┌────────────┼──────────────┐
                     ▼            ▼              ▼
                raw recorder   in-memory      detector (sweep + sibling
               (gzip hourly)     books         coherence, frozen params)
                                  │              │ confirmed signal
                                  ▼              ▼
                            paper desk ◄── IOC vs live book, price cap,
                                  │         verified fees, target/timeout/settle
                                  ▼
                       SQLite (signals, trades, latency, eventlog)
                                  │
                            FastAPI + WS ──► dashboard
```

## The road to real money

This engine is deliberately **paper-only**. Real execution is gated behind the
pre-registered kill conditions (see dashboard): ≥50 live signals with a positive
event-clustered EV confidence interval AND recorded-book fill rates confirming the
backtest fill model. Only then does an execution module (with hard risk limits,
canary sizing, and a physical kill switch) get built.
