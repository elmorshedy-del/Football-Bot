from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    p.write_text(text.replace(old, new, 1))


# Do not destroy other-mode evidence now that every study surface is scoped.
replace(
    "app/engine.py",
    '''        if self.mode == "live":\n            store.purge_non_live()  # clean demo/legacy rows so live P&L starts fresh\n            if config.PAPER_EXECUTION_V2:\n''',
    '''        if self.mode == "live":\n            # Mode-scoped queries isolate live evidence; deleting demo/legacy\n            # rows here would make the explicit all-mode archival export lie.\n            if config.PAPER_EXECUTION_V2:\n''',
)

# Demo/replay collection also owns signal forward watches; recovery must not be
# coupled to PAPER_EXECUTION_V2 or to the live transport branch.
replace(
    "app/engine.py",
    '''        else:\n            from .replay import DemoReplay\n            asyncio.create_task(DemoReplay(self).run())\n            self.ws_state = "demo"\n            store.log_event("sys", "engine started in DEMO mode (replaying real Madrid tapes)")\n        if config.PAPER_EXECUTION_V2:\n''',
    '''        else:\n            rebuilt = self.rebuild_signal_paths()\n            if rebuilt:\n                store.log_event(\n                    "paper",\n                    f"rebuilt {rebuilt} unfinalized signal forward path(s) "\n                    "as incomplete after restart",\n                )\n            from .replay import DemoReplay\n            asyncio.create_task(DemoReplay(self).run())\n            self.ws_state = "demo"\n            store.log_event("sys", "engine started in DEMO mode (replaying real Madrid tapes)")\n        if config.PAPER_EXECUTION_V2:\n''',
)

# Update stale collision semantics in the ownership suite.
old = '''    def test_an_ignored_collision_does_not_clear_the_buffer(self):\n        """INSERT OR IGNORE returning cleanly is not proof of durability."""\n        pos = self.open_trade()\n        self.desk._record_exec_path(pos, live_book(no={49.0: 100.0}), 51.0, 1001.0)\n        self.desk._flush_exec_path(pos)\n        self.assertEqual(len(self.path_rows(pos.tid)), 1)\n\n        # Re-buffer a row that collides with the durable sequence key.\n        collision = dict(pos.exec_path_last_row) if hasattr(\n            pos, "exec_path_last_row") else None\n        pos.exec_path = [{\n            "kind": "position", "trade_id": pos.tid, "signal_id": pos.signal_id,\n            "event": pos.event, "market": pos.market, "side": pos.side,\n            "strategy": pos.strategy, "anchor_ts": pos.entry_ts, "dt_ms": 99.0,\n            "bid": 99.0, "bid_size": 1.0, "exec_px": 99.0, "qty": 1.0,\n            "sample_seq": 1, "availability": "quote", "terminal": 0,\n        }]\n        _ = collision\n\n        written = store.insert_bid_path(list(pos.exec_path))\n        self.assertEqual(\n            written, 0,\n            "a fully ignored batch must report zero durable rows, not the input length",\n        )\n'''
new = '''    def test_conflicting_trade_sequence_rolls_back_close_and_keeps_position(self):\n        """A same-key/different-payload retry is corruption, never success."""\n        pos = self.open_trade()\n        self.desk._record_exec_path(pos, live_book(no={49.0: 100.0}), 51.0, 1001.0)\n        self.desk._flush_exec_path(pos)\n        self.assertEqual(len(self.path_rows(pos.tid)), 1)\n\n        pos.exec_path = [{\n            "kind": "position", "trade_id": pos.tid, "signal_id": pos.signal_id,\n            "event": pos.event, "market": pos.market, "side": pos.side,\n            "strategy": pos.strategy, "anchor_ts": pos.entry_ts, "dt_ms": 99.0,\n            "bid": 99.0, "bid_size": 1.0, "exec_px": 99.0, "qty": 1.0,\n            "sample_seq": 1, "availability": "quote", "terminal": 0,\n        }]\n        before = list(pos.exec_path)\n\n        self.assertFalse(self.desk.close(pos, 60.0, "target"))\n        self.assertEqual(self.trade_row(pos.tid)["status"], "open")\n        self.assertIn(pos.tid, self.desk.positions)\n        self.assertEqual(pos.exec_path, before, "the conflicting buffer lost ownership")\n        self.assertEqual(len(self.path_rows(pos.tid)), 1, "conflict committed a partial close")\n        self.assertTrue(any("path_sequence_conflict" in message for _, message in self.errors))\n'''
replace("tests/test_close_ownership.py", old, new)

