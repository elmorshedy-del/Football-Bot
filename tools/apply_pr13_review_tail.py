from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    p.write_text(text.replace(old, new, 1))


def insert_before_main(path, block):
    p = Path(path)
    text = p.read_text()
    marker = "\n\nif __name__ == \"__main__\":\n"
    if marker not in text:
        raise RuntimeError(f"{path}: main marker missing")
    p.write_text(text.replace(marker, "\n" + block.rstrip() + marker, 1))


# The browser failure is a real CSS bug, not a flaky visibility assertion.
style = Path("static/style.css")
css = style.read_text()
if css.count("var(--yellow)") != 2:
    raise RuntimeError(f"expected two undefined --yellow uses, got {css.count('var(--yellow)')}")
style.write_text(css.replace("var(--yellow)", "var(--amber)"))

# The review requires a runtime N+1 assertion over a non-trivial result set.
runtime_block = r'''
    def test_trade_and_signal_list_endpoints_do_not_query_paths_per_row(self):
        store.set_mode("demo")
        demo_trade_ids = []
        for index in range(25):
            event = f"DEMO-{index:02d}"
            sid = self.signal(event=event, started=False)
            demo_trade_ids.append(self.trade(sid, event=event))
        store.set_mode("live")
        live_sid = self.signal(event="LIVE-LEAK", started=False)
        self.trade(live_sid, event="LIVE-LEAK")

        traced = []
        store._conn.set_trace_callback(traced.append)
        try:
            trades = run(main.trades(mode="demo", limit=100))
            signals = run(main.signals(mode="demo", limit=100))
        finally:
            store._conn.set_trace_callback(None)

        self.assertEqual({row["id"] for row in trades["open"]}, set(demo_trade_ids))
        self.assertEqual(len(signals), 25)
        self.assertTrue(all(row["mode"] == "demo" for row in trades["open"]))
        self.assertTrue(all(row["mode"] == "demo" for row in signals))
        path_selects = [
            sql for sql in traced
            if sql.lstrip().upper().startswith("SELECT")
            and "BID_PATH_SAMPLES" in sql.upper()
        ]
        self.assertEqual(
            path_selects, [],
            f"list endpoints performed path queries for 25-row result sets: {path_selects}",
        )

    def test_settlement_terminal_cannot_become_executable_peak(self):
        rows = [
            self.path_row(seq=1, bid=90.0),
            self.path_row(seq=2, bid=None, terminal=1, availability="terminal"),
        ]
        rows[-1]["dt_ms"] = 2000.0
        summary = store.bid_path_summary(rows)
        self.assertEqual(summary["peak_bid"], 90.0)
        self.assertEqual(summary["last_bid"], 90.0)
        self.assertEqual(summary["samples_priced"], 1)

    def test_terminal_closes_peak_duration_but_is_not_priced(self):
        rows = [
            self.path_row(seq=1, bid=90.0),
            self.path_row(seq=2, bid=90.0),
            self.path_row(seq=3, bid=None, terminal=1, availability="terminal"),
        ]
        rows[0]["dt_ms"] = 0.0
        rows[1]["dt_ms"] = 1000.0
        rows[2]["dt_ms"] = 2500.0
        summary = store.bid_path_summary(rows)
        self.assertEqual(summary["samples_total"], 3)
        self.assertEqual(summary["samples_priced"], 2)
        self.assertEqual(summary["ms_at_peak"], 2500.0)
        self.assertEqual(summary["gap_count"], 0)
'''
insert_before_main("tests/test_pr13_followup_blockers.py", runtime_block)

