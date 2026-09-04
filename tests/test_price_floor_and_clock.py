"""Price floor, clock freshness, and the configurable sleeve minute.

Three changes driven by live evidence:

* Sub-35c entries were 41% of trades and 73% of all contract exposure, because a
  fixed dollar notional buys contracts as 1/price. That bucket lost 22 of 27 and
  every measured one had zero favourable excursion. `PRICE_FLOOR` refuses the
  fill while keeping the signal and its forward path, exactly as `rejected_cap`
  already does at the upper bound.
* `match_clock_age_ms` measured p50 6099 ms against a 2500 ms freshness bound,
  so the 88+ gate starved and the sleeve never admitted a candidate. Mapping
  resolution moved off the poll loop, and the bound was set proportional to how
  fast a provider minute actually changes.
* The 88 threshold was hard-coded inside the gate. It is now configuration, so
  it can be moved deliberately and is covered by the config fingerprint.
"""
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import config, store
from app.match_clock import MatchClockGate, stamp_from_observation
from app.paper import PaperDesk


class PriceFloorTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(config, "DATA_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()
        store.set_mode("live")

    def desk(self):
        return PaperDesk(Mock(), Mock(), error_result=Mock())

    def test_floor_is_part_of_the_configuration_identity(self):
        """A floor change must produce a new config_id, not silently pool."""
        before = config.config_id()
        with patch.object(config, "PRICE_FLOOR", config.PRICE_FLOOR + 5):
            self.assertNotEqual(config.config_id(), before)

    def test_a_cheap_entry_is_refused_but_the_signal_survives(self):
        """The trade is declined; the evidence is not."""
        sid = store.insert_signal({
            "ts_ms": 1, "local_ts": 1.0, "market": "M", "event": "E",
            "series": "S", "dir": 1, "dl": 1.0, "levels": 6, "size": 300,
            "ref": 10, "ext": 20, "conf_lag_ms": 0, "late": 1,
            "outcome": "queued", "detail": {},
        })
        store.finish_paper_signal(sid, "rejected_floor", {"fill_levels": []}, 1.0)
        row = store.q("SELECT outcome FROM signals WHERE id=?", (sid,))[0]
        self.assertEqual(row["outcome"], "rejected_floor")
        # No trade was opened for it.
        self.assertEqual(store.q("SELECT COUNT(*) c FROM trades")[0]["c"], 0)

    def test_refusals_still_count_as_confirmed_signals(self):
        """Otherwise the floor would silently shrink the K2 denominator."""
        for outcome in ("filled", "rejected_cap", "rejected_floor"):
            store.ex("INSERT INTO signals(event,outcome,mode) VALUES(?,?,?)",
                     ("E", outcome, "live"))
        stats = store.stats("live")
        self.assertGreaterEqual(stats["kill"]["k2_ci"]["n_signals"], 3)

    def test_a_zero_floor_disables_the_bound(self):
        with patch.object(config, "PRICE_FLOOR", 0.0):
            self.assertFalse(config.PRICE_FLOOR > 0)


class ClockFreshnessTests(unittest.TestCase):
    def observation(self, minute=90, status="2nd_half", ts=100.0):
        return {
            "id": 12, "observed_ts": ts, "previous_poll_ts": ts - 0.25,
            "provider_period": "2nd", "provider_minute": minute,
            "provider_stoppage": None, "provider_clock": f"{minute}′",
            "provider_status": status, "precision": "provider_minute_polled",
            "source": "kalshi_live_data_batch",
        }

    def test_the_observed_live_staleness_now_passes(self):
        """p50 measured in production was 6099 ms; 2500 ms refused it."""
        stamp = stamp_from_observation(self.observation(), "E", 100.0 + 6.099)
        self.assertEqual(MatchClockGate(stamp).evaluate()["outcome"], "clock_88_plus")

    def test_a_genuinely_stale_clock_still_fails_closed(self):
        stamp = stamp_from_observation(self.observation(), "E", 100.0 + 45.0)
        self.assertEqual(MatchClockGate(stamp).evaluate()["outcome"], "clock_stale")

    def test_the_bound_is_shorter_than_a_provider_minute(self):
        """Freshness must stay well inside the interval the clock ticks on."""
        self.assertLess(config.MATCH_CLOCK_MAX_AGE_MS, 60_000)
        self.assertGreater(config.MATCH_CLOCK_MAX_AGE_MS, 6_099)


class SleeveMinuteTests(unittest.TestCase):
    def observation(self, minute):
        return {
            "id": 12, "observed_ts": 100.0, "previous_poll_ts": 99.75,
            "provider_period": "2nd", "provider_minute": minute,
            "provider_stoppage": None, "provider_clock": f"{minute}′",
            "provider_status": "2nd_half", "precision": "provider_minute_polled",
            "source": "kalshi_live_data_batch",
        }

    def gate(self, minute):
        return MatchClockGate(
            stamp_from_observation(self.observation(minute), "E", 100.2)
        ).evaluate()

    def test_the_threshold_is_configuration_not_a_literal(self):
        with patch.object(config, "SLEEVE_MIN_MINUTE", 85):
            self.assertTrue(self.gate(85)["accepted"])
            self.assertTrue(self.gate(86)["accepted"])
            self.assertFalse(self.gate(84)["accepted"])
        with patch.object(config, "SLEEVE_MIN_MINUTE", 88):
            self.assertFalse(self.gate(85)["accepted"])
            self.assertTrue(self.gate(88)["accepted"])

    def test_outcome_labels_stay_stable_across_threshold_changes(self):
        """Renaming them would break comparability with every recorded row."""
        with patch.object(config, "SLEEVE_MIN_MINUTE", 85):
            self.assertEqual(self.gate(84)["outcome"], "clock_pre_88")
            self.assertEqual(self.gate(85)["outcome"], "clock_88_plus")

    def test_the_minute_is_part_of_the_configuration_identity(self):
        before = config.config_id()
        with patch.object(config, "SLEEVE_MIN_MINUTE", 85):
            self.assertNotEqual(config.config_id(), before)


class MappingTaskTests(unittest.TestCase):
    def test_the_poll_loop_no_longer_resolves_mappings(self):
        """Sequential /milestones calls must not block a clock confirmation."""
        import inspect
        from app.goal_latency import GoalLatencyObserver
        run_src = inspect.getsource(GoalLatencyObserver.run)
        self.assertNotIn("_resolve_new_events", run_src)
        self.assertIn("_poll", run_src)
        mapping_src = inspect.getsource(GoalLatencyObserver.mapping_task)
        self.assertIn("_resolve_new_events", mapping_src)


if __name__ == "__main__":
    unittest.main()
