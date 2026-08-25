"""Kalshi REST + WebSocket client with RSA-PSS request signing."""
import asyncio
import base64
import json
import time

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config


def _load_private_key():
    pem = config.KALSHI_PRIVATE_KEY
    if not pem and config.KALSHI_PRIVATE_KEY_PATH:
        with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            pem = f.read().decode()
    if not pem:
        return None
    return serialization.load_pem_private_key(pem.encode(), password=None)


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
        """Reconcile subscription set to `tickers` (adds/removes)."""
        async with self._lock:
            want = set(tickers)
            if not self.connected or not want and not self._subscribed:
                self._subscribed = want if self.connected else set()
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
                        self.on_message(m, time.time(), time.monotonic())
            except Exception as e:
                self.connected = False
                self.on_state(f"disconnected: {type(e).__name__}")
                await asyncio.sleep(3)
