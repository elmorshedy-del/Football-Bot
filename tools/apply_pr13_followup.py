from pathlib import Path


def replace(path, old, new):
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one replacement, found {count}")
    target.write_text(text.replace(old, new, 1))


# app/store.py — strict exactly-once path persistence, restart marker, cap, terminal semantics.
replace(
    "app/store.py",
    '''    try:\n        _conn.execute("ALTER TABLE signals ADD COLUMN forward_path_finalized REAL")\n    except sqlite3.OperationalError:\n        pass\n''',
    '''    try:\n        _conn.execute("ALTER TABLE signals ADD COLUMN forward_path_finalized REAL")\n    except sqlite3.OperationalError:\n        pass\n    # Written in the same signal INSERT transaction when forward-path capture is\n    # enabled.  A started-but-unfinalized watch is recoverable even when the\n    # process died before its first quote row reached SQLite.\n    try:\n        _conn.execute("ALTER TABLE signals ADD COLUMN forward_path_started_ts REAL")\n    except sqlite3.OperationalError:\n        pass\n''',
)

replace(
    "app/store.py",
    '''BID_PATH_MAX_SAMPLES = 4000\n\n\ndef insert_bid_path(rows):\n''',
    '''BID_PATH_MAX_SAMPLES = 4000\n\n\nclass PathSequenceConflict(RuntimeError):\n    """A path sequence key already exists with a different durable payload."""\n\n\nclass PathSampleCapExceeded(RuntimeError):\n    """Writing this path would exceed the durable 4,000-row invariant."""\n\n\ndef insert_bid_path(rows):\n''',
)

replace(
    "app/store.py",
    '''def insert_bid_path(rows):\n    """Persist one buffered path in a single transaction.\n\n    The caller accumulates samples in memory for the life of a position or a\n    decline window and flushes once.  Committing per sample would add a\n    synchronous fsync to the asyncio hot path for every book update.\n\n    Returns the number of rows that actually became durable, which is NOT the\n    input length: `INSERT OR IGNORE` silently drops a row whose sequence key\n    already exists.  Returning the input length let a caller clear its buffer\n    on a batch that wrote nothing.\n    """\n    if not rows:\n        return 0\n    payload = _bid_path_payload(rows)\n    with _lock:\n        before = _conn.total_changes\n        _conn.executemany(_BID_PATH_INSERT, payload)\n        written = _conn.total_changes - before\n        _conn.commit()\n    return written\n''',
    '''def insert_bid_path(rows):\n    """Persist one buffered path atomically with strict retry validation.\n\n    A duplicate sequence key is accepted only when every persisted field is\n    identical to the durable row.  Conflicting payloads raise a stable error,\n    leave the caller's buffer owned, and never turn a short write into success.\n    The return value is the number of input rows proven durable (new or exact\n    idempotent retries).\n    """\n    if not rows:\n        return 0\n    with _lock:\n        try:\n            pending = _validated_new_path_payloads(rows)\n            _enforce_path_caps(pending)\n            if pending:\n                _conn.executemany(_BID_PATH_INSERT, pending)\n            _conn.commit()\n        except Exception:\n            _conn.rollback()\n            raise\n    return len(rows)\n''',
)

replace(
    "app/store.py",
    '''_BID_PATH_INSERT = """INSERT OR IGNORE INTO bid_path_samples(\n''',
    '''_BID_PATH_INSERT = """INSERT INTO bid_path_samples(\n''',
)

