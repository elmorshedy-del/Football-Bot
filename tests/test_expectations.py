"""Every measured number beside the bound something already declares for it.

The operator's question -- "is latency, and everything else I expect a number
for, following what was intended or exceeding it?" -- could not be answered from
the dashboard.  The latency table prints p50/p95 and a `Threshold` column that
is populated for exactly one kind (`order_arrival_ms`, from kill condition K4);
every other row showed a number with nothing to judge it against.

The fix is a reporting layer, and these tests pin the two things that make it
trustworthy: a bound appears only where something already declares one, and the
layer stays out of the trading path.  Attaching the extra thresholds to
`latency_kind_summary` instead would have been shorter and wrong -- `state` is
what K4 reads in `Engine.status`, so a reporting bound would silently have
become one that can stop the bot trading.
"""
import tempfile
import unittest
from unittest.mock import patch

from app import config
from app import store


def summary(kind, **fields):
    base = {"kind": kind, "n": 100, "p50": None, "p95": None, "max": None,
            "invalid": 0, "latest_ts": 1.0, "age_s": 1.0,
            "threshold_ms": None, "state": "PASS"}
    base.update(fields)
    return base


class ExpectationBoundsTests(unittest.TestCase):
    """Where each bound comes from, and what happens where there is none."""

    def rows(self, readiness=None, counters=None):
        return {row["key"]: row for row in
                store.expectations(readiness=readiness or {},
                                   counters=counters or {})["rows"]}

    def test_the_order_arrival_bound_is_the_kill_condition(self):
        row = self.rows()["order_arrival_ms"]
        self.assertEqual(row["bound"], store.K4_THRESHOLD_MS)
        self.assertEqual(row["statistic"], "p95")
        self.assertIn("K4", row["source"])

    def test_declared_bounds_follow_the_running_configuration(self):
        """A bound is read from the knob, not copied from it."""
        with patch.object(config, "MATCH_CLOCK_MAX_AGE_MS", 4321.0):
            self.assertEqual(self.rows()["match_clock_age_ms"]["bound"], 4321.0)
        with patch.object(config, "PAPER_ENTRY_LATENCY_MS", 90.0):
            self.assertEqual(self.rows()["paper_entry_ms"]["bound"], 90.0)
        with patch.object(config, "GOAL_LATENCY_POLL_MS", 500.0):
            self.assertEqual(self.rows()["match_response_ms"]["bound"], 500.0)
        with patch.object(config, "WS_QUEUE_STALL_DEPTH", 0), \
                patch.object(config, "WS_QUEUE_MAX", 20000):
            self.assertEqual(self.rows()["backlog_frames"]["bound"], 10000)

    def test_a_number_with_no_declared_bound_says_so(self):
        """The alternative is inventing one, which would read as design intent."""
        for key in ("feed_ingress_ms", "decision_ms", "paper_exit_ms",
                    "scheduler_lag_ms"):
            row = self.rows({key: summary(key, p50=5.0, p95=9.0)})[key]
            self.assertIsNone(row["bound"], key)
            self.assertEqual(row["verdict"], "UNBOUNDED", key)
            self.assertEqual(row["source"], "No declared bound", key)

    def test_the_unbounded_numbers_are_listed_so_the_gap_is_visible(self):
        report = store.expectations(readiness={}, counters={})
        self.assertEqual(
            sorted(report["unbounded"]),
            ["decision_ms", "feed_ingress_ms", "paper_exit_ms", "scheduler_lag_ms"])


class ExpectationVerdictTests(unittest.TestCase):
    def verdict(self, kind, **fields):
        report = store.expectations(readiness={kind: summary(kind, **fields)},
                                    counters={})
        return next(row for row in report["rows"] if row["key"] == kind)

    def test_a_measurement_inside_its_bound_is_within(self):
        row = self.verdict("order_arrival_ms", p95=120.0)
        self.assertEqual(row["verdict"], "WITHIN")
        self.assertEqual(row["observed"], 120.0)

    def test_a_measurement_past_its_bound_is_exceeding(self):
        row = self.verdict("order_arrival_ms", p95=2_895_826.0)
        self.assertEqual(row["verdict"], "EXCEEDING")
        report = store.expectations(
            readiness={"order_arrival_ms":
                       summary("order_arrival_ms", p95=2_895_826.0)},
            counters={})
        self.assertIn("order_arrival_ms", report["exceeding"])

    def test_a_stale_or_collecting_measurement_never_reads_as_passing(self):
        """The 2026-09-05 stall left `order_arrival_ms` STALE at p95 of 38
        minutes.  Reporting that as WITHIN because the last sample predates the
        window would be worse than reporting nothing."""
        self.assertEqual(self.verdict("order_arrival_ms", p95=10.0,
                                      state="STALE")["verdict"], "STALE")
        self.assertEqual(self.verdict("order_arrival_ms", p95=10.0, n=3,
                                      state="COLLECTING")["verdict"], "NO DATA")
        self.assertEqual(self.verdict("order_arrival_ms", p95=None,
                                      state="INVALID")["verdict"], "NO DATA")

    def test_a_missing_kind_is_no_data_not_a_pass(self):
        row = next(r for r in store.expectations(readiness={}, counters={})["rows"]
                   if r["key"] == "order_arrival_ms")
        self.assertEqual(row["verdict"], "NO DATA")
        self.assertIsNone(row["observed"])


class IntegrityCounterTests(unittest.TestCase):
    """Counts whose intended value is zero, judged the same way."""

    def rows(self, **counters):
        return {row["key"]: row for row in
                store.expectations(readiness={}, counters=counters)["rows"]}

    def test_zero_drops_and_zero_failures_are_within(self):
        rows = self.rows(queue_dropped_total=0, feed_event_failures=0,
                         archive_failures=0, recorder_failures=0)
        for key in ("queue_dropped_total", "feed_event_failures",
                    "archive_failures", "recorder_failures"):
            self.assertEqual(rows[key]["verdict"], "WITHIN", key)
            self.assertEqual(rows[key]["bound"], 0, key)

    def test_any_dropped_frame_is_exceeding(self):
        """A dropped frame is market data this process never saw; there is no
        acceptable non-zero level of it, only a bounded one."""
        rows = self.rows(queue_dropped_total=1)
        self.assertEqual(rows["queue_dropped_total"]["verdict"], "EXCEEDING")

    def test_an_unavailable_counter_is_no_data_not_zero(self):
        rows = self.rows()
        self.assertEqual(rows["archive_failures"]["verdict"], "NO DATA")
        self.assertIsNone(rows["archive_failures"]["observed"])


class ReportingStaysOutOfTheTradingPathTests(unittest.TestCase):
    """The invariant that makes this safe to add at all."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("app.store.config.DATA_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def test_only_order_arrival_carries_a_threshold_in_the_readiness_summary(self):
        """K4 reads `latency_kind_summary(...)['state']`.  If the reporting
        bounds were attached there, a slow score feed or a stale match clock
        would raise BREACH and gate the bot's evidence, which no kill condition
        ever said it should."""
        for kind in store.LATENCY_KINDS:
            got = store.latency_kind_summary(kind, limit=1)["threshold_ms"]
            if kind == "order_arrival_ms":
                self.assertEqual(got, store.K4_THRESHOLD_MS)
            else:
                self.assertIsNone(got, kind)

    def test_expectations_are_derived_and_never_write(self):
        calls = []
        with patch.object(store, "ex", side_effect=lambda *a, **k: calls.append(a)):
            store.expectations(readiness={}, counters={"queue_dropped_total": 5})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
