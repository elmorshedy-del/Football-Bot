"""PR 13 review §1.5: acceptance through the real dashboard in a real browser.

The shipped `static/index.html`, `static/app.js` and `static/style.css` are
served over HTTP and driven with actual clicks.  Only the API layer is
intercepted, which the review explicitly permits ("intercept or fixture the real
path endpoint"); nothing about the page or its JavaScript is synthesised.

The suite FAILS rather than skips when Chromium is missing and
`REQUIRE_BROWSER_TESTS=1`, so continuous integration cannot go green by
quietly skipping browser acceptance.
"""
import json
import os
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

try:  # pragma: no cover - availability probe
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


_CHROMIUM_CANDIDATES = (
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
)


def chromium_path():
    """Resolve a Chromium binary, or None.

    An explicitly configured CHROMIUM_EXECUTABLE is authoritative: if it is set
    and missing, that is an error, not a reason to quietly fall back to some
    other browser the operator did not ask for.
    """
    configured = os.environ.get("CHROMIUM_EXECUTABLE")
    if configured:
        return configured if os.path.isfile(configured) else None
    for candidate in _CHROMIUM_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    # Playwright's own managed download, used by `playwright install` in CI.
    if sync_playwright is not None:
        try:
            with sync_playwright() as play:
                return play.chromium.executable_path
        except Exception:  # noqa: BLE001 - absence is the answer we want
            return None
    return None


def _browser_unavailable():
    if sync_playwright is None:
        return "playwright is not installed"
    if chromium_path() is None:
        return "chromium is not available"
    return None


def _skip_or_fail():
    """Skip locally, but fail loudly where the browser is mandatory."""
    reason = _browser_unavailable()
    if reason and os.environ.get("REQUIRE_BROWSER_TESTS") == "1":
        raise AssertionError(
            f"browser acceptance is mandatory here but {reason}. "
            "Provision Chromium or unset REQUIRE_BROWSER_TESTS."
        )
    return reason


# --------------------------------------------------------------------------- fixtures

GAPPED_SAMPLES = [
    {"dt_ms": 0.0, "bid": 90.0, "bid_size": 40.0, "exec_px": 90.0, "qty": 10.0,
     "availability": "quote", "terminal": 0, "sample_seq": 1},
    {"dt_ms": 1000.0, "bid": None, "bid_size": None, "exec_px": None, "qty": 10.0,
     "availability": "gap", "terminal": 0, "sample_seq": 2},
    {"dt_ms": 2000.0, "bid": 70.0, "bid_size": 30.0, "exec_px": 70.0, "qty": 10.0,
     "availability": "quote", "terminal": 0, "sample_seq": 3},
    {"dt_ms": 2500.0, "bid": 72.0, "bid_size": None, "exec_px": None, "qty": 10.0,
     "availability": "terminal", "terminal": 1, "sample_seq": 4},
]

PATH_SUMMARY = {
    "samples": 3, "samples_total": 4, "samples_priced": 3, "segments": 2,
    "gap_count": 1, "gap_duration_ms": 1000, "unknown_gap_duration_ms": 0,
    "first_bid": 90.0, "last_bid": 72.0, "peak_bid": 90.0, "peak_dt_ms": 0.0,
    "peak_bid_size": 40.0, "peak_exec_px": 90.0, "ms_at_peak": 1000,
    "trough_bid": 70.0, "trough_dt_ms": 2000.0, "path_travelled_c": 2.0,
    "displacement_c": 18.0, "path_efficiency": None, "span_ms": 2500,
    "truncated": False, "dropped_samples": 0,
}

TRADE = {
    "id": 51, "signal_id": 7, "market": "KXGAME-ARS", "event": "KXGAME",
    "series": "KXGAME", "dir": 1, "side": "yes", "strategy": "price_only_late_score",
    "entry_ts": 1772325600.0, "entry_px": 60.0, "size": 10.0,
    "exit_ts": 1772325660.0, "exit_px": 72.0, "exit_reason": "sleeve_profit_lock",
    "gross": 1.2, "fees": 0.2, "net": 1.0, "mae": 0.4, "status": "closed",
    "mode": "live", "max_executable_bid": 90.0,
    "max_executable_bid_ts": 1772325610.0, "mfe_c": 30.0,
    "high_after_entry_s": 10.0, "bid_path_summary": PATH_SUMMARY,
    "display_game": "Arsenal vs Manchester City",
    "display_leg": "Arsenal", "display_contract": "Arsenal wins",
    "match_clock": {
        "provider_clock": "90+5′", "provider_minute": 90, "provider_stoppage": 5,
        "gate_outcome": "clock_88_plus", "usable_for_88_gate": True,
        "observation_id": 4212, "age_ms": 240.0, "source": "kalshi_live_data_batch",
    },
    "trigger": {"strategy": "price_only_late_score", "outcome": "filled",
                "observed": {}, "thresholds": {}, "price_only_inference": {}},
    "timing": {"entry_ts": 1772325600.0, "exit_ts": 1772325660.0},
}

