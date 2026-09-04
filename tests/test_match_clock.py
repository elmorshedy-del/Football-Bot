import json
import tempfile
import unittest
from unittest.mock import patch

from app.match_clock import (
    MatchClockGate,
    MatchClockTracker,
    evaluate_clock_gate,
    normalize_status,
    parse_current_clock,
    parse_clock_text,
    parse_stored_stamp,
    stamp_from_observation,
    unusable_stamp,
)
from app import config, store


class ClockParserTests(unittest.TestCase):
    def test_table_driven_clock_text(self):
        cases = [
            ("87'", 87, None, "87′"),
            ("88'", 88, None, "88′"),
            ("90'", 90, None, "90′"),
            ("90+1'", 90, 1, "90+1′"),
            ("90+12’", 90, 12, "90+12′"),
            ("90+5′", 90, 5, "90+5′"),
            (88, 88, None, "88′"),
            (90.0, 90, None, "90′"),
            ("  90 + 5 ' ", 90, 5, "90+5′"),
            ("not a clock", None, None, None),
            ("", None, None, None),
            (None, None, None, None),
            (True, None, None, None),
            (4512, None, None, None),
            (-1, None, None, None),
        ]
        for value, minute, stoppage, rendered in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_clock_text(value), (minute, stoppage, rendered))

    def test_current_clock_uses_details_time_first(self):
        parsed = parse_current_clock({
            "time": "90+5'",
            "match_clock": "88'",
            "status": "live",
            "half": "2nd",
            "last_play": {"time": "48'", "description": "old goal"},
            "home_significant_events": [{"time": "48'", "event_type": "score_change"}],
        })
        self.assertEqual(parsed.provider_minute, 90)
        self.assertEqual(parsed.provider_stoppage, 5)
        self.assertEqual(parsed.provider_clock, "90+5′")
        self.assertEqual(parsed.provider_period, "2nd")
        self.assertEqual(parsed.provider_status, "live")
        self.assertEqual(parsed.source_field, "time")

    def test_current_clock_never_comes_from_last_play_or_significant_events(self):
        parsed = parse_current_clock({
            "last_play": {"time": "90+5'", "description": "penalty"},
            "home_significant_events": [{"time": "90+5'", "event_type": "score_change"}],
            "away_significant_events": [{"time": "48'"}],
        })
        self.assertIsNone(parsed.provider_minute)
        self.assertIsNone(parsed.provider_clock)

    def test_status_text_clock_and_malformed_numeric_clock_are_ignored(self):
        parsed = parse_current_clock({
            "clock": 4512,
            "status_text": "90+3' 2nd half live",
        })
        self.assertEqual(parsed.provider_minute, 90)
        self.assertEqual(parsed.provider_stoppage, 3)
        self.assertEqual(parsed.source_field, "status_text")
        self.assertEqual(parsed.provider_period, "2nd")
        self.assertEqual(parsed.provider_status, "live")


    def test_clock_is_not_read_from_leading_digits_in_prose(self):
        """A period ordinal or score must never be read as the match minute.

        Regression: the parser previously took the first integer anywhere in the
        string, so "2nd Half 90+5'" parsed as minute 2 and "1-0 90+5'" as minute
        1.  Both declined a genuine 88+ clock as clock_pre_88 while persisting a
        confident-looking wrong stamp.
        """
        for text, minute, stoppage in (
            ("90+3' 2nd half live", 90, 3),
            ("2nd half 90+3' live", 90, 3),
            ("2nd Half 90+5'", 90, 5),
            ("1-0 90+5'", 90, 5),
            ("2nd Half 1-0 90+5'", 90, 5),
            ("HT 45+1'", 45, 1),
            ("Second Half, 88'", 88, None),
        ):
            with self.subTest(text=text):
                parsed = parse_clock_text(text)
                self.assertEqual(parsed[0], minute)
                self.assertEqual(parsed[1], stoppage)

    def test_unmarked_integer_is_a_clock_only_when_it_is_the_whole_value(self):
        for text in ("90", " 90 ", "88"):
            with self.subTest(text=text):
                self.assertEqual(parse_clock_text(text)[0], int(text.strip()))
        # Inside prose an unmarked integer is ambiguous and must be refused
        # rather than guessed.
        for text in ("2nd half", "1-0", "Second Half", "halftime 1-0"):
            with self.subTest(text=text):
                self.assertEqual(parse_clock_text(text), (None, None, None))

    def test_prose_clock_reaches_the_88_gate(self):
        """The whole point of the parser: a real 90+5 must open the sleeve."""
        for details in (
            {"time": "90+5'", "status": "live"},
            {"status_text": "2nd Half 90+5'", "status": "live"},
            {"status_text": "1-0 90+5'", "status": "live"},
            {"status_text": "2nd Half 1-0 90+5'"},
        ):
            with self.subTest(details=details):
                parsed = parse_current_clock(details)
                self.assertEqual(parsed.provider_minute, 90)
                accepted, outcome, usable, _reason = evaluate_clock_gate(
                    parsed, age_ms=100.0, mapped=True,
                )
                self.assertTrue(accepted)
                self.assertTrue(usable)
                self.assertEqual(outcome, "clock_88_plus")

    def test_current_clock_field_lookup_is_case_insensitive(self):
        """`time` resolves like every other clock field, not by exact key.

        Regression: `details.get("time")` bypassed the compact key matching used
        for match_clock/game_clock/clock, so a capitalized `Time` key was
        recorded in raw_context but ignored by the parser.
        """
        parsed = parse_current_clock({"Time": "90+5'", "status": "live"})
        self.assertEqual(parsed.provider_minute, 90)
        self.assertEqual(parsed.provider_stoppage, 5)
        self.assertEqual(parsed.source_field, "time")

    def test_unclassifiable_prose_does_not_become_a_period_or_status(self):
        """Raw scoreboard text must not be persisted as a period/status label."""
        parsed = parse_current_clock({"status_text": "1-0 90+5'"})
        self.assertEqual(parsed.provider_period, "2nd")
        self.assertEqual(parsed.provider_status, "live")
        self.assertNotIn("1-0", str(parsed.provider_period))
        self.assertNotIn("1-0", str(parsed.provider_status))


