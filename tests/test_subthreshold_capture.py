"""Sub-threshold capture: record the near misses without trading them.

Over the first 7.9 days of live capture, every accepted sweep sat hard against
the detector floor (dl p10 0.818 against a 0.8 minimum, levels p10 5 against 5,
size p10 218 against 200).  The population just below the cut was therefore
large and left no row at all, so DL_MIN/LEVELS_MIN/SIZE_MIN could only be
re-fitted by replaying the raw feed.  These tests pin the capture, and pin that
it stays strictly outside the trading path.
"""
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import config, store
from app.detector import Detector
from app.engine import Engine
from app.match_clock import MatchClockTracker


class SubthresholdDetectorTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.detector = Detector(subthreshold_sink=self.seen.append)

    def feed(self, detector, ticker="M", base_ms=0, prices=(50.0,), size=40.0,
             taker="yes", reference=20.0):
        """Build a reference window, then one burst at ``base_ms``.

        Returns the first tradeable candidate the burst produced, since the
        detector emits it as soon as the thresholds are crossed and suppresses
        the rest of the burst under the episode cooldown.
        """
        for i in range(6):
            detector.on_trade(ticker, base_ms - 2000 + i * 10, reference, 1.0, taker)
        candidate = None
        for i, px in enumerate(prices):
            out = detector.on_trade(ticker, base_ms + i, px, size, taker)
            candidate = candidate or out
        return candidate

    def settle(self, detector, ticker="M", at_ms=10_000_000):
        """Close any open burst window so held near misses are emitted."""
        detector.flush_subthreshold(at_ms)

    def test_a_near_miss_is_reported_and_not_traded(self):
        candidate = self.feed(self.detector, prices=(40.0, 41.0, 42.0), size=40.0)
        self.assertIsNone(candidate)
        self.settle(self.detector)
        self.assertEqual(len(self.seen), 1)
        observation = self.seen[0]
        self.assertEqual(observation["ticker"], "M")
        self.assertIn("size", observation["below"])
        self.assertGreater(observation["dl"], 0)

    def test_a_tradeable_sweep_is_never_reported_as_a_near_miss(self):
        """The pre-echo case: a sweep is below the floor part-way up.

        Prices 40..47 report levels=3 two milliseconds before the same burst
        becomes a tradeable levels=8 candidate.  Recording that instant would
        put a pre-echo of a real sweep into the near-miss inventory.
        """
        candidate = self.feed(
            self.detector, prices=tuple(40.0 + i for i in range(8)), size=400.0,
        )
        self.assertIsNotNone(candidate)
        self.settle(self.detector)
        self.assertEqual(self.seen, [])

    def test_capture_can_be_switched_off_without_touching_trading(self):
        with patch.object(config, "SUBTHRESHOLD_CAPTURE", False):
            self.assertIsNone(self.feed(self.detector, prices=(40.0, 41.0, 42.0)))
            self.settle(self.detector)
        self.assertEqual(self.seen, [])

    def test_the_research_floor_bounds_what_is_recorded(self):
        with patch.object(config, "SUBTHRESHOLD_DL_MIN", 99.0):
            self.feed(self.detector, prices=(40.0, 41.0, 42.0))
            self.settle(self.detector)
        self.assertEqual(self.seen, [])

    def test_the_held_observation_is_the_best_of_its_burst(self):
        self.feed(self.detector, prices=(40.0, 41.0, 42.0), size=40.0)
        self.settle(self.detector)
        self.assertEqual(len(self.seen), 1)
        # 42 is the burst extreme, so the reported displacement is the
        # burst's best attempt at the floor, not its first tick.
        self.assertEqual(self.seen[0]["ext"], 42.0)
        self.assertEqual(self.seen[0]["levels"], 3)

    def test_the_rate_limit_is_per_market_and_does_not_roll_forward(self):
        """A sustained flurry must not push admission indefinitely.

        The trading cooldown advances its window on every suppressed candidate,
        which lets a busy market emit nothing at all.  Research capture must
        not repeat that: after one interval a new observation is admitted even
        though bursts never stopped arriving.
        """
        with patch.object(config, "SUBTHRESHOLD_COOLDOWN_S", 1.0):
            for base in range(0, 6000, 500):
                self.feed(self.detector, base_ms=100_000 + base,
                          prices=(40.0, 41.0, 42.0), size=40.0)
            self.settle(self.detector)
        self.assertGreaterEqual(len(self.seen), 2)
        gaps = [b["ts_ms"] - a["ts_ms"] for a, b in zip(self.seen, self.seen[1:])]
        self.assertTrue(all(gap >= 1000 for gap in gaps), gaps)

    def test_a_failing_sink_cannot_break_the_trading_path(self):
        def explode(_observation):
            raise RuntimeError("sink down")

        detector = Detector(subthreshold_sink=explode)
        self.assertIsNone(self.feed(detector, prices=(40.0, 41.0, 42.0)))
        detector.flush_subthreshold(10_000_000)
        tradeable = self.feed(
            detector, ticker="N", prices=tuple(40.0 + i for i in range(8)),
            size=400.0,
        )
        self.assertIsNotNone(tradeable)

    def test_default_detector_reports_nothing(self):
        """An unwired detector behaves exactly as before this feature."""
        detector = Detector()
        self.assertIsNone(self.feed(detector, prices=(40.0, 41.0, 42.0)))
        detector.flush_subthreshold(10_000_000)


