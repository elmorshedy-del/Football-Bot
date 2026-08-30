"""Streaming late-game shock detector — the frozen Gate A signal, live.

Signal (validated Aug 2026, 2,377-event backtest + holdout):
  - a sweep: trades within a 150ms window moving one direction,
    >= LEVELS_MIN distinct prices, >= SIZE_MIN contracts,
    log-odds displacement vs 2s-median reference >= DL_MIN
  - sibling coherence: another leg of the same match printed its own big
    sweep within +-CONF_MS, opposite sign (mutually exclusive legs must
    offset). Median confirmation lag observed historically: ~1ms.
"""
import math
import time
from collections import deque

from . import config

BURST_MS = 150
REF_LO_MS, REF_HI_MS = 2100, 150
BIG_DL, BIG_LEVELS = 0.25, 3


def logit(pc):
    p = min(max(pc / 100.0, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


class MarketState:
    def __init__(self, ticker):
        self.ticker = ticker
        self.trades = deque()          # (ts_ms, px, sz, taker)
        self.big_bursts = deque()      # (ts_ms, signed_dl) recent notable sweeps
        self.last_candidate_ms = -1e18

    def evict(self, now_ms):
        while self.trades and self.trades[0][0] < now_ms - 300_000:
            self.trades.popleft()
        while self.big_bursts and self.big_bursts[0][0] < now_ms - 5_000:
            self.big_bursts.popleft()


class Detector:
    def __init__(self):
        self.markets = {}

    def state(self, ticker):
        if ticker not in self.markets:
            self.markets[ticker] = MarketState(ticker)
        return self.markets[ticker]

    def on_trade(self, ticker, ts_ms, px, sz, taker):
        """Feed one trade. Returns a candidate dict when the sweep threshold is
        crossed (sibling confirmation is the engine's job)."""
        st = self.state(ticker)
        st.trades.append((ts_ms, px, sz, taker))
        st.evict(ts_ms)

        burst = [t for t in st.trades if t[0] >= ts_ms - BURST_MS]
        if not burst:
            return None
        buy = sum(t[2] for t in burst if t[3] == "yes")
        sell = sum(t[2] for t in burst if t[3] == "no")
        d = 1 if buy >= sell else -1
        prices = [t[1] for t in burst]
        levels = len(set(round(p, 1) for p in prices))
        size = buy + sell
        ref_w = sorted(t[1] for t in st.trades
                       if ts_ms - REF_LO_MS <= t[0] < ts_ms - REF_HI_MS)
        if not ref_w:
            return None
        ref = ref_w[len(ref_w) // 2]
        if not (0.5 <= ref <= 99.5):
            return None
        ext = max(prices) if d > 0 else min(prices)
        dl = (logit(ext) - logit(ref)) * d
        signed = logit(ext) - logit(ref)

        # register notable sweeps for sibling confirmation
        if dl >= BIG_DL and levels >= BIG_LEVELS:
            if not st.big_bursts or st.big_bursts[-1][0] < ts_ms - 5 or \
                    abs(st.big_bursts[-1][1]) < abs(signed):
                st.big_bursts.append((ts_ms, signed))

        if dl < config.DL_MIN or levels < config.LEVELS_MIN or size < config.SIZE_MIN:
            return None
        if ts_ms - st.last_candidate_ms < config.EPISODE_COOLDOWN_S * 1000:
            st.last_candidate_ms = ts_ms
            return None
        st.last_candidate_ms = ts_ms
        return {"ticker": ticker, "ts_ms": ts_ms, "dir": d, "dl": round(dl, 3),
                "signed": signed, "levels": levels, "size": round(size, 1),
                "ref": round(ref, 2), "ext": ext, "local_ts": time.time()}

    def confirm(self, candidate, sibling_tickers):
        """Look for an opposite-sign big sweep on a sibling within +-CONF_MS.
        Returns (confirmed, lag_ms)."""
        best = None
        for sib in sibling_tickers:
            st = self.markets.get(sib)
            if not st:
                continue
            for (bts, bsigned) in reversed(st.big_bursts):
                lag = bts - candidate["ts_ms"]
                if abs(lag) > config.CONF_MS:
                    continue
                if config.CONF_SIGN and bsigned * candidate["signed"] >= 0:
                    continue
                if best is None or abs(lag) < abs(best):
                    best = lag
        return (best is not None), best
