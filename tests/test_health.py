import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.engine import Engine


class HealthStatusTests(unittest.TestCase):
    @staticmethod
    def engine():
        engine = Engine.__new__(Engine)
        engine.feed_lag = deque([10.0, 20.0])
        engine.recorder = SimpleNamespace(
            total=20,
            status=lambda: {"healthy": True, "failures": 0, "last_error": None},
        )
        engine.goal_latency = None
        engine.ws_state = "connected"
        engine.errors = deque(maxlen=50)
        engine.started = time.time() - 60
        engine.mode = "live"
        engine.meta = {}
        engine.event_markets = {}
        engine.n_trades = 0
        engine.desk = SimpleNamespace(
            positions={}, pending_entries=[], pending_exits={}, kill=False,
        )
        engine.demo_status = ""
        engine.cred_error = ""
        engine.n_foreign = 0
        return engine

    @patch("app.engine.store.latency_readiness", return_value={
        "order_arrival_ms": {
            "state": "BREACH", "p95": 3642.1875, "n": 50, "threshold_ms": 250,
        },
    })
    @patch("app.engine.store.database_health",
           return_value={"healthy": True, "status": "connected"})
    def test_k4_breach_prevents_all_systems_good(self, _database, _latency):
        status = self.engine().status()

        self.assertFalse(status["health"]["ok"])
        self.assertTrue(status["health"]["runtime_ok"])
        self.assertEqual(status["health"]["banner"], "latency_breach")
        self.assertIn("latency breached", status["health"]["banner_text"])
        self.assertEqual(status["health"]["checks"]["latency_evidence"]["status"], "BREACH")

    @patch("app.engine.store.latency_readiness", return_value={
        "order_arrival_ms": {"state": "COLLECTING", "p95": None, "n": 4, "threshold_ms": 250},
    })
    @patch("app.engine.store.database_health",
           return_value={"healthy": True, "status": "connected"})
    def test_collecting_latency_is_runtime_healthy_not_all_good(self, _database, _latency):
        status = self.engine().status()

        self.assertTrue(status["health"]["ok"])
        self.assertEqual(status["health"]["banner"], "evidence_not_ready")
        self.assertIn("paper evidence not ready", status["health"]["banner_text"])

    @patch("app.engine.store.latency_readiness", return_value={
        "order_arrival_ms": {"state": "PASS", "p95": 120.0, "n": 40, "threshold_ms": 250},
    })
    @patch("app.engine.store.database_health",
           return_value={"healthy": True, "status": "connected"})
    def test_all_checks_can_report_healthy(self, _database, _latency):
        status = self.engine().status()

        self.assertTrue(status["health"]["ok"])
        self.assertEqual(status["health"]["banner"], "all_systems_good")
        self.assertTrue(all(
            check["healthy"] for check in status["health"]["checks"].values()
        ))
        self.assertEqual(status["health"]["recent_errors"], [])

    @patch("app.engine.store.latency_readiness", return_value={
        "order_arrival_ms": {"state": "PASS", "p95": 120.0, "n": 40, "threshold_ms": 250},
    })
    @patch("app.engine.store.database_health",
           return_value={"healthy": False, "status": "error", "error": "disk full"})
    def test_disconnect_database_and_recent_error_are_visible(self, _database, _latency):
        engine = self.engine()
        engine.ws_state = "disconnected: ConnectionClosed"
        engine.errors.append({
            "ts": time.time(), "component": "paper_exit", "message": "write failed",
        })

        status = engine.status()

        self.assertFalse(status["health"]["ok"])
        self.assertFalse(status["health"]["checks"]["websocket"]["healthy"])
        self.assertFalse(status["health"]["checks"]["database"]["healthy"])
        self.assertFalse(status["health"]["checks"]["paper_execution"]["healthy"])
        self.assertFalse(status["health"]["checks"]["recent_backend_faults"]["healthy"])
        self.assertEqual(status["health"]["recent_errors"][0]["message"], "write failed")

    @patch("app.engine.store.latency_readiness", return_value={
        "order_arrival_ms": {"state": "PASS", "p95": 120.0, "n": 40, "threshold_ms": 250},
    })
    @patch("app.engine.store.database_health",
           return_value={"healthy": True, "status": "connected"})
    def test_recent_non_execution_fault_prevents_false_all_good(self, _database, _latency):
        engine = self.engine()
        engine.errors.append({
            "ts": time.time(), "component": "study_export", "message": "disk full",
        })

        status = engine.status()

        self.assertFalse(status["health"]["ok"])
        self.assertTrue(status["health"]["checks"]["paper_execution"]["healthy"])
        self.assertFalse(status["health"]["checks"]["recent_backend_faults"]["healthy"])
        self.assertEqual(status["health"]["checks"]["recent_backend_faults"]["status"],
                         "1 recent")

    @patch("app.engine.store.latency_readiness", return_value={
        "order_arrival_ms": {"state": "PASS", "p95": 120.0, "n": 40, "threshold_ms": 250},
    })
    @patch("app.engine.store.database_health",
           return_value={"healthy": True, "status": "connected"})
    def test_stale_mapped_match_feed_is_visible(self, _database, _latency):
        engine = self.engine()
        engine.goal_latency = SimpleNamespace(status=lambda: {
            "enabled": True,
            "poll_ms": 250.0,
            "mapped_matches": 1,
            "last_poll_ts": time.time() - 10.0,
            "last_response_ms": 120.0,
            "last_error": None,
        })

        status = engine.status()
        check = status["health"]["checks"]["match_event_feed"]

        self.assertFalse(status["health"]["ok"])
        self.assertFalse(check["healthy"])
        self.assertEqual(check["status"], "stale")
        self.assertEqual(check["last_response_ms"], 120.0)

    @patch("app.engine.store.log_event")
    def test_websocket_disconnect_is_recorded_and_broadcast(self, _log):
        engine = self.engine()
        engine.q = Mock()
        engine._last_error_key = None
        engine._last_error_ts = 0.0

        engine.on_ws_state("disconnected: TimeoutError")

        self.assertEqual(engine.ws_state, "disconnected: TimeoutError")
        self.assertEqual(engine.errors[-1]["component"], "websocket")
        engine.q.put_nowait.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class ClockCoverageHealthTests(unittest.TestCase):
    """B2: clock coverage must gate the health banner."""

    def test_all_systems_good_is_impossible_with_clock_faults(self):
        from app.engine import _clock_coverage_check
        check = _clock_coverage_check({
            "watched": 1, "mapped": 0, "clock_present": 0, "clock_fresh": 0,
            "clock_stale": 0, "clock_gate_candidate_misses": 12,
            "faults": [{"event": "EV", "reason": "unmapped"}], "mapping_errors": [],
        })
        self.assertFalse(check["healthy"])
        self.assertIn("mapped", check["status"])
        self.assertIn("88-gate", check["status"])

    def test_a_healthy_clock_feed_reports_observing(self):
        from app.engine import _clock_coverage_check
        check = _clock_coverage_check({
            "watched": 2, "mapped": 2, "clock_present": 2, "clock_fresh": 2,
            "clock_stale": 0, "clock_gate_candidate_misses": 0,
            "faults": [], "mapping_errors": [],
        })
        self.assertTrue(check["healthy"])
        self.assertEqual(check["status"], "observing")

    def test_stale_clocks_and_mapping_errors_are_faults(self):
        from app.engine import _clock_coverage_check
        stale = _clock_coverage_check({
            "watched": 1, "mapped": 1, "clock_present": 1, "clock_fresh": 0,
            "clock_stale": 1, "clock_gate_candidate_misses": 0,
            "faults": [], "mapping_errors": [],
        })
        self.assertFalse(stale["healthy"])
        errored = _clock_coverage_check({
            "watched": 1, "mapped": 1, "clock_present": 1, "clock_fresh": 1,
            "clock_stale": 0, "clock_gate_candidate_misses": 0,
            "faults": [], "mapping_errors": [{"event": "EV", "error": "boom"}],
        })
        self.assertFalse(errored["healthy"])

    def test_clock_coverage_participates_in_runtime_ok(self):
        """It must be a runtime check, not evidence-readiness."""
        import inspect
        from app import engine
        source = inspect.getsource(engine.Engine.status)
        self.assertIn('"match_clock": _clock_coverage_check', source)
        self.assertIn('if name != "latency_evidence"', source)


