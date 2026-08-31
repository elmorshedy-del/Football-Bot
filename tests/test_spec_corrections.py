"""Keep the spec-corrections notes discoverable and binding.

docs/SPEC_CORRECTIONS_AND_DEVIATIONS.md records where the production-integrity
specification is wrong, under-specified, or deliberately exceeded.  A document
can be ignored; a failing test cannot.  These assertions make removing the
pointers a CI failure rather than a silent regression, so an architect revising
the specification is routed to the corrections before re-issuing wording that is
known to reintroduce a defect.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "docs" / "SPEC_CORRECTIONS_AND_DEVIATIONS.md"
SPEC = ROOT / "docs" / "PRODUCTION_INTEGRITY_IMPLEMENTATION_SPEC.md"
AGENTS = ROOT / "AGENTS.md"


class SpecCorrectionsAreDiscoverableTests(unittest.TestCase):
    def test_notes_file_exists_and_is_substantive(self):
        self.assertTrue(NOTES.is_file(), f"{NOTES.name} was deleted")
        self.assertGreater(len(NOTES.read_text().splitlines()), 100)

    def test_specification_carries_the_stop_banner(self):
        """An architect editing the spec must hit the pointer immediately."""
        head = "\n".join(SPEC.read_text().splitlines()[:25])
        self.assertIn("SPEC_CORRECTIONS_AND_DEVIATIONS.md", head)
        self.assertIn("STOP", head)

    def test_agents_file_requires_the_notes_before_the_specification(self):
        text = AGENTS.read_text()
        self.assertIn("SPEC_CORRECTIONS_AND_DEVIATIONS.md", text)
        # Ordering is the point: the corrections outrank the specification.
        self.assertLess(
            text.index("SPEC_CORRECTIONS_AND_DEVIATIONS.md"),
            text.index("PRODUCTION_INTEGRITY_IMPLEMENTATION_SPEC.md"),
            "the corrections notes must be listed before the specification",
        )

    def test_every_recorded_item_is_still_present(self):
        """Entries may be resolved, but not quietly dropped."""
        text = NOTES.read_text()
        for item in (
            "## C1", "## C2", "## C3", "## C4", "## C5", "## C6", "## C7",
            "## E1", "## E2", "## E3",
            "## H1", "## H2", "## H3", "## H4",
        ):
            self.assertIn(item, text, f"{item} was removed from the notes")

    def test_load_bearing_invariants_are_named_in_the_notes(self):
        """The claims a future edit is most likely to undo without noticing."""
        text = NOTES.read_text()
        for claim in (
            "Position.bid_path",          # H1: exit logic depends on its shape
            "sleeve_oscillation",         # H1: one of the four path-derived exits
            "BID_PATH_FLUSH_EVERY",       # H2: no sync commit on the hot path
            "store.ex()",                 # H3: known unaddressed latency cause
            "ALL SYSTEMS GOOD",           # H4: contractual literal
            "exec_px",                    # E1: null rather than approximated
            "SIGNAL_PATH_WINDOW_S",       # E2: decline outcomes
            "2nd Half 90+5",              # C1: the parse that must never return 2
        ):
            self.assertIn(claim, text, f"notes no longer mention {claim}")


if __name__ == "__main__":
    unittest.main()
