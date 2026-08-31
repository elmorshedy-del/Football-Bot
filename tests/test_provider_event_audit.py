"""Merge blocker 5: provider-event auditing must be complete."""
from pathlib import Path
import unittest

from app.audit import match_signal_event


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

    def test_corrections_link_across_polls(self):
        """previous_fingerprint reset every poll, so a correction arriving on a
        later poll than the goal it revises was stored unlinked."""
        source = (ROOT / "app" / "goal_latency.py").read_text()
        self.assertIn("self.last_substantive_fingerprint", source)
        self.assertIn(
            "previous_fingerprint = self.last_substantive_fingerprint.get(event)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
