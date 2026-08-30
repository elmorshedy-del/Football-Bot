from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "index.html").read_text()
        cls.css = (ROOT / "static" / "style.css").read_text()
        cls.js = (ROOT / "static" / "app.js").read_text()

    def test_audit_and_health_surfaces_are_present(self):
        for element_id in (
            "health-panel", "health-checks", "error-list", "sleeve-cards",
            "signal-list", "event-list", "latency-chart", "evidence-list",
            "export-button", "runtime-event-poll", "runtime-event-response",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("ALL SYSTEMS GOOD", self.js)
        self.assertIn("Causation is not established", self.js)

    def test_frontend_does_not_silently_swallow_promise_failures(self):
        self.assertNotIn("catch(() => {})", self.js)
        self.assertIn("recordClientError", self.js)
        self.assertIn("socket.onerror", self.js)
        self.assertIn("socket.onclose", self.js)

    def test_primary_ledgers_use_additive_display_names(self):
        self.assertIn("signal.display_game", self.js)
        self.assertIn("signal.display_contract", self.js)
        self.assertIn("trade.display_game", self.js)
        self.assertIn("position.display_game", self.js)
        self.assertIn('rawDetails("Raw identifiers', self.js)

    def test_phone_breakpoints_wrap_grids_and_raw_text(self):
        self.assertIn("@media (max-width: 760px)", self.css)
        self.assertIn("@media (max-width: 420px)", self.css)
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertNotIn("text-overflow: ellipsis", self.css)


if __name__ == "__main__":
    unittest.main()