replace(
    "app/store.py",
    '''def _bid_path_payload(rows):\n    return [\n        (\n            row.get("kind"), row.get("trade_id"), row.get("signal_id"),\n            row.get("event"), row.get("market"), row.get("side"),\n            row.get("strategy"), row.get("anchor_ts"), row.get("dt_ms"),\n            row.get("bid"), row.get("bid_size"), row.get("exec_px"),\n            row.get("qty"), _mode,\n            row.get("sample_seq"),\n            row.get("availability") or ("quote" if _is_priced(row.get("bid")) else "gap"),\n            1 if row.get("terminal") else 0,\n        )\n        for row in rows\n    ]\n\n\ndef _read_rows(cursor):\n''',
    '''def _bid_path_payload(rows):\n    return [\n        (\n            row.get("kind"), row.get("trade_id"), row.get("signal_id"),\n            row.get("event"), row.get("market"), row.get("side"),\n            row.get("strategy"), row.get("anchor_ts"), row.get("dt_ms"),\n            row.get("bid"), row.get("bid_size"), row.get("exec_px"),\n            row.get("qty"), _mode,\n            row.get("sample_seq"),\n            row.get("availability") or ("quote" if _is_priced(row.get("bid")) else "gap"),\n            1 if row.get("terminal") else 0,\n        )\n        for row in rows\n    ]\n\n\n_BID_PATH_FIELD_NAMES = (\n    "kind", "trade_id", "signal_id", "event", "market", "side", "strategy",\n    "anchor_ts", "dt_ms", "bid", "bid_size", "exec_px", "qty", "mode",\n    "sample_seq", "availability", "terminal",\n)\n_BID_PATH_FIELD_SQL = ",".join(_BID_PATH_FIELD_NAMES)\n\n\ndef _payload_map(payload):\n    return dict(zip(_BID_PATH_FIELD_NAMES, payload))\n\n\ndef _path_sequence_key(payload):\n    row = _payload_map(payload)\n    seq = row.get("sample_seq")\n    if seq is None:\n        return None\n    if row.get("trade_id") is not None:\n        return ("trade_id", row["trade_id"], row.get("kind"), seq)\n    if row.get("signal_id") is not None:\n        return ("signal_id", row["signal_id"], row.get("kind"), seq)\n    return None\n\n\ndef _durable_payload_for_key(key):\n    if key is None:\n        return None\n    owner_column, owner_id, kind, seq = key\n    cursor = _conn.execute(\n        f"SELECT {_BID_PATH_FIELD_SQL} FROM bid_path_samples "\n        f"WHERE {owner_column}=? AND kind=? AND sample_seq=? LIMIT 1",\n        (owner_id, kind, seq),\n    )\n    return cursor.fetchone()\n\n\ndef _sequence_conflict(key):\n    owner_column, owner_id, kind, seq = key\n    return PathSequenceConflict(\n        f"path_sequence_conflict: {owner_column}={owner_id} kind={kind!r} "\n        f"sample_seq={seq}"\n    )\n\n\ndef _validated_new_path_payloads(rows):\n    """Return only genuinely new rows after proving duplicate keys idempotent."""\n    pending = []\n    pending_by_key = {}\n    for payload in _bid_path_payload(rows or []):\n        key = _path_sequence_key(payload)\n        if key is None:\n            pending.append(payload)\n            continue\n        durable = _durable_payload_for_key(key)\n        if durable is not None:\n            if tuple(durable) != tuple(payload):\n                raise _sequence_conflict(key)\n            continue\n        prior = pending_by_key.get(key)\n        if prior is not None:\n            if tuple(prior) != tuple(payload):\n                raise _sequence_conflict(key)\n            continue\n        pending_by_key[key] = payload\n        pending.append(payload)\n    return pending\n\n\ndef _enforce_path_caps(payloads):\n    """Reject a batch before INSERT when its durable owner would exceed the cap."""\n    grouped = {}\n    for payload in payloads:\n        row = _payload_map(payload)\n        if row.get("trade_id") is not None:\n            key = ("trade_id", row["trade_id"], None)\n        elif row.get("signal_id") is not None:\n            # Signal ids also appear on position rows.  A decline watch owns only\n            # its own kind, so position history cannot consume the watch's cap.\n            key = ("signal_id", row["signal_id"], row.get("kind"))\n        else:\n            continue\n        grouped[key] = grouped.get(key, 0) + 1\n    for (owner_column, owner_id, kind), incoming in grouped.items():\n        if kind is None:\n            current = _conn.execute(\n                f"SELECT COUNT(*) FROM bid_path_samples WHERE {owner_column}=?",\n                (owner_id,),\n            ).fetchone()[0]\n        else:\n            current = _conn.execute(\n                f"SELECT COUNT(*) FROM bid_path_samples WHERE {owner_column}=? AND kind=?",\n                (owner_id, kind),\n            ).fetchone()[0]\n        if current + incoming > BID_PATH_MAX_SAMPLES:\n            raise PathSampleCapExceeded(\n                f"path_sample_cap_exhausted: {owner_column}={owner_id} "\n                f"durable={current} incoming={incoming} cap={BID_PATH_MAX_SAMPLES}"\n            )\n\n\ndef _read_rows(cursor):\n''',
)

