"""Kalshi REST + WebSocket client with RSA-PSS request signing."""
import asyncio
import base64
import inspect
import json
import re
import time

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config
from .sequence import SubscriptionSequenceTracker


def _normalize_pem(raw):
    """Rebuild a clean PEM from a value mangled by env-var/textbox pasting.

    Handles: surrounding quotes, literal backslash-n instead of newlines, all
    framing collapsed onto one line, stray whitespace in the base64 body.
    Reconstructs BEGIN/END markers with the body rewrapped at 64 cols so
    cryptography's parser accepts it regardless of how it arrived.
    """
    s = raw.strip().strip('"').strip("'")
    if "\\n" in s or "\\r" in s:                       # literal escapes
        s = s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "")
    m = re.search(r"-----BEGIN ([A-Z0-9 ]+?)-----(.*?)-----END \1-----", s, re.DOTALL)
    if not m:
        return s                                        # not PEM-shaped; let loader try
    label, body = m.group(1).strip(), m.group(2)
    b64 = re.sub(r"[^A-Za-z0-9+/=]", "", body)          # keep only base64 chars
    wrapped = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"


def _candidate_blobs():
    """Yield byte blobs to try loading, from every supported source/encoding.

    Covers: base64 of a DER key, base64 of a PEM file, a PEM string (possibly
    with mangled newlines), and a key file path. Order is cheap→robust."""
    blobs = []
    if config.KALSHI_PRIVATE_KEY_B64:
        raw = re.sub(r"\s+", "", config.KALSHI_PRIVATE_KEY_B64.strip().strip('"').strip("'"))
        try:
            dec = base64.b64decode(raw)          # bytes: could be DER or PEM-text
            blobs.append(dec)
            try:                                  # if it's actually PEM text, normalize it
                blobs.append(_normalize_pem(dec.decode()).encode())
            except Exception:
                pass
        except Exception as e:
            raise ValueError(f"KALSHI_PRIVATE_KEY_B64 is not valid base64: {e}")
    if config.KALSHI_PRIVATE_KEY:
        blobs.append(_normalize_pem(config.KALSHI_PRIVATE_KEY).encode())
        blobs.append(config.KALSHI_PRIVATE_KEY.encode())
    if config.KALSHI_PRIVATE_KEY_PATH:
        with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            data = f.read()
        blobs.append(data)
        try:
            blobs.append(_normalize_pem(data.decode()).encode())
        except Exception:
            pass
    return blobs


def _load_private_key():
    blobs = _candidate_blobs()
    if not blobs:
        return None
    errors = []
    for data in blobs:
        for loader in (serialization.load_pem_private_key,
                       serialization.load_der_private_key):
            try:
                return loader(data, password=None)
            except Exception as e:
                errors.append(f"{loader.__name__.split('_')[1].upper()}: {type(e).__name__}")
    raise ValueError(
        "Kalshi private key could not be parsed (tried PEM and DER on all "
        "provided forms). Ensure KALSHI_PRIVATE_KEY_B64 is the base64 of your "
        "unencrypted .key file. Attempts: " + ", ".join(dict.fromkeys(errors)))


class KalshiClient:
    def __init__(self):
        self._key = _load_private_key() if config.has_credentials() else None
        self._http = httpx.AsyncClient(base_url=config.KALSHI_REST, timeout=30)

    def _sign(self, method, path):
        """RSA-PSS/SHA-256 over '{ts_ms}{METHOD}{path}' (path WITHOUT query params)."""
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path}".encode()
        sig = self._key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    async def get(self, path, **params):
        """GET with retry/backoff. path like '/markets'. Signs without query."""
        headers = self._sign("GET", "/trade-api/v2" + path) if self._key else {}
        backoff = 1.0
        for _ in range(6):
            try:
                r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None},
                                          headers=headers)
                if r.status_code == 429:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 20)
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20)
        raise RuntimeError(f"GET {path} failed after retries")

    async def paged(self, path, list_key, limit=1000, **params):
        cursor = None
        out = []
        while True:
            resp = await self.get(path, limit=limit, cursor=cursor, **params)
            items = resp.get(list_key) or []
            out.extend(items)
            cursor = resp.get("cursor")
            if not cursor or not items:
                return out

    async def close(self):
        await self._http.aclose()


