"""Episode admission: the cooldown anchor and the confirmation wait.

Two defects shaped the recorded episode inventory rather than any rule the
operator chose:

* the episode cooldown re-armed on every *suppressed* candidate, so a market
  printing sweeps faster than the interval could be silenced indefinitely;
* an unconfirmed candidate was held for a fixed 200ms of wall clock against an
  observed feed lag p95 of 0.9-1.1s, so siblings whose exchange timestamps were
  within CONF_MS were dropped as unconfirmed purely for arriving late. 76% of
  Gate A signals in the first live study ended `unconfirmed`.
"""
import unittest
from unittest.mock import Mock, patch

from app import config
from app.detector import Detector
from app.engine import Engine


class EpisodeCooldownTests(unittest.TestCase):
    def sweep(self, detector, ticker, ts_ms):
        """One burst comfortably over every Gate-A floor."""
        for i in range(6):
            detector.on_trade(ticker, ts_ms - 2000 + i * 10, 20.0, 1.0, "yes")
        out = None
        for i in range(8):
            out = detector.on_trade(ticker, ts_ms + i, 40.0 + i, 400.0, "yes") or out
        return out

    def test_the_cooldown_anchors_on_the_last_emitted_candidate(self):
        """A flurry inside the interval must not push admission indefinitely.

        Sweeps are spaced 3s apart: wider than the 2.1s reference window, so
        each burst prices cleanly, but inside the 5s episode cooldown. The
        candidate is emitted 4ms into a burst, so the anchor is ~100,004.
        """
        detector = Detector()
        with patch.object(config, "EPISODE_COOLDOWN_S", 5):
            self.assertIsNotNone(self.sweep(detector, "M", 100_000))
            # 3s later: inside the interval relative to the emitted candidate.
            self.assertIsNone(self.sweep(detector, "M", 103_000))
            # 6s after the emitted candidate, so the interval has elapsed and
            # this must be admitted. Before the fix the suppressed sweep above
            # had pushed the anchor to ~103,007, leaving only 3s and returning
            # None: a market printing faster than the interval was silenced.
            self.assertIsNotNone(self.sweep(detector, "M", 106_000))


class ConfirmationWaitTests(unittest.TestCase):
    def make_engine(self):
        engine = Engine.__new__(Engine)
        engine.meta = {"T": {"event": "E", "series": "S"},
                       "D": {"event": "E", "series": "S"}}
        engine.event_markets = {"E": ["T", "D"]}
        engine.prices = {}
        engine.pending = []
        engine.n_trades = 0
        engine.detector = Mock()
        engine.record_signal = Mock(return_value=1)
        engine.act_on_signal = Mock()
        engine.broadcast = Mock()
        engine._observe_sleeve = Mock()
        return engine

    def candidate(self):
        return {"ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
                "dl": 1.0, "levels": 6, "size": 300.0, "ref": 40.0, "ext": 55.0}

    def test_a_fresh_confirmation_still_trades(self):
        engine = self.make_engine()
        engine.detector.on_trade.return_value = None
        engine.detector.confirm.return_value = (True, 3.0)
        engine.pending = [{"cand": self.candidate(), "siblings": ["D"],
                           "queued_at": __import__("time").time(),
                           "deadline": __import__("time").time() + 2.0}]
        engine.process_trade("D", 1001, 44.0, 10.0, "no", 1.0)
        engine.act_on_signal.assert_called_once()
        engine.record_signal.assert_not_called()

    def test_a_late_confirmation_is_recorded_but_never_traded(self):
        """Coherent on the exchange clock, learned too late to act on."""
        engine = self.make_engine()
        engine.detector.on_trade.return_value = None
        engine.detector.confirm.return_value = (True, 3.0)
        stale = __import__("time").time() - 1.0
        engine.pending = [{"cand": self.candidate(), "siblings": ["D"],
                           "queued_at": stale,
                           "deadline": stale + config.CONF_WAIT_S}]
        engine.process_trade("D", 1001, 44.0, 10.0, "no", 1.0)
        engine.act_on_signal.assert_not_called()
        engine.record_signal.assert_called_once()
        self.assertEqual(engine.record_signal.call_args.args[2], "confirmed_late")
        # The confirmation lag is preserved: it is the measurement.
        self.assertEqual(engine.record_signal.call_args.args[1], 3.0)

    def test_the_tradeable_bound_preserves_the_previous_behaviour(self):
        """The longer wait must be additive evidence, not a trading change."""
        self.assertEqual(config.CONF_TRADE_MAX_AGE_S, 0.2)
        self.assertGreater(config.CONF_WAIT_S, config.CONF_TRADE_MAX_AGE_S)

    def test_a_candidate_that_never_confirms_still_expires(self):
        engine = self.make_engine()
        engine.detector.on_trade.return_value = None
        engine.detector.confirm.return_value = (False, None)
        past = __import__("time").time() - 10.0
        engine.pending = [{"cand": self.candidate(), "siblings": ["D"],
                           "queued_at": past, "deadline": past + 2.0}]
        engine.process_trade("D", 1001, 44.0, 10.0, "no", 1.0)
        engine.act_on_signal.assert_not_called()
        self.assertEqual(engine.record_signal.call_args.args[2], "unconfirmed")
        self.assertEqual(engine.pending, [])


if __name__ == "__main__":
    unittest.main()