replace(
    "app/store.py",
    '''    if rows:\n        _conn.executemany(_BID_PATH_INSERT, _bid_path_payload(rows))\n    samples = _read_rows(_conn.execute(\n''',
    '''    pending = _validated_new_path_payloads(rows or [])\n    for payload in pending:\n        if _payload_map(payload).get(owner_column) != owner_id:\n            raise ValueError(\n                f"path owner mismatch: expected {owner_column}={owner_id}"\n            )\n    _enforce_path_caps(pending)\n    if pending:\n        _conn.executemany(_BID_PATH_INSERT, pending)\n    samples = _read_rows(_conn.execute(\n''',
)

replace(
    "app/store.py",
    '''def unfinalized_signal_paths():\n    """Signals that have forward-path rows but never recorded a finalization.\n\n    These are watches whose process died inside the observation window.  Their\n    in-memory tail is gone, so they are rebuilt and labelled incomplete rather\n    than presented as a complete path.\n    """\n    scope, scope_args = mode_clause("s")\n    return q(\n        "SELECT DISTINCT s.id, s.local_ts FROM signals s"\n        " JOIN bid_path_samples p ON p.signal_id=s.id AND p.kind='decline'"\n        f" WHERE s.forward_path_finalized IS NULL{scope} ORDER BY s.id",\n        scope_args,\n    )\n''',
    '''def unfinalized_signal_paths():\n    """Forward watches that started durably but never recorded finalization."""\n    scope, scope_args = mode_clause("s")\n    return q(\n        "SELECT s.id, s.local_ts, s.market, s.event FROM signals s"\n        f" WHERE s.forward_path_started_ts IS NOT NULL "\n        f"AND s.forward_path_finalized IS NULL{scope} ORDER BY s.id",\n        scope_args,\n    )\n''',
)

replace(
    "app/store.py",
    '''    for row in rows:\n        dt = row.get("dt_ms")\n        if _is_priced(row.get("bid")):\n''',
    '''    for row in rows:\n        dt = row.get("dt_ms")\n        if row.get("terminal") or row.get("availability") == "terminal":\n            # A terminal is an end timestamp, not a quote and not an outage.\n            # It closes the current availability segment without contributing\n            # price, travel, peak/trough, or samples_priced.\n            if current is not None:\n                current["close_dt"] = dt\n                current = None\n            continue\n        if _is_priced(row.get("bid")):\n''',
)

replace(
    "app/store.py",
    '''def insert_signal(s):\n    cur = ex("""INSERT INTO signals(ts_ms,local_ts,market,event,series,dir,dl,levels,size,\n                ref,ext,conf_lag_ms,late,outcome,detail,mode,match_clock_snapshot)\n                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",\n             (s["ts_ms"], s["local_ts"], s["market"], s["event"], s["series"], s["dir"],\n              s["dl"], s["levels"], s["size"], s["ref"], s["ext"], s.get("conf_lag_ms"),\n              1 if s.get("late") else 0, s["outcome"], json.dumps(s.get("detail") or {}), _mode,\n              _stamp_text(s.get("match_clock_snapshot"))))\n    return cur.lastrowid\n''',
    '''def insert_signal(s):\n    cur = ex("""INSERT INTO signals(ts_ms,local_ts,market,event,series,dir,dl,levels,size,\n                ref,ext,conf_lag_ms,late,outcome,detail,mode,match_clock_snapshot,\n                forward_path_started_ts)\n                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",\n             (s["ts_ms"], s["local_ts"], s["market"], s["event"], s["series"], s["dir"],\n              s["dl"], s["levels"], s["size"], s["ref"], s["ext"], s.get("conf_lag_ms"),\n              1 if s.get("late") else 0, s["outcome"], json.dumps(s.get("detail") or {}), _mode,\n              _stamp_text(s.get("match_clock_snapshot")),\n              s.get("forward_path_started_ts")))\n    return cur.lastrowid\n''',
)

# app/paper.py — terminal is a timestamp only, never a fabricated quote.
replace(
    "app/paper.py",
    '''            # The executed exit price is not an observed book quote; bid_size\n            # and exec_px stay null so the two stay semantically distinct.\n            "bid": exit_px, "bid_size": None, "exec_px": None,\n''',
    '''            # The executed exit price belongs on trades.exit_px.  A terminal\n            # row is only the end timestamp for availability and must never be\n            # relabelled as an executable quote.\n            "bid": None, "bid_size": None, "exec_px": None,\n''',
)

