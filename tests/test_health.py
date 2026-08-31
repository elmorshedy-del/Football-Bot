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
