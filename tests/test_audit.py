import unittest

from app.audit import build_trigger, match_signal_event, schedule_window, timing_fields
from app.match_events import normalize_match_event


def observation(row_id, event, observed_ts, before, after, kind="goal"):
    normalized = normalize_match_event(kind, before, after, {
        "details": {"minute": 89, "stoppage_time": 2},
    })
    return {
        "id": row_id,
        "event": event,
        "observed_ts": observed_ts,
        "change_kind": kind,
        "score_before": before,
        "score_after": after,
        "normalized_event": normalized,
        "response_ms": 18.5,
        "detail": {
            "poll_uncertainty_ms": 250.0,
            "live_data": {"milestone_id": "M1", "details": {"minute": 89}},
        },
    }


def signal(local_ts=100.0, inferred_state="equal_score_0", event="E"):
    return {
        "id": 4,
        "event": event,
        "local_ts": local_ts,
        "ts_ms": int((local_ts - 0.05) * 1000),
        "outcome": "filled",
        "dl": 0.8,
        "levels": 5,
        "size": 200.0,
        "conf_lag_ms": 7.0,
        "ref": 35.0,
        "ext": 60.0,
        "detail": {
            "strategy": "price_only_late_score",
            "order_arrival_ms": 200.0,
            "paper_latency_ms": 150.0,
            "sleeve": {"inferred_state": inferred_state, "target_spread_c": 2.0},
        },
    }


class TriggerEventAuditTests(unittest.TestCase):
    def test_market_first_reports_positive_feed_delay_without_claiming_causality(self):
        matched = match_signal_event(signal(), [
            observation(1, "E", 102.0, {"homeScore": 1, "awayScore": 0},
                        {"homeScore": 1, "awayScore": 1}),
        ], window_s=20)

        self.assertEqual(matched["timing_relation"], "market_signal_first")
        self.assertEqual(matched["event_minus_signal_ms"], 2000.0)
        self.assertEqual(matched["association"], "state_consistent")
        self.assertEqual(matched["causality"], "not_established")
        self.assertEqual(matched["match_status"], "nearest_same_match_event")

    def test_feed_first_reports_negative_feed_delay(self):
        matched = match_signal_event(signal(), [
            observation(1, "E", 98.0, {"homeScore": 1, "awayScore": 0},
                        {"homeScore": 1, "awayScore": 1}),
        ], window_s=20)

        self.assertEqual(matched["timing_relation"], "match_feed_first")
        self.assertEqual(matched["event_minus_signal_ms"], -2000.0)

    def test_nearest_same_event_equalizer_is_state_consistent(self):
        rows = [
            observation(9, "OTHER", 100.1, {"homeScore": 0, "awayScore": 0},
                        {"homeScore": 1, "awayScore": 0}),
            observation(2, "E", 101.0, {"homeScore": 1, "awayScore": 0},
                        {"homeScore": 1, "awayScore": 1}),
            observation(3, "E", 105.0, {"homeScore": 1, "awayScore": 1},
                        {"homeScore": 2, "awayScore": 1}),
        ]

        matched = match_signal_event(signal(), rows, window_s=20)

        self.assertEqual(matched["observation_id"], 2)
        self.assertEqual(matched["state_consistency"], "equalizer_consistent")
        self.assertEqual(matched["association"], "state_consistent")
        self.assertEqual(matched["canonical_event"]["human_label"],
                         "Away goal observed · 1–0 → 1–1")
        self.assertEqual(matched["raw_provider_payload"]["milestone_id"], "M1")

    def test_one_goal_lead_inference_is_state_consistent(self):
        matched = match_signal_event(signal(inferred_state="one_goal_lead_+1"), [
            observation(1, "E", 101.0, {"homeScore": 0, "awayScore": 0},
                        {"homeScore": 1, "awayScore": 0}),
        ])

        self.assertEqual(matched["state_consistency"], "one_goal_lead_consistent")

    def test_score_correction_is_explicitly_a_reversal(self):
        matched = match_signal_event(signal(), [
            observation(1, "E", 101.0, {"homeScore": 1, "awayScore": 1},
                        {"homeScore": 1, "awayScore": 0}, "score_correction"),
        ])

        self.assertEqual(matched["state_consistency"], "correction_or_reversal")
        self.assertEqual(matched["canonical_event"]["canonical_type"],
                         "score_correction.away")

    def test_wrong_post_score_is_marked_state_mismatch(self):
        matched = match_signal_event(signal(), [
            observation(1, "E", 101.0, {"homeScore": 1, "awayScore": 1},
                        {"homeScore": 2, "awayScore": 1}),
        ])

        self.assertEqual(matched["state_consistency"], "state_mismatch")

    def test_no_event_inside_fixed_window_returns_auditable_empty_match(self):
        matched = match_signal_event(signal(), [
            observation(1, "E", 121.0, {"homeScore": 0, "awayScore": 0},
                        {"homeScore": 1, "awayScore": 0}),
        ], window_s=20)

        self.assertEqual(matched["match_status"], "no_nearby_same_match_event")
        self.assertIsNone(matched["canonical_event"])
        self.assertEqual(matched["window_s"], 20.0)

    def test_trigger_and_all_execution_times_are_explicit(self):
        row = signal()
        trigger = build_trigger(row)
        timing = timing_fields(row, {
            "entry_ts": 100.15, "exit_ts": 130.0, "exit_reason": "sleeve_scratch",
        })

        self.assertEqual(trigger["strategy"], "price_only_late_score")
        self.assertEqual(trigger["observed"]["distinct_price_levels"], 5)
        self.assertEqual(trigger["price_only_inference"]["inferred_state"], "equal_score_0")
        self.assertAlmostEqual(timing["paper_order_arrival_ts"], 100.15)
        self.assertEqual(timing["entry_ts"], 100.15)
        self.assertEqual(timing["exit_ts"], 130.0)
        self.assertIsNone(timing["settlement_ts"])

    def test_expiration_proxy_is_explicit_and_auditable(self):
        row = signal(local_ts=100.0)
        row["expected_expiration_time"] = "1970-01-01T00:02:30Z"

        window = schedule_window(row)

        self.assertEqual(window["seconds_to_expected_expiration"], 50.0)
        self.assertTrue(window["inside_configured_window"])
        self.assertIn("not a verified live match clock", window["assumption"])

    def test_settlement_time_is_not_mislabeled_as_an_active_exit(self):
        timing = timing_fields(signal(), {
            "entry_ts": 100.15, "exit_ts": 190.0, "exit_reason": "settle",
        })

        self.assertIsNone(timing["exit_ts"])
        self.assertEqual(timing["settlement_ts"], 190.0)

    def test_audit_helpers_do_not_replace_stored_market_or_event_names(self):
        row = signal()
        row["market"] = "KXRAW-CONTRACT-ID"
        before = dict(row)

        row["trigger"] = build_trigger(row)
        row["matched_event"] = match_signal_event(row, [])

        self.assertEqual(row["market"], "KXRAW-CONTRACT-ID")
        self.assertEqual(row["event"], "E")
        self.assertEqual(before["market"], row["market"])


if __name__ == "__main__":
    unittest.main()