class SubthresholdEngineTests(unittest.TestCase):
    """The engine writes observations without touching the trading machinery."""

    def make_engine(self):
        engine = Engine.__new__(Engine)
        engine.meta = {"T": {"event": "E", "series": "S",
                             "close_time": "2026-09-02T20:00:00Z"}}
        engine.mode = "live"
        engine.clock_tracker = MatchClockTracker()
        engine._record_error = Mock()
        return engine

    def observation(self, **over):
        row = {"ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
               "dl": 0.42, "signed": 0.42, "levels": 4, "size": 120.0,
               "ref": 40.0, "ext": 46.0, "below": ["dl", "levels"]}
        row.update(over)
        return row

    @patch("app.engine.store.insert_signal", return_value=7)
    def test_the_observation_is_recorded_as_its_own_outcome(self, insert):
        engine = self.make_engine()
        engine.record_subthreshold(self.observation())
        written = insert.call_args.args[0]
        self.assertEqual(written["outcome"], "subthreshold")
        self.assertEqual(written["detail"]["strategy"], "subthreshold_observer")
        self.assertEqual(written["detail"]["below"], ["dl", "levels"])
        self.assertEqual(written["detail"]["trading_floor"]["dl_min"], config.DL_MIN)
        # No forward path: these are numerous by design.
        self.assertIsNone(written["forward_path_started_ts"])
        # Never confirmed, so no confirmation lag may be implied.
        self.assertIsNone(written["conf_lag_ms"])

    @patch("app.engine.store.insert_signal", return_value=7)
    def test_recording_never_moves_a_clock_health_counter(self, _insert):
        """A near miss is not a candidate, so it cannot register a gate miss."""
        engine = self.make_engine()
        before = engine.clock_tracker.clock_gate_candidate_misses
        engine.record_subthreshold(self.observation())
        self.assertEqual(
            engine.clock_tracker.clock_gate_candidate_misses, before)
        engine._record_error.assert_not_called()

    @patch("app.engine.store.insert_signal", side_effect=RuntimeError("db down"))
    def test_a_write_failure_is_reported_and_contained(self, _insert):
        engine = self.make_engine()
        engine.record_subthreshold(self.observation())
        engine._record_error.assert_called_once()
        self.assertEqual(engine._record_error.call_args.args[0], "subthreshold")


class SubthresholdStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(config, "DATA_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()
        store.set_mode("live")

    def add(self, outcome, strategy, dl=1.0):
        return store.insert_signal({
            "ts_ms": 1, "local_ts": 1.0, "market": "M", "event": "E",
            "series": "S", "dir": 1, "dl": dl, "levels": 6, "size": 300,
            "ref": 40, "ext": 50, "conf_lag_ms": None, "late": 1,
            "outcome": outcome, "detail": {"strategy": strategy},
        })

    def test_observations_stay_out_of_the_sleeve_funnels(self):
        self.add("filled", "gate_a")
        for _ in range(5):
            self.add("subthreshold", "subthreshold_observer", dl=0.4)
        stats = store.stats("live")
        self.assertEqual(stats["subthreshold_observations"], 5)
        gate_a = stats["sleeves"]["gate_a"]["signals"]
        self.assertNotIn("subthreshold", gate_a)
        self.assertEqual(gate_a.get("filled"), 1)

    def test_observations_never_count_toward_a_kill_gate(self):
        for _ in range(80):
            self.add("subthreshold", "subthreshold_observer", dl=0.4)
        stats = store.stats("live")
        self.assertEqual(stats["kill"]["k2_ci"]["n_signals"], 0)
        self.assertEqual(stats["kill"]["k2_ci"]["status"], "COLLECTING")

    def test_observations_carry_the_features_a_refit_needs(self):
        self.add("subthreshold", "subthreshold_observer", dl=0.42)
        row = store.q("SELECT * FROM signals WHERE outcome='subthreshold'")[0]
        for field in ("dl", "levels", "size", "ref", "ext", "dir", "ts_ms"):
            self.assertIsNotNone(row[field], field)
        self.assertIsNotNone(row["config_id"])


if __name__ == "__main__":
    unittest.main()