class ClockLineageAndFreshnessTests(unittest.TestCase):
    """Merge blockers 1 and 2 from architect review."""

    def clock(self, minute, stoppage=None, period="2nd", status="live"):
        from app.match_clock import ParsedClock
        rendered = f"{minute}+{stoppage}'" if stoppage else f"{minute}'"
        return ParsedClock(period, minute, stoppage, rendered, status, "time", {})

    def timing(self, ts):
        return {"received_wall": ts, "started_wall": ts - 0.05,
                "response_ms": 50.0, "previous_poll_ts": ts - 0.25}

    def tracker(self):
        t = MatchClockTracker()
        t.set_mapping("EV", "m1")
        return t

    def test_unchanged_poll_keeps_the_persisted_observation_id(self):
        """B1: the 88 gate must never accept a clock with no database row.

        An unchanged poll confirms an existing reading.  Replacing the cache
        dropped the persisted row id and advanced observed_ts to a time with no
        matching row, while the gate still accepted.
        """
        t = self.tracker()
        row = t.observe("EV", "m1", self.clock(90, 5), self.timing(1000.0))
        self.assertIsNotNone(row)
        t.promote("EV", 123)
        self.assertIsNone(t.observe("EV", "m1", self.clock(90, 5), self.timing(1002.0)))
        stamp = t.stamp("EV", 1002.1)
        self.assertEqual(stamp["observation_id"], 123)
        self.assertEqual(stamp["observed_ts"], 1000.0, "observed_ts must anchor the persisted row")
        self.assertEqual(stamp["confirmed_ts"], 1002.0, "confirmation time must advance")
        self.assertTrue(stamp["usable_for_88_gate"])

    def test_an_accepted_stamp_always_carries_lineage(self):
        t = self.tracker()
        t.observe("EV", "m1", self.clock(90, 5), self.timing(1000.0))
        t.promote("EV", 77)
        for tick in (1001.0, 1002.0, 1003.0):
            t.observe("EV", "m1", self.clock(90, 5), self.timing(tick))
            stamp = t.stamp("EV", tick + 0.05)
            if stamp["usable_for_88_gate"]:
                self.assertIsNotNone(
                    stamp["observation_id"],
                    "an accepted 88+ stamp with no observation id has no lineage",
                )

    def test_a_changed_clock_still_creates_a_new_observation(self):
        t = self.tracker()
        t.observe("EV", "m1", self.clock(90, 4), self.timing(1000.0))
        row = t.observe("EV", "m1", self.clock(90, 5), self.timing(1002.0))
        self.assertIsNotNone(row, "a real clock change must persist a new row")
        self.assertEqual(row["provider_stoppage"], 5)

    def test_freshness_is_not_conflated_with_88_eligibility(self):
        """B2a: a fresh minute-70 clock is fresh, just not yet eligible."""
        t = self.tracker()
        t.observe("EV", "m1", self.clock(70), self.timing(1000.0))
        t.promote("EV", 41)
        coverage = t.coverage({"EV"}, now=1000.1)
        self.assertEqual(coverage["clock_present"], 1)
        self.assertEqual(coverage["clock_fresh"], 1)
        self.assertEqual(coverage["clock_stale"], 0)
        self.assertEqual(coverage["faults"], [])

    def test_coverage_freshness_decays_and_agrees_with_the_gate(self):
        """B2b: coverage must not report fresh once the gate calls it stale."""
        t = self.tracker()
        t.observe("EV", "m1", self.clock(90), self.timing(1000.0))
        t.promote("EV", 42)
        self.assertEqual(t.coverage({"EV"}, now=1000.1)["clock_fresh"], 1)
        self.assertEqual(t.stamp("EV", 1000.1)["gate_outcome"], "clock_88_plus")
        # Derived from the configured bound rather than a literal: the bound is
        # tuned against measured feed latency, and this test asserts the two
        # readers agree about it, not what its value happens to be.
        stale_at = 1000.0 + (config.MATCH_CLOCK_MAX_AGE_MS / 1000.0) + 1.0
        late = t.coverage({"EV"}, now=stale_at)
        self.assertEqual(late["clock_fresh"], 0)
        self.assertEqual(late["clock_stale"], 1)
        self.assertEqual(t.stamp("EV", stale_at)["gate_outcome"], "clock_stale")
        self.assertIn("stale", [row["reason"] for row in late["faults"]])


