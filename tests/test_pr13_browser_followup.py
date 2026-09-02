"""Independent PR #13 follow-up: real-browser acceptance for full operator flows."""
import copy
import json
from urllib.parse import parse_qs, urlsplit

from test_dashboard_browser import (
    API_FIXTURES,
    GAPPED_SAMPLES,
    LOSS_TRADE,
    PATH_SUMMARY,
    TRADE,
    DashboardBrowserTests,
)


GOOD_CHECKS = {
    "websocket": {"healthy": True, "status": "connected"},
    "recorder": {"healthy": True, "status": "recording"},
    "match_event_feed": {"healthy": True, "status": "observing"},
    "paper_execution": {"healthy": True, "status": "healthy"},
    "database": {"healthy": True, "status": "healthy"},
    "credentials": {"healthy": True, "status": "configured"},
}


def health_status(banner="all_systems_good", checks=None, recent_errors=None):
    title = {
        "all_systems_good": "ALL SYSTEMS GOOD",
        "evidence_not_ready": "Runtime healthy · paper evidence not ready",
        "attention_required": "ATTENTION REQUIRED",
    }[banner]
    return {
        "mode": "live",
        "started": 1772325000.0,
        "ws_state": "connected",
        "matches": 2,
        "recorded": 321,
        "health": {
            "ok": banner != "attention_required",
            "runtime_ok": banner != "attention_required",
            "banner": banner,
            "banner_text": title,
            "checks": copy.deepcopy(checks or GOOD_CHECKS),
            "recent_errors": copy.deepcopy(recent_errors or []),
        },
    }


def fixtures():
    data = copy.deepcopy(API_FIXTURES)
    winner = copy.deepcopy(TRADE)
    winner["matched_event"] = {
        "association": "state_consistent",
        "delta_s": 0.8,
        "canonical_event": {
            "human_label": "Arsenal goal",
            "provider_description": "Arsenal scored",
            "provider_clock": "90+5′",
        },
    }
    winner["trigger"] = {
        "strategy": "price_only_late_score",
        "outcome": "filled",
        "observed": {"log_odds_displacement": 1.5, "distinct_price_levels": 5, "contracts": 4},
        "thresholds": {"min_log_odds_displacement": 1.0, "min_distinct_price_levels": 3, "min_contracts": 3},
        "price_only_inference": {"inferred_state": "one_goal_lead"},
    }

    loser = copy.deepcopy(LOSS_TRADE)
    loser["strategy"] = "gate_a"
    loser["matched_event"] = {"association": "unmatched"}
    loser["match_clock"] = {
        "provider_clock": "84′", "provider_minute": 84, "provider_stoppage": 0,
        "gate_outcome": "clock_pre_88", "usable_for_88_gate": False,
        "unusable_reason": "pre_88", "observation_id": 4213, "age_ms": 180.0,
        "source": "kalshi_live_data_batch",
    }
    loser["trigger"] = {"strategy": "gate_a", "outcome": "confirmed", "observed": {}, "thresholds": {}}

    signal_live = {
        "id": 7, "local_ts": 1772325590.0, "market": "KXGAME-ARS", "event": "KXGAME",
        "series": "KXGAME", "dir": 1, "strategy": "price_only_late_score", "outcome": "filled",
        "display_game": "Arsenal vs Manchester City", "display_leg": "Arsenal",
        "display_contract": "Arsenal wins", "mode": "live",
        "match_clock": copy.deepcopy(winner["match_clock"]),
        "trigger": copy.deepcopy(winner["trigger"]),
        "matched_event": copy.deepcopy(winner["matched_event"]),
        "timing": {"signal_ts": 1772325590.0, "entry_ts": 1772325600.0},
        "schedule_window": {},
    }
    signal_declined = {
        "id": 8, "local_ts": 1772325500.0, "market": "KXGAME2-CHE", "event": "KXGAME2",
        "series": "KXGAME", "dir": 1, "strategy": "gate_a", "outcome": "sleeve_clock_pre_88",
        "display_game": "Chelsea vs Liverpool", "display_leg": "Chelsea",
        "display_contract": "Chelsea wins", "mode": "live",
        "match_clock": copy.deepcopy(loser["match_clock"]),
        "trigger": {"strategy": "gate_a", "outcome": "sleeve_clock_pre_88", "observed": {}, "thresholds": {}},
        "matched_event": {"association": "unmatched"}, "timing": {}, "schedule_window": {},
    }

    data["/api/status"] = health_status(
        recent_errors=[{
            "ts": 1772325400.0, "component": "recorder",
            "message": "fixture recorder recovered after a write retry",
        }]
    )
    data["/api/config"] = {
        "sleeve_start_before_expiry_min": 2, "sleeve_after_expiry_min": 12,
        "league_names": {"KXGAME": "Premier League"},
    }
    data["/api/trades"] = {"open": [], "closed": [winner, loser]}
    data["/api/signals"] = [signal_live, signal_declined]
    data["/api/stats"] = {
        "closed": 2, "open": 0, "gross": 0.6, "fees": 0.4, "net": 0.2,
        "win_pct": 50.0, "exit_reasons": {}, "signals": {}, "evidence": {}, "ci95": None,
        "leagues": {
            "KXGAME": {
                "display_name": "Premier League", "n": 2, "net": 0.2, "gross": 0.6,
                "fees": 0.4, "win_pct": 50.0, "net_per_trade": 0.1,
                "sleeves": {
                    "gate_a": {"n": 1, "net": -0.8, "gross": -0.6, "fees": 0.2, "win_pct": 0.0, "net_per_trade": -0.8},
                    "price_only_late_score": {"n": 1, "net": 1.0, "gross": 1.2, "fees": 0.2, "win_pct": 100.0, "net_per_trade": 1.0},
                },
            }
        },
    }
    data["/api/latency"] = {
        "order_arrival_ms": {"kind": "order_arrival_ms", "state": "READY", "n": 30, "p50": 80.0, "p95": 120.0, "max": 145.0, "age_s": 2.0, "threshold_ms": 250.0},
        "feed_ingress_ms": {"kind": "feed_ingress_ms", "state": "READY", "n": 30, "p50": 12.0, "p95": 24.0, "max": 31.0, "age_s": 1.0, "threshold_ms": 100.0},
    }
    data["/api/eventlog"] = [{"ts": 1772325400.0, "kind": "paper", "msg": "Recovered recorder write"}]
    return data


