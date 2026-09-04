import unittest
from unittest.mock import Mock, patch

from app.engine import Engine
from app.late_score_sleeve import SleeveDecision
from app.match_clock import MatchClockTracker, parse_current_clock


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
        engine.clock_tracker = MatchClockTracker()
        parsed = parse_current_clock({
            "time": "90+5'", "half": "2nd", "status": "live",
        })
        engine.clock_tracker.observe("E", "M", parsed, {
            "received_wall": 1.0, "started_wall": 0.999,
            "previous_poll_ts": 0.75, "response_ms": 5.0,
        })
        engine.clock_tracker.promote("E", 9)
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

    def test_al_hazm_90_plus_5_clock_reaches_price_classifier(self):
        engine = self.make_engine()
        engine.late_score_sleeve.classify.return_value = SleeveDecision(
            True, "accepted",
            {"strategy": "price_only_late_score_v1", "feed_independent": True},
        )
        engine.desk.try_enter.return_value = "filled"
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.PRICE_ONLY_SLEEVE_MODE", "enforce"), \
                patch("app.engine.config.LATE_ONLY", False), \
                patch("app.engine.store.add_latency"), \
                patch("app.engine.store.update_signal_outcome"):
            outcomes = engine.act_on_signal(candidate, 10.0)

        self.assertEqual(outcomes["price_only_late_score"], "filled")
        engine.late_score_sleeve.classify.assert_called_once()
        self.assertNotEqual(outcomes["price_only_late_score"], "sleeve_outside_window")

    @patch("app.match_clock.config.SLEEVE_MIN_MINUTE", 88)
    def test_minute_87_fails_closed_and_skips_classifier(self):
        """Pinned at 88 so this keeps asserting that a below-threshold minute
        short-circuits before the classifier, whatever the shipped default is."""
        engine = self.make_engine()
        parsed = parse_current_clock({"time": "87'", "half": "2nd", "status": "live"})
        engine.clock_tracker.observe("E", "M", parsed, {
            "received_wall": 1.0, "started_wall": 0.999,
            "previous_poll_ts": 0.75, "response_ms": 5.0,
        })
        # A reading is only decision-visible once persisted, so the 87 must be
        # promoted to replace the 90+5 established by make_engine().
        engine.clock_tracker.promote("E", 10)
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.PRICE_ONLY_SLEEVE_MODE", "enforce"):
            outcomes = engine.act_on_signal(candidate, 10.0)

        self.assertEqual(outcomes["price_only_late_score"], "sleeve_clock_pre_88")
        engine.late_score_sleeve.classify.assert_not_called()
        engine.desk.try_enter.assert_not_called()

    def test_expected_expiration_cannot_open_the_clock_gate(self):
        engine = self.make_engine()
        engine.clock_tracker = MatchClockTracker()
        engine.meta["T"]["close_time"] = "1970-01-01T00:00:01Z"
        candidate = {
            "ticker": "T", "ts_ms": 1000, "local_ts": 1.0, "dir": 1,
            "dl": 1.0, "levels": 5, "size": 200.0, "ref": 40.0, "ext": 60.0,
        }

        with patch("app.engine.config.PRICE_ONLY_SLEEVE_MODE", "enforce"), \
                patch("app.engine.config.SLEEVE_START_BEFORE_EXPIRY_MIN", 10_000), \
                patch("app.engine.config.SLEEVE_AFTER_EXPIRY_MIN", 10_000):
            outcomes = engine.act_on_signal(candidate, 10.0)

        self.assertEqual(outcomes["price_only_late_score"], "sleeve_clock_unmapped")
        engine.late_score_sleeve.classify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
