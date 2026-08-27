"""Football-Bot — FastAPI app: dashboard, REST API, live WebSocket feed."""
import asyncio
import json
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, store
from .engine import Engine

app = FastAPI(title="Football-Bot")
_clients = set()
_queue = asyncio.Queue(maxsize=2000)
engine = None


@app.on_event("startup")
async def startup():
    global engine
    store.init()
    engine = Engine(_queue)
    await engine.start()
    asyncio.create_task(_pump())


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
            "paper_execution_v2": config.PAPER_EXECUTION_V2,
            "league_prior": config.LEAGUE_PRIOR}


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
        d["legs"][tk] = {"last": ps.get("last"), "bid": ps.get("bid"), "ask": ps.get("ask"),
                         "spark": list(ps.get("spark") or [])[-60:]}
    return list(out.values())


@app.get("/api/signals")
async def signals(limit: int = 60):
    return store.q("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/trades")
async def trades(limit: int = 200):
    rows = store.q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r.pop("book_at_entry", None)
    opens = [engine.desk.pos_dict(p, p.best_bid) for p in engine.desk.positions.values()]
    return {"open": opens, "closed": [r for r in rows if r["status"] == "closed"]}


@app.get("/api/stats")
async def stats():
    return store.stats()


@app.get("/api/equity")
async def equity():
    rows = store.q("SELECT exit_ts, net FROM trades WHERE status='closed' ORDER BY exit_ts")
    cum, out = 0.0, []
    for r in rows:
        cum += r["net"] or 0
        out.append([int((r["exit_ts"] or 0) * 1000), round(cum, 2)])
    return out


@app.get("/api/latency")
async def latency():
    rows = store.q("SELECT kind, ms FROM latency ORDER BY ts DESC LIMIT 1000")
    by = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r["ms"])
    out = {}
    for k, v in by.items():
        v = sorted(v)
        out[k] = {"n": len(v), "p50": round(v[len(v) // 2], 1),
                  "p95": round(v[int(0.95 * len(v))], 1) if len(v) > 20 else None,
                  "hist": v[-200:]}
    return out


@app.get("/api/eventlog")
async def eventlog(limit: int = 80):
    return store.q("SELECT * FROM eventlog ORDER BY rowid DESC LIMIT ?", (limit,))


@app.post("/api/kill")
async def kill(payload: dict):
    engine.desk.kill = bool(payload.get("on"))
    store.log_event("sys", f"KILL SWITCH {'ENGAGED' if engine.desk.kill else 'RELEASED'}")
    engine.broadcast({"type": "log", "text": f"⛔ Kill switch {'ON' if engine.desk.kill else 'OFF'}"})
    return {"kill": engine.desk.kill}


@app.post("/api/flatten")
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
