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
            "signal-list", "event-list", "latency-chart", "latency-table",
            "evidence-list", "export-button", "runtime-event-poll",
            "runtime-event-response", "timing-diagnostics", "league-chart",
            "league-table", "association-chart", "chart-tooltip",
            "clock-coverage-panel", "clock-coverage", "clock-faults",
            "clock-observations",
            "export-panel", "export-audit-button", "export-full-button",
            "export-cancel-button", "export-progress", "export-error",
            "raw-segment-list",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("ALL SYSTEMS GOOD", self.js)
        self.assertIn("Causation is not established", self.js)
        # Header + system-tab download buttons declare their scope so downloadExport
        # picks the right product; a bare data-export-scope="audit" is the browser default.
        self.assertIn('data-export-scope="audit"', self.html)
        self.assertIn('data-export-scope="full"', self.html)
        # Backend banner keys are consumed as-is; do not paper over BREACH.
        for banner in ("all_systems_good", "evidence_not_ready", "latency_breach"):
            self.assertIn(banner, self.js)

    def test_frontend_does_not_silently_swallow_promise_failures(self):
        self.assertNotIn("catch(() => {})", self.js)
        self.assertIn("recordClientError", self.js)
        self.assertIn("socket.onerror", self.js)
        self.assertIn("socket.onclose", self.js)

    def test_large_export_uses_prepare_poll_and_native_download(self):
        # Prepare now carries a scope, and the poll must accept queued in
        # addition to preparing (a full raw job spends real time queued).
        self.assertIn("/api/export/prepare?scope=", self.js)
        self.assertIn("/api/export/jobs/", self.js)
        # Cancel goes through the same /jobs/{id}/cancel path (any id variable name).
        import re
        self.assertRegex(self.js, r"/api/export/jobs/\$\{encodeURIComponent\([^)]+\)\}/cancel")
        self.assertIn('job.status === "queued"', self.js)
        self.assertIn('job.status === "preparing"', self.js)
        self.assertNotIn("await response.blob()", self.js)
        # Per-segment raw download uses a native anchor href, no blob().
        self.assertIn("/api/export/raw/${encodeURIComponent(row.name)}", self.js)

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
        for field in ("query", "strategy", "match", "result", "association", "gate", "period"):
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
        self.assertIn("@media (max-width: 360px)", self.css)
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertNotIn("text-overflow: ellipsis", self.css)

    def test_admin_token_survives_transient_failures(self):
        """Only a 401 may invalidate the operator's admin token.

        The kill switch runs through adminPost; a transient 5xx there must not
        wipe the token and force a re-prompt mid-incident.
        """
        admin_post = self.js.split("async function adminPost", 1)[1]
        admin_post = admin_post.split("\nasync function", 1)[0]
        self.assertIn("response.status === 401", admin_post)
        # The blanket clear in the catch block must be gone.
        catch_block = admin_post.split("} catch", 1)[1]
        self.assertNotIn("removeItem", catch_block)

    def test_raw_segment_list_does_not_require_a_completed_full_export(self):
        """Per-segment downloads are the fallback when the full archive is
        impractical, so the list must be reachable without building one."""
        self.assertIn('safeName === "system"', self.js)
        self.assertIn("refreshRawSegments", self.js)

    def test_clock_observations_are_rendered_not_just_fetched(self):
        self.assertIn("/api/match-clocks", self.js)
        self.assertIn("state.clocks?.observations", self.js)
        self.assertIn("renderClockObservations", self.js)

    def test_trade_card_renders_the_execution_path_not_just_the_peak(self):
        """A scalar peak cannot show whether it was a spike or a plateau."""
        for token in (
            "pathSparkline", "trade.bid_path_summary", "bid-path-svg",
            "summary.ms_at_peak", "summary.peak_exec_px", "summary.path_efficiency",
        ):
            self.assertIn(token, self.js)
        self.assertIn(".bid-path-line", self.css)
        self.assertIn(".bid-path-peak", self.css)

    def test_trade_and_signal_surface_persisted_clock_and_high(self):
        # Persisted clock stamps and executable-bid highs must render without
        # opening raw JSON — trade card and signal card both show them.
        for token in (
            "clockStampBlock", "tradeHighBlock", "lossPath",
            "trade.match_clock", "signal.match_clock",
            "trade.max_executable_bid", "trade.mfe_c", "trade.high_after_entry_s",
            "gateOutcome(signal)",
        ):
            self.assertIn(token, self.js)
        # 88-gate outcomes are shown to the operator on both cards.
        self.assertIn("humanClockGate", self.js)
        # Clock coverage panel gets its data from the status payload.
        self.assertIn("state.status?.clock_coverage", self.js)
        # Per-kind latency query renders every canonical kind, not just non-zero.
        self.assertIn("CANONICAL_LATENCY_KINDS", self.js)


if __name__ == "__main__":
    unittest.main()
