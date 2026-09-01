import asyncio
import sqlite3
import threading
import unittest
from unittest.mock import patch

from app.goal_latency import (
    GoalLatencyObserver,
    classify_score_change,
    correlate_market_window,
    score_signature,
)
from app.match_clock import MatchClockGate


class ScoreInterpretationTests(unittest.TestCase):
    def test_extracts_nested_and_camel_case_scores_only(self):
        details = {
            "clock": 4512,
            "homeScore": 1,
            "away_score": "0",
            "periodScores": [{"home": 1, "away": 0}],
            "isLive": True,
        }

        self.assertEqual(score_signature(details), {
            "away_score": 0.0,
            "homeScore": 1.0,
            "periodScores.0.away": 0.0,
            "periodScores.0.home": 1.0,
        })

    def test_score_increase_is_goal_and_decrease_is_correction(self):
        before = {"homeScore": 1.0, "awayScore": 0.0}
        goal = {"homeScore": 1.0, "awayScore": 1.0}
        correction = {"homeScore": 1.0, "awayScore": 0.0}

        self.assertEqual(classify_score_change(before, goal), "goal")
        self.assertEqual(classify_score_change(goal, correction), "score_correction")


class MarketCorrelationTests(unittest.TestCase):
    def test_chooses_nearest_observation_of_each_kind(self):
        rows = [
            {"kind": "book", "mono": 8.0, "wall": 108.0},
            {"kind": "trade", "mono": 9.5, "wall": 109.5},
            {"kind": "book", "mono": 9.8, "wall": 109.8},
            {"kind": "trade", "mono": 10.2, "wall": 110.2},
        ]

        before = correlate_market_window(rows, 10.0, before=True)
        after = correlate_market_window(rows, 10.0, before=False)

        self.assertEqual(before["book"]["delta_ms"], -200.0)
        self.assertEqual(before["trade"]["delta_ms"], -500.0)
        self.assertEqual(after["trade"]["delta_ms"], 200.0)
        self.assertNotIn("book", after)


class ObserverFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_payload_is_baseline_and_next_score_is_recorded(self):
        class Client:
            def __init__(self):
                self.live = [
                    {"milestone_id": "M", "type": "soccer",
                     "details": {"homeScore": 0, "awayScore": 0}},
                    {"milestone_id": "M", "type": "soccer",
                     "details": {"homeScore": 1, "awayScore": 0}},
                ]

            async def get(self, path, **_params):
                if path == "/milestones":
                    return {"milestones": [{
                        "id": "M", "related_event_tickers": ["E"],
                    }]}
                return {"live_datas": [self.live.pop(0)]}

        active = {"E"}
        market_rows = [
            {"kind": "book", "mono": 0.0, "wall": 1.0, "ticker": "T"},
        ]
        observer = GoalLatencyObserver(Client(), lambda: active,
                                       lambda *_args: market_rows)

        with (
            patch("app.goal_latency.store.insert_goal_latency", return_value=7) as insert,
            patch("app.goal_latency.store.insert_match_clock", return_value=1),
            patch("app.goal_latency.store.upsert_provider_event", return_value=(1, True)),
            patch("app.goal_latency.store.add_latency"),
            patch("app.goal_latency.store.log_event"),
        ):
            await observer._resolve_new_events()
            await observer._poll()
            self.assertEqual(insert.call_count, 0)
            await observer._poll()

        self.assertEqual(insert.call_count, 1)
        recorded = insert.call_args.args[0]
        self.assertEqual(recorded["change_kind"], "goal")
        self.assertEqual(recorded["score_before"]["homeScore"], 0.0)
        self.assertEqual(recorded["score_after"]["homeScore"], 1.0)
        self.assertEqual(observer.goals, 1)
        self.assertIsNotNone(observer.status()["last_response_ms"])

        active.clear()
        await observer._resolve_new_events()
        self.assertEqual(observer.milestones, {})
        self.assertEqual(observer.events_by_milestone, {})


