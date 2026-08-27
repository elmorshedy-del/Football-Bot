"""Kalshi REST + WebSocket client with RSA-PSS request signing."""
import asyncio
import base64
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


class KalshiWS:
    """Authenticated WebSocket with subscribe/update helpers and reconnect."""

    def __init__(self, on_message, on_state=None):
        self._key = _load_private_key()
        self.on_message = on_message
        self.on_state = on_state or (lambda s: None)
        self._ws = None
        self._cmd_id = 0
        self._orderbook_sid = None
        self._trade_sid = None
        self._subscribed = set()
        self._sequences = SubscriptionSequenceTracker()
        self._ignored_orderbook_sids = set()
        self._lock = asyncio.Lock()
        self.connected = False

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
                for sid in (self._orderbook_sid, self._trade_sid):
                    if sid is not None:
                        try:
                            await self._send("unsubscribe", {"sids": [sid]})
                        except Exception:
                            pass
                self._orderbook_sid = self._trade_sid = None
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
                    for sid in (self._orderbook_sid, self._trade_sid):
                        if sid is not None:
                            await self._send("update_subscription",
                                             {"sid": sid, "action": action, "market_tickers": group})
            self._subscribed = want

    async def request_snapshot(self, ticker):
        if self._orderbook_sid is not None:
            await self._send("update_subscription", {"sid": self._orderbook_sid,
                                                     "action": "get_snapshot",
                                                     "market_tickers": [ticker]})

    async def _recover_orderbook(self, sid, expected, received):
        """Invalidate the current book stream and subscribe for fresh snapshots."""
        tickers = sorted(self._subscribed)
        self.on_message({
            "type": "orderbook_gap",
            "msg": {"sid": sid, "expected": expected, "received": received,
                    "market_tickers": tickers},
        }, time.time(), time.monotonic())
        async with self._lock:
            if isinstance(sid, int):
                self._ignored_orderbook_sids.add(sid)
                self._sequences.reset(sid)
            current_sid = self._orderbook_sid
            stale_sid = current_sid if current_sid is not None else sid
            if stale_sid is not None:
                self._ignored_orderbook_sids.add(stale_sid)
                self._sequences.reset(stale_sid)
                try:
                    await self._send("unsubscribe", {"sids": [stale_sid]})
                except Exception:
                    pass
            self._orderbook_sid = None
            if tickers:
                await self._send("subscribe", {
                    "channels": ["orderbook_delta"],
                    "market_tickers": tickers,
                })

    async def run(self):
        """Connect-consume-reconnect loop."""
        while True:
            try:
                async with websockets.connect(config.KALSHI_WS, additional_headers=self._headers(),
                                              ping_interval=10, ping_timeout=20,
                                              max_size=2 ** 23) as ws:
                    self._ws = ws
                    self.connected = True
                    self._orderbook_sid = self._trade_sid = None
                    self._sequences.reset()
                    self._ignored_orderbook_sids.clear()
                    subs = self._subscribed
                    self._subscribed = set()
                    self.on_state("connected")
                    if subs:
                        await self.set_markets(subs)
                    async for raw in ws:
                        m = json.loads(raw)
                        t = m.get("type")
                        if t == "subscribed":
                            ch = (m.get("msg") or {}).get("channel")
                            sid = (m.get("msg") or {}).get("sid")
                            if ch == "orderbook_delta":
                                self._orderbook_sid = sid
                            elif ch == "trade":
                                self._trade_sid = sid
                        if t in ("orderbook_snapshot", "orderbook_delta"):
                            sid = m.get("sid")
                            if sid in self._ignored_orderbook_sids:
                                continue
                            seq = m.get("seq")
                            previous = self._sequences.last(sid)
                            status = self._sequences.track(sid, seq)
                            if status == "duplicate":
                                continue
                            if status == "gap":
                                expected = previous + 1 if previous is not None else seq
                                await self._recover_orderbook(sid, expected, seq)
                                continue
                        self.on_message(m, time.time(), time.monotonic())
            except Exception as e:
                self.connected = False
                self.on_state(f"disconnected: {type(e).__name__}")
                await asyncio.sleep(3)
