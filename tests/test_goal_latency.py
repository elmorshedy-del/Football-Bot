import unittest
from unittest.mock import patch

from app.goal_latency import (
    GoalLatencyObserver,
    classify_score_change,
    correlate_market_window,
    score_signature,
)


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


if __name__ == "__main__":
    unittest.main()
