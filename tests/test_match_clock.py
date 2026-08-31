import json
import tempfile
import unittest
from unittest.mock import patch

from app.match_clock import (
    MatchClockGate,
    MatchClockTracker,
    evaluate_clock_gate,
    parse_current_clock,
    parse_clock_text,
    parse_stored_stamp,
    stamp_from_observation,
    unusable_stamp,
)
from app import store


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

    def test_87_rejects_88_and_stoppage_accept(self):
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
        self.tracker.latest["E"]["id"] = row_id
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
        self.tracker.latest["E"]["id"] = row_id
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