# app/engine.py — durable zero-row watches, retained failed rebuilds, latched owner faults.
replace(
    "app/engine.py",
    '''        self.signal_path_fault = None\n        self.errors = deque(maxlen=50)\n''',
    '''        self.signal_path_fault = None\n        self._signal_path_failed_owners = set()\n        self.errors = deque(maxlen=50)\n''',
)

replace(
    "app/engine.py",
    '''        for watch in self._signal_paths:\n            if watch["market"] != ticker:\n''',
    '''        for watch in self._signal_paths:\n            if watch.get("retry_only"):\n                continue\n            if watch["market"] != ticker:\n''',
)

replace(
    "app/engine.py",
    '''    def _flush_signal_path(self, watch, final=False):\n''',
    '''    def _mark_signal_path_failure(self, signal_id, exc):\n        self._signal_path_failed_owners.add(signal_id)\n        self.signal_path_fault = "signal_path_persistence_failed"\n        self._record_error("signal_path", exc)\n\n    def _mark_signal_path_success(self, signal_id):\n        self._signal_path_failed_owners.discard(signal_id)\n        if not self._signal_path_failed_owners:\n            self.signal_path_fault = None\n\n    def _flush_signal_path(self, watch, final=False):\n''',
)

replace(
    "app/engine.py",
    '''        except Exception as exc:\n            # Keep the rows for the next attempt rather than dropping them.\n            self._record_error("signal_path", exc)\n            return False\n        if isinstance(written, int) and written < len(rows):\n            self._record_error("signal_path", RuntimeError(\n                f"signal {watch['signal_id']}: {len(rows) - written} of {len(rows)} "\n                "path rows collided with an existing sequence key",\n            ))\n        watch["rows"] = watch["rows"][len(rows):]\n        return True\n''',
    '''        except Exception as exc:\n            # Keep the rows and the owning watch. A sequence conflict is a\n            # current health fault until this exact owner later commits.\n            self._mark_signal_path_failure(watch["signal_id"], exc)\n            return False\n        if isinstance(written, int) and written < len(rows):\n            exc = RuntimeError(\n                f"signal {watch['signal_id']}: short path persistence "\n                f"{written}/{len(rows)}"\n            )\n            self._mark_signal_path_failure(watch["signal_id"], exc)\n            return False\n        watch["rows"] = watch["rows"][len(rows):]\n        self._mark_signal_path_success(watch["signal_id"])\n        return True\n''',
)

replace(
    "app/engine.py",
    '''        except Exception as exc:\n            self.signal_path_fault = "signal_path_persistence_failed"\n            self._record_error("signal_path", exc)\n            return False\n        watch["rows"] = []\n        self.signal_path_fault = None\n        return True\n''',
    '''        except Exception as exc:\n            self._mark_signal_path_failure(watch["signal_id"], exc)\n            return False\n        watch["rows"] = []\n        self._mark_signal_path_success(watch["signal_id"])\n        return True\n''',
)

replace(
    "app/engine.py",
    '''        rebuilt = 0\n        for row in store.unfinalized_signal_paths():\n            watch = {"signal_id": row["id"], "rows": [], "dropped": 0}\n            if self._finalize_signal_path(\n                watch, incomplete_reason="in_memory_tail_lost_on_restart",\n            ):\n                rebuilt += 1\n        return rebuilt\n''',
    '''        rebuilt = 0\n        for row in store.unfinalized_signal_paths():\n            watch = {\n                "signal_id": row["id"], "rows": [], "dropped": 0,\n                "market": row.get("market"), "event": row.get("event"),\n                "expires_at": 0.0, "retry_only": True,\n            }\n            if self._finalize_signal_path(\n                watch, incomplete_reason="in_memory_tail_lost_on_restart",\n            ):\n                rebuilt += 1\n            else:\n                # Startup failure must retain an owned retry object.  A local\n                # dictionary that falls out of scope cannot ever recover.\n                self._signal_paths.append(watch)\n        return rebuilt\n''',
)