class CurrentClockHealthTests(unittest.TestCase):
    """Handoff section 4: current state and cumulative evidence are separate."""

    def tracker(self, event="EV"):
        from app.match_clock import MatchClockTracker
        t = MatchClockTracker()
        t.set_mapping(event, "m1")
        return t

    def parsed(self, minute=None, period=None, status="live", stoppage=None):
        from app.match_clock import ParsedClock
        rendered = None
        if minute is not None:
            rendered = f"{minute}+{stoppage}'" if stoppage else f"{minute}'"
        return ParsedClock(period, minute, stoppage, rendered, status, "time", {})

    def timing(self, ts):
        return {"received_wall": ts, "started_wall": ts - 0.05,
                "response_ms": 50.0, "previous_poll_ts": ts - 0.25}

    def event_row(self, coverage, event="EV"):
        rows = [row for row in coverage.get("events") or [] if row["event"] == event]
        self.assertEqual(len(rows), 1, f"expected exactly one coverage row for {event}")
        return rows[0]

    def test_mapped_pre_match_without_clock_is_healthy_waiting(self):
        from app.engine import _clock_coverage_check
        t = self.tracker()
        t.observe("EV", "m1", self.parsed(status="pre-match"), self.timing(1000.0))

        coverage = t.coverage({"EV"}, now=1000.05)
        row = self.event_row(coverage)
        self.assertEqual(row["state"], "waiting")
        self.assertIsNone(row["current_fault"])
        self.assertTrue(_clock_coverage_check(coverage)["healthy"])

    def test_live_provider_without_persisted_clock_is_fault(self):
        from app.engine import _clock_coverage_check
        t = self.tracker()
        t.observe("EV", "m1", self.parsed(status="live"), self.timing(1000.0))

        coverage = t.coverage({"EV"}, now=1000.05)
        row = self.event_row(coverage)
        self.assertEqual(row["state"], "fault")
        self.assertEqual(row["current_fault"], "missing_clock")
        self.assertFalse(_clock_coverage_check(coverage)["healthy"])

    def test_active_candidate_with_missing_or_stale_clock_is_fault(self):
        from app.engine import _clock_coverage_check
        t = self.tracker()
        t.observe("EV", "m1", self.parsed(status=None), self.timing(1000.0))
        t.mark_candidate_active("EV", True)

        coverage = t.coverage({"EV"}, now=1000.05)
        row = self.event_row(coverage)
        self.assertTrue(row["candidate_active"])
        self.assertEqual(row["state"], "fault")
        self.assertFalse(_clock_coverage_check(coverage)["healthy"])

    def test_persisted_reconfirmation_clears_current_fault_but_keeps_total_miss_count(self):
        from app.engine import _clock_coverage_check
        t = self.tracker()
        t.observe("EV", "m1", self.parsed(status="live"), self.timing(1000.0))
        t.clock_gate_candidate_misses += 3
        self.assertFalse(_clock_coverage_check(t.coverage({"EV"}, now=1000.05))["healthy"])

        t.observe("EV", "m1", self.parsed(90, "2nd", "live"), self.timing(1001.0))
        t.promote("EV", 12)

        coverage = t.coverage({"EV"}, now=1001.05)
        row = self.event_row(coverage)
        self.assertEqual(row["state"], "observing")
        self.assertIsNone(row["current_fault"])
        self.assertEqual(coverage["clock_gate_candidate_misses_total"], 3)
        self.assertTrue(
            _clock_coverage_check(coverage)["healthy"],
            "a historical miss must not block recovery forever",
        )

    def test_pending_idless_clock_never_counts_present_or_fresh(self):
        t = self.tracker()
        t.observe("EV", "m1", self.parsed(90, "2nd", "live"), self.timing(1000.0))

        coverage = t.coverage({"EV"}, now=1000.05)
        row = self.event_row(coverage)
        self.assertIsNone(row["observation_id"])
        self.assertFalse(row["clock_present"])
        self.assertFalse(row["clock_fresh"])
        self.assertEqual(coverage["clock_present"], 0)
        self.assertEqual(coverage["clock_fresh"], 0)

    def test_clock_freshness_decays_at_status_time(self):
        t = self.tracker()
        t.observe("EV", "m1", self.parsed(90, "2nd", "live"), self.timing(1000.0))
        t.promote("EV", 5)

        self.assertTrue(self.event_row(t.coverage({"EV"}, now=1000.05))["clock_fresh"])
        later = t.coverage({"EV"}, now=1030.0)
        self.assertFalse(self.event_row(later)["clock_fresh"])
        self.assertEqual(self.event_row(later)["current_fault"], "stale")

    def test_all_good_is_impossible_during_current_clock_fault_and_returns_after_recovery(self):
        from app.engine import _clock_coverage_check
        t = self.tracker()
        t.observe("EV", "m1", self.parsed(90, "2nd", "live"), self.timing(1000.0))
        t.promote("EV", 9)
        self.assertTrue(_clock_coverage_check(t.coverage({"EV"}, now=1000.05))["healthy"])

        stale = t.coverage({"EV"}, now=1030.0)
        self.assertFalse(_clock_coverage_check(stale)["healthy"])

        t.observe("EV", "m1", self.parsed(91, "2nd", "live"), self.timing(1031.0))
        t.promote("EV", 10)
        recovered = t.coverage({"EV"}, now=1031.05)
        self.assertTrue(
            _clock_coverage_check(recovered)["healthy"],
            "all-good must be reachable again after the current fault clears",
        )
