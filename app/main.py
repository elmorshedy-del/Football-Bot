"""Football-Bot — FastAPI app: dashboard, REST API, live WebSocket feed."""
import asyncio
import json
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import config, exporter, store
from .audit import (
    build_trigger,
    json_object,
    match_signal_event,
    normalized_event as current_normalized_event,
    schedule_window,
    signal_strategy,
    timing_fields,
)
from .engine import Engine
from .match_clock import parse_stored_stamp

_clients = set()
_queue = asyncio.Queue(maxsize=2000)
engine = None
_export_job = None
_export_jobs = {}
_export_tasks = set()
_export_lock = threading.Lock()
_raw_download_token = secrets.token_urlsafe(32)
EXPORT_JOB_TTL_S = 3600
_RANGE_SPEC = re.compile(r"bytes=(\d*)-(\d*)\Z")


def require_admin(x_admin_token: str | None = Header(default=None)):
    if not config.ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured")
    if not secrets.compare_digest(x_admin_token or "", config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")


@asynccontextmanager
async def lifespan(_app):
    global engine, _queue
    _queue = asyncio.Queue(maxsize=2000)
    store.init()
    engine = Engine(_queue)
    await engine.start()
    pump_task = asyncio.create_task(_pump())
    try:
        yield
    finally:
        pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await pump_task
        for task in tuple(_export_tasks):
            task.cancel()
        with suppress(OSError):
            engine.recorder.close()
        await engine.client.close()


app = FastAPI(title="Football-Bot", lifespan=lifespan)


def _human_identifier(value):
    text = re.sub(r"^KX", "", value or "", flags=re.IGNORECASE)
    text = re.sub(r"GAME", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[-_]+", " · ", text)
    text = re.sub(r"\s+", " ", text).strip(" ·")
    return text.title() or "Unknown match"


def _display_names(ticker, fallback_event=None, stored_title=None, stored_leg=None,
                   stored_game=None):
    """Presentation-only soccer names; stored identifiers remain untouched."""
    meta = engine.meta.get(ticker, {}) if engine else {}
    raw = (meta.get("title") or stored_title or ticker or "").strip()
    parts = [part.strip(" -") for part in raw.replace("–", "—").split("—") if part.strip(" -")]
    fallback = fallback_event or meta.get("event") or ticker
    game_source = meta.get("game_title") or stored_game or (
        parts[0] if len(parts) > 1 else raw
    )
    game_source = re.sub(r"\s+Winner\?\s*$", "", game_source, flags=re.IGNORECASE)
    game = game_source if game_source and game_source != ticker else _human_identifier(fallback)
    suffix = (ticker or "").rsplit("-", 1)[-1]
    provider_leg = (meta.get("leg_title") or stored_leg or "").strip()
    if suffix.upper() in {"TIE", "DRAW", "X"} or provider_leg.lower() in {"tie", "draw"}:
        leg = "Draw"
    elif provider_leg:
        leg = re.sub(r"\s+wins?\s*$", "", provider_leg, flags=re.IGNORECASE).strip()
    elif len(parts) > 1:
        leg = parts[-1]
    else:
        title_leg = re.sub(r"\s+wins?\s*$", "", raw, flags=re.IGNORECASE).strip()
        leg = title_leg if title_leg and title_leg not in {ticker, game} else _human_identifier(suffix)
    return {"display_game": game, "display_leg": leg,
            "display_contract": "Draw" if leg == "Draw" else f"{leg} wins"}


def _rows_by_signal_id(signal_ids):
    signal_ids = sorted({int(value) for value in signal_ids if value is not None})
    if not signal_ids:
        return {}
    marks = ",".join("?" for _ in signal_ids)
    return {row["id"]: row for row in store.q(
        f"""SELECT s.*,m.title AS market_title,m.display_game AS market_game,
                   m.display_leg AS market_leg,m.close_time AS expected_expiration_time
              FROM signals s
              LEFT JOIN markets m ON m.ticker=s.market
             WHERE s.id IN ({marks})""", signal_ids,
    )}


def _trades_by_signal_id(signal_ids):
    signal_ids = sorted({int(value) for value in signal_ids if value is not None})
    if not signal_ids:
        return {}
    marks = ",".join("?" for _ in signal_ids)
    return {row["signal_id"]: row for row in store.q(
        f"SELECT * FROM trades WHERE signal_id IN ({marks})", signal_ids,
    )}


def _event_observations(signals):
    timed = [row for row in signals if isinstance(row.get("local_ts"), (int, float))]
    events = sorted({row.get("event") for row in timed if row.get("event")})
    if not events:
        return []
    marks = ",".join("?" for _ in events)
    lower = min(row["local_ts"] for row in timed) - config.EVENT_MATCH_WINDOW_S
    upper = max(row["local_ts"] for row in timed) + config.EVENT_MATCH_WINDOW_S
    score_rows = store.q(
        f"""SELECT * FROM goal_latency_observations
              WHERE event IN ({marks}) AND observed_ts BETWEEN ? AND ?
              ORDER BY observed_ts""",
        (*events, lower, upper),
    )
    provider_rows = store.q(
        f"""SELECT id, event, first_observed_ts AS observed_ts, first_observed_ts,
                    last_observed_ts, canonical_type, canonical_side, fingerprint,
                    provider_clock, provider_minute, provider_stoppage,
                    normalized_event, raw_payload, response_ms
               FROM provider_match_events
              WHERE event IN ({marks}) AND first_observed_ts BETWEEN ? AND ?
              ORDER BY first_observed_ts""",
        (*events, lower, upper),
    )
    return list(score_rows) + list(provider_rows)


def _decorate_signal(row, observations, trade=None):
    row["detail"] = json_object(row.get("detail"))
    row["strategy"] = (
        (trade or {}).get("strategy") or signal_strategy(row)
    )
    row["trigger"] = build_trigger(row)
    row["timing"] = timing_fields(row, trade)
    row["schedule_window"] = schedule_window(row)
    row["matched_event"] = match_signal_event(row, observations)
    row["match_clock"] = parse_stored_stamp(row.get("match_clock_snapshot"))
    row.update(_display_names(
        row.get("market"), row.get("event"), row.get("market_title"),
        row.get("market_leg"), row.get("market_game"),
    ))
    return row


async def _pump():
    while True:
        msg = await _queue.get()
        data = json.dumps(msg)
        dead = []
        for ws in _clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.discard(ws)


@app.get("/api/health")
async def health():
    return {"ok": True, "mode": engine.mode if engine else "starting"}


@app.get("/api/status")
async def status():
    return engine.status()


@app.get("/api/config")
async def get_config():
    return {"dl_min": config.DL_MIN, "levels_min": config.LEVELS_MIN,
            "size_min": config.SIZE_MIN, "conf_ms": config.CONF_MS,
            "conf_sign": config.CONF_SIGN, "price_cap": config.PRICE_CAP,
            "notional_usd": config.NOTIONAL_USD, "target": config.TARGET,
            "timeout_s": config.TIMEOUT_S, "lockout_s": config.LOCKOUT_S,
            "late_only": config.LATE_ONLY, "use_stop": config.USE_STOP,
            "price_only_sleeve_mode": config.PRICE_ONLY_SLEEVE_MODE,
            "sleeve_start_before_expiry_min": config.SLEEVE_START_BEFORE_EXPIRY_MIN,
            "sleeve_after_expiry_min": config.SLEEVE_AFTER_EXPIRY_MIN,
            "sleeve_min_team_gain_pp": config.SLEEVE_MIN_TEAM_GAIN_PP,
            "sleeve_min_draw_gain_pp": config.SLEEVE_MIN_DRAW_GAIN_PP,
            "sleeve_min_explained": config.SLEEVE_MIN_EXPLAINED,
            "sleeve_max_spread_c": config.SLEEVE_MAX_SPREAD_C,
            "sleeve_scratch_arm_c": config.SLEEVE_SCRATCH_ARM_C,
            "sleeve_trail_arm_c": config.SLEEVE_TRAIL_ARM_C,
            "sleeve_timeout_s": config.SLEEVE_TIMEOUT_S,
            "paper_execution_v2": config.PAPER_EXECUTION_V2,
            "goal_latency_observer": config.GOAL_LATENCY_OBSERVER,
            "goal_latency_poll_ms": config.GOAL_LATENCY_POLL_MS,
            "event_match_window_s": config.EVENT_MATCH_WINDOW_S,
            "match_clock_max_age_ms": config.MATCH_CLOCK_MAX_AGE_MS,
            "league_prior": config.LEAGUE_PRIOR,
            "league_names": config.LEAGUE_NAMES}


@app.get("/api/matches")
async def matches():
    out = {}
    for tk, m in engine.meta.items():
        ev = m["event"]
        d = out.setdefault(ev, {"event": ev, "series": m["series"],
                                "title": (m.get("title") or "").split("—")[0].strip() or ev,
                                "close_time": m.get("close_time"), "late": False, "legs": {}})
        ps = engine.prices.get(tk, {})
        d["late"] = d["late"] or engine.is_late(tk)
        display = _display_names(tk, ev)
        d["title"] = display["display_game"]
        d["legs"][tk] = {"last": ps.get("last"), "bid": ps.get("bid"), "ask": ps.get("ask"),
                         "display_name": display["display_leg"],
                         "spark": list(ps.get("spark") or [])[-60:]}
    return list(out.values())


@app.get("/api/signals")
async def signals(limit: int = 60):
    limit = max(1, min(limit, 500))
    rows = store.q(
        """SELECT s.*,m.title AS market_title,m.display_game AS market_game,
                  m.display_leg AS market_leg,m.close_time AS expected_expiration_time
             FROM signals s
             LEFT JOIN markets m ON m.ticker=s.market
            ORDER BY s.id DESC LIMIT ?""",
        (limit,),
    )
    trades_by_signal = _trades_by_signal_id(row["id"] for row in rows)
    observations = _event_observations(rows)
    for row in rows:
        _decorate_signal(row, observations, trades_by_signal.get(row["id"]))
        row["forward_path_summary"] = json_object(row.get("forward_path_summary")) or None
        row["forward_path_url"] = f"/api/signals/{row['id']}/path"
    return rows


@app.get("/api/trades")
async def trades(limit: int = 200):
    limit = max(1, min(limit, 500))
    rows = store.q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
    opens = [engine.desk.pos_dict(p, p.best_bid) for p in engine.desk.positions.values()]
    signal_rows = _rows_by_signal_id(
        [row.get("signal_id") for row in rows] + [row.get("signal_id") for row in opens],
    )
    observations = _event_observations(signal_rows.values())
    for r in rows:
        r.pop("book_at_entry", None)
        signal = signal_rows.get(r.get("signal_id"))
        r.update(_display_names(
            r.get("market"), r.get("event"),
            (signal or {}).get("market_title"), (signal or {}).get("market_leg"),
            (signal or {}).get("market_game"),
        ))
        if signal:
            signal["detail"] = json_object(signal.get("detail"))
            signal["strategy"] = r.get("strategy") or signal_strategy(signal)
            r["trigger"] = build_trigger(signal)
            r["timing"] = timing_fields(signal, r)
            r["schedule_window"] = schedule_window(signal)
            r["matched_event"] = match_signal_event(signal, observations)
            r["match_clock"] = parse_stored_stamp(signal.get("match_clock_snapshot"))
        r["high_after_entry_s"] = (
            round(r["max_executable_bid_ts"] - r["entry_ts"], 3)
            if isinstance(r.get("max_executable_bid_ts"), (int, float))
            and isinstance(r.get("entry_ts"), (int, float)) else None
        )
        if r.get("mfe_c") is None and r.get("max_executable_bid") is not None \
                and r.get("entry_px") is not None:
            r["mfe_c"] = max(0.0, r["max_executable_bid"] - r["entry_px"])
        # Read the summary persisted at close.  Fetching samples per row here
        # meant up to 500 extra queries and millions of rows per refresh; the
        # full path is served by /api/trades/{id}/path on demand.
        r["bid_path_summary"] = json_object(r.get("bid_path_summary")) or None
        r["bid_path_url"] = f"/api/trades/{r['id']}/path"
    for row in opens:
        signal = signal_rows.get(row.get("signal_id"))
        row.update(_display_names(
            row.get("market"), row.get("event"),
            (signal or {}).get("market_title"), (signal or {}).get("market_leg"),
            (signal or {}).get("market_game"),
        ))
        if signal:
            signal["detail"] = json_object(signal.get("detail"))
            signal["strategy"] = row.get("strategy") or signal_strategy(signal)
            row["trigger"] = build_trigger(signal)
            row["timing"] = timing_fields(signal, row)
            row["schedule_window"] = schedule_window(signal)
            row["matched_event"] = match_signal_event(signal, observations)
            row["match_clock"] = parse_stored_stamp(signal.get("match_clock_snapshot"))
    return {"open": opens, "closed": [r for r in rows if r["status"] == "closed"]}


@app.get("/api/stats")
async def stats():
    return store.stats()


@app.get("/api/equity")
async def equity():
    rows = store.q(
        """SELECT exit_ts,net,strategy FROM trades
             WHERE status='closed' ORDER BY exit_ts,id"""
    )
    cumulative = {"combined": 0.0, "gate_a": 0.0, "price_only_late_score": 0.0}
    out = {key: [] for key in cumulative}
    for r in rows:
        strategy = ("price_only_late_score" if r.get("strategy") in {
            "price_only_late_score", "price_only_late_score_v1",
        } else "gate_a")
        ts_ms = int((r["exit_ts"] or 0) * 1000)
        cumulative[strategy] += r["net"] or 0
        cumulative["combined"] += r["net"] or 0
        out[strategy].append([ts_ms, round(cumulative[strategy], 2)])
        out["combined"].append([ts_ms, round(cumulative["combined"], 2)])
    return out


@app.get("/api/latency")
async def latency():
    readiness = store.latency_readiness()
    out = {}
    for kind, summary in readiness.items():
        hist_aliases = store.LATENCY_KIND_ALIASES.get(kind, (kind,))
        marks = ",".join("?" for _ in hist_aliases)
        hist_rows = store.q(
            f"""SELECT ms FROM latency WHERE kind IN ({marks})
                 ORDER BY ts DESC LIMIT 200""",
            hist_aliases,
        )
        out[kind] = {
            **summary,
            "hist": [row["ms"] for row in reversed(hist_rows)
                     if isinstance(row.get("ms"), (int, float))],
        }
    return out


@app.get("/api/goal-latency")
async def goal_latency(limit: int = 100):
    limit = max(1, min(limit, 500))
    rows = store.q(
        "SELECT * FROM goal_latency_observations ORDER BY id DESC LIMIT ?", (limit,),
    )
    for row in rows:
        for field in ("score_before", "score_after", "normalized_event", "detail"):
            try:
                row[field] = json.loads(row[field])
            except (TypeError, json.JSONDecodeError):
                pass
        row["normalized_event"] = current_normalized_event(row)
        row.update(_display_names("", row.get("event")))
    return rows


@app.get("/api/eventlog")
async def eventlog(limit: int = 80):
    return store.q("SELECT * FROM eventlog ORDER BY rowid DESC LIMIT ?", (limit,))


@app.get("/api/match-clocks")
async def match_clocks(limit: int = 100):
    limit = max(1, min(limit, 500))
    rows = store.q(
        "SELECT * FROM match_clock_observations ORDER BY id DESC LIMIT ?", (limit,),
    )
    for row in rows:
        try:
            row["raw_context"] = json.loads(row["raw_context"])
        except (TypeError, json.JSONDecodeError):
            pass
    coverage = (
        engine.clock_tracker.coverage(engine.watched_events)
        if engine and getattr(engine, "clock_tracker", None) else {}
    )
    return {"coverage": coverage, "observations": rows}


@app.get("/api/trades/{trade_id}/path")
async def trade_bid_path(trade_id: int, limit: int = 2000):
    """Full execution path for one trade.  Bounded and fetched on demand."""
    limit = max(1, min(limit, store.BID_PATH_MAX_SAMPLES))
    samples = store.bid_path_for_trade(trade_id, limit=limit)
    return {"trade_id": trade_id, "samples": samples,
            "summary": store.bid_path_summary(samples),
            "truncated": len(samples) >= limit}


@app.get("/api/signals/{signal_id}/path")
async def signal_forward_path(signal_id: int, limit: int = 2000):
    """Forward price path recorded after one signal, accepted or declined."""
    limit = max(1, min(limit, store.BID_PATH_MAX_SAMPLES))
    samples = store.bid_path_for_signal(signal_id, limit=limit)
    return {"signal_id": signal_id, "samples": samples,
            "summary": store.bid_path_summary(samples),
            "truncated": len(samples) >= limit}


@app.get("/api/provider-events")
async def provider_events(limit: int = 100):
    limit = max(1, min(limit, 500))
    rows = store.q(
        "SELECT * FROM provider_match_events ORDER BY id DESC LIMIT ?", (limit,),
    )
    for row in rows:
        for field in ("normalized_event", "raw_payload"):
            try:
                row[field] = json.loads(row[field])
            except (TypeError, json.JSONDecodeError):
                pass
        row.update(_display_names("", row.get("event")))
    return rows


def _remove_export(path):
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _public_export_job(job):
    return {
        "job_id": job["job_id"],
        "scope": job.get("scope") or "full",
        "status": job["status"],
        "created_at": job["created_at"],
        "bytes": job.get("bytes"),
        "processed_bytes": job.get("processed_bytes") or 0,
        "total_bytes": job.get("total_bytes") or 0,
        "processed_segments": job.get("processed_segments") or 0,
        "total_segments": job.get("total_segments") or 0,
        "error": job.get("error"),
        "error_code": job.get("error_code"),
    }


def _lookup_job(job_id):
    if not job_id:
        return None
    job = _export_jobs.get(job_id)
    if job is None and _export_job and _export_job.get("job_id") == job_id:
        job = _export_job
    if job is None:
        return None
    if not secrets.compare_digest(job_id, job["job_id"]):
        return None
    return job


def _active_full_job():
    for job in _export_jobs.values():
        if job.get("scope") == "full" and job.get("status") in {"queued", "preparing"}:
            return job
    if _export_job and _export_job.get("scope") == "full" and \
            _export_job.get("status") in {"queued", "preparing"}:
        return _export_job
    return None


def _export_cookie_path(job_id):
    return f"/api/export/jobs/{job_id}"


def _set_export_cookie(response, job):
    response.set_cookie(
        "footballbot_export_job", job["download_token"], max_age=EXPORT_JOB_TTL_S,
        httponly=True, secure=True, samesite="strict",
        path=_export_cookie_path(job["job_id"]),
    )
    return response


def _release_export_lease(job_id, path=None):
    job = _lookup_job(job_id)
    if job is None:
        if path:
            _remove_export(path)
        return
    with _export_lock:
        job["leases"] = max(0, int(job.get("leases") or 0) - 1)
        status = job.get("status")
        remaining = job["leases"]
        stale_path = job.get("path")
        if remaining == 0 and status in {"expired", "cancelled", "error"}:
            job["path"] = None
        else:
            stale_path = None
    if stale_path:
        _remove_export(stale_path)


def _expire_export_jobs(now=None):
    now = now or time.time()
    removable = []
    with _export_lock:
        for job in list(_export_jobs.values()):
            anchor = job.get("ready_at") or job.get("created_at") or now
            if now - anchor < EXPORT_JOB_TTL_S:
                continue
            if job.get("status") in {"queued", "preparing"}:
                continue
            if int(job.get("leases") or 0) > 0:
                job["status"] = "expired"
                job["error_code"] = job.get("error_code") or "EXPIRED"
                continue
            removable.append(job.get("path"))
            job.update(status="expired", error_code="EXPIRED", path=None,
                       error=job.get("error") or "Study export expired.")
    for path in removable:
        _remove_export(path)


def _register_job(job):
    global _export_job
    with _export_lock:
        _export_jobs[job["job_id"]] = job
        _export_job = job
    return job


def _update_job(job_id, **fields):
    job = _lookup_job(job_id)
    if job is None:
        return None
    with _export_lock:
        job.update(fields)
    return job


def _ranged_file_response(path, filename, media_type, range_header=None, background=None):
    if not isinstance(range_header, str) or not range_header.strip():
        range_header = None
    file_size = os.path.getsize(path)
    if not range_header:
        response = FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            background=background,
        )
        response.headers["Accept-Ranges"] = "bytes"
        return response
    match = _RANGE_SPEC.match(range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail="Invalid Range",
                            headers={"Content-Range": f"bytes */{file_size}"})
    start_text, end_text = match.groups()
    if start_text == "" and end_text == "":
        raise HTTPException(status_code=416, detail="Invalid Range",
                            headers={"Content-Range": f"bytes */{file_size}"})
    if start_text == "":
        suffix = int(end_text)
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    if start >= file_size or start > end:
        raise HTTPException(
            status_code=416, detail="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    end = min(end, file_size - 1)
    length = end - start + 1

    def iterator():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    return StreamingResponse(
        iterator(), status_code=206, media_type=media_type, headers=headers,
        background=background,
    )


def _new_export_job(scope, raw_paths):
    total_bytes = 0
    total_segments = 0
    if scope == "full":
        for path in raw_paths:
            try:
                total_bytes += os.path.getsize(path)
                total_segments += 1
            except OSError:
                pass
    return {
        "job_id": secrets.token_urlsafe(18),
        "download_token": secrets.token_urlsafe(32),
        "scope": scope,
        "status": "queued",
        "created_at": time.time(),
        "ready_at": None,
        "path": None,
        "bytes": None,
        "processed_bytes": 0,
        "total_bytes": total_bytes,
        "processed_segments": 0,
        "total_segments": total_segments,
        "error": None,
        "error_code": None,
        "leases": 0,
        "cancel_requested": False,
    }


async def _build_export_job(job_id, mode, raw_paths, snapshot_path, scope):
    job = _lookup_job(job_id)
    if job is None:
        _remove_export(snapshot_path)
        return
    _update_job(job_id, status="preparing")

    def progress(payload):
        _update_job(
            job_id,
            processed_bytes=payload.get("processed_bytes") or 0,
            total_bytes=payload.get("total_bytes") or 0,
            processed_segments=payload.get("processed_segments") or 0,
            total_segments=payload.get("total_segments") or 0,
        )

    def cancel_check():
        current = _lookup_job(job_id)
        return current is None or bool(current.get("cancel_requested"))

    try:
        path, _manifest = await asyncio.to_thread(
            exporter.build_study_bundle, None, mode, raw_paths, snapshot_path,
            scope == "full", progress, cancel_check, scope,
        )
    except exporter.ExportCancelled:
        current = _lookup_job(job_id)
        if current is not None:
            _update_job(
                job_id, status="cancelled", error="Study export cancelled.",
                error_code="CANCELLED",
            )
        return
    except Exception as exc:  # noqa: BLE001 - surface failure without stopping collection
        current = _lookup_job(job_id)
        if current is not None:
            _update_job(
                job_id, status="error", error="Study export preparation failed.",
                error_code="PREPARE_FAILED",
            )
        engine._record_error("study_export", exc)
        return
    current = _lookup_job(job_id)
    if current is None or current.get("cancel_requested"):
        _remove_export(path)
        if current is not None:
            _update_job(
                job_id, status="cancelled", error="Study export cancelled.",
                error_code="CANCELLED", path=None,
            )
        return
    _update_job(
        job_id, status="ready", path=path, bytes=os.path.getsize(path),
        error=None, error_code=None, ready_at=time.time(),
        processed_bytes=current.get("total_bytes") or 0,
        processed_segments=current.get("total_segments") or 0,
    )


@app.post("/api/export/prepare", dependencies=[Depends(require_admin)], status_code=202)
async def prepare_study_export(scope: str = "audit"):
    """Start one non-blocking export job and return a pollable identifier."""
    scope = (scope or "audit").lower()
    if scope not in {"audit", "full"}:
        raise HTTPException(status_code=400, detail="scope must be audit or full")
    _expire_export_jobs()
    if scope == "full":
        existing = _active_full_job()
        if existing is not None:
            response = JSONResponse(_public_export_job(existing), status_code=202)
            return _set_export_cookie(response, existing)
    def _prepare_inputs():
        """Recorder rotation, path enumeration, SQLite backup, and the per-file
        stat walk in _new_export_job are all blocking filesystem work.  Running
        them inline stalled the event loop for the whole snapshot before the 202
        was even returned, so live collection paused while a download was
        requested."""
        engine.recorder.checkpoint_for_export()
        raw_paths = exporter.raw_feed_paths()
        snapshot_path = exporter.prepare_database_snapshot()
        return raw_paths, snapshot_path, _new_export_job(scope, raw_paths)

    try:
        raw_paths, snapshot_path, job = await asyncio.to_thread(_prepare_inputs)
    except Exception as exc:  # noqa: BLE001 - keep collection alive and expose the fault
        engine._record_error("study_export", exc)
        raise HTTPException(
            status_code=500,
            detail="Study export failed; see System status for the recorded fault.",
        ) from exc
    _register_job(job)
    task = asyncio.create_task(_build_export_job(
        job["job_id"], engine.mode, raw_paths, snapshot_path, scope,
    ))
    _export_tasks.add(task)
    task.add_done_callback(_export_tasks.discard)
    response = JSONResponse(_public_export_job(job), status_code=202)
    return _set_export_cookie(response, job)


@app.get("/api/export/jobs/{job_id}", dependencies=[Depends(require_admin)])
async def study_export_status(job_id: str):
    _expire_export_jobs()
    job = _lookup_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Study export job not found")
    if job["status"] == "ready" and not os.path.isfile(job.get("path") or ""):
        _update_job(
            job_id, status="error", error="Prepared export is no longer available.",
            error_code="UNAVAILABLE",
        )
        job = _lookup_job(job_id)
    return _public_export_job(job)


@app.post("/api/export/jobs/{job_id}/cancel", dependencies=[Depends(require_admin)])
async def cancel_study_export(job_id: str):
    job = _lookup_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Study export job not found")
    stale = None
    with _export_lock:
        job["cancel_requested"] = True
        if job["status"] in {"queued", "preparing"}:
            job["status"] = "cancelled"
            job["error"] = "Study export cancelled."
            job["error_code"] = "CANCELLED"
        elif job["status"] == "ready" and int(job.get("leases") or 0) == 0:
            stale = job.get("path")
            job["path"] = None
            job["status"] = "cancelled"
            job["error"] = "Study export cancelled."
            job["error_code"] = "CANCELLED"
    _remove_export(stale)
    return _public_export_job(job)


@app.get("/api/export/jobs/{job_id}/download")
async def download_study_export(
    job_id: str,
    x_admin_token: str | None = Header(default=None),
    footballbot_export_job: str | None = Cookie(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
):
    _expire_export_jobs()
    job = _lookup_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Study export job not found")
    header_ok = bool(config.ADMIN_TOKEN) and secrets.compare_digest(
        x_admin_token or "", config.ADMIN_TOKEN,
    )
    cookie_ok = secrets.compare_digest(
        footballbot_export_job or "", job["download_token"],
    )
    if not header_ok and not cookie_ok:
        raise HTTPException(status_code=401, detail="invalid export authorization")
    if job["status"] in {"queued", "preparing"}:
        raise HTTPException(status_code=425, detail="Study export is still preparing")
    if job["status"] == "cancelled":
        raise HTTPException(status_code=410, detail="Study export cancelled.")
    if job["status"] == "expired":
        raise HTTPException(status_code=410, detail="Study export expired.")
    if job["status"] != "ready" or not os.path.isfile(job.get("path") or ""):
        raise HTTPException(status_code=410, detail=job.get("error") or
                            "Study export is unavailable")
    path = job["path"]
    with _export_lock:
        job["leases"] = int(job.get("leases") or 0) + 1
    response = _ranged_file_response(
        path,
        filename=os.path.basename(path),
        media_type="application/zip",
        range_header=range_header,
        background=BackgroundTask(_release_export_lease, job_id, path),
    )
    return _set_export_cookie(response, job)


@app.get("/api/export/raw", dependencies=[Depends(require_admin)])
async def list_raw_segments():
    """List immutable recorder segments without copying bodies."""
    payload = {"segments": exporter.raw_inventory()}
    response = JSONResponse(payload)
    response.set_cookie(
        "footballbot_export_raw", _raw_download_token, max_age=EXPORT_JOB_TTL_S,
        httponly=True, secure=True, samesite="strict", path="/api/export/raw",
    )
    return response


@app.get("/api/export/raw/{name}")
async def download_raw_segment(
    name: str,
    x_admin_token: str | None = Header(default=None),
    footballbot_export_raw: str | None = Cookie(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
):
    header_ok = bool(config.ADMIN_TOKEN) and secrets.compare_digest(
        x_admin_token or "", config.ADMIN_TOKEN,
    )
    cookie_ok = secrets.compare_digest(
        footballbot_export_raw or "", _raw_download_token,
    )
    if not header_ok and not cookie_ok:
        raise HTTPException(status_code=401, detail="invalid export authorization")
    path = exporter.safe_raw_segment_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Raw segment not found")
    return _ranged_file_response(
        str(path),
        filename=path.name,
        media_type="application/gzip",
        range_header=range_header,
    )


@app.get("/api/export", dependencies=[Depends(require_admin)])
async def export_study_data():
    """Download a consistent paper-study snapshot without exposing credentials."""
    try:
        # Finalize the active gzip before selecting immutable raw segments. New
        # frames open a fresh active file, which belongs to the next export.
        engine.recorder.checkpoint_for_export()
        raw_paths = exporter.raw_feed_paths()
        # This synchronous backup runs before yielding the event loop, so the
        # database and selected raw files share one explicit capture boundary.
        snapshot_path = exporter.prepare_database_snapshot()
        path, _manifest = await asyncio.to_thread(
            exporter.build_study_bundle, None, engine.mode, raw_paths, snapshot_path,
            True, None, None, "full",
        )
    except Exception as exc:  # noqa: BLE001 - keep collection alive and expose the fault
        engine._record_error("study_export", exc)
        raise HTTPException(
            status_code=500,
            detail="Study export failed; see System status for the recorded fault.",
        ) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=os.path.basename(path),
        background=BackgroundTask(_remove_export, path),
    )


@app.post("/api/kill", dependencies=[Depends(require_admin)])
async def kill(payload: dict):
    engine.desk.kill = bool(payload.get("on"))
    store.log_event("sys", f"KILL SWITCH {'ENGAGED' if engine.desk.kill else 'RELEASED'}")
    engine.broadcast({"type": "log", "text": f"⛔ Kill switch {'ON' if engine.desk.kill else 'OFF'}"})
    return {"kill": engine.desk.kill}


@app.post("/api/flatten", dependencies=[Depends(require_admin)])
async def flatten():
    engine.desk.flatten_all()
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "status": engine.status(),
                                       "stats": store.stats()}))
        while True:
            await ws.receive_text()  # keepalive/no-op
    except (WebSocketDisconnect, Exception):
        _clients.discard(ws)


static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