replace(
    "app/engine.py",
    '''            "detail": cand.get("detail") or {},\n            "match_clock_snapshot": stamp,\n        })\n''',
    '''            "detail": cand.get("detail") or {},\n            "match_clock_snapshot": stamp,\n            "forward_path_started_ts": (\n                cand.get("local_ts") or time.time()\n                if config.SIGNAL_PATH_WINDOW_S else None\n            ),\n        })\n''',
)

replace(
    "app/engine.py",
    '''            now = time.time()\n            for p in [p for p in self.pending if now >= p["deadline"]]:\n''',
    '''            now = time.time()\n            # Also retries startup watches when no new book frame arrives.\n            self._expire_signal_paths(now)\n            for p in [p for p in self.pending if now >= p["deadline"]]:\n''',
)

replace(
    "app/engine.py",
    '''            if config.PAPER_EXECUTION_V2:\n                self.desk.restore_open_positions(store.load_open_paper_positions())\n                # Watches whose process died inside their observation window\n                # are finalized and labelled incomplete, so no signal is left\n                # with a half-written forward path and no finalization marker.\n                rebuilt = self.rebuild_signal_paths()\n                if rebuilt:\n                    store.log_event(\n                        "paper",\n                        f"rebuilt {rebuilt} unfinalized signal forward path(s) "\n                        "as incomplete after restart",\n                    )\n                for pos in self.desk.positions.values():\n                    self._remember_fill({\n                        "strategy": pos.strategy,\n                        "ticker": pos.market,\n                        "ts_ms": pos.entry_ts * 1000.0,\n                    })\n''',
    '''            if config.PAPER_EXECUTION_V2:\n                self.desk.restore_open_positions(store.load_open_paper_positions())\n            # Signal collection is independent of realistic paper execution.\n            # Always reconcile started-but-unfinalized forward watches.\n            rebuilt = self.rebuild_signal_paths()\n            if rebuilt:\n                store.log_event(\n                    "paper",\n                    f"rebuilt {rebuilt} unfinalized signal forward path(s) "\n                    "as incomplete after restart",\n                )\n            if config.PAPER_EXECUTION_V2:\n                for pos in self.desk.positions.values():\n                    self._remember_fill({\n                        "strategy": pos.strategy,\n                        "ticker": pos.market,\n                        "ts_ms": pos.entry_ts * 1000.0,\n                    })\n''',
)

# app/main.py — mode-safe links/open positions and explicit all-mode archive export.
replace(
    "app/main.py",
    '''        row["forward_path_url"] = f"/api/signals/{row['id']}/path"\n''',
    '''        row["forward_path_url"] = f"/api/signals/{row['id']}/path?mode={selector}"\n''',
)
replace(
    "app/main.py",
    '''        r["bid_path_url"] = f"/api/trades/{r['id']}/path"\n''',
    '''        r["bid_path_url"] = f"/api/trades/{r['id']}/path?mode={selector}"\n''',
)

replace(
    "app/main.py",
    '''    rows = store.q(\n        f"SELECT * FROM trades WHERE 1=1{scope} ORDER BY id DESC LIMIT ?",\n        (*scope_args, limit),\n    )\n    _label_modes(rows)\n    opens = [engine.desk.pos_dict(p, p.best_bid) for p in engine.desk.positions.values()]\n    signal_rows = _rows_by_signal_id(\n        [row.get("signal_id") for row in rows] + [row.get("signal_id") for row in opens],\n        mode=selector,\n    )\n''',
    '''    open_rows = store.q(\n        f"SELECT * FROM trades WHERE status='open'{scope} ORDER BY id",\n        scope_args,\n    )\n    closed_rows = store.q(\n        f"SELECT * FROM trades WHERE status='closed'{scope} ORDER BY id DESC LIMIT ?",\n        (*scope_args, limit),\n    )\n    rows = open_rows + closed_rows\n    _label_modes(rows)\n    live_marks = {}\n    if engine is not None and selector in {engine.mode, "all"}:\n        live_marks = {\n            p.tid: engine.desk.pos_dict(p, p.best_bid)\n            for p in engine.desk.positions.values()\n        }\n    signal_rows = _rows_by_signal_id(\n        [row.get("signal_id") for row in rows], mode=selector,\n    )\n''',
)

