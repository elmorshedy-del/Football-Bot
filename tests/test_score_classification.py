"""B7: a period boundary is not a goal.

`classify_score_change` diffed the WHOLE numeric signature with a default of
0.0 for absent keys.  `score_signature` collects everything under a key
containing "score", and Kalshi's `details.period_scores` is a list of
`{away_score, home_score, number, type}`, so a second-half kickoff appended
`period_scores.1.number = 2` and the diff read a positive delta.  199 of 476
rows labelled `goal` in the first live study carried `side=unknown` and an
unchanged score, inflating any goal-rate statistic by roughly 2x.

The fixture below is built from the real payload shape: `home_same_game_score`,
`away_same_game_score`, `period_scores`, `half`, `status`, `status_text`,
`time` and `last_play`.
"""
import json
import tempfile
import unittest
from unittest.mock import patch

from app import config, store
from app.goal_latency import (
    GoalLatencyObserver,
    classify_score_change,
    score_signature,
)
from app.match_events import normalize_match_event, score_values


def live_details(home=0, away=0, periods=((0, 0),), half="1st",
                 time_text="43'", status="live", last_play=None):
    """A payload shaped like the real Kalshi `live_data.details` object."""
    return {
        "home_same_game_score": home,
        "away_same_game_score": away,
        "period_scores": [
            {"away_score": away_score, "home_score": home_score,
             "number": index + 1, "type": "REGULAR"}
            for index, (home_score, away_score) in enumerate(periods)
        ],
        "half": half,
        "status": status,
        "status_text": f"{half} Half {home}-{away} {time_text}",
        "time": time_text,
        "last_play": last_play or {
            "description": "Throw-in", "occurence_ts": 1_757_100_000,
        },
    }


class ScoreValueKeyTests(unittest.TestCase):
    def test_structural_keys_are_not_scores(self):
        signature = score_signature(live_details(home=1, away=0))

        self.assertIn("period_scores.0.number", signature,
                      "the fixture must reproduce the structural key")
        values = score_values(signature)
        self.assertNotIn("period_scores.0.number", values)
        self.assertEqual(sorted(values), [
            "away_same_game_score", "home_same_game_score",
            "period_scores.0.away_score", "period_scores.0.home_score",
        ])

    def test_aggregate_scores_are_kept(self):
        values = score_values({
            "home_aggregate_score": 3.0, "away_aggregate_score": 1.0,
            "period_scores.0.type": 1.0,
        })
        self.assertEqual(sorted(values),
                         ["away_aggregate_score", "home_aggregate_score"])


class ClassificationTests(unittest.TestCase):
    def signature(self, **kwargs):
        return score_signature(live_details(**kwargs))

    def test_a_second_half_kickoff_is_a_schema_change_not_a_goal(self):
        """The exact defect: `period_scores[1]` appears with number=2."""
        before = self.signature(home=1, away=0, periods=((1, 0),), half="1st",
                                time_text="45+2'")
        after = self.signature(home=1, away=0, periods=((1, 0), (0, 0)),
                               half="2nd", time_text="46'")

        self.assertNotEqual(before, after, "the raw signature must have changed")
        self.assertEqual(classify_score_change(before, after),
                         "score_schema_change")

    def test_a_genuine_goal_is_still_a_goal(self):
        before = self.signature(home=1, away=0, periods=((1, 0), (0, 0)),
                                half="2nd", time_text="88'")
        after = self.signature(home=1, away=1, periods=((1, 0), (0, 1)),
                               half="2nd", time_text="89'")

        self.assertEqual(classify_score_change(before, after), "goal")

    def test_a_goal_in_a_newly_appearing_period_is_a_goal(self):
        """A key that appears ABOVE zero is a real score."""
        before = self.signature(home=0, away=0, periods=((0, 0),))
        after = self.signature(home=1, away=0, periods=((0, 0), (1, 0)),
                               half="2nd")

        self.assertEqual(classify_score_change(before, after), "goal")

    def test_a_correction_is_still_a_correction(self):
        before = self.signature(home=2, away=0, periods=((2, 0),))
        after = self.signature(home=1, away=0, periods=((1, 0),))

        self.assertEqual(classify_score_change(before, after),
                         "score_correction")

    def test_a_correction_beats_a_goal_when_both_appear(self):
        before = self.signature(home=1, away=1, periods=((1, 1),))
        after = self.signature(home=0, away=2, periods=((0, 2),))

        self.assertEqual(classify_score_change(before, after),
                         "score_correction")

    def test_a_disappearing_key_is_a_schema_change_not_a_correction(self):
        before = {"home_same_game_score": 1.0, "away_same_game_score": 0.0,
                  "period_scores.1.home_score": 0.0}
        after = {"home_same_game_score": 1.0, "away_same_game_score": 0.0}

        self.assertEqual(classify_score_change(before, after),
                         "score_schema_change")

    def test_a_pure_structural_change_is_a_schema_change(self):
        before = {"home_same_game_score": 1.0, "period_scores.0.number": 1.0}
        after = {"home_same_game_score": 1.0, "period_scores.0.number": 2.0}

        self.assertEqual(classify_score_change(before, after),
                         "score_schema_change")

    def test_the_schema_change_normalizes_without_claiming_a_side(self):
        before = score_signature(live_details(home=1, away=0, periods=((1, 0),)))
        after = score_signature(live_details(
            home=1, away=0, periods=((1, 0), (0, 0)), half="2nd"))
        kind = classify_score_change(before, after)

        normalized = normalize_match_event(kind, before, after, {})

        self.assertEqual(normalized["canonical_type"],
                         "score_schema_change.unknown")
        self.assertEqual(normalized["side"], "unknown")


class PollSequenceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(config, "DATA_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        if store._conn is not None:
            store._conn.close()
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def test_the_clock_observation_stores_its_poll_sequence(self):
        for seq in (1, 2, 3):
            store.insert_match_clock({
                "observed_ts": float(seq), "poll_started_ts": seq - 0.25,
                "response_ms": 5.0, "event": "EV", "milestone_id": "m1",
                "provider_minute": 80 + seq, "raw_context": {},
                "poll_seq": seq,
            })

        rows = store.q(
            "SELECT poll_seq FROM match_clock_observations ORDER BY id")
        self.assertEqual([row["poll_seq"] for row in rows], [1, 2, 3])

    def test_the_goal_observation_stores_its_poll_sequence(self):
        for seq in (1, 2):
            store.insert_goal_latency({
                "observed_ts": float(seq), "event": "EV", "milestone_id": "m1",
                "change_kind": "goal", "score_before": {}, "score_after": {},
                "poll_started_ts": seq - 0.25, "response_ms": 5.0,
                "detail": {}, "poll_seq": seq,
            })

        rows = store.q(
            "SELECT poll_seq FROM goal_latency_observations ORDER BY id")
        self.assertEqual([row["poll_seq"] for row in rows], [1, 2])

    def test_a_historical_row_keeps_a_null_sequence(self):
        """Never backfilled: the counter did not exist when it was written."""
        store.insert_match_clock({
            "observed_ts": 1.0, "poll_started_ts": 0.75, "response_ms": 5.0,
            "event": "EV", "milestone_id": "m1", "raw_context": {},
        })

        row = store.q("SELECT poll_seq FROM match_clock_observations")[0]
        self.assertIsNone(row["poll_seq"])

    def test_the_observer_counter_is_monotonic_across_polls(self):
        observer = GoalLatencyObserver.__new__(GoalLatencyObserver)
        observer.polls = 0
        seen = []

        class Client:
            async def get(self, *_args, **_kwargs):
                return {"live_datas": []}

        observer.client = Client()
        observer.events_by_milestone = {"m1": "EV"}
        observer.last_poll_wall = None
        observer.last_response_ms = None

        original = store.add_latency
        try:
            store.add_latency = lambda *_a, **_k: None
            import asyncio

            async def run():
                for _ in range(3):
                    await observer._poll()
                    seen.append(observer.polls)

            asyncio.run(run())
        finally:
            store.add_latency = original

        self.assertEqual(seen, [1, 2, 3])


class AuditWindowTests(unittest.TestCase):
    def test_the_window_is_ninety_seconds_and_not_a_strategy_parameter(self):
        self.assertEqual(config.EVENT_MATCH_WINDOW_S, 90.0)
        self.assertNotIn("EVENT_MATCH_WINDOW_S", config.STRATEGY_PARAM_NAMES)

    def test_widening_the_window_does_not_repartition_the_study(self):
        before = config.config_id()
        with patch.object(config, "EVENT_MATCH_WINDOW_S", 20.0):
            self.assertEqual(config.config_id(), before)

    def test_the_manifest_still_reports_it_as_observability(self):
        from app import exporter
        self.assertIn("EVENT_MATCH_WINDOW_S", exporter._OBSERVABILITY_NAMES)
        exported = json.dumps(exporter.non_secret_config())
        self.assertIn("event_match_window_s", exported)


if __name__ == "__main__":
    unittest.main()
