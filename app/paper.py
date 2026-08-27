"""Paper executor: fills simulated IOC orders against the LIVE recorded book.

This is the module that answers the question backtests cannot: what depth
actually rests at the moment our order would arrive. Fees use the verified
official formula. No real orders are ever sent."""
import math
import time
from dataclasses import dataclass

from . import config, store
from .execution import ShadowBooks


def fee_dollars(contracts, price_c):
    p = price_c / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100.0


class Position:
    def __init__(self, tid, signal_id, market, event, series, d, side, entry_px, size, ref, ext):
        self.tid = tid
        self.signal_id = signal_id
        self.market = market
        self.event = event
        self.series = series
        self.dir = d
        self.side = side              # 'yes' or 'no' (what we bought)
        self.entry_px = entry_px      # in side-space cents
        self.size = size
        self.initial_size = size
        self.remaining = size
        self.ref = ref                # signal reference (yes-space)
        self.ext = ext
        self.entry_ts = time.time()
        self.mae = 0.0                # max adverse excursion (side-space cents)
        self.best_bid = entry_px
        self.shadow_stop_hit_px = None
        self.realized_gross = 0.0
        self.exit_fees = 0.0
        self.exit_qty = 0.0
        self.exit_vwap_num = 0.0

    def unrealized(self, bid):
        return self.realized_gross + (bid - self.entry_px) * self.remaining / 100.0


@dataclass
class PendingEntry:
    signal_id: int
    sig: dict
    meta: dict
    queued_wall: float
    due_mono: float