class PR13BrowserFollowupTests(DashboardBrowserTests):
    """The stricter browser checks requested by the independent follow-up."""

    # Do not duplicate the base class's seven tests in this subclass.
    test_clicking_show_path_loads_and_renders_a_visible_chart = None
    test_a_failed_path_request_leaves_a_visible_error = None
    test_a_gapped_path_draws_multiple_subpaths_and_no_cross_gap_line = None
    test_terminal_is_time_only_and_chart_legend_explains_markers = None
    test_mobile_360_has_no_horizontal_overflow_on_any_tab = None
    test_mobile_360_shows_required_trade_fields_without_truncation = None
    test_filters_league_view_and_download_control_respond = None

    def open_dashboard(self, viewport=None, status_sequence=None, export_behavior="ready"):
        page = self.browser.new_page(viewport=viewport or {"width": 1280, "height": 900})

        def teardown():
            try:
                page.unroute_all(behavior="ignoreErrors")
            except Exception:
                pass
            page.close()

        self.addCleanup(teardown)
        self.console_errors = []
        self.api_requests = []
        page.on("pageerror", lambda exc: self.console_errors.append(str(exc)))
        page.add_init_script("""
            class StableWebSocket {
              constructor() {
                this.readyState = 0;
                setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen({}); }, 0);
              }
              send() {}
              close() { this.readyState = 3; }
            }
            StableWebSocket.CONNECTING = 0; StableWebSocket.OPEN = 1;
            StableWebSocket.CLOSING = 2; StableWebSocket.CLOSED = 3;
            window.WebSocket = StableWebSocket;
        """)

        data = fixtures()
        statuses = list(status_sequence or [data["/api/status"]])
        status_calls = {"n": 0}
        export_polls = {}

        def job(scope, status, processed=0):
            return {
                "job_id": f"{scope}-job", "scope": scope, "status": status,
                "bytes": 4096 if status == "ready" else None,
                "processed_bytes": processed, "total_bytes": 4096,
                "processed_segments": 1 if processed else 0, "total_segments": 2,
            }

        def handle(route, request):
            parsed = urlsplit(request.url)
            endpoint = parsed.path
            self.api_requests.append((request.method, endpoint, parsed.query))

            if "/path" in endpoint:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "trade_id": TRADE["id"], "mode": "live", "samples": GAPPED_SAMPLES,
                    "summary": PATH_SUMMARY, "truncated": False,
                }))
                return

            if endpoint == "/api/status":
                index = min(status_calls["n"], len(statuses) - 1)
                status_calls["n"] += 1
                route.fulfill(status=200, content_type="application/json", body=json.dumps(statuses[index]))
                return

            if endpoint == "/api/export/prepare":
                scope = parse_qs(parsed.query).get("scope", ["audit"])[0]
                if export_behavior == "error":
                    route.fulfill(status=500, content_type="application/json",
                                  body=json.dumps({"detail": "snapshot failed in browser fixture"}))
                    return
                start_status = "queued" if export_behavior in {"progress", "cancel"} else "ready"
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(job(scope, start_status)))
                return

            if endpoint.startswith("/api/export/jobs/") and endpoint.endswith("/cancel"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"status": "cancelled"}))
                return

            if endpoint.startswith("/api/export/jobs/") and endpoint.endswith("/download"):
                route.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "application/zip",
                        "Content-Disposition": "attachment; filename=football-study.zip",
                    },
                    body="PK-browser-fixture",
                )
                return

            if endpoint.startswith("/api/export/jobs/"):
                scope = endpoint.split("/")[4].replace("-job", "")
                count = export_polls.get(scope, 0) + 1
                export_polls[scope] = count
                status = "preparing" if export_behavior == "progress" and count == 1 else "ready"
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(job(scope, status, 2048 if status == "preparing" else 4096)))
                return

            if endpoint == "/api/export/raw":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"segments": []}))
                return

            payload = data.get(endpoint, {})
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        page.route("**/api/**", handle)
        page.goto(f"http://127.0.0.1:{self.port}/index.html")
        page.wait_for_selector("#trade-list .trade-story", state="attached", timeout=20000)
        page.evaluate("sessionStorage.setItem('footballbot_admin_token', 'browser-test-token')")
        return page

    @staticmethod
    def trade_count(page):
        return len(page.query_selector_all("#trade-list .trade-story"))

    def reset_filters(self, page):
        page.click('#trade-filters [data-reset-filters]')
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 2")

    def test_all_trade_filters_and_reset_drive_real_rendering(self):
        page = self.open_dashboard()
        self.show_trades_tab(page)
        self.assertEqual(self.trade_count(page), 2)

        page.fill('#trade-filters [data-filter-field="query"]', "Arsenal")
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 1")
        self.reset_filters(page)

        page.select_option('#trade-filters [data-filter-field="strategy"]', "gate_a")
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 1")
        self.assertIn("Chelsea", page.inner_text("#trade-list"))
        self.reset_filters(page)

        page.select_option('#trade-filters [data-filter-field="match"]', "Chelsea vs Liverpool")
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 1")
        self.reset_filters(page)

        page.select_option('#trade-filters [data-filter-field="result"]', "loss")
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 1")
        self.reset_filters(page)

        page.select_option('#trade-filters [data-filter-field="gate"]', "declined")
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 1")
        self.assertIn("Clock before minute 88", page.inner_text("#trade-list"))
        self.reset_filters(page)

        page.select_option('#trade-filters [data-filter-field="association"]', "unmatched")
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 1")
        self.reset_filters(page)

        page.select_option('#trade-filters [data-filter-field="period"]', "1")
        page.wait_for_function("() => document.querySelectorAll('#trade-list .trade-story').length === 0")
        self.reset_filters(page)
        self.assertEqual(self.console_errors, [])

    def test_health_waiting_fault_and_recovery_are_all_visible(self):
        waiting = health_status("evidence_not_ready")
        failed_checks = copy.deepcopy(GOOD_CHECKS)
        failed_checks["recorder"] = {"healthy": False, "status": "write failed"}
        fault = health_status(
            "attention_required", failed_checks,
            [{"ts": 1772325600.0, "component": "recorder", "message": "recorder write failed"}],
        )
        healthy = health_status("all_systems_good")
        page = self.open_dashboard(status_sequence=[waiting, fault, healthy])

        page.wait_for_function("() => document.querySelector('#health-title').textContent.includes('paper evidence not ready')")
        self.assertIn("Collecting evidence", page.inner_text("#health-indicator"))

        page.evaluate("refreshAll()")
        page.wait_for_function("() => document.querySelector('#health-title').textContent === 'ATTENTION REQUIRED'")
        self.assertIn("recorder write failed", page.inner_text("#error-list"))

        page.evaluate("refreshAll()")
        page.wait_for_function("() => document.querySelector('#health-title').textContent === 'ALL SYSTEMS GOOD'")
        self.assertIn("Healthy", page.inner_text("#health-indicator"))
        self.assertEqual(self.console_errors, [])

    def test_export_progress_native_download_cancel_and_visible_error(self):
        page = self.open_dashboard(export_behavior="progress")
        page.click('[data-tab="system"]')
        page.wait_for_selector("#panel-system:not([hidden])")

        with page.expect_download(timeout=12000) as download_info:
            page.click("#export-full-button")
            page.wait_for_function("() => document.querySelector('#export-progress').textContent.includes('Queued')")
            page.wait_for_function("() => document.querySelector('#export-progress').textContent.includes('Preparing')", timeout=7000)
        download = download_info.value
        self.assertTrue(download.suggested_filename.endswith(".zip"))
        self.assertTrue(any(path.endswith("/full-job/download") for _, path, _ in self.api_requests))

        # All-mode archive is a distinct, operator-visible product and uses a native download.
        page2 = self.open_dashboard(export_behavior="ready")
        page2.click('[data-tab="system"]')
        with page2.expect_download(timeout=7000):
            page2.click("#export-archive-button")
        self.assertTrue(any(method == "POST" and path == "/api/export/prepare" and "scope=archive" in query
                            for method, path, query in self.api_requests))

        page3 = self.open_dashboard(export_behavior="cancel")
        page3.click('[data-tab="system"]')
        page3.click("#export-full-button")
        page3.wait_for_selector("#export-cancel-button:not([hidden])", timeout=5000)
        page3.click("#export-cancel-button")
        page3.wait_for_function("() => !document.querySelector('#export-cancel-button') || document.querySelector('#export-cancel-button').hidden", timeout=7000)
        self.assertTrue(any(method == "POST" and path.endswith("/full-job/cancel")
                            for method, path, _ in self.api_requests))

        page4 = self.open_dashboard(export_behavior="error")
        page4.click('[data-tab="system"]')
        page4.click("#export-audit-button")
        error = page4.wait_for_selector("#export-error:not([hidden])", timeout=5000)
        self.assertIn("snapshot failed", error.inner_text())

    def test_mobile_360_exposes_trade_signal_league_latency_and_errors_without_clipping(self):
        page = self.open_dashboard(viewport={"width": 360, "height": 780})
        expectations = {
            "trades": ("Trailing profit lock", "90+5", "After entry", "10 seconds", "Arsenal"),
            "signals": ("Paper order filled", "State-consistent match event", "88+ clock accepted", "Arsenal"),
            "leagues": ("Premier League", "+$0.20", "2 trades"),
            "system": ("Order arrival (K4)", "80.0", "120.0"),
        }
        for tab, required in expectations.items():
            page.click(f'[data-tab="{tab}"]')
            panel = page.wait_for_selector(f"#panel-{tab}:not([hidden])", timeout=5000)
            text = panel.inner_text()
            for value in required:
                self.assertIn(value, text, f"{value!r} missing from 360px {tab} tab")
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            self.assertLessEqual(overflow, 1, f"{tab} overflows 360px by {overflow}px")

        page.click("#error-details summary")
        self.assertIn("fixture recorder recovered", page.inner_text("#error-list"))
        clipped = page.evaluate("""() => [...document.querySelectorAll('#panel-system *')]
            .filter(el => el.children.length === 0 && el.clientWidth > 0
                       && el.scrollWidth > el.clientWidth + 1).length""")
        self.assertEqual(clipped, 0, "a required 360px system field is clipped")
        self.assertEqual(self.console_errors, [])