# Signal fixtures now model a durably-started watch and the new per-owner latch.
replace(
    "tests/test_signal_path_ownership.py",
    '''        eng.signal_path_fault = None\n        self.engine = eng\n''',
    '''        eng.signal_path_fault = None\n        eng._signal_path_failed_owners = set()\n        self.engine = eng\n''',
)
replace(
    "tests/test_signal_path_ownership.py",
    '''            "ref": 40.0, "ext": 60.0, "outcome": "unconfirmed", "detail": {},\n        })\n''',
    '''            "ref": 40.0, "ext": 60.0, "outcome": "unconfirmed", "detail": {},\n            "forward_path_started_ts": 1000.0,\n        })\n''',
)

# Browser fixture: terminal is an end timestamp, never a fabricated quote.
replace(
    "tests/test_dashboard_browser.py",
    '''    {"dt_ms": 2500.0, "bid": 72.0, "bid_size": None, "exec_px": None, "qty": 10.0,\n     "availability": "terminal", "terminal": 1, "sample_seq": 4},\n''',
    '''    {"dt_ms": 2500.0, "bid": None, "bid_size": None, "exec_px": None, "qty": 10.0,\n     "availability": "terminal", "terminal": 1, "sample_seq": 4},\n''',
)
replace(
    "tests/test_dashboard_browser.py",
    '''    "samples": 3, "samples_total": 4, "samples_priced": 3, "segments": 2,\n    "gap_count": 1, "gap_duration_ms": 1000, "unknown_gap_duration_ms": 0,\n    "first_bid": 90.0, "last_bid": 72.0, "peak_bid": 90.0, "peak_dt_ms": 0.0,\n    "peak_bid_size": 40.0, "peak_exec_px": 90.0, "ms_at_peak": 1000,\n    "trough_bid": 70.0, "trough_dt_ms": 2000.0, "path_travelled_c": 2.0,\n    "displacement_c": 18.0, "path_efficiency": None, "span_ms": 2500,\n''',
    '''    "samples": 2, "samples_total": 4, "samples_priced": 2, "segments": 2,\n    "gap_count": 1, "gap_duration_ms": 1000, "unknown_gap_duration_ms": 0,\n    "first_bid": 90.0, "last_bid": 70.0, "peak_bid": 90.0, "peak_dt_ms": 0.0,\n    "peak_bid_size": 40.0, "peak_exec_px": 90.0, "ms_at_peak": 1000,\n    "trough_bid": 70.0, "trough_dt_ms": 2000.0, "path_travelled_c": 0.0,\n    "displacement_c": 20.0, "path_efficiency": None, "span_ms": 2500,\n''',
)

# Chart domain includes the terminal timestamp; it gets a vertical end marker,
# not a price point. Add explicit legend/semantics for browser acceptance.
replace(
    "static/app.js",
    '''  const segments = segmentsFromSamples(samples);\n  const priced = segments.flat();\n  if (priced.length < 2) return "";\n  const xs = priced.map(row => row.dt_ms), ys = priced.map(row => row.bid);\n  const minX = Math.min(...xs), maxX = Math.max(...xs);\n''',
    '''  const segments = segmentsFromSamples(samples);\n  const priced = segments.flat();\n  if (priced.length < 2) return "";\n  const allTimes = samples.map(row => row.dt_ms).filter(finite);\n  const xs = priced.map(row => row.dt_ms), ys = priced.map(row => row.bid);\n  const minX = Math.min(...allTimes), maxX = Math.max(...allTimes);\n''',
)
replace(
    "static/app.js",
    '''  const entryY = py(trade.entry_px).toFixed(1);\n  const peakX = px(summary.peak_dt_ms).toFixed(1), peakY = py(summary.peak_bid).toFixed(1);\n''',
    '''  const entryY = py(trade.entry_px).toFixed(1);\n  const peakX = px(summary.peak_dt_ms).toFixed(1), peakY = py(summary.peak_bid).toFixed(1);\n  const terminal = [...samples].reverse().find(row => row.terminal || row.availability === "terminal");\n  const terminalX = terminal && finite(terminal.dt_ms) ? px(terminal.dt_ms).toFixed(1) : null;\n  const terminalMarker = terminalX == null ? "" : `<line class="bid-path-terminal" x1="${terminalX}" x2="${terminalX}" y1="${pad}" y2="${height - pad}"/>`;\n''',
)
replace(
    "static/app.js",
    '''  return `<div class="bid-path"><div class="bid-path-head"><span>Executable bid path</span><strong>${integer(summary.samples)} quotes over ${escapeHtml(duration((summary.span_ms || 0) / 1000))}${gapNote}${truncatedNote}</strong></div><svg class="bid-path-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Held-side executable bid from entry to exit"><line class="bid-path-entry" x1="${pad}" x2="${width - pad}" y1="${entryY}" y2="${entryY}"/><path class="bid-path-line" d="${d}"/><circle class="bid-path-peak" cx="${peakX}" cy="${peakY}" r="3.5"/></svg><div class="bid-path-facts"><div><span>Peak held</span><strong>${escapeHtml(relativeMs(summary.ms_at_peak))}</strong></div><div><span>Fillable at peak</span><strong>${escapeHtml(reachable)}</strong></div><div><span>Round trip</span><strong>${cents(summary.path_travelled_c)} · ${escapeHtml(efficiency)}</strong></div></div></div>`;\n''',
    '''  return `<div class="bid-path"><div class="bid-path-head"><span>Executable bid path</span><strong>${integer(summary.samples)} quotes over ${escapeHtml(duration((summary.span_ms || 0) / 1000))}${gapNote}${truncatedNote}</strong></div><svg class="bid-path-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Held-side executable bid path with entry, peak, gaps and end-of-availability"><line class="bid-path-entry" x1="${pad}" x2="${width - pad}" y1="${entryY}" y2="${entryY}"/>${terminalMarker}<path class="bid-path-line" d="${d}"/><circle class="bid-path-peak" cx="${peakX}" cy="${peakY}" r="3.5"/></svg><div class="bid-path-legend" aria-label="Path chart legend"><span><i class="legend-entry"></i>Entry price</span><span><i class="legend-path"></i>Executable bid</span><span><i class="legend-peak"></i>Peak executable bid</span><span><i class="legend-terminal"></i>End of availability</span></div><p class="bid-path-note">The terminal marker is time only; the exit price is not plotted as a bid.</p><div class="bid-path-facts"><div><span>Peak held</span><strong>${escapeHtml(relativeMs(summary.ms_at_peak))}</strong></div><div><span>Fillable at peak</span><strong>${escapeHtml(reachable)}</strong></div><div><span>Round trip</span><strong>${cents(summary.path_travelled_c)} · ${escapeHtml(efficiency)}</strong></div></div></div>`;\n''',
)

