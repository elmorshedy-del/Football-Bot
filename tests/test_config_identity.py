"""Configuration provenance: every row must name the configuration behind it.

The first live study reported one net of -$609.02 across 56 closed trades.  It
was really two configurations pooled: 27 trades at -$630.13 written before
Aug 30, and 29 at +$21.11 after.  No aggregate over that pool answered any
question about either, and nothing in the schema could separate them after the
fact.  These tests pin the identity that makes the separation possible.
"""
import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import config, exporter, store


class ConfigIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_content_addressed(self):
        first = config.config_id()
        self.assertEqual(first, config.config_id())
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        record = config.config_record()
        self.assertEqual(record["config_id"], first)
        self.assertEqual(record["code_fingerprint"], config.CODE_FINGERPRINT)

    def test_a_changed_parameter_is_a_new_identity(self):
        before = config.config_id()
        with patch.object(config, "DL_MIN", config.DL_MIN + 0.1):
            after = config.config_id()
        self.assertNotEqual(before, after)
        self.assertEqual(config.config_id(), before)

    def test_a_changed_strategy_source_is_a_new_identity(self):
        """A code edit is a configuration change even when no parameter moves.

        This is the case the live study actually hit: the two eras ran the same
        environment variables and different code.
        """
        original = config.CODE_FINGERPRINT
        with patch.object(config, "CODE_FINGERPRINT", "0" * 12):
            self.assertNotEqual(config.config_id(), None)
            changed = config.config_id()
        with patch.object(config, "CODE_FINGERPRINT", original):
            self.assertNotEqual(changed, config.config_id())

    def test_fingerprint_covers_every_strategy_source(self):
        here = os.path.dirname(os.path.abspath(config.__file__))
        for name in config._STRATEGY_SOURCES:
            self.assertTrue(os.path.isfile(os.path.join(here, name)), name)
        self.assertIn("detector.py", config._STRATEGY_SOURCES)
        self.assertIn("late_score_sleeve.py", config._STRATEGY_SOURCES)

    def test_observability_settings_do_not_change_the_identity(self):
        """A read-only observer cannot change a trading decision."""
        before = config.config_id()
        with patch.object(config, "GOAL_LATENCY_POLL_MS", 999.0):
            self.assertEqual(config.config_id(), before)

    def test_manifest_and_identity_share_one_parameter_list(self):
        exported = exporter.non_secret_config()
        for name in config.STRATEGY_PARAM_NAMES:
            self.assertIn(name.lower(), exported, name)


class ConfigStampTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(config, "DATA_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()
        store.set_mode("live")

    def signal(self, **over):
        row = {"ts_ms": 1, "local_ts": 1.0, "market": "M", "event": "E",
               "series": "S", "dir": 1, "dl": 1.0, "levels": 6, "size": 300,
               "ref": 40, "ext": 50, "conf_lag_ms": 0, "late": 1,
               "outcome": "filled", "detail": {}}
        row.update(over)
        return store.insert_signal(row)

    def test_every_signal_and_trade_carries_the_active_identity(self):
        active = store.current_config_id()
        self.assertEqual(active, config.config_id())
        sid = self.signal()
        store.insert_trade({"signal_id": sid, "market": "M", "event": "E",
                            "series": "S", "dir": 1, "side": "yes",
                            "entry_ts": 1.0, "entry_px": 50.0, "size": 100,
                            "cap": 58, "notional": 100})
        self.assertEqual(store.q("SELECT config_id FROM signals")[0]["config_id"], active)
        self.assertEqual(store.q("SELECT config_id FROM trades")[0]["config_id"], active)

    def test_the_identity_resolves_to_its_own_parameters(self):
        rows = store.q("SELECT * FROM config_versions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["config_id"], store.current_config_id())
        self.assertEqual(rows[0]["code_fingerprint"], config.CODE_FINGERPRINT)
        params = json.loads(rows[0]["params"])
        self.assertEqual(params["dl_min"], config.DL_MIN)
        self.assertEqual(params["price_cap"], config.PRICE_CAP)

    def test_restart_under_one_configuration_is_not_a_new_one(self):
        first = store.q("SELECT first_seen_ts FROM config_versions")[0]["first_seen_ts"]
        store.register_config_version()
        rows = store.q("SELECT config_id, first_seen_ts FROM config_versions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["first_seen_ts"], first)

    def test_two_configurations_stay_separable_in_one_database(self):
        """The exact failure the live study hit, reproduced and now separable."""
        era_one = store.current_config_id()
        self.signal()
        with patch.object(config, "TIMEOUT_S", config.TIMEOUT_S + 60):
            era_two = store.register_config_version()
            self.signal()
        self.assertNotEqual(era_one, era_two)
        counts = {row["config_id"]: row["n"] for row in store.q(
            "SELECT config_id, COUNT(*) n FROM signals GROUP BY 1")}
        self.assertEqual(counts, {era_one: 1, era_two: 1})
        self.assertEqual(
            len(store.q("SELECT 1 FROM config_versions")), 2)

    def test_rows_predating_provenance_keep_a_null_identity(self):
        """Unknown provenance is preserved as unknown, never backfilled."""
        store.ex("INSERT INTO signals(ts_ms,local_ts,market,event,series,dir,"
                 "dl,levels,size,ref,ext,outcome,mode) "
                 "VALUES(2,2.0,'M','E','S',1,1.0,6,300,40,50,'filled','live')")
        legacy = store.q("SELECT config_id FROM signals WHERE ts_ms=2")[0]
        self.assertIsNone(legacy["config_id"])

    def test_collection_survives_a_provenance_write_failure(self):
        """A broken registry must never stop the study recording signals."""
        with patch.object(store, "ex", side_effect=store.sqlite3.Error("boom")):
            self.assertIsNotNone(store.register_config_version())
        self.assertIsNotNone(self.signal())


if __name__ == "__main__":
    unittest.main()
