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
        # Separate from the trading cooldown so research capture can never
        # move a trading decision.
        self.last_subthreshold_ms = -1e18
        # Best near miss of the burst currently in progress, held until the
        # burst window closes (see Detector._hold_subthreshold).
        self.pending_subthreshold = None

    def evict(self, now_ms):
        while self.trades and self.trades[0][0] < now_ms - 300_000:
            self.trades.popleft()
        while self.big_bursts and self.big_bursts[0][0] < now_ms - 5_000:
            self.big_bursts.popleft()


class Detector:
    """Emits tradeable candidates, and optionally reports the near misses.

    `subthreshold_sink` is a research channel, not a trading one.  It receives
    bursts that cleared a looser research floor but not the live Gate-A
    thresholds, so the recorded study contains the population just below the
    cut.  Without it those bursts leave no row at all and `DL_MIN`,
    `LEVELS_MIN` and `SIZE_MIN` can only be re-fitted by replaying the raw
    feed: the first 7.9 days of live capture showed accepted sweeps piled hard
    against every floor (dl p10 0.818 against a 0.8 minimum, levels p10 5
    against 5, size p10 218 against 200), so that invisible population is
    large.  The sink cannot influence what is traded; it is called only on the
    branch that has already declined to produce a candidate.
    """

    def __init__(self, subthreshold_sink=None):
        self.markets = {}
        self.subthreshold_sink = subthreshold_sink

    def state(self, ticker):
        if ticker not in self.markets:
            self.markets[ticker] = MarketState(ticker)
        return self.markets[ticker]

    def _hold_subthreshold(self, st, observation):
        """Hold the burst's best near miss until the burst is known to be over.

        A sweep is evaluated on every trade, so it crosses the floor part-way
        up: prices 40..47 report levels=3 two milliseconds before the same
        burst becomes a tradeable levels=8 candidate.  Reporting immediately
        would fill the near-miss inventory with pre-echoes of sweeps that did
        trade, and a threshold re-fitted on that inventory would be fitted to
        an artifact of tick arrival.  So the observation is held, upgraded
        while the burst grows, dropped outright if the burst becomes tradeable,
        and only emitted once the burst window has closed.
        """
        if self.subthreshold_sink is None or not config.SUBTHRESHOLD_CAPTURE:
            return
        held = st.pending_subthreshold
        if held is None or observation["dl"] >= held["dl"]:
            st.pending_subthreshold = observation

    def _flush_subthreshold(self, st, now_ms):
        """Emit a held near miss once no further trade can extend its burst."""
        held = st.pending_subthreshold
        if held is None or now_ms - held["ts_ms"] <= BURST_MS:
            return
        st.pending_subthreshold = None
        if (held["dl"] < config.SUBTHRESHOLD_DL_MIN
                or held["levels"] < config.SUBTHRESHOLD_LEVELS_MIN
                or held["size"] < config.SUBTHRESHOLD_SIZE_MIN):
            return
        if held["ts_ms"] - st.last_subthreshold_ms < config.SUBTHRESHOLD_COOLDOWN_S * 1000:
            # Deliberately not advanced on suppression.  Rolling the window
            # forward on every suppressed burst would let a sustained flurry
            # push the next admission indefinitely and silently shape the
            # observation inventory by trade arrival pattern.
            return
        st.last_subthreshold_ms = held["ts_ms"]
        try:
            self.subthreshold_sink(held)
        except Exception:
            # Research capture must never break the live trading path.
            pass

    def flush_subthreshold(self, now_ms):
        """Flush held near misses for markets that have gone quiet."""
        if self.subthreshold_sink is None or not config.SUBTHRESHOLD_CAPTURE:
            return
        for st in list(self.markets.values()):
            self._flush_subthreshold(st, now_ms)

    def on_trade(self, ticker, ts_ms, px, sz, taker):
        """Feed one trade. Returns a candidate dict when the sweep threshold is
        crossed (sibling confirmation is the engine's job)."""
        st = self.state(ticker)
        # Any held near miss whose burst window has closed is settled before
        # this trade is considered part of a new burst.
        self._flush_subthreshold(st, ts_ms)
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
            self._hold_subthreshold(st, {
                "ticker": ticker, "ts_ms": ts_ms, "dir": d, "dl": round(dl, 3),
                "signed": signed, "levels": levels, "size": round(size, 1),
                "ref": round(ref, 2), "ext": ext, "local_ts": time.time(),
                "below": sorted(
                    name for name, failed in (
                        ("dl", dl < config.DL_MIN),
                        ("levels", levels < config.LEVELS_MIN),
                        ("size", size < config.SIZE_MIN),
                    ) if failed
                ),
            })
            return None
        # This burst cleared the trading floor, so any near miss held from an
        # earlier trade in the same burst was a partial view of a tradeable
        # sweep, not a near miss.  Drop it rather than record it.
        st.pending_subthreshold = None
        if ts_ms - st.last_candidate_ms < config.EPISODE_COOLDOWN_S * 1000:
            # The window is anchored to the last *emitted* candidate, not to
            # the last suppressed one.  Advancing it here re-armed the cooldown
            # on every suppression, so a market printing sweeps faster than the
            # interval could be silenced indefinitely and the episode inventory
            # was shaped by trade arrival pattern rather than by the rule.
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