replace(
    "static/style.css",
    '''.bid-path-entry { stroke: var(--subtle); stroke-width: 1; stroke-dasharray: 3 3; }\n.bid-path-peak { fill: var(--green); }\n.bid-path-facts {\n''',
    '''.bid-path-entry { stroke: var(--subtle); stroke-width: 1; stroke-dasharray: 3 3; }\n.bid-path-terminal { stroke: var(--yellow); stroke-width: 1; stroke-dasharray: 2 3; }\n.bid-path-peak { fill: var(--green); }\n.bid-path-legend { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; color: var(--subtle); font-size: .72rem; }\n.bid-path-legend span { display: inline-flex; align-items: center; gap: 5px; }\n.bid-path-legend i { display: inline-block; width: 14px; height: 2px; background: var(--blue); }\n.bid-path-legend .legend-entry { background: var(--subtle); border-top: 1px dashed var(--subtle); }\n.bid-path-legend .legend-peak { width: 7px; height: 7px; border-radius: 50%; background: var(--green); }\n.bid-path-legend .legend-terminal { background: var(--yellow); border-top: 1px dashed var(--yellow); }\n.bid-path-note { margin: 6px 0 0; color: var(--subtle); font-size: .72rem; }\n.bid-path-facts {\n''',
)
replace(
    "static/style.css",
    '''  .export-actions .button, #export-button, #export-audit-button, #export-full-button, #export-cancel-button {\n''',
    '''  .export-actions .button, #export-button, #export-audit-button, #export-full-button, #export-archive-button, #export-cancel-button {\n''',
)

# Browser contract for terminal, markers and legend.
needle = '''    # -------------------------------------------------------------- responsive\n'''
addition = '''    def test_terminal_is_time_only_and_chart_legend_explains_markers(self):\n        page = self.open_dashboard()\n        self.show_trades_tab(page)\n        button = page.wait_for_selector("[data-load-path]", timeout=10000)\n        with page.expect_request(lambda r: "/path" in r.url, timeout=10000):\n            button.click()\n        page.wait_for_selector("#trade-list svg.bid-path-svg", timeout=10000)\n\n        terminal = page.wait_for_selector("#trade-list .bid-path-terminal", timeout=5000)\n        self.assertTrue(terminal.is_visible(), "terminal timestamp has no end marker")\n        path_d = page.get_attribute("#trade-list path.bid-path-line", "d") or ""\n        terminal_x = float(terminal.get_attribute("x1"))\n        last_path_x = max(float(token.split(",")[0][1:]) for token in path_d.split())\n        self.assertGreater(terminal_x, last_path_x,\n                           "terminal time was plotted as a price point instead of an end marker")\n\n        legend = page.wait_for_selector("#trade-list .bid-path-legend", timeout=5000)\n        text = legend.inner_text()\n        for label in ("Entry price", "Executable bid", "Peak executable bid", "End of availability"):\n            self.assertIn(label, text)\n        note = page.inner_text("#trade-list .bid-path-note")\n        self.assertIn("exit price is not plotted as a bid", note)\n\n'''
replace("tests/test_dashboard_browser.py", needle, addition + needle)

Path("tools/apply_pr13_acceptance.py").unlink()
Path(".github/workflows/apply-pr13-acceptance.yml").unlink()