# Restart-boundary cap ownership: 3,999 -> exactly one terminal at 4,000;
# a legacy/exhausted 4,000-row path may not fabricate row 4,001 or release owner.
close_block = r'''
    def _seed_position_path(self, pos, count):
        rows = []
        for seq in range(1, count + 1):
            rows.append({
                "kind": "position", "trade_id": pos.tid, "signal_id": pos.signal_id,
                "event": pos.event, "market": pos.market, "side": pos.side,
                "strategy": pos.strategy, "anchor_ts": pos.entry_ts,
                "dt_ms": float(seq), "bid": 50.0, "bid_size": 100.0,
                "exec_px": 50.0, "qty": pos.remaining, "sample_seq": seq,
                "availability": "quote", "terminal": 0,
            })
        store.insert_bid_path(rows)

    def _restart_owned_position(self, tid):
        self.desk.positions = {}
        self.desk.realistic = True
        self.desk.restore_open_positions(store.load_open_paper_positions())
        restored = self.desk.positions[tid]
        self.desk.realistic = False
        return restored

    def test_restart_at_3999_rows_closes_with_exactly_4000_including_terminal(self):
        pos = self.open_trade()
        self._seed_position_path(pos, 3999)
        restored = self._restart_owned_position(pos.tid)
        self.assertEqual(restored.exec_path_total, 3999)

        self.assertTrue(self.desk.close(restored, 60.0, "target"))
        rows = self.path_rows(pos.tid)
        self.assertEqual(len(rows), 4000)
        self.assertEqual(max(row["sample_seq"] for row in rows), 4000)
        self.assertEqual(sum(row["terminal"] for row in rows), 1)
        self.assertNotIn(pos.tid, self.desk.positions)

    def test_exhausted_legacy_path_never_writes_row_4001_or_releases_owner(self):
        pos = self.open_trade()
        self._seed_position_path(pos, 4000)
        restored = self._restart_owned_position(pos.tid)
        self.assertEqual(restored.exec_path_total, 4000)

        self.assertFalse(self.desk.close(restored, 60.0, "target"))
        rows = self.path_rows(pos.tid)
        self.assertEqual(len(rows), 4000)
        self.assertEqual(max(row["sample_seq"] for row in rows), 4000)
        self.assertEqual(sum(row["terminal"] for row in rows), 0)
        self.assertIn(pos.tid, self.desk.positions)
        self.assertEqual(self.trade_row(pos.tid)["status"], "open")
        self.assertTrue(any("path_sample_cap_exhausted" in message for _, message in self.errors))
'''
insert_before_main("tests/test_close_ownership.py", close_block)

# Startup recovery ownership + independent health latch + PAPER_EXECUTION_V2 isolation.
replace_once("tests/test_signal_path_ownership.py", "import tempfile\n", "import asyncio\nimport tempfile\n")
signal_block = r'''
    def test_failed_startup_rebuild_retains_retry_owner_then_recovers(self):
        sid = self.signal()
        with patch("app.engine.store.finalize_signal_path_with_rows",
                   side_effect=OSError("disk full")):
            rebuilt = self.engine.rebuild_signal_paths()

        self.assertEqual(rebuilt, 0)
        self.assertEqual(len(self.engine._signal_paths), 1)
        watch = self.engine._signal_paths[0]
        self.assertTrue(watch.get("retry_only"))
        self.assertEqual(watch["signal_id"], sid)
        self.assertEqual(self.engine.signal_path_fault, "signal_path_persistence_failed")
        self.assertIsNone(self.signal_row(sid)["forward_path_finalized"])

        self.engine._expire_signal_paths(1.0)
        self.assertEqual(len(self.engine._signal_paths), 0)
        self.assertIsNotNone(self.signal_row(sid)["forward_path_finalized"])
        self.assertEqual(
            self.signal_row(sid)["path_incomplete_reason"],
            "in_memory_tail_lost_on_restart",
        )
        self.assertIsNone(self.engine.signal_path_fault)

    def test_one_success_cannot_clear_another_failed_watch_fault(self):
        failed_sid = self.signal()
        successful_sid = self.signal()
        real_finalize = store.finalize_signal_path_with_rows

        def selective(signal_id, *args, **kwargs):
            if signal_id == failed_sid:
                raise OSError("failed owner")
            return real_finalize(signal_id, *args, **kwargs)

        with patch("app.engine.store.finalize_signal_path_with_rows", side_effect=selective):
            rebuilt = self.engine.rebuild_signal_paths()

        self.assertEqual(rebuilt, 1)
        self.assertEqual([w["signal_id"] for w in self.engine._signal_paths], [failed_sid])
        self.assertIn(failed_sid, self.engine._signal_path_failed_owners)
        self.assertNotIn(successful_sid, self.engine._signal_path_failed_owners)
        self.assertEqual(self.engine.signal_path_fault, "signal_path_persistence_failed")

        self.engine._expire_signal_paths(1.0)
        self.assertEqual(len(self.engine._signal_paths), 0)
        self.assertEqual(self.engine._signal_path_failed_owners, set())
        self.assertIsNone(self.engine.signal_path_fault)

    def test_signal_rebuild_runs_when_paper_execution_v2_is_disabled(self):
        eng = engine_module.Engine.__new__(engine_module.Engine)
        eng.mode = "demo"
        eng.ws_state = ""
        calls = []
        eng.rebuild_signal_paths = lambda: calls.append("rebuild") or 0

        class DummyReplay:
            def __init__(self, _engine):
                pass

            async def run(self):
                return None

        def discard_task(coro):
            coro.close()
            return object()

        with patch("app.engine.config.PAPER_EXECUTION_V2", False), \
                patch("app.engine.asyncio.create_task", side_effect=discard_task), \
                patch("app.replay.DemoReplay", DummyReplay):
            asyncio.run(eng.start())

        self.assertEqual(calls, ["rebuild"])
'''
insert_before_main("tests/test_signal_path_ownership.py", signal_block)

Path("tools/apply_pr13_review_tail.py").unlink()
Path(".github/workflows/apply-pr13-review-tail.yml").unlink()
