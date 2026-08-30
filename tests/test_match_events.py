import unittest

from app.match_events import event_consistency, normalize_match_event, score_pair


class MatchEventNormalizationTests(unittest.TestCase):
    def test_home_goal_has_canonical_name_score_and_provider_clock(self):
        normalized = normalize_match_event(
            "goal",
            {"homeScore": 0.0, "awayScore": 0.0},
            {"homeScore": 1.0, "awayScore": 0.0},
            {"details": {"minute": 90, "stoppageTime": 3, "period": 2}},
        )

        self.assertEqual(normalized["schema"], "football.match_event.v1")
        self.assertEqual(normalized["canonical_type"], "goal_observed.home")
        self.assertEqual(normalized["side"], "home")
        self.assertEqual(normalized["score_transition"], "0–0 → 1–0")
        self.assertEqual(normalized["provider_minute"], 90)
        self.assertEqual(normalized["provider_stoppage"], 3)
        self.assertEqual(normalized["provider_period"], 2)
        self.assertEqual(normalized["human_label"], "Home goal observed · 0–0 → 1–0")

    def test_away_equalizer_is_consistent_with_equal_score_inference(self):
        normalized = normalize_match_event(
            "goal",
            {"homeScore": 1.0, "awayScore": 0.0},
            {"homeScore": 1.0, "awayScore": 1.0},
        )

        self.assertEqual(normalized["canonical_type"], "goal_observed.away")
        self.assertEqual(
            event_consistency("equal_score_0", normalized),
            "equalizer_consistent",
        )

    def test_score_decrease_is_an_explicit_correction(self):
        normalized = normalize_match_event(
            "score_correction",
            {"homeScore": 1.0, "awayScore": 1.0},
            {"homeScore": 1.0, "awayScore": 0.0},
        )

        self.assertEqual(normalized["canonical_type"], "score_correction.away")
        self.assertEqual(normalized["human_label"], "Away score correction · 1–1 → 1–0")
        self.assertEqual(
            event_consistency("equal_score_0", normalized),
            "correction_or_reversal",
        )

    def test_nested_total_score_beats_period_score_and_clock_can_be_missing(self):
        signature = {
            "score.home": 2.0,
            "score.away": 1.0,
            "periodScores.0.home": 1.0,
            "periodScores.0.away": 1.0,
        }
        normalized = normalize_match_event(
            "goal",
            {"score.home": 1.0, "score.away": 1.0},
            signature,
            {"details": {}},
        )

        self.assertEqual(score_pair(signature), {"home": 2.0, "away": 1.0})
        self.assertIsNone(normalized["provider_minute"])
        self.assertIsNone(normalized["provider_clock"])
        self.assertEqual(normalized["score_transition"], "1–1 → 2–1")

    def test_al_hazm_penalty_equalizer_has_complete_canonical_event(self):
        before = {
            "home_same_game_score": 0.0,
            "away_same_game_score": 1.0,
            "period_scores.0.home_score": 0.0,
            "period_scores.0.away_score": 0.0,
        }
        after = {
            "home_same_game_score": 1.0,
            "away_same_game_score": 1.0,
            "home_aggregate_score": 1.0,
            "away_aggregate_score": 1.0,
            "period_scores.1.home_score": 1.0,
            "period_scores.1.away_score": 1.0,
        }
        live_data = {"details": {
            "half": "2nd",
            "home_significant_events": [{
                "event_type": "score_change",
                "player": "Pululu, Afimico",
                "time": "90+5'",
            }],
            "away_significant_events": [{
                "event_type": "score_change",
                "player": "Martínez, Roger",
                "time": "48'",
            }],
            "last_play": {
                "description": "Afimico Pululu scores from the spot to level the match at 1 - 1.",
            },
        }}

        normalized = normalize_match_event("goal", before, after, live_data)

        self.assertEqual(score_pair(after), {"home": 1.0, "away": 1.0})
        self.assertEqual(normalized["canonical_type"], "goal_observed.home")
        self.assertEqual(normalized["score_transition"], "0–1 → 1–1")
        self.assertEqual(normalized["provider_minute"], 90)
        self.assertEqual(normalized["provider_stoppage"], 5)
        self.assertEqual(normalized["provider_clock"], "90+5'")
        self.assertEqual(normalized["event_method"], "penalty")
        self.assertEqual(normalized["scorer"], "Afimico Pululu")
        self.assertEqual(
            normalized["human_label"], "Home penalty scored · 0–1 → 1–1",
        )


if __name__ == "__main__":
    unittest.main()