class ClockGateAndStampTests(unittest.TestCase):
    def observation(self, minute=90, stoppage=5, period="2nd", status="live", ts=100.0):
        return {
            "id": 12,
            "observed_ts": ts,
            "previous_poll_ts": ts - 0.25,
            "provider_period": period,
            "provider_minute": minute,
            "provider_stoppage": stoppage,
            "provider_clock": f"{minute}+{stoppage}′" if stoppage else f"{minute}′",
            "provider_status": status,
            "precision": "provider_minute_polled",
            "source": "kalshi_live_data_batch",
        }

    @patch.object(config, "SLEEVE_MIN_MINUTE", 88)
    def test_87_rejects_88_and_stoppage_accept(self):
        """Boundary behaviour of the gate, pinned at 88 rather than reading the
        shipped default, so this keeps asserting `threshold - 1 refuses,
        threshold accepts` however the default is later tuned."""
        for minute, stoppage, accepted, outcome in (
            (87, None, False, "clock_pre_88"),
            (88, None, True, "clock_88_plus"),
            (89, None, True, "clock_88_plus"),
            (90, None, True, "clock_88_plus"),
            (90, 5, True, "clock_88_plus"),
        ):
            stamp = stamp_from_observation(
                self.observation(minute, stoppage), "E", 100.2,
            )
            gate = MatchClockGate(stamp).evaluate()
            self.assertEqual(gate["accepted"], accepted, minute)
            self.assertEqual(gate["outcome"], outcome)

    def test_first_half_final_and_suspended_reject(self):
        for period, status, outcome in (
            ("1st", "live", "clock_first_half"),
            ("2nd", "final", "clock_final"),
            ("2nd", "suspended", "clock_suspended"),
        ):
            stamp = stamp_from_observation(
                self.observation(90, 1, period, status), "E", 100.2,
            )
            self.assertEqual(MatchClockGate(stamp).evaluate()["outcome"], outcome)

    def test_period_shaped_status_is_live_play(self):
        """Kalshi reports the running period in the status field.

        Production regression: ``2nd_half`` compacted to a token no status set
        matched, so ``normalize_status`` returned it verbatim and the gate
        refused every in-play match as ``clock_not_live``.  Over the first 7.9
        days of live capture that rejected 100% of price-only candidates and the
        sleeve never admitted a single one.  Counts below are the observed
        provider vocabulary from that capture.
        """
        for raw in ("2nd_half", "1st_half", "2ND_HALF", "second half", "extra_time"):
            with self.subTest(status=raw):
                self.assertEqual(normalize_status(raw), "live")
        # A stoppage is not play, and must keep failing closed.
        for raw, expected in (
            ("halftime", "half-time"),
            ("half-time", "half-time"),
            ("final", "final"),
            ("suspended", "suspended"),
        ):
            with self.subTest(status=raw):
                self.assertEqual(normalize_status(raw), expected)

    @patch.object(config, "SLEEVE_MIN_MINUTE", 88)
    def test_second_half_88_reaches_the_gate(self):
        """The exact production shape: minute 88, period 2nd, status 2nd_half.

        Pinned at 88 because this is the regression test for the status fix, and
        it must keep testing the status matcher rather than the current
        threshold. Minute 87 appears below as the one-below-threshold case.
        """
        stamp = stamp_from_observation(
            self.observation(88, None, "2nd", "2nd_half"), "E", 100.2,
        )
        gate = MatchClockGate(stamp).evaluate()
        self.assertTrue(gate["accepted"])
        self.assertEqual(gate["outcome"], "clock_88_plus")
        self.assertTrue(stamp["usable_for_88_gate"])
        # Fixing the status must not weaken any other boundary.
        for minute, period, status, outcome in (
            (87, "2nd", "2nd_half", "clock_pre_88"),
            (40, "1st", "1st_half", "clock_first_half"),
            (45, "half-time", "halftime", "clock_half_time"),
            (90, "final", "final", "clock_final"),
        ):
            with self.subTest(minute=minute, status=status):
                other = stamp_from_observation(
                    self.observation(minute, None, period, status), "E", 100.2,
                )
                evaluated = MatchClockGate(other).evaluate()
                self.assertFalse(evaluated["accepted"])
                self.assertEqual(evaluated["outcome"], outcome)

    def test_stale_and_unmapped_fail_closed(self):
        stamp = stamp_from_observation(self.observation(ts=90.0), "E", 100.2)
        self.assertEqual(MatchClockGate(stamp).evaluate()["outcome"], "clock_stale")
        unmapped = stamp_from_observation(None, "E", 100.2, mapped=False)
        self.assertEqual(MatchClockGate(unmapped).evaluate()["outcome"], "clock_unmapped")
        self.assertFalse(unmapped["usable_for_88_gate"])

    def test_legacy_stamp_does_not_fabricate_a_minute(self):
        stamp = parse_stored_stamp(None)
        self.assertIsNone(stamp["provider_minute"])
        self.assertEqual(stamp["unusable_reason"], "legacy_signal_recorded_before_clock_stamps")
        self.assertFalse(stamp["usable_for_88_gate"])


class ClockPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()
        store.set_mode("live")
        self.tracker = MatchClockTracker()

    def tearDown(self):
        store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def test_unchanged_polls_do_not_flood_sqlite(self):
        parsed = parse_current_clock({"time": "90+5'", "half": "2nd", "status": "live"})
        timing = {
            "received_wall": 10.0, "started_wall": 9.99,
            "previous_poll_ts": 9.75, "response_ms": 12.0,
        }
        first = self.tracker.observe("E", "M", parsed, timing)
        self.assertIsNotNone(first)
        row_id = store.insert_match_clock(first)
        self.tracker.promote("E", row_id)
        timing["received_wall"] = 10.25
        self.assertIsNone(self.tracker.observe("E", "M", parsed, timing))
        self.assertEqual(store.q("SELECT COUNT(*) AS n FROM match_clock_observations")[0]["n"], 1)
        later = parse_current_clock({"time": "90+6'", "half": "2nd", "status": "live"})
        timing["received_wall"] = 10.5
        changed = self.tracker.observe("E", "M", later, timing)
        self.assertIsNotNone(changed)
        store.insert_match_clock(changed)
        self.assertEqual(store.q("SELECT COUNT(*) AS n FROM match_clock_observations")[0]["n"], 2)

    def test_every_signal_persists_a_complete_stamp_and_trade_inherits_it(self):
        parsed = parse_current_clock({"time": "90+5'", "half": "2nd", "status": "live"})
        row = self.tracker.observe("E", "M", parsed, {
            "received_wall": 50.0, "started_wall": 49.99,
            "previous_poll_ts": 49.75, "response_ms": 8.0,
        })
        row_id = store.insert_match_clock(row)
        self.tracker.promote("E", row_id)
        stamp = self.tracker.stamp("E", 50.1)
        signal_id = store.insert_signal({
            "ts_ms": 50100, "local_ts": 50.1, "market": "T", "event": "E",
            "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 200,
            "ref": 40, "ext": 60, "late": True, "outcome": "filled",
            "match_clock_snapshot": stamp,
        })
        stored = store.q("SELECT match_clock_snapshot FROM signals WHERE id=?", (signal_id,))[0]
        loaded = json.loads(stored["match_clock_snapshot"])
        self.assertEqual(loaded["observation_id"], row_id)
        self.assertEqual(loaded["provider_clock"], "90+5′")
        self.assertTrue(loaded["usable_for_88_gate"])
        self.assertEqual(parse_stored_stamp(stored["match_clock_snapshot"])["provider_minute"], 90)

    def test_old_volume_adds_clock_tables_without_fabricating_legacy_stamps(self):
        store._conn.close()
        store._conn = None
        import os
        import sqlite3
        db_path = os.path.join(self.tempdir.name, "footballbot.db")
        os.unlink(db_path)
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE signals(
              id INTEGER PRIMARY KEY, ts_ms INTEGER, local_ts REAL, market TEXT,
              event TEXT, series TEXT, dir INTEGER, dl REAL, levels INTEGER,
              size REAL, ref REAL, ext REAL, conf_lag_ms REAL, late INTEGER,
              outcome TEXT, detail TEXT);
            CREATE TABLE trades(
              id INTEGER PRIMARY KEY, signal_id INTEGER, market TEXT, event TEXT,
              series TEXT, dir INTEGER, side TEXT, entry_ts REAL, entry_px REAL,
              size REAL, cap REAL, notional REAL, exit_ts REAL, exit_px REAL,
              exit_reason TEXT, gross REAL, fees REAL, net REAL, mae REAL,
              shadow_stop_px REAL, book_at_entry TEXT, status TEXT DEFAULT 'open');
        """)
        conn.execute(
            "INSERT INTO signals(event,outcome,detail) VALUES(?,?,?)",
            ("E", "filled", "{}"),
        )
        conn.commit()
        conn.close()
        store.init()
        columns = {row["name"] for row in store.q("PRAGMA table_info(signals)")}
        self.assertIn("match_clock_snapshot", columns)
        tables = {row["name"] for row in store.q(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("match_clock_observations", tables)
        self.assertIn("provider_match_events", tables)
        row = store.q("SELECT match_clock_snapshot FROM signals")[0]
        self.assertIsNone(row["match_clock_snapshot"])
        self.assertEqual(
            parse_stored_stamp(row["match_clock_snapshot"])["unusable_reason"],
            "legacy_signal_recorded_before_clock_stamps",
        )


class TwoPhaseClockPublicationTests(unittest.TestCase):
    """Handoff section 3: a candidate clock is invisible until its row id exists."""

    def clock(self, minute, stoppage=None, period="2nd", status="live"):
        from app.match_clock import ParsedClock
        rendered = f"{minute}+{stoppage}'" if stoppage else f"{minute}'"
        return ParsedClock(period, minute, stoppage, rendered, status, "time", {})

    def timing(self, ts, previous=None):
        return {"received_wall": ts, "started_wall": ts - 0.05,
                "response_ms": 50.0,
                "previous_poll_ts": ts - 0.25 if previous is None else previous}

    def tracker(self):
        t = MatchClockTracker()
        t.set_mapping("EV", "m1")
        return t

    def observation(self, **overrides):
        row = {
            "id": 12,
            "observed_ts": 100.0,
            "confirmed_ts": 100.0,
            "previous_poll_ts": 99.75,
            "confirmation_previous_poll_ts": 99.75,
            "provider_period": "2nd",
            "provider_minute": 90,
            "provider_stoppage": 5,
            "provider_clock": "90+5′",
            "provider_status": "live",
            "precision": "provider_minute_polled",
            "source": "kalshi_live_data_batch",
        }
        row.update(overrides)
        return row

    def test_unpersisted_clock_fails_closed_before_minute_logic(self):
        """An id-less observation must never reach 88+ acceptance."""
        for bad_id in (None, 0, -1, "12"):
            with self.subTest(observation_id=bad_id):
                stamp = stamp_from_observation(
                    self.observation(id=bad_id), "EV", 100.2,
                )
                self.assertFalse(stamp["usable_for_88_gate"])
                self.assertEqual(stamp["gate_outcome"], "clock_unpersisted")
                self.assertEqual(stamp["unusable_reason"], "unpersisted")

                gate = MatchClockGate(stamp).evaluate()
                self.assertFalse(gate["accepted"])
                self.assertEqual(gate["outcome"], "clock_unpersisted")
                self.assertFalse(gate["usable_for_88_gate"])
                self.assertEqual(gate["unusable_reason"], "unpersisted")

    def test_gate_object_fails_closed_on_unpersisted_id_even_when_mapped(self):
        """MatchClockGate must apply the invariant itself, not trust its caller."""
        gate = MatchClockGate({
            "event": "EV", "observation_id": None, "provider_period": "2nd",
            "provider_minute": 90, "provider_stoppage": 5,
            "provider_clock": "90+5′", "provider_status": "live", "age_ms": 40.0,
            "source": "kalshi_live_data_batch",
        }, mapped=True).evaluate()
        self.assertFalse(gate["accepted"])
        self.assertEqual(gate["outcome"], "clock_unpersisted")

    def test_unchanged_poll_preserves_id_and_uses_latest_confirmation_interval(self):
        """Receipts 10.00/10.25/10.50 keep one id and report +250ms, never -250."""
        t = self.tracker()
        row = t.observe("EV", "m1", self.clock(90, 5), self.timing(10.00, previous=9.75))
        self.assertIsNotNone(row)
        t.promote("EV", 55)

        self.assertIsNone(
            t.observe("EV", "m1", self.clock(90, 5), self.timing(10.25, previous=10.00)))
        self.assertIsNone(
            t.observe("EV", "m1", self.clock(90, 5), self.timing(10.50, previous=10.25)))

        stamp = t.stamp("EV", 10.55)
        self.assertEqual(stamp["observation_id"], 55)
        self.assertEqual(stamp["observed_ts"], 10.00)
        self.assertEqual(stamp["confirmed_ts"], 10.50)
        self.assertEqual(stamp["confirmation_previous_poll_ts"], 10.25)
        self.assertEqual(stamp["poll_uncertainty_ms"], 250.0)
        self.assertGreaterEqual(stamp["poll_uncertainty_ms"], 0.0)
        self.assertAlmostEqual(stamp["established_age_ms"], 550.0, places=3)
        self.assertAlmostEqual(stamp["age_ms"], 50.0, places=3)

    def test_future_observation_and_confirmation_fail_closed(self):
        """A receipt later than the signal is refused, not coerced to age zero."""
        future_confirm = stamp_from_observation(
            self.observation(observed_ts=100.0, confirmed_ts=101.0), "EV", 100.2,
        )
        self.assertFalse(future_confirm["usable_for_88_gate"])
        self.assertEqual(future_confirm["gate_outcome"], "clock_future")
        self.assertEqual(future_confirm["unusable_reason"], "future_timestamp")
        self.assertNotEqual(future_confirm["age_ms"], 0.0)
        self.assertLess(future_confirm["age_ms"], 0.0)

        future_observe = stamp_from_observation(
            self.observation(observed_ts=101.0, confirmed_ts=101.0), "EV", 100.2,
        )
        self.assertFalse(future_observe["usable_for_88_gate"])
        self.assertEqual(future_observe["gate_outcome"], "clock_future")

    def test_restart_requires_new_provider_confirmation_for_freshness(self):
        """A database row alone is not a fresh live confirmation."""
        restarted = MatchClockTracker()
        restarted.set_mapping("EV", "m1")

        stamp = restarted.stamp("EV", 10.10)
        self.assertFalse(stamp["usable_for_88_gate"])
        self.assertNotEqual(stamp["gate_outcome"], "clock_88_plus")
        self.assertEqual(restarted.coverage({"EV"}, now=10.10)["clock_fresh"], 0)

        restarted.observe("EV", "m1", self.clock(90, 5), self.timing(10.00))
        self.assertEqual(
            restarted.coverage({"EV"}, now=10.10)["clock_fresh"], 0,
            "an unpromoted candidate must not count as fresh",
        )
        restarted.promote("EV", 91)
        self.assertEqual(restarted.coverage({"EV"}, now=10.10)["clock_fresh"], 1)

    def test_candidate_is_invisible_until_promoted(self):
        t = self.tracker()
        t.observe("EV", "m1", self.clock(90, 5), self.timing(10.00))

        stamp = t.stamp("EV", 10.05)
        self.assertNotEqual(stamp["gate_outcome"], "clock_88_plus")
        self.assertFalse(stamp["usable_for_88_gate"])
        self.assertEqual(t.coverage({"EV"}, now=10.05)["clock_present"], 0)

    def test_failed_persist_keeps_previous_observation_and_leaves_identity_open(self):
        t = self.tracker()
        t.observe("EV", "m1", self.clock(89), self.timing(10.00))
        t.promote("EV", 7)

        self.assertIsNotNone(
            t.observe("EV", "m1", self.clock(90, 5), self.timing(10.25)))
        t.fail_persist("EV", RuntimeError("database is locked"))

        stamp = t.stamp("EV", 10.30)
        self.assertEqual(stamp["observation_id"], 7, "previous persisted row stays visible")
        self.assertEqual(stamp["provider_minute"], 89)

        retry = t.observe("EV", "m1", self.clock(90, 5), self.timing(10.50))
        self.assertIsNotNone(retry, "an identical poll must retry an uncommitted identity")


class ClockDatabaseLineageTests(unittest.TestCase):
    """Handoff section 3.3: an accepted stamp resolves to exactly one row."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()
        store.set_mode("live")

    def tearDown(self):
        store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def test_every_accepted_clock_stamp_resolves_to_matching_database_row(self):
        tracker = MatchClockTracker()
        tracker.set_mapping("EV", "m1")
        accepted = 0
        for index, minute in enumerate((70, 88, 89, 90, 90)):
            ts = 1000.0 + index
            parsed = parse_current_clock({
                "time": f"{minute}'", "half": "2nd", "status": "live",
            })
            row = tracker.observe("EV", "m1", parsed, {
                "received_wall": ts, "started_wall": ts - 0.05,
                "response_ms": 50.0, "previous_poll_ts": ts - 0.25,
            })
            if row is not None:
                row_id = store.insert_match_clock(row)
                tracker.promote("EV", row_id)

            stamp = tracker.stamp("EV", ts + 0.05)
            if not stamp["usable_for_88_gate"]:
                continue
            accepted += 1
            observation_id = stamp["observation_id"]
            self.assertIsInstance(observation_id, int)
            self.assertGreater(observation_id, 0)

            rows = store.q(
                "SELECT * FROM match_clock_observations WHERE id=?", (observation_id,),
            )
            self.assertEqual(len(rows), 1, "stamp must resolve to exactly one row")
            persisted = rows[0]
            self.assertEqual(persisted["event"], stamp["event"])
            self.assertEqual(persisted["provider_period"], stamp["provider_period"])
            self.assertEqual(persisted["provider_minute"], stamp["provider_minute"])
            self.assertEqual(persisted["provider_stoppage"], stamp["provider_stoppage"])
            self.assertEqual(persisted["provider_clock"], stamp["provider_clock"])
            self.assertEqual(persisted["provider_status"], stamp["provider_status"])
            self.assertEqual(persisted["source"], stamp["source"])
            self.assertEqual(persisted["observed_ts"], stamp["observed_ts"])
        self.assertGreater(accepted, 0, "fixture must produce accepted stamps")

    def test_new_rows_record_their_source_and_legacy_null_stays_legacy_unknown(self):
        tracker = MatchClockTracker()
        tracker.set_mapping("EV", "m1")
        parsed = parse_current_clock({"time": "90'", "half": "2nd", "status": "live"})
        row = tracker.observe("EV", "m1", parsed, {
            "received_wall": 1000.0, "started_wall": 999.95,
            "response_ms": 50.0, "previous_poll_ts": 999.75,
        })
        row_id = store.insert_match_clock(row)
        stored = store.q(
            "SELECT source FROM match_clock_observations WHERE id=?", (row_id,),
        )[0]
        self.assertEqual(stored["source"], "kalshi_live_data_batch")

        store.ex(
            "INSERT INTO match_clock_observations(observed_ts,poll_started_ts,"
            "response_ms,event,milestone_id,provider_minute,precision,raw_context)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (900.0, 899.9, 50.0, "EV", "m1", 90, "provider_minute_polled", "{}"),
        )
        legacy = store.q(
            "SELECT source FROM match_clock_observations WHERE observed_ts=900.0",
        )[0]
        self.assertIsNone(legacy["source"], "legacy rows keep null source in storage")


class ExpectedExpirationIsolationTests(unittest.TestCase):
    def test_expected_expiration_cannot_enter_the_gate_object(self):
        stamp = unusable_stamp("E", 1.0, "missing_clock", gate_outcome="clock_missing")
        stamp["expected_expiration_time"] = "2026-08-30T22:00:00Z"
        result = MatchClockGate(stamp).evaluate()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["outcome"], "clock_missing")
        self.assertNotIn("expected_expiration", MatchClockGate.__slots__)


if __name__ == "__main__":
    unittest.main()
