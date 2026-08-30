import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.engine import Engine
from app.late_score_sleeve import SleeveDecision


class SignalPersistenceTests(unittest.TestCase):
    def make_engine(self):
        engine = Engine.__new__(Engine)
        engine.meta = {"T": {"event": "E", "series": "S"}}
        engine.books = {"T": object()}
        engine.event_markets = {"E": ["T", "D", "A"]}
        engine.desk = Mock()
        engine.detector = Mock()
        engine.late_score_sleeve = Mock()
        engine.detector.state.return_value = SimpleNamespace(last_entry_ms=None)
        engine.is_late = Mock(return_value=True)
        engine.is_sleeve_window = Mock(return_value=True)
        engine.record_signal = Mock(return_value=42)
        engine._announce_signal = Mock()
        return engine

    @patch("app.engine.store.add_latency")
    @patch("app.engine.store.update_signal_outcome")
    def test_trade_uses_persisted_signal_id(self, update_outcome, _latency):
        engine = self.make_engine()
        engine.desk.try_enter.return_value = "filled"
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.LATE_ONLY", False):
            engine.act_on_signal(candidate, 10.0)

        self.assertEqual(engine.desk.try_enter.call_args.args[0], 42)
        update_outcome.assert_called_once_with(42, "filled", {})
        engine._announce_signal.assert_called_once_with(42, candidate, 10.0, "filled")

    @patch("app.engine.store.add_latency")
    @patch("app.engine.store.update_signal_outcome")
    def test_execution_error_is_recorded_without_false_fill(self, update_outcome, _latency):
        engine = self.make_engine()
        engine.desk.try_enter.side_effect = RuntimeError("database unavailable")
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.LATE_ONLY", False):
            engine.act_on_signal(candidate, 10.0)

        self.assertEqual(update_outcome.call_args.args[1], "execution_error")
        self.assertIn("database unavailable", update_outcome.call_args.args[2]["error"])

    def test_price_only_sleeve_rejection_never_reaches_execution(self):
        engine = self.make_engine()
        engine.late_score_sleeve.classify.return_value = SleeveDecision(
            False,
            "incoherent_sibling_rise",
            {"strategy": "price_only_late_score_v1", "feed_independent": True},
        )
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.PRICE_ONLY_SLEEVE_MODE", "enforce"):
            engine.act_on_signal(candidate, 10.0)

        engine.record_signal.assert_called_once_with(
            candidate, 10.0, "sleeve_incoherent_sibling_rise",
        )
        engine.desk.try_enter.assert_not_called()
        self.assertEqual(candidate["sleeve"]["decision"], "incoherent_sibling_rise")


if __name__ == "__main__":
    unittest.main()