replace(
    "app/main.py",
    '''        r["bid_path_summary"] = json_object(r.get("bid_path_summary")) or None\n        r["bid_path_url"] = f"/api/trades/{r['id']}/path?mode={selector}"\n    for row in opens:\n        signal = signal_rows.get(row.get("signal_id"))\n        row.update(_display_names(\n            row.get("market"), row.get("event"),\n            (signal or {}).get("market_title"), (signal or {}).get("market_leg"),\n            (signal or {}).get("market_game"),\n        ))\n        if signal:\n            signal["detail"] = json_object(signal.get("detail"))\n            signal["strategy"] = row.get("strategy") or signal_strategy(signal)\n            row["trigger"] = build_trigger(signal)\n            row["timing"] = timing_fields(signal, row)\n            row["schedule_window"] = schedule_window(signal)\n            row["matched_event"] = match_signal_event(signal, observations)\n            row["match_clock"] = parse_stored_stamp(signal.get("match_clock_snapshot"))\n    return {"open": opens, "closed": [r for r in rows if r["status"] == "closed"]}\n''',
    '''        r["bid_path_summary"] = json_object(r.get("bid_path_summary")) or None\n        r["bid_path_url"] = f"/api/trades/{r['id']}/path?mode={selector}"\n        mark = live_marks.get(r["id"])\n        if r.get("status") == "open" and mark and r.get("mode") == engine.mode:\n            # Storage is the selector source of truth; in-memory state may only\n            # enrich the matching active-mode parent, never select rows.\n            for key in ("bid", "upnl", "size", "initial_size",\n                        "max_executable_bid", "max_executable_bid_ts",\n                        "mfe_c", "high_after_entry_s"):\n                if key in mark:\n                    r[key] = mark[key]\n    return {\n        "open": [r for r in rows if r["status"] == "open"],\n        "closed": [r for r in rows if r["status"] == "closed"],\n    }\n''',
)

replace(
    "app/main.py",
    '''        "scope": job.get("scope") or "full",\n        "status": job["status"],\n''',
    '''        "scope": job.get("scope") or "full",\n        "mode_selector": job.get("mode_selector"),\n        "all_mode_archival_export": bool(job.get("all_modes")),\n        "status": job["status"],\n''',
)

replace(
    "app/main.py",
    '''def _new_export_job(scope, raw_paths):\n    total_bytes = 0\n    total_segments = 0\n    if scope == "full":\n''',
    '''def _new_export_job(scope, raw_paths, mode_selector):\n    total_bytes = 0\n    total_segments = 0\n    if scope in {"full", "archive"}:\n''',
)
replace(
    "app/main.py",
    '''        "scope": scope,\n        "status": "queued",\n''',
    '''        "scope": scope,\n        "mode_selector": mode_selector,\n        "all_modes": scope == "archive",\n        "status": "queued",\n''',
)

replace(
    "app/main.py",
    '''        path, _manifest = await asyncio.to_thread(\n            exporter.build_study_bundle, None, mode, raw_paths, snapshot_path,\n            scope == "full", progress, cancel_check, scope, boundary,\n        )\n''',
    '''        exporter_scope = "full" if scope == "archive" else scope\n        path, _manifest = await asyncio.to_thread(\n            exporter.build_study_bundle, None, mode, raw_paths, snapshot_path,\n            exporter_scope == "full", progress, cancel_check, exporter_scope, boundary,\n            scope == "archive",\n        )\n''',
)

replace(
    "app/main.py",
    '''async def prepare_study_export(scope: str = "audit"):\n    """Start one non-blocking export job and return a pollable identifier."""\n    scope = (scope or "audit").lower()\n    if scope not in {"audit", "full"}:\n        raise HTTPException(status_code=400, detail="scope must be audit or full")\n''',
    '''async def prepare_study_export(scope: str = "audit", mode: str | None = None):\n    """Start one non-blocking export job and return a pollable identifier."""\n    scope = (scope or "audit").lower()\n    if scope not in {"audit", "full", "archive"}:\n        raise HTTPException(status_code=400, detail="scope must be audit, full, or archive")\n    selector = _mode_selector(mode)\n    if scope == "archive":\n        if mode is not None and selector != "all":\n            raise HTTPException(status_code=400, detail="archive scope requires mode=all")\n        selector = "all"\n    elif selector == "all":\n        raise HTTPException(status_code=400, detail="use scope=archive for all-mode export")\n''',
)

