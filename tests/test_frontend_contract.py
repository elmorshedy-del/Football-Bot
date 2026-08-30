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
            "timing-diagnostics", "league-chart", "league-table",
            "association-chart", "chart-tooltip",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("ALL SYSTEMS GOOD", self.js)
        self.assertIn("Causation is not established", self.js)

    def test_frontend_does_not_silently_swallow_promise_failures(self):
        self.assertNotIn("catch(() => {})", self.js)
        self.assertIn("recordClientError", self.js)
        self.assertIn("socket.onerror", self.js)
        self.assertIn("socket.onclose", self.js)

    def test_large_export_uses_prepare_poll_and_native_download(self):
        self.assertIn('fetch("/api/export/prepare"', self.js)
        self.assertIn("/api/export/jobs/", self.js)
        self.assertNotIn("await response.blob()", self.js)

    def test_primary_ledgers_use_additive_display_names(self):
        self.assertIn("signal.display_game", self.js)
        self.assertIn("signal.display_contract", self.js)
        self.assertIn("trade.display_game", self.js)
        self.assertIn("position.display_game", self.js)
        self.assertIn('rawDetails("Raw identifiers', self.js)

    def test_tabs_filters_and_sentence_first_audit_are_contractual(self):
        for tab in ("overview", "trades", "signals", "leagues", "markets", "system"):
            self.assertIn(f'data-tab="{tab}"', self.html)
            self.assertIn(f'id="panel-{tab}"', self.html)
        for field in ("query", "strategy", "match", "result", "association", "period"):
            self.assertIn(f'data-filter-field="{field}"', self.js)
        self.assertIn("Filters apply to both audit views", self.js)
        self.assertIn("decisionSentence(signal)", self.js)
        self.assertIn("Not supplied by provider", self.js)
        self.assertNotIn("Not observed data", self.js)

    def test_trade_event_timeline_and_timing_proxy_are_visible(self):
        for audit_field in (
            "provider_description", "provider_clock", "provider_occurrence_ts",
            "occurrence_minus_signal_ms", "event_observed_ts", "event_minus_signal_ms",
        ):
            self.assertIn(audit_field, self.js)
        self.assertIn("sleeve_outside_window", self.js)
        self.assertIn("not a verified live match clock", self.js)
        self.assertIn("Schedule proxy, not live match time", self.js)

    def test_restored_analytics_have_sleeve_splits_and_rich_equity(self):
        self.assertIn("league.sleeves?.[leagueSleeve]", self.js)
        self.assertIn("Reconciled total", self.js)
        self.assertIn("drawdown-area", self.js)
        self.assertIn("chart-zero", self.js)
        self.assertIn("data-chart-tip", self.js)

    def test_phone_breakpoints_wrap_grids_and_raw_text(self):
        self.assertIn("@media (max-width: 760px)", self.css)
        self.assertIn("@media (max-width: 420px)", self.css)
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertNotIn("text-overflow: ellipsis", self.css)


if __name__ == "__main__":
    unittest.main()