def _backlog_call_style(callback):
    """Decide how ``backlog`` is handed to an ``on_message`` callback.

    Older callers take ``(msg, wall, mono)``; the engine takes an explicit
    ``backlog`` keyword.  The signature is inspected once at construction so
    the per-frame dispatch is a plain call.
    """
    try:
        params = list(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return "none"
    for param in params:
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return "keyword"
        if param.name == "backlog" and param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            return "keyword"
    if any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in params):
        return "positional"
    positional = [param for param in params if param.kind in (
        inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    return "positional" if len(positional) >= 4 else "none"


# Frames the consumer processes between two voluntary yields.  The reader and
# the websockets keepalive task only run when the consumer yields, so a busy
# consumer must give the loop back regularly; sixteen frames is well under a
# millisecond of handler time and keeps arrival stamps within that of receipt.
CONSUMER_YIELD_EVERY = 16
RECONNECT_DELAY_S = 3.0


class KalshiWS:
    """Authenticated WebSocket with subscribe/update helpers and reconnect.

    Receipt and processing are split into two coroutines.  The reader only
    does ``recv`` and stamps each raw frame with its arrival time before
    queueing it; the consumer parses and routes.  The queue is unbounded on
    purpose: its depth is the measured processing backlog, reported as
    ``backlog`` and exported per frame, so that falling behind the exchange
    is visible instead of being folded into every downstream timestamp.
    """

    def __init__(self, on_message, on_state=None, on_feed_event=None):
        self._key = _load_private_key()
        self.on_message = on_message
        self._call_style = _backlog_call_style(on_message)
        self.on_state = on_state or (lambda s: None)
        self.on_feed_event = on_feed_event
        self._ws = None
        self._cmd_id = 0
        self._orderbook_sid = None
        self._trade_sid = None
        self._lifecycle_sid = None
        self._subscribed = set()
        self._sequences = SubscriptionSequenceTracker()
        self._recovering_orderbooks = {}
        self._lock = asyncio.Lock()
        self.connected = False
        self._queue = None
        self.frames_received = 0
        self.frames_consumed = 0
        self.frames_discarded = 0
        self.connections = 0
        self.disconnects = 0
        self.max_backlog = 0
        self.feed_event_failures = 0

    @property
    def backlog(self):
        """Frames received but not yet processed (current queue depth)."""
        return self._queue.qsize() if self._queue is not None else 0

    def _dispatch(self, message, wall, mono, backlog):
        if self._call_style == "keyword":
            self.on_message(message, wall, mono, backlog=backlog)
        elif self._call_style == "positional":
            self.on_message(message, wall, mono, backlog)
        else:
            self.on_message(message, wall, mono)

    def _emit(self, kind, detail=None):
        """Report a feed-health event; a ledger fault is counted, never raised.

        Emission sits on the socket path, so a database or recorder problem
        must not disconnect the feed.  The count is reported in `status()` so
        a silent ledger is visible rather than assumed complete.
        """
        if self.on_feed_event is None:
            return
        try:
            self.on_feed_event(kind, detail)
        except Exception:
            self.feed_event_failures += 1

    def _headers(self):
        ts = str(int(time.time() * 1000))
        msg = f"{ts}GET/trade-api/ws/v2".encode()
        sig = self._key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    async def _send(self, cmd, params):
        self._cmd_id += 1
        await self._ws.send(json.dumps({"id": self._cmd_id, "cmd": cmd, "params": params}))
        return self._cmd_id

    async def set_markets(self, tickers):
        """Reconcile subscription set to `tickers` (adds/removes).

        CRITICAL: never leave a subscription with zero market filters —
        Kalshi treats a filterless subscription as ALL MARKETS (firehose).
        If the wanted set empties, tear the subscription down entirely."""
        async with self._lock:
            want = set(tickers)
            if not self.connected or not want and not self._subscribed:
                self._subscribed = want if self.connected else set()
                return
            if not want and self._subscribed:
                for sid in self._subscription_sids():
                    if sid is not None:
                        try:
                            await self._send("unsubscribe", {"sids": [sid]})
                        except Exception:
                            pass
                if self._orderbook_sid is not None:
                    self._sequences.reset(self._orderbook_sid)
                    self._recovering_orderbooks.pop(self._orderbook_sid, None)
                self._orderbook_sid = self._trade_sid = self._lifecycle_sid = None
                self._subscribed = set()
                return
            add = sorted(want - self._subscribed)
            rem = sorted(self._subscribed - want)
            if self._orderbook_sid is None and add:
                await self._send("subscribe", {"channels": ["orderbook_delta", "trade",
                                                            "market_lifecycle_v2"],
                                               "market_tickers": add})
                self._subscribed = set(add)
                return
            for action, group in (("add_markets", add), ("delete_markets", rem)):
                if group and self._orderbook_sid is not None:
                    for sid in self._subscription_sids():
                        if sid is not None:
                            await self._send("update_subscription",
                                             {"sid": sid, "action": action, "market_tickers": group})
            self._subscribed = want
            for recovery_sid, pending in list(self._recovering_orderbooks.items()):
                pending.intersection_update(want)
                if not pending:
                    self._recovering_orderbooks.pop(recovery_sid, None)
                    self._emit("snapshot_complete",
                               {"sid": recovery_sid, "reason": "targets_dropped"})

    def _subscription_sids(self):
        return tuple(dict.fromkeys(sid for sid in (
            self._orderbook_sid, self._trade_sid, self._lifecycle_sid,
        ) if sid is not None))

    def _record_subscription(self, channel, sid):
        if channel == "orderbook_delta":
            self._orderbook_sid = sid
        elif channel == "trade":
            self._trade_sid = sid
        elif channel == "market_lifecycle_v2":
            self._lifecycle_sid = sid

    async def request_snapshot(self, ticker):
        async with self._lock:
            if self._orderbook_sid is not None and ticker in self._subscribed:
                # Deliberately not a ledger event: this fires once per rejected
                # delta, which is unbounded while a book is being rebuilt.  The
                # ledger records recovery (`gap` -> `snapshot_requested`), which
                # is what explains a discontinuity in the study.
                await self._send("update_subscription", {"sid": self._orderbook_sid,
                                                         "action": "get_snapshot",
                                                         "market_tickers": [ticker]})

    async def _recover_orderbook(self, sid, expected, received):
        """Invalidate books and request snapshots without replacing the stream.

        Kalshi documents ``get_snapshot`` as a non-mutating subscription update.
        Keeping the same subscription id avoids stale-stream and id-reuse races
        caused by unsubscribe/resubscribe recovery.
        """
        async with self._lock:
            active_sid = self._orderbook_sid if self._orderbook_sid is not None else sid
            if active_sid is None or (self._orderbook_sid is not None and sid != active_sid):
                return
            tickers = sorted(self._subscribed)
            self._recovering_orderbooks[active_sid] = set(tickers)
            self._emit("gap", {"sid": active_sid, "expected": expected,
                               "received": received, "invalidated": len(tickers),
                               "backlog": self.backlog})
            self._dispatch({
                "type": "orderbook_gap",
                "msg": {"sid": active_sid, "expected": expected, "received": received,
                        "market_tickers": tickers},
            }, time.time(), time.monotonic(), self.backlog)
            if tickers:
                await self._send("update_subscription", {
                    "sid": active_sid,
                    "action": "get_snapshot",
                    "market_tickers": tickers,
                })
                self._emit("snapshot_requested",
                           {"sid": active_sid, "markets": len(tickers), "reason": "gap"})

    async def _accept_orderbook_frame(self, message):
        """Validate and recovery-gate one order-book frame."""
        sid = message.get("sid")
        if not self._subscribed:
            return False
        if self._orderbook_sid is not None and sid != self._orderbook_sid:
            return False

        seq = message.get("seq")
        previous = self._sequences.last(sid)
        status = self._sequences.track(sid, seq)
        if status == "duplicate":
            return False
        if status == "gap":
            expected = previous + 1 if previous is not None else seq
            await self._recover_orderbook(sid, expected, seq)
            return False

        pending = self._recovering_orderbooks.get(sid)
        if pending is None:
            return True
        body = message.get("msg") or {}
        ticker = body.get("market_ticker")
        if message.get("type") == "orderbook_snapshot" and ticker in pending:
            pending.remove(ticker)
            if not pending:
                self._recovering_orderbooks.pop(sid, None)
                self._emit("snapshot_complete", {"sid": sid, "reason": "snapshots_received"})
            return True
        return ticker not in pending

    async def _handle_raw(self, raw, wall, mono, backlog):
        """Parse and route one received frame (the former inline run() body)."""
        m = json.loads(raw)
        t = m.get("type")
        if t == "subscribed":
            ch = (m.get("msg") or {}).get("channel")
            sid = (m.get("msg") or {}).get("sid")
            if ch == "orderbook_delta":
                if self._orderbook_sid != sid and self._sequences.last(sid) is None:
                    self._sequences.reset(sid)
            self._record_subscription(ch, sid)
            self._emit("subscribed", {"channel": ch, "sid": sid,
                                      "markets": len(self._subscribed)})
        if (t in ("orderbook_snapshot", "orderbook_delta")
                and not await self._accept_orderbook_frame(m)):
            return
        self._dispatch(m, wall, mono, backlog)

    async def _read(self, ws, queue):
        """Receive frames and stamp their arrival; nothing else runs here."""
        async for raw in ws:
            queue.put_nowait((raw, time.time(), time.monotonic()))
            self.frames_received += 1

    async def _consume(self, queue):
        """Drain the arrival queue; yields periodically so the reader keeps up."""
        since_yield = 0
        while True:
            raw, wall, mono = await queue.get()
            backlog = queue.qsize()
            if backlog > self.max_backlog:
                self.max_backlog = backlog
            await self._handle_raw(raw, wall, mono, backlog)
            self.frames_consumed += 1
            since_yield += 1
            if since_yield >= CONSUMER_YIELD_EVERY:
                since_yield = 0
                await asyncio.sleep(0)

    def _discard_backlog(self):
        queue, self._queue = self._queue, None
        if queue is None:
            return 0
        discarded = queue.qsize()
        self.frames_discarded += discarded
        return discarded

    async def run(self):
        """Connect-consume-reconnect loop."""
        while True:
            try:
                async with websockets.connect(config.KALSHI_WS, additional_headers=self._headers(),
                                              ping_interval=10, ping_timeout=20,
                                              max_size=2 ** 23) as ws:
                    self._ws = ws
                    self.connected = True
                    self.connections += 1
                    self._orderbook_sid = self._trade_sid = self._lifecycle_sid = None
                    self._sequences.reset()
                    self._recovering_orderbooks.clear()
                    subs = self._subscribed
                    self._subscribed = set()
                    self._queue = asyncio.Queue()
                    self.on_state("connected")
                    self._emit("connected", {"connection": self.connections,
                                             "resubscribe": len(subs)})
                    if subs:
                        await self.set_markets(subs)
                        self._emit("resubscribed", {"markets": len(subs)})
                    tasks = (asyncio.ensure_future(self._read(ws, self._queue)),
                             asyncio.ensure_future(self._consume(self._queue)))
                    try:
                        done, _pending = await asyncio.wait(
                            tasks, return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        # Whichever side finished first, or an outer cancellation
                        # at shutdown, must not leave the other task orphaned.
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    for task in done:
                        # Re-raise the reader's close error or a consumer fault so
                        # the disconnect is recorded and the connection rebuilt.
                        task.result()
                    raise ConnectionError("websocket stream ended")
            except Exception as e:
                self.connected = False
                self.disconnects += 1
                discarded = self._discard_backlog()
                self.on_state(f"disconnected: {type(e).__name__}")
                self._emit("disconnected", {"error": type(e).__name__,
                                            "message": str(e)[:200],
                                            "backlog_discarded": discarded})
                await asyncio.sleep(RECONNECT_DELAY_S)

    def status(self):
        return {
            "connected": self.connected,
            "backlog": self.backlog,
            "max_backlog": self.max_backlog,
            "frames_received": self.frames_received,
            "frames_consumed": self.frames_consumed,
            "frames_discarded": self.frames_discarded,
            "connections": self.connections,
            "disconnects": self.disconnects,
            "feed_event_failures": self.feed_event_failures,
        }