replace(
    "app/main.py",
    '''        return raw_paths, snapshot_path, _new_export_job(scope, raw_paths)\n''',
    '''        return raw_paths, snapshot_path, _new_export_job(scope, raw_paths, selector)\n''',
)
replace(
    "app/main.py",
    '''            job["job_id"], engine.mode, raw_paths, snapshot_path, scope, boundary,\n''',
    '''            job["job_id"], selector, raw_paths, snapshot_path, scope, boundary,\n''',
)

# static/index.html + static/app.js — operator-visible all-mode archival product.
replace(
    "static/index.html",
    '''            <button id="export-full-button" class="button" type="button" data-export-scope="full">Prepare full raw handoff</button>\n            <button id="export-cancel-button" class="button quiet" type="button" hidden>Cancel full export</button>\n''',
    '''            <button id="export-full-button" class="button" type="button" data-export-scope="full">Prepare full raw handoff</button>\n            <button id="export-archive-button" class="button" type="button" data-export-scope="archive">Prepare all-mode archive</button>\n            <button id="export-cancel-button" class="button quiet" type="button" hidden>Cancel active export</button>\n''',
)

replace(
    "static/app.js",
    '''  "export-full-button": "Prepare full raw handoff",\n};\n''',
    '''  "export-full-button": "Prepare full raw handoff",\n  "export-archive-button": "Prepare all-mode archive",\n};\n''',
)

replace(
    "static/app.js",
    '''async function downloadExport(scope = "audit") {\n  scope = scope === "full" ? "full" : "audit";\n''',
    '''async function downloadExport(scope = "audit") {\n  scope = ["audit", "full", "archive"].includes(scope) ? scope : "audit";\n''',
)
replace(
    "static/app.js",
    '''  if (!token) token = window.prompt(scope === "full" ?\n    "Admin token for the full raw handoff (multi-GB export, cancellable)" :\n    "Admin token for the audit bundle download");\n''',
    '''  if (!token) token = window.prompt(scope === "archive" ?\n    "Admin token for the all-mode archival handoff" : scope === "full" ?\n    "Admin token for the full raw handoff (multi-GB export, cancellable)" :\n    "Admin token for the audit bundle download");\n''',
)
replace(
    "static/app.js",
    '''  if (scope === "full" && cancelButton) cancelButton.hidden = false;\n''',
    '''  if (scope !== "audit" && cancelButton) cancelButton.hidden = false;\n''',
)
replace(
    "static/app.js",
    '''  const buttonId = scope === "full" ? "export-full-button" : "export-audit-button";\n  const button = byId(buttonId) || byId("export-button");\n  const scopeLabel = scope === "full" ? "Full raw handoff" : "Audit bundle";\n''',
    '''  const buttonId = scope === "archive" ? "export-archive-button" : scope === "full" ? "export-full-button" : "export-audit-button";\n  const button = byId(buttonId) || byId("export-button");\n  const scopeLabel = scope === "archive" ? "All-mode archive" : scope === "full" ? "Full raw handoff" : "Audit bundle";\n''',
)
replace(
    "static/app.js",
    '''    if (scope === "full") refreshRawSegments();\n''',
    '''    if (scope !== "audit") refreshRawSegments();\n''',
)
replace(
    "static/app.js",
    '''  // Prefer cancelling the full job (audit bundles finish in seconds).\n  const scope = activeExports.has("full") ? "full" : activeExports.keys().next().value;\n''',
    '''  // Prefer cancelling a heavy raw/archive job (audit bundles finish quickly).\n  const scope = activeExports.has("archive") ? "archive" : activeExports.has("full") ? "full" : activeExports.keys().next().value;\n''',
)

# Update the old terminal expectation so existing tests reflect terminal semantics.
replace(
    "tests/test_bid_path.py",
    '''        bids = [row["bid"] for row in pos.exec_path]\n        self.assertEqual(bids, [90.0, None, 70.0])\n''',
    '''        bids = [row["bid"] for row in pos.exec_path]\n        self.assertEqual(bids, [90.0, None, None])\n''',
)

# The applicator is intentionally one-shot; the resulting PR should contain only
# product/test changes, not a self-modifying maintenance hook.
Path("tools/apply_pr13_followup.py").unlink()
Path(".github/workflows/apply-pr13-followup.yml").unlink()