LOSS_TRADE = dict(
    TRADE, id=52, net=-0.8, gross=-0.6, exit_reason="sleeve_scratch",
    display_game="Chelsea vs Liverpool", display_leg="Chelsea",
    display_contract="Chelsea wins", event="KXGAME2",
)

API_FIXTURES = {
    "/api/status": {
        "mode": "live", "health": {"ok": True, "runtime_ok": True,
                                   "banner": "all_good",
                                   "banner_text": "ALL SYSTEMS GOOD", "checks": {}},
        "started": 1772325000.0, "ws_state": "connected",
    },
    "/api/config": {"sleeve_start_before_expiry_min": 2,
                    "sleeve_after_expiry_min": 12},
    "/api/matches": [],
    "/api/trades": {"open": [], "closed": [TRADE, LOSS_TRADE]},
    "/api/signals": [],
    "/api/stats": {"closed": 2, "open": 0, "gross": 0.6, "fees": 0.4, "net": 0.2,
                   "win_pct": 50.0, "exit_reasons": {}, "signals": {},
                   "evidence": {}, "ci95": None},
    "/api/goal-latency": [],
    "/api/latency": {},
    "/api/equity": {"combined": [[1772325660000, 1.0]], "gate_a": [],
                    "price_only_late_score": [[1772325660000, 1.0]]},
    "/api/eventlog": [],
    "/api/match-clocks": {"coverage": {}, "observations": []},
    "/api/provider-events": [],
}


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):  # keep test output clean
        pass


