"""Pull a small self-contained fixture for `lab/detect` tests, straight from the
public Kalshi API (no authentication).

This exists so that `lab/detect` can be developed and tested without waiting on
`lab/ingest`.  It writes one JSON file per game under `_fixtures/`, holding the
merged 3-leg trade tape, the 1-minute candles and the Kalshi milestone/live_data
match events.  Nothing here is on the import path of the detector; it is a
developer tool, run by hand:

    python -m lab.detect.fetch_fixture KXMLSGAME-26SEP05ATXSJ

Manners follow CONTRACT.md §3.7: <=5 req/s, 6 attempts, backoff 1s x2 cap 20s.
"""
from __future__ import annotations

import calendar
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fixtures")

_last_call = [0.0]
MIN_INTERVAL_S = 0.2  # ~5 req/s
CANDLE_CHUNK_S = 4000 * 60  # the 1m candle endpoint caps a request at 5000 candles


def _get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    delay = 1.0
    for attempt in range(6):
        gap = MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if gap > 0:
            time.sleep(gap)
        _last_call[0] = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 5:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 20.0)
    raise RuntimeError("exhausted retries: " + url)


def _paged(path, params, key):
    out = []
    cursor = ""
    while True:
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        page = _get(path, q)
        rows = page.get(key) or []
        out.extend(rows)
        cursor = page.get("cursor") or ""
        if not cursor or not rows:
            return out


def fetch_game(event_ticker):
    markets = _get("/markets", {"event_ticker": event_ticker, "limit": 100})["markets"]
    if len(markets) != 3:
        raise RuntimeError(f"{event_ticker}: expected 3 legs, got {len(markets)}")

    trades = {}
    for m in markets:
        trades[m["ticker"]] = _paged(
            "/markets/trades", {"ticker": m["ticker"], "limit": 1000}, "trades"
        )

    # Candle window: the whole life of the tape, padded, in unix seconds.
    stamps = [
        _iso_us(t["created_time"]) // 1_000_000
        for rows in trades.values()
        for t in rows
    ]
    start_ts, end_ts = min(stamps) - 3600, max(stamps) + 600
    # The 1-minute endpoint caps a request at 5000 candles, so chunk the span.
    candles = {}
    for m in markets:
        rows = []
        lo = start_ts
        while lo < end_ts:
            hi = min(lo + CANDLE_CHUNK_S, end_ts)
            rows.extend(
                _get(
                    f"/series/KXMLSGAME/markets/{m['ticker']}/candlesticks",
                    {"start_ts": lo, "end_ts": hi, "period_interval": 1},
                ).get("candlesticks", [])
            )
            lo = hi
        candles[m["ticker"]] = rows

    live = None
    milestone_id = None
    stones = _get("/milestones", {"limit": 10, "related_event_ticker": event_ticker})
    for stone in stones.get("milestones", []):
        if event_ticker in (stone.get("related_event_tickers") or []):
            milestone_id = stone.get("id")
            break
    if milestone_id:
        live = _get(
            "/live_data/batch",
            {"milestone_ids": [milestone_id], "include_player_stats": "false"},
        )

    return {
        "event_ticker": event_ticker,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "markets": markets,
        "trades": trades,
        "candlesticks": candles,
        "milestone_id": milestone_id,
        "live_data": live,
    }


def _iso_us(text):
    """RFC3339 with optional fractional seconds -> integer microseconds."""
    body = text.rstrip("Z")
    date, _, frac = body.partition(".")
    base = time.strptime(date, "%Y-%m-%dT%H:%M:%S")
    secs = calendar.timegm(base)
    micros = int((frac + "000000")[:6]) if frac else 0
    return secs * 1_000_000 + micros


def path_for(event_ticker):
    return os.path.join(FIXTURE_DIR, f"{event_ticker}.json.gz")


def main(argv):
    if not argv:
        argv = ["KXMLSGAME-26SEP05ATXSJ", "KXMLSGAME-26SEP05MIAATL"]
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    for event_ticker in argv:
        blob = fetch_game(event_ticker)
        dest = path_for(event_ticker)
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            json.dump(blob, fh, separators=(",", ":"), sort_keys=True)
        n = sum(len(v) for v in blob["trades"].values())
        print(f"{event_ticker}: {n} prints -> {dest} ({os.path.getsize(dest)/1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
