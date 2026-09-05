"""B8e: the bounded detector scan must be output-identical to the full scan.

`Detector.on_trade` filtered the whole 300 s trade deque twice per trade -- once
for the 150 ms burst and once for the 2.1 s reference window.  Measured cost on
2026-09-04: 33 us/trade normally, 813 us/trade with a 9,000-trade deque, which
is the state of the hottest markets in the minutes this strategy trades in.

The optimisation reads both windows from the newest end of the deque and stops
at the window edge, which is only equivalent while the deque is ordered by
`ts_ms`; `MarketState.ordered` falls back to the exhaustive filter otherwise.
This test replays the bundled real tape (all three legs of Espanyol vs Real
Madrid, merged and sorted) through a copy of the original implementation and
through the current one, and requires byte-identical candidates, near misses
and `big_bursts` registrations.
"""
import json
import os
import unittest

from app import config
from app.detector import (
    BIG_DL,
    BIG_LEVELS,
    BURST_MS,
    REF_HI_MS,
    REF_LO_MS,
    Detector,
    logit,
)
from app.replay import MKTS, parse_us

DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "demo_data")


class ReferenceDetector(Detector):
    """`Detector` exactly as it was before the bounded scan (the reference).

    Copied verbatim so the comparison is against the shipped behaviour rather
    than against a re-derivation of it.
    """

    def on_trade(self, ticker, ts_ms, px, sz, taker, context=None):
        st = self.state(ticker)
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

        if dl >= BIG_DL and levels >= BIG_LEVELS:
            if not st.big_bursts or st.big_bursts[-1][0] < ts_ms - 5 or \
                    abs(st.big_bursts[-1][1]) < abs(signed):
                st.big_bursts.append((ts_ms, signed))

        if dl < config.DL_MIN or levels < config.LEVELS_MIN or size < config.SIZE_MIN:
            self._hold_subthreshold(st, {
                "ticker": ticker, "ts_ms": ts_ms, "dir": d, "dl": round(dl, 3),
                "signed": signed, "levels": levels, "size": round(size, 1),
                "ref": round(ref, 2), "ext": ext, "local_ts": 0.0,
                "below": sorted(
                    name for name, failed in (
                        ("dl", dl < config.DL_MIN),
                        ("levels", levels < config.LEVELS_MIN),
                        ("size", size < config.SIZE_MIN),
                    ) if failed
                ),
            })
            return None
        st.pending_subthreshold = None
        if ts_ms - st.last_candidate_ms < config.EPISODE_COOLDOWN_S * 1000:
            return None
        st.last_candidate_ms = ts_ms
        return {"ticker": ticker, "ts_ms": ts_ms, "dir": d, "dl": round(dl, 3),
                "signed": signed, "levels": levels, "size": round(size, 1),
                "ref": round(ref, 2), "ext": ext, "local_ts": 0.0}


def demo_tape():
    """All three legs of the bundled real tape, merged and time-ordered."""
    rows = []
    for ticker in MKTS:
        path = os.path.join(DEMO_DIR, ticker + ".jsonl")
        with open(path) as source:
            for line in source:
                row = json.loads(line)
                rows.append((
                    parse_us(row["created_time"]) // 1000, ticker,
                    float(row["yes_price_dollars"]) * 100,
                    float(row["count_fp"]), row["taker_side"],
                ))
    rows.sort()
    return rows


def comparable(row):
    """Drop the fields that are wall-clock or capture metadata, not signal."""
    if row is None:
        return None
    return tuple(sorted(
        (key, value) for key, value in row.items()
        if key not in ("local_ts", "context")
    ))


def replay(detector_class, tape):
    """Return the full observable trace of one implementation over the tape."""
    near_misses = []
    detector = detector_class(subthreshold_sink=near_misses.append)
    candidates = []
    bursts = []
    for ts_ms, ticker, px, sz, taker in tape:
        out = detector.on_trade(ticker, ts_ms, px, sz, taker)
        if out is not None:
            candidates.append(comparable(out))
        state = detector.markets[ticker]
        bursts.append((ticker, tuple(state.big_bursts)))
    detector.flush_subthreshold(tape[-1][0] + 10_000)
    return candidates, bursts, [comparable(row) for row in near_misses]


class DetectorScanEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tape = demo_tape()

    def test_the_bundled_real_tape_produces_an_identical_trace(self):
        self.assertGreater(len(self.tape), 20_000, "the demo tape did not load")

        old_candidates, old_bursts, old_misses = replay(ReferenceDetector, self.tape)
        new_candidates, new_bursts, new_misses = replay(Detector, self.tape)

        self.assertGreater(len(old_candidates), 0, "the tape produced no candidate")
        self.assertEqual(new_candidates, old_candidates,
                         "the bounded scan changed the candidate sequence")
        self.assertEqual(new_bursts, old_bursts,
                         "the bounded scan changed a big_bursts registration")
        self.assertEqual(new_misses, old_misses,
                         "the bounded scan changed the near-miss inventory")

    def test_out_of_order_arrivals_fall_back_to_the_exhaustive_scan(self):
        """The bounded read is only valid on an ordered deque, so a market that
        prints out of order must keep the original behaviour exactly."""
        shuffled = list(self.tape[:4000])
        for index in range(200, len(shuffled) - 1, 97):
            shuffled[index], shuffled[index + 1] = shuffled[index + 1], shuffled[index]

        old_candidates, old_bursts, old_misses = replay(ReferenceDetector, shuffled)
        new_candidates, new_bursts, new_misses = replay(Detector, shuffled)

        self.assertEqual(new_candidates, old_candidates)
        self.assertEqual(new_bursts, old_bursts)
        self.assertEqual(new_misses, old_misses)

    def test_an_out_of_order_print_disarms_the_bounded_read(self):
        detector = Detector()
        detector.on_trade("M", 1_000, 50.0, 1.0, "yes")
        self.assertTrue(detector.state("M").ordered)
        detector.on_trade("M", 900, 50.0, 1.0, "yes")
        self.assertFalse(detector.state("M").ordered,
                         "an out-of-order print must disable the suffix read")
        detector.on_trade("M", 1_100, 50.0, 1.0, "yes")
        self.assertFalse(detector.state("M").ordered, "the fallback must be sticky")


if __name__ == "__main__":
    unittest.main()
