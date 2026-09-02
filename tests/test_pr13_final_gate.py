"""Small final guard for the independent PR #13 follow-up handoff."""
from pathlib import Path
import unittest


class PR13FinalIntegrationGate(unittest.TestCase):
    def test_no_temporary_fix_workflows_or_stale_finalizer_names_remain(self):
        self.assertFalse(Path("tools/fix_pr13_signal_test_hook.py").exists())
        self.assertFalse(Path(".github/workflows/fix-pr13-signal-test-hook.yml").exists())
        signal_tests = Path("tests/test_signal_path_ownership.py").read_text()
        self.assertNotIn("finalize_signal_path_with_rows", signal_tests)
        self.assertIn("finalize_signal_path", signal_tests)

    def test_terminal_marker_uses_a_defined_dashboard_color(self):
        css = Path("static/style.css").read_text()
        self.assertIn(".bid-path-terminal", css)
        self.assertIn("var(--amber)", css)
        self.assertNotIn("var(--yellow)", css)

    def test_followup_browser_suite_covers_operator_flows(self):
        browser_tests = Path("tests/test_pr13_browser_followup.py").read_text()
        for required in (
            "test_all_trade_filters_and_reset_drive_real_rendering",
            "test_health_waiting_fault_and_recovery_are_all_visible",
            "test_export_progress_native_download_cancel_and_visible_error",
            "test_mobile_360_exposes_trade_signal_league_latency_and_errors_without_clipping",
        ):
            self.assertIn(required, browser_tests)


if __name__ == "__main__":
    unittest.main()