class ClockPersistenceHandoffTests(unittest.IsolatedAsyncioTestCase):
    """Handoff section 3: persistence must gate decision visibility."""

    DETAILS = {"time": "90+5'", "half": "2nd", "status": "live"}

    def observer(self):
        class Client:
            async def get(self, _path, **_params):
                return {"live_datas": []}

        self.enterContext(patch("app.goal_latency.store.log_event"))
        return GoalLatencyObserver(Client(), lambda: {"E"}, lambda *_a: [])

    def timing(self, ts):
        return {"started_wall": ts - 0.05, "received_wall": ts,
                "received_mono": ts, "response_ms": 50.0}

    async def test_signal_during_blocked_clock_insert_rejects_unpersisted_clock(self):
        """A signal racing a pending insert must never see the candidate."""
        release = threading.Event()
        entered = threading.Event()
        observed = {}

        def blocking_insert(_row):
            entered.set()
            if not release.wait(timeout=5.0):
                raise AssertionError("insert was never released")
            return 31

        observer = self.observer()
        observer.clock_tracker.set_mapping("E", "m1")

        with patch("app.goal_latency.store.insert_match_clock", blocking_insert):
            task = asyncio.ensure_future(
                observer._record_clock("E", "m1", self.DETAILS, self.timing(1000.0)))
            await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5.0)

            # The trading loop runs here, while the insert is still pending.
            observed["stamp"] = observer.clock_tracker.stamp("E", 1000.05)
            observed["gate"] = MatchClockGate(observed["stamp"]).evaluate()

            release.set()
            await task

        self.assertNotEqual(
            observed["stamp"]["gate_outcome"], "clock_88_plus",
            "an unpersisted candidate was accepted by the 88+ gate",
        )
        self.assertFalse(observed["stamp"]["usable_for_88_gate"])
        self.assertFalse(observed["gate"]["accepted"])
        self.assertEqual(observed["gate"]["outcome"], "clock_unpersisted")

        settled = observer.clock_tracker.stamp("E", 1000.10)
        self.assertEqual(settled["observation_id"], 31)
        self.assertTrue(settled["usable_for_88_gate"])

    async def test_failed_clock_insert_retries_identical_next_poll(self):
        """A failed insert must leave the identity uncommitted so it retries."""
        calls = []

        def flaky_insert(row):
            calls.append(dict(row))
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return 44

        observer = self.observer()
        observer.clock_tracker.set_mapping("E", "m1")

        with patch("app.goal_latency.store.insert_match_clock", flaky_insert):
            await observer._record_clock("E", "m1", self.DETAILS, self.timing(1000.0))

            failed = observer.clock_tracker.stamp("E", 1000.05)
            self.assertFalse(
                failed["usable_for_88_gate"],
                "no acceptance may occur before a successful id is published",
            )

            await observer._record_clock("E", "m1", self.DETAILS, self.timing(1000.25))

        self.assertEqual(
            len(calls), 2,
            "an identical poll after a failed insert must retry the insert",
        )
        settled = observer.clock_tracker.stamp("E", 1000.30)
        self.assertEqual(settled["observation_id"], 44)
        self.assertTrue(settled["usable_for_88_gate"])

    async def test_persistence_failure_is_reported_as_a_current_fault(self):
        def always_fails(_row):
            raise sqlite3.OperationalError("disk I/O error")

        observer = self.observer()
        observer.clock_tracker.set_mapping("E", "m1")

        with patch("app.goal_latency.store.insert_match_clock", always_fails):
            await observer._record_clock("E", "m1", self.DETAILS, self.timing(1000.0))

        coverage = observer.clock_tracker.coverage({"E"}, now=1000.05)
        reasons = [row["reason"] for row in coverage["faults"]]
        self.assertIn("clock_persistence_failed", reasons)
        self.assertEqual(coverage["clock_present"], 0)


if __name__ == "__main__":
    unittest.main()
