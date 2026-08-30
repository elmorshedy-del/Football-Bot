import unittest
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
        engine.last_entry_ms = {}
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
        update_outcome.assert_called_once_with(42, "filled", {"strategy": "gate_a"})
        executed = engine.desk.try_enter.call_args.args[1]
        self.assertEqual(executed["strategy"], "gate_a")
        self.assertEqual(executed["detail"], {"strategy": "gate_a"})
        engine._announce_signal.assert_called_once_with(42, executed, 10.0, "filled")

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

        recorded = engine.record_signal.call_args.args[0]
        self.assertEqual(engine.record_signal.call_args.args[1:], (
            10.0, "sleeve_incoherent_sibling_rise",
        ))
        engine.desk.try_enter.assert_not_called()
        self.assertNotIn("sleeve", candidate)
        self.assertEqual(recorded["strategy"], "price_only_late_score")
        self.assertEqual(recorded["sleeve"]["decision"], "incoherent_sibling_rise")

    @patch("app.engine.store.update_signal_outcome")
    @patch("app.engine.store.add_latency")
    def test_parallel_rejection_in_price_sleeve_does_not_block_gate_a(
            self, _latency, _update_outcome):
        engine = self.make_engine()
        engine.desk.try_enter.return_value = "filled"
        engine.record_signal.side_effect = [41, 42]
        engine.late_score_sleeve.classify.return_value = SleeveDecision(
            False,
            "incoherent_sibling_rise",
            {"strategy": "price_only_late_score_v1", "feed_independent": True},
        )
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.PRICE_ONLY_SLEEVE_MODE", "parallel"), \
                patch("app.engine.config.LATE_ONLY", False):
            outcomes = engine.act_on_signal(candidate, 10.0)

        self.assertEqual(outcomes, {
            "gate_a": "filled",
            "price_only_late_score": "sleeve_incoherent_sibling_rise",
        })
        self.assertEqual(engine.desk.try_enter.call_count, 1)
        self.assertEqual(engine.desk.try_enter.call_args.args[1]["strategy"], "gate_a")
        rejected = engine.record_signal.call_args_list[1].args[0]
        self.assertEqual(rejected["strategy"], "price_only_late_score")

    def test_parallel_queues_two_strategy_specific_orders(self):
        engine = self.make_engine()
        engine.record_signal.side_effect = [41, 42]
        engine.late_score_sleeve.classify.return_value = SleeveDecision(
            True,
            "accepted",
            {"strategy": "price_only_late_score_v1", "feed_independent": True},
        )
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.PRICE_ONLY_SLEEVE_MODE", "parallel"), \
                patch("app.engine.config.PAPER_EXECUTION_V2", True), \
                patch("app.engine.config.LATE_ONLY", False), \
                patch("app.engine.store.add_latency"):
            outcomes = engine.act_on_signal(candidate, 10.0)

        self.assertEqual(outcomes, {
            "gate_a": "queued", "price_only_late_score": "queued",
        })
        queued = engine.desk.queue_enter.call_args_list
        self.assertEqual([call.args[0] for call in queued], [41, 42])
        self.assertEqual([call.args[1]["strategy"] for call in queued], [
            "gate_a", "price_only_late_score",
        ])
        self.assertNotIn("strategy", candidate)

    @patch("app.engine.store.update_signal_outcome")
    @patch("app.engine.store.add_latency")
    def test_lockout_is_scoped_to_strategy_and_market(
            self, _latency, _update_outcome):
        engine = self.make_engine()
        engine.last_entry_ms[("price_only_late_score", "T")] = 900
        engine.record_signal.side_effect = [41, 42]
        engine.desk.try_enter.return_value = "filled"
        engine.late_score_sleeve.classify.return_value = SleeveDecision(
            True, "accepted", {"strategy": "price_only_late_score_v1"},
        )
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.PRICE_ONLY_SLEEVE_MODE", "parallel"), \
                patch("app.engine.config.LOCKOUT_S", 120), \
                patch("app.engine.config.LATE_ONLY", False):
            outcomes = engine.act_on_signal(candidate, 10.0)

        self.assertEqual(outcomes["gate_a"], "filled")
        self.assertEqual(outcomes["price_only_late_score"], "strategy_lockout")
        self.assertEqual(engine.desk.try_enter.call_count, 1)
        self.assertIn(("gate_a", "T"), engine.last_entry_ms)


if __name__ == "__main__":
    unittest.main()