class DashboardBrowserTests(unittest.TestCase):
    """Real page, real JavaScript, real clicks."""

    @classmethod
    def setUpClass(cls):
        reason = _skip_or_fail()
        if reason:
            raise unittest.SkipTest(reason)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_Handler, directory=str(STATIC)))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls._play = sync_playwright().start()
        cls.browser = cls._play.chromium.launch(executable_path=chromium_path())

    @classmethod
    def tearDownClass(cls):
        # Contexts first, then the browser, then the driver: closing out of
        # order leaves in-flight protocol calls with nothing to resolve against.
        for context in list(cls.browser.contexts):
            context.close()
        cls.browser.close()
        cls._play.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def open_dashboard(self, viewport=None, path_response="ok"):
        """Load the shipped dashboard with the API layer intercepted."""
        page = self.browser.new_page(viewport=viewport or {"width": 1280, "height": 900})

        def _teardown():
            # Drop route handlers BEFORE closing. A handler still registered
            # when the browser goes away leaves an unresolved continuation,
            # which surfaces as an unretrieved TargetClosedError future during
            # interpreter teardown -- noise that would mask a real leak.
            try:
                page.unroute_all(behavior="ignoreErrors")
            except Exception:  # noqa: BLE001 - teardown must not mask failures
                pass
            page.close()

        self.addCleanup(_teardown)
        self.console_errors = []
        page.on("pageerror", lambda exc: self.console_errors.append(str(exc)))

        def handle(route, request):
            url = request.url.split("?")[0]
            endpoint = url.split(f":{self.port}")[-1]
            if "/path" in endpoint:
                if path_response == "fail":
                    route.fulfill(status=500, content_type="application/json",
                                  body=json.dumps({"detail": "path unavailable"}))
                    return
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "trade_id": TRADE["id"], "mode": "live",
                    "samples": GAPPED_SAMPLES, "summary": PATH_SUMMARY,
                    "truncated": False,
                }))
                return
            payload = API_FIXTURES.get(endpoint)
            if payload is None:
                route.fulfill(status=200, content_type="application/json", body="{}")
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload))

        page.route("**/api/**", handle)
        page.goto(f"http://127.0.0.1:{self.port}/index.html")
        # The trades panel is hidden until its tab is selected, so wait for the
        # cards to be attached (hydrated) rather than visible.
        page.wait_for_selector("#trade-list .trade-story", state="attached",
                               timeout=20000)
        return page

    def show_trades_tab(self, page):
        page.click('[data-tab="trades"]')
        page.wait_for_selector("#panel-trades:not([hidden])", timeout=5000)

    # ------------------------------------------------------------------ click

    def test_clicking_show_path_loads_and_renders_a_visible_chart(self):
        page = self.open_dashboard()
        self.show_trades_tab(page)

        button = page.wait_for_selector("[data-load-path]", timeout=10000)
        self.assertIn("Show path", button.inner_text())

        with page.expect_request(lambda r: "/path" in r.url, timeout=10000):
            button.click()

        chart = page.wait_for_selector("#trade-list svg.bid-path-svg", timeout=10000)
        self.assertTrue(chart.is_visible(), "the chart rendered but is not visible")
        self.assertEqual(self.console_errors, [], "the click raised a page error")

    def test_a_failed_path_request_leaves_a_visible_error(self):
        page = self.open_dashboard(path_response="fail")
        self.show_trades_tab(page)

        button = page.wait_for_selector("[data-load-path]", timeout=10000)
        with page.expect_request(lambda r: "/path" in r.url, timeout=10000):
            button.click()

        error = page.wait_for_selector(".path-error", timeout=10000)
        self.assertTrue(error.is_visible(), "a failed fetch disappeared silently")
        self.assertIn("Could not load the path", error.inner_text())
        self.assertIsNone(
            page.query_selector("#trade-list svg.bid-path-svg"),
            "a chart was drawn from a failed request",
        )

    def test_a_gapped_path_draws_multiple_subpaths_and_no_cross_gap_line(self):
        page = self.open_dashboard()
        self.show_trades_tab(page)
        button = page.wait_for_selector("[data-load-path]", timeout=10000)
        with page.expect_request(lambda r: "/path" in r.url, timeout=10000):
            button.click()
        page.wait_for_selector("#trade-list svg.bid-path-svg", timeout=10000)

        d = page.get_attribute("#trade-list path.bid-path-line", "d")
        self.assertIsNotNone(d)
        self.assertGreaterEqual(
            d.count("M"), 2,
            f"a gapped path must start a new subpath at the outage; d={d!r}",
        )
        first_segment = d.split(" M")[0]
        self.assertNotIn(
            "L", first_segment,
            f"the pre-gap point was joined across the outage; d={d!r}",
        )

    # -------------------------------------------------------------- responsive

    def test_mobile_360_has_no_horizontal_overflow_on_any_tab(self):
        page = self.open_dashboard(viewport={"width": 360, "height": 780})
        for tab in ("overview", "trades", "signals", "leagues", "markets", "system"):
            with self.subTest(tab=tab):
                page.click(f'[data-tab="{tab}"]')
                page.wait_for_selector(f"#panel-{tab}:not([hidden])", timeout=5000)
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                self.assertLessEqual(
                    overflow, 1,
                    f"the {tab} tab overflows the 360px viewport by {overflow}px",
                )

    def test_mobile_360_shows_required_trade_fields_without_truncation(self):
        page = self.open_dashboard(viewport={"width": 360, "height": 780})
        self.show_trades_tab(page)
        card = page.wait_for_selector("#trade-list .trade-story", timeout=10000)
        text = card.inner_text()

        for required in ("2026", "90+5", "Arsenal"):
            with self.subTest(required=required):
                self.assertIn(required, text,
                              f"{required!r} is missing from the 360px trade card")
        self.assertNotIn("…", text, "a required field was ellipsized at 360px")

        truncated = page.evaluate(
            """() => [...document.querySelectorAll('#trade-list .trade-story *')]
                 .filter(el => el.children.length === 0
                            && el.scrollWidth > el.clientWidth + 1).length"""
        )
        self.assertEqual(truncated, 0, "a leaf field is clipped at 360px")

    # ------------------------------------------------------------------ flows

    def test_filters_league_view_and_download_control_respond(self):
        page = self.open_dashboard()
        self.show_trades_tab(page)
        self.assertEqual(
            len(page.query_selector_all("#trade-list .trade-story")), 2,
            "fixture must render both trades before filtering",
        )

        page.fill('#trade-filters [data-filter-field="query"]', "Arsenal")
        page.wait_for_function(
            "() => document.querySelectorAll('#trade-list .trade-story').length === 1",
            timeout=5000,
        )

        page.click('[data-tab="leagues"]')
        page.wait_for_selector("#panel-leagues:not([hidden])", timeout=5000)
        self.assertTrue(page.query_selector("#league-table"))

        export = page.wait_for_selector("#export-button", timeout=5000)
        self.assertTrue(export.is_visible(), "the download control is not reachable")
        self.assertEqual(self.console_errors, [])


if __name__ == "__main__":
    unittest.main()