class PaperDesk:
    def __init__(self, broadcast, entry_result=None, realistic=None):
        self.positions = {}           # tid -> Position
        self.broadcast = broadcast
        self.entry_result = entry_result
        self.kill = False
        self.realistic = config.PAPER_EXECUTION_V2 if realistic is None else realistic
        self.shadows = ShadowBooks()
        self.pending_entries = []
        self.pending_exits = {}       # trade id -> (due_mono, queued_wall, reason, floor)

    def invalidate_books(self, tickers):
        if self.realistic:
            self.shadows.invalidate(tickers)

    # ---------- entries ----------
    def queue_enter(self, signal_id, sig, meta, now_mono=None, now_wall=None):
        """Queue an IOC to reach the book after configured entry latency."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        now_wall = time.time() if now_wall is None else now_wall
        self.pending_entries.append(PendingEntry(
            signal_id=signal_id,
            sig=dict(sig),
            meta=dict(meta),
            queued_wall=now_wall,
            due_mono=now_mono + config.PAPER_ENTRY_LATENCY_MS / 1000.0,
        ))
        return "queued"

    def process_pending(self, live_books, now_mono=None, now_wall=None):
        """Execute every paper order whose simulated arrival time has passed."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        now_wall = time.time() if now_wall is None else now_wall
        ready = [p for p in self.pending_entries if p.due_mono <= now_mono]
        self.pending_entries = [p for p in self.pending_entries if p.due_mono > now_mono]
        for pending in ready:
            self._execute_entry(pending, live_books, now_wall)
        for tid, order in list(self.pending_exits.items()):
            due_mono, queued_wall, reason, floor = order
            if due_mono <= now_mono:
                self._execute_exit(tid, live_books, now_wall, queued_wall, reason, floor)

    def _execute_entry(self, pending, live_books, now_wall):
        if self.kill:
            self._report_entry(pending, "killed", now_wall, [])
            return
        book = live_books.get(pending.sig["ticker"])
        if book is None or not book.ok:
            self._report_entry(pending, "no_book", now_wall, [])
            return
        shadow = self.shadows.ensure(pending.sig["ticker"], book)
        if not shadow.ok:
            self._report_entry(pending, "no_book", now_wall, [])
            return
        side = "yes" if pending.sig["dir"] > 0 else "no"
        arrival_book = shadow.snapshot_dict()
        fill = shadow.buy(side, config.NOTIONAL_USD, config.PRICE_CAP)
        if fill.quantity < 1 or fill.vwap is None:
            self._report_entry(pending, "rejected_cap", now_wall, fill.levels)
            return
        arrival_book["fill_levels"] = fill.levels
        tid = store.insert_trade({
            "signal_id": pending.signal_id,
            "market": pending.sig["ticker"],
            "event": pending.meta["event"],
            "series": pending.meta["series"],
            "dir": pending.sig["dir"],
            "side": side,
            "entry_ts": now_wall,
            "entry_px": round(fill.vwap, 2),
            "size": round(fill.quantity, 1),
            "cap": config.PRICE_CAP,
            "notional": config.NOTIONAL_USD,
            "book_at_entry": arrival_book,
        })
        pos = Position(
            tid, pending.signal_id, pending.sig["ticker"], pending.meta["event"],
            pending.meta["series"], pending.sig["dir"], side, fill.vwap,
            fill.quantity, pending.sig["ref"], pending.sig["ext"],
        )
        pos.entry_ts = now_wall
        self.positions[tid] = pos
        self.broadcast({"type": "trade_open", "trade": self.pos_dict(pos)})
        store.log_event(
            "trade", f"OPEN {side.upper()} {pending.sig['ticker']} "
            f"{fill.quantity:.1f}@{fill.vwap:.1f} across {len(fill.levels)} levels",
        )
        self._report_entry(pending, "filled", now_wall, fill.levels)

    def _report_entry(self, pending, outcome, now_wall, levels):
        latency_ms = max(0.0, (now_wall - pending.queued_wall) * 1000.0)
        store.add_latency("paper_entry", latency_ms)
        detail = {"paper_latency_ms": round(latency_ms, 3), "fill_levels": levels}
        if self.entry_result is not None:
            self.entry_result(pending.signal_id, pending.sig, outcome, detail)

    def try_enter(self, signal_id, sig, meta, book):
        """Original immediate paper path, preserved exactly while V2 is off."""
        if self.kill:
            return "killed"
        if book is None or not book.ok:
            return "no_book"
        side = "yes" if sig["dir"] > 0 else "no"
        ladder = book.ask_ladder(side)
        want_usd = config.NOTIONAL_USD
        size, cost, vwap_num = 0.0, 0.0, 0.0
        for px, avail in ladder:
            if px > config.PRICE_CAP:
                break
            take = min(avail, (want_usd - cost) / (px / 100.0))
            if take <= 0:
                break
            size += take
            cost += take * px / 100.0
            vwap_num += take * px
            if cost >= want_usd - 0.01:
                break
        if size < 1:
            return "rejected_cap"
        entry_px = vwap_num / size
        tid = store.insert_trade({
            "signal_id": signal_id, "market": sig["ticker"], "event": meta["event"],
            "series": meta["series"], "dir": sig["dir"], "side": side,
            "entry_ts": time.time(), "entry_px": round(entry_px, 2), "size": round(size, 1),
            "cap": config.PRICE_CAP, "notional": want_usd,
            "book_at_entry": book.snapshot_dict(),
        })
        pos = Position(tid, signal_id, sig["ticker"], meta["event"], meta["series"],
                       sig["dir"], side, entry_px, size, sig["ref"], sig["ext"])
        self.positions[tid] = pos
        self.broadcast({"type": "trade_open", "trade": self.pos_dict(pos)})
        store.log_event("trade", f"OPEN {side.upper()} {sig['ticker']} {size:.0f}@{entry_px:.1f}")
        return "filled"

    def pos_dict(self, pos, bid=None):
        return {"id": pos.tid, "market": pos.market, "event": pos.event, "series": pos.series,
                "side": pos.side, "dir": pos.dir, "entry_px": round(pos.entry_px, 2),
                "size": round(pos.remaining if self.realistic else pos.size, 1),
                "initial_size": round(pos.initial_size, 1), "entry_ts": pos.entry_ts,
                "bid": bid, "upnl": round(pos.unrealized(bid), 2) if bid is not None else None}

    def on_book(self, ticker, book):
        """Mark open positions on this market; handle target exits + shadow metrics."""
        for pos in [p for p in self.positions.values() if p.market == ticker]:
            bid = book.best_yes_bid() if pos.side == "yes" else book.best_no_bid()
            if bid is None:
                continue
            pos.best_bid = bid
            adverse = pos.entry_px - bid
            if adverse > pos.mae:
                pos.mae = adverse
            # shadow stop (recorded, not acted on unless USE_STOP)
            ext_side = pos.ext if pos.side == "yes" else 100 - pos.ext
            ref_side = pos.ref if pos.side == "yes" else 100 - pos.ref
            stop_lvl = ext_side - config.STOP_FRAC * (ext_side - ref_side)
            if bid <= stop_lvl and pos.shadow_stop_hit_px is None:
                pos.shadow_stop_hit_px = bid
                if config.USE_STOP:
                    if self.realistic:
                        self._queue_exit(pos, "stop", 0.0)
                    else:
                        self.close(pos, bid, "stop")
                    continue
            if bid >= config.TARGET:
                if self.realistic:
                    self._queue_exit(pos, "target", config.TARGET)
                else:
                    self.close(pos, config.TARGET, "target")

    def check_timeouts(self):
        now = time.time()
        for pos in list(self.positions.values()):
            if now - pos.entry_ts > config.TIMEOUT_S:
                if self.realistic:
                    self._queue_exit(pos, "timeout", 0.0)
                else:
                    self.close(pos, pos.best_bid, "timeout")

    def _queue_exit(self, pos, reason, price_floor):
        if pos.tid in self.pending_exits:
            return
        self.pending_exits[pos.tid] = (
            time.monotonic() + config.PAPER_EXIT_LATENCY_MS / 1000.0,
            time.time(),
            reason,
            price_floor,
        )

    def _execute_exit(self, tid, live_books, now_wall, queued_wall, reason, price_floor):
        pos = self.positions.get(tid)
        if pos is None:
            self.pending_exits.pop(tid, None)
            return
        book = live_books.get(pos.market)
        if book is None or not book.ok:
            self.pending_exits.pop(tid, None)
            return
        shadow = self.shadows.ensure(pos.market, book)
        if not shadow.ok:
            self.pending_exits.pop(tid, None)
            return
        fill = shadow.sell(pos.side, pos.remaining, price_floor)
        self.pending_exits.pop(tid, None)
        store.add_latency("paper_exit", max(0.0, (now_wall - queued_wall) * 1000.0))
        if fill.quantity <= 1e-9 or fill.vwap is None:
            return
        pos.remaining = max(0.0, pos.remaining - fill.quantity)
        pos.realized_gross += (fill.vwap - pos.entry_px) * fill.quantity / 100.0
        pos.exit_qty += fill.quantity
        pos.exit_vwap_num += fill.vwap * fill.quantity
        if config.FEE_EXIT_TAKER:
            pos.exit_fees += fee_dollars(fill.quantity, fill.vwap)
        if pos.remaining > 1e-9:
            self.broadcast({"type": "trade_partial", "trade": {
                "id": pos.tid, "market": pos.market, "side": pos.side,
                "filled": round(fill.quantity, 1), "remaining": round(pos.remaining, 1),
                "exit_px": round(fill.vwap, 2), "reason": reason,
            }})
            store.log_event(
                "trade", f"PARTIAL CLOSE {pos.market} {fill.quantity:.1f}@{fill.vwap:.1f}; "
                f"{pos.remaining:.1f} remains",
            )
            return
        self._finalize_realistic(pos, reason)

    def settle_market(self, ticker, result):
        """result: 'yes'|'no' in market (YES) space."""
        still_pending = []
        for pending in self.pending_entries:
            if pending.sig["ticker"] == ticker:
                self._report_entry(pending, "expired", time.time(), [])
            else:
                still_pending.append(pending)
        self.pending_entries = still_pending
        for pos in [p for p in self.positions.values() if p.market == ticker]:
            won = (result == "yes") == (pos.side == "yes")
            exit_px = 100.0 if won else 0.0
            if self.realistic:
                qty = pos.remaining
                pos.realized_gross += (exit_px - pos.entry_px) * qty / 100.0
                pos.exit_qty += qty
                pos.exit_vwap_num += exit_px * qty
                pos.remaining = 0.0
                self._finalize_realistic(pos, "settle")
            else:
                self.close(pos, exit_px, "settle")

    def _finalize_realistic(self, pos, reason):
        exit_px = pos.exit_vwap_num / pos.exit_qty if pos.exit_qty > 1e-9 else pos.entry_px
        gross = pos.realized_gross
        fees = fee_dollars(pos.initial_size, pos.entry_px) + pos.exit_fees
        net = gross - fees
        store.close_trade(
            pos.tid, round(exit_px, 2), reason, round(gross, 2), round(fees, 2),
            round(net, 2), round(pos.mae, 2), pos.shadow_stop_hit_px,
        )
        self.positions.pop(pos.tid, None)
        self.pending_exits.pop(pos.tid, None)
        self.broadcast({"type": "trade_close", "trade": {
            "id": pos.tid, "market": pos.market, "series": pos.series, "side": pos.side,
            "entry_px": round(pos.entry_px, 2), "exit_px": round(exit_px, 2),
            "size": round(pos.initial_size, 1), "reason": reason, "net": round(net, 2),
        }})
        store.log_event("trade", f"CLOSE {pos.market} {reason} net ${net:+.2f}")

    def close(self, pos, exit_px, reason):
        exit_px = min(max(exit_px, 0.0), 100.0)
        gross = (exit_px - pos.entry_px) * pos.size / 100.0
        fees = fee_dollars(pos.size, pos.entry_px)
        if reason != "settle" and config.FEE_EXIT_TAKER:
            fees += fee_dollars(pos.size, exit_px)
        net = gross - fees
        store.close_trade(pos.tid, round(exit_px, 2), reason, round(gross, 2),
                          round(fees, 2), round(net, 2), round(pos.mae, 2),
                          pos.shadow_stop_hit_px)
        self.positions.pop(pos.tid, None)
        self.broadcast({"type": "trade_close", "trade": {
            "id": pos.tid, "market": pos.market, "series": pos.series, "side": pos.side,
            "entry_px": round(pos.entry_px, 2), "exit_px": round(exit_px, 2),
            "size": round(pos.size, 1), "reason": reason, "net": round(net, 2)}})
        store.log_event("trade", f"CLOSE {pos.market} {reason} net ${net:+.2f}")

    def flatten_all(self, reason="flatten"):
        for pos in list(self.positions.values()):
            if self.realistic:
                self._queue_exit(pos, reason, 0.0)
            else:
                self.close(pos, pos.best_bid, reason)
