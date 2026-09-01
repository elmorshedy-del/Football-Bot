"""Merge blocker 5: provider-event auditing must be complete."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import store
from app.audit import match_signal_event
from app.match_events import provider_occurrence


ROOT = Path(__file__).resolve().parents[1]


def provider_row(**overrides):
    """A provider_match_events row shaped as _event_observations returns it."""
    row = {
        "id": 42, "event": "KXGAME", "observed_ts": 1005.0,
        "first_observed_ts": 1005.0, "last_observed_ts": 1005.0,
        "canonical_type": "penalty.scored", "canonical_side": "home",
        "fingerprint": "fp-1", "previous_fingerprint": "fp-0",
        "provider_clock": "90+5'", "provider_minute": 90, "provider_stoppage": 5,
        "previous_poll_ts": 1004.75, "response_ms": 120.0,
        "normalized_event": {
            "canonical_type": "penalty.scored", "human_label": "Penalty scored",
            "provider_description": "Pululu scores from the spot", "scorer": "Pululu",
        },
        "raw_payload": {"details": {"last_play": {"occurence_ts": 998.5}}},
    }
    row.update(overrides)
    return row


class ProviderAssociationTests(unittest.TestCase):
    def test_canonical_provider_event_carries_full_audit_detail(self):
        """Reproduced: occurrence time, poll uncertainty and raw payload were
        all empty, because the association read `detail` (a goal_latency
        column) and provider rows carry `raw_payload`."""
        matched = match_signal_event({"local_ts": 1000.0, "event": "KXGAME"},
                                     [provider_row()])
        self.assertEqual(matched["provider_occurrence_ts"], 998.5)
        self.assertEqual(matched["occurrence_minus_signal_ms"], -1500.0)
        self.assertEqual(matched["provider_poll_uncertainty_ms"], 250.0)
        self.assertTrue(matched["raw_provider_payload"])
        self.assertNotEqual(matched["association"], "unmatched")

    def test_missing_previous_poll_leaves_uncertainty_null_not_invented(self):
        matched = match_signal_event(
            {"local_ts": 1000.0, "event": "KXGAME"},
            [provider_row(previous_poll_ts=None)],
        )
        self.assertIsNone(matched["provider_poll_uncertainty_ms"])

    def test_association_never_claims_causation(self):
        matched = match_signal_event({"local_ts": 1000.0, "event": "KXGAME"},
                                     [provider_row()])
        self.assertEqual(matched["causality"], "not_established")
        self.assertNotIn("caused_by", str(matched))


class ProviderLedgerWiringTests(unittest.TestCase):
    def test_api_selects_the_columns_the_association_needs(self):
        source = (ROOT / "app" / "main.py").read_text()
        block = source.split("provider_match_events", 1)[0]
        self.assertIn("previous_poll_ts", block)
        self.assertIn("previous_fingerprint", block)

    def test_dashboard_fetches_and_renders_the_canonical_ledger(self):
        """The match-feed view was score-change-only."""
        js = (ROOT / "static" / "app.js").read_text()
        html = (ROOT / "static" / "index.html").read_text()
        self.assertIn("/api/provider-events", js)
        self.assertIn("renderProviderEvents", js)
        self.assertIn("previous_fingerprint", js)
        self.assertIn('id="provider-event-list"', html)

class ProviderOccurrenceCanonicalizationTests(unittest.TestCase):
    """Handoff section 6.2: fixed precedence over real provider payload shapes."""

    def test_root_occurence_ts_on_a_significant_row(self):
        ts, source, reason = provider_occurrence({
            "event_type": "score_change", "occurence_ts": 998.5, "time": "90+5'",
        })
        self.assertEqual(ts, 998.5)
        self.assertEqual(source, "raw.occurence_ts")
        self.assertIsNone(reason)

    def test_root_corrected_occurrence_spelling(self):
        ts, source, reason = provider_occurrence({"occurrence_ts": 998.5})
        self.assertEqual(ts, 998.5)
        self.assertEqual(source, "raw.occurrence_ts")
        self.assertIsNone(reason)

    def test_full_payload_last_play_both_spellings(self):
        misspelled = provider_occurrence(
            {"details": {"last_play": {"occurence_ts": 100.0}}})
        self.assertEqual(misspelled[0], 100.0)
        self.assertEqual(misspelled[1], "raw.details.last_play.occurence_ts")

        corrected = provider_occurrence(
            {"details": {"last_play": {"occurrence_ts": 200.0}}})
        self.assertEqual(corrected[0], 200.0)
        self.assertEqual(corrected[1], "raw.details.last_play.occurrence_ts")

    def test_precedence_prefers_the_individual_row_over_the_full_payload(self):
        ts, source, _reason = provider_occurrence({
            "occurence_ts": 1.0,
            "occurrence_ts": 2.0,
            "details": {"last_play": {"occurence_ts": 3.0}},
        })
        self.assertEqual(ts, 1.0)
        self.assertEqual(source, "raw.occurence_ts")

    def test_absent_timestamp_is_null_with_the_exact_reason(self):
        ts, source, reason = provider_occurrence({"event_type": "score_change"})
        self.assertIsNone(ts)
        self.assertIsNone(source)
        self.assertEqual(reason, "provider_field_absent")

    def test_invalid_timestamps_are_refused_not_coerced(self):
        for value in (True, False, float("nan"), float("inf"), float("-inf"),
                      "998.5", "", None, -1.0, [998.5], {"ts": 998.5}):
            with self.subTest(value=value):
                ts, source, reason = provider_occurrence({"occurence_ts": value})
                self.assertIsNone(ts, f"{value!r} must not become an occurrence")
                self.assertIsNone(source)
                self.assertIn(reason, {"provider_field_invalid", "provider_field_absent"})

    def test_receipt_time_is_never_substituted_for_occurrence(self):
        ts, _source, reason = provider_occurrence({
            "first_observed_ts": 1005.0, "observed_ts": 1005.0, "time": "90+5'",
        })
        self.assertIsNone(ts)
        self.assertEqual(reason, "provider_field_absent")


class ProviderOccurrencePersistenceTests(unittest.TestCase):
    """Handoff section 6.2/6.3 against real SQLite."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()
        store.set_mode("live")

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def row(self, fingerprint="fp-1", canonical="goal.observed", payload=None,
            event="KXGAME", previous=None, observed_ts=1000.0):
        return {
            "observed_ts": observed_ts, "poll_started_ts": observed_ts - 0.05,
            "previous_poll_ts": observed_ts - 0.25, "response_ms": 50.0,
            "event": event, "milestone_id": "m1", "fingerprint": fingerprint,
            "previous_fingerprint": previous, "canonical_type": canonical,
            "canonical_side": "home", "provider_period": "2nd",
            "provider_minute": 90, "provider_stoppage": 5,
            "provider_clock": "90+5′",
            "normalized_event": {"canonical_type": canonical},
            "raw_payload": payload if payload is not None else {"occurence_ts": 998.5},
        }

    def stored(self, row_id):
        return store.q(
            "SELECT * FROM provider_match_events WHERE id=?", (row_id,),
        )[0]

    def test_normalized_occurrence_columns_are_persisted(self):
        row_id, _new = store.upsert_provider_event(self.row())
        stored = self.stored(row_id)
        self.assertEqual(stored["provider_occurrence_ts"], 998.5)
        self.assertEqual(stored["provider_occurrence_source"], "raw.occurence_ts")
        self.assertIsNone(stored["provider_occurrence_unavailable_reason"])

    def test_absent_occurrence_persists_the_reason_and_no_fabricated_time(self):
        row_id, _new = store.upsert_provider_event(
            self.row(payload={"event_type": "score_change"}))
        stored = self.stored(row_id)
        self.assertIsNone(stored["provider_occurrence_ts"])
        self.assertEqual(
            stored["provider_occurrence_unavailable_reason"], "provider_field_absent")
        self.assertNotEqual(stored["provider_occurrence_ts"], stored["observed_ts"])

    def test_duplicate_refresh_preserves_original_occurrence(self):
        row_id, _new = store.upsert_provider_event(self.row())
        refreshed = self.row(payload={"occurence_ts": 4444.0}, observed_ts=2000.0)
        store.upsert_provider_event(refreshed)

        stored = self.stored(row_id)
        self.assertEqual(stored["provider_occurrence_ts"], 998.5)
        self.assertIn("998.5", stored["raw_payload"])
        self.assertEqual(stored["last_observed_ts"], 2000.0)

    def test_correction_links_to_persisted_prior_substantive_event_after_restart(self):
        goal_id, _new = store.upsert_provider_event(self.row("fp-goal", "goal.observed"))
        self.assertTrue(goal_id)

        # A restart loses last_substantive_fingerprint entirely.
        resolved = store.previous_substantive_fingerprint("KXGAME")
        self.assertEqual(resolved, "fp-goal")

        correction_id, _new = store.upsert_provider_event(
            self.row("fp-corr", "score.correction", previous=resolved))
        self.assertEqual(self.stored(correction_id)["previous_fingerprint"], "fp-goal")

    def test_correction_cannot_link_across_events(self):
        store.upsert_provider_event(self.row("fp-a", "goal.observed", event="EVENT_A"))
        self.assertIsNone(store.previous_substantive_fingerprint("EVENT_B"))

    def test_correction_never_links_to_another_correction(self):
        store.upsert_provider_event(self.row("fp-goal", "goal.observed"))
        store.upsert_provider_event(
            self.row("fp-corr1", "score.correction", observed_ts=1001.0))

        self.assertEqual(store.previous_substantive_fingerprint("KXGAME"), "fp-goal")

    def test_lineage_lookup_is_mode_scoped(self):
        store.set_mode("demo")
        store.upsert_provider_event(self.row("fp-demo", "goal.observed"))
        store.set_mode("live")
        self.assertIsNone(
            store.previous_substantive_fingerprint("KXGAME"),
            "a live correction must not link to a demo event",
        )


if __name__ == "__main__":
    unittest.main()
