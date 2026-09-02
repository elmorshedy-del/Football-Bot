"""Paper executor: fills simulated IOC orders against the LIVE recorded book.

This is the module that answers the question backtests cannot: what depth
actually rests at the moment our order would arrive. Fees use the verified
official formula. No real orders are ever sent."""
import math
import json
import time
from collections import deque
from dataclasses import dataclass

from . import config, store

# Samples buffered before an incremental write; bounds crash loss.
BID_PATH_FLUSH_EVERY = 250
from .execution import ShadowBooks
from .late_score_sleeve import sleeve_exit_reason


class UnsupportedFeeSchedule(ValueError):
    pass


TAKER_QUADRATIC_FEE_TYPES = frozenset({
    "quadratic",
    "quadratic_with_maker_fees",
})


def fee_dollars(contracts, price_c, fee_type="quadratic", fee_multiplier=1.0):
    """Return the taker fee for a fill under a supported live series schedule.

    Maker-enabled series still use Kalshi's standard quadratic formula for an
    immediately executable (taker) order. V2 never posts resting maker orders.
    """
    if fee_type not in TAKER_QUADRATIC_FEE_TYPES or fee_multiplier is None:
        raise UnsupportedFeeSchedule(f"unsupported fee schedule: {fee_type!r}")
    p = price_c / 100.0
    return math.ceil(0.07 * float(fee_multiplier) * contracts * p * (1 - p) * 100) / 100.0


def level_fees(levels, fee_type="quadratic", fee_multiplier=1.0):
    """Calculate fees at each executed price instead of at aggregate VWAP."""
    return [
        (price, quantity, fee_dollars(quantity, price, fee_type, fee_multiplier))
        for price, quantity in levels
    ]


class Position:
    def __init__(self, tid, signal_id, market, event, series, d, side, entry_px, size, ref, ext,
                 fee_type="quadratic", fee_multiplier=1.0, sleeve=None,
                 strategy="gate_a"):
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
        self.fee_type = fee_type
        self.fee_multiplier = fee_multiplier
        self.entry_ts = time.time()
        self.mae = 0.0                # max adverse excursion (side-space cents)
        self.best_bid = entry_px
        self.shadow_stop_hit_px = None
        self.realized_gross = 0.0
        self.exit_fees = 0.0
        self.entry_fees = 0.0
        self.exit_qty = 0.0
        self.exit_vwap_num = 0.0
        self.sleeve = dict(sleeve or {})
        self.strategy = strategy or "gate_a"
        self.sleeve_anchor_bid = None
        self.peak_bid = entry_px
        self.bid_path = deque(maxlen=240)
        # Persisted execution path, separate from `bid_path` above, which the
        # sleeve exit logic owns and must keep at its current shape/length.
        self.exec_path = []
        self.exec_path_last = None
        self.exec_path_dropped = 0
        # Total ever buffered for this position.  The cap must be checked
        # against this, not len(exec_path): the buffer is emptied on every
        # incremental flush, so a length check silently resets the cap.
        self.exec_path_total = 0
        self.exec_path_flush_failed = False
        self.high_dirty = False
        self.max_executable_bid = None
        self.max_executable_bid_ts = None
        self.mfe_c = None

    def unrealized(self, bid):
        return self.realized_gross + (bid - self.entry_px) * self.remaining / 100.0


@dataclass
class PendingEntry:
    signal_id: int
    sig: dict
    meta: dict
    queued_wall: float
    due_mono: float
    attempts: int = 0


@dataclass
class PendingExit:
    due_mono: float
    queued_wall: float
    reason: str
    price_floor: float
    attempts: int = 0


class PaperDesk:
    def __init__(self, broadcast, entry_result=None, realistic=None, error_result=None):
        self.positions = {}           # tid -> Position
        self.broadcast = broadcast
        self.entry_result = entry_result
        self.error_result = error_result
        self.kill = False
        self.realistic = config.PAPER_EXECUTION_V2 if realistic is None else realistic
        self.shadow_books = {
            "gate_a": ShadowBooks(),
            "price_only_late_score": ShadowBooks(),
        }
        # Stable aliases retained for existing tests and operational tooling.
        self.shadows = self.shadow_books["gate_a"]
        self.sleeve_shadows = self.shadow_books["price_only_late_score"]
        self.pending_entries = []
        self.pending_exits = {}       # trade id -> PendingExit

    def restore_open_positions(self, rows):
        """Rehydrate durable positions after a process restart."""
        if not self.realistic:
            return 0
        for row in rows:
            size = float(row["size"] or 0.0)
            remaining = float(row["remaining"] if row["remaining"] is not None else size)
            if remaining <= 1e-9:
                continue
            signal_detail = row.get("signal_detail") or {}
            if isinstance(signal_detail, str):
                try:
                    signal_detail = json.loads(signal_detail)
                except (TypeError, json.JSONDecodeError):
                    signal_detail = {}
            sleeve = signal_detail.get("sleeve") or (
                signal_detail if signal_detail.get("strategy") == "price_only_late_score_v1" else {}
            )
            strategy = row.get("strategy") or (
                "price_only_late_score" if sleeve else "gate_a"
            )
            if strategy == "price_only_late_score_v1":
                strategy = "price_only_late_score"
            pos = Position(
                row["id"], row["signal_id"], row["market"], row["event"], row["series"],
                row["dir"], row["side"], float(row["entry_px"]), size,
                row.get("ref"), row.get("ext"), row.get("fee_type") or "quadratic",
                row.get("fee_multiplier") if row.get("fee_multiplier") is not None else 1.0,
                sleeve, strategy,
            )
            pos.entry_ts = float(row["entry_ts"])
            pos.remaining = remaining
            pos.realized_gross = float(row["realized_gross"] or 0.0)
            pos.entry_fees = float(row["entry_fees"] or 0.0)
            accrued_fees = float(row["accrued_fees"] or pos.entry_fees)
            pos.exit_fees = max(0.0, accrued_fees - pos.entry_fees)
            pos.exit_qty = float(row["exit_qty"] or 0.0)
            pos.exit_vwap_num = float(row["exit_vwap_num"] or 0.0)
            pos.mae = float(row["mae"] or 0.0)
            pos.shadow_stop_hit_px = row["shadow_stop_px"]
            pos.max_executable_bid = row.get("max_executable_bid")
            pos.max_executable_bid_ts = row.get("max_executable_bid_ts")
            pos.mfe_c = row.get("mfe_c")
            # Resume the path at the durable maximum sequence.  Starting from
            # zero made the first post-restart quote reuse sequence 1, which the
            # partial unique index ignored, so the observation was lost in
            # silence and the cap restarted from empty.
            durable_seq = int(row.get("path_max_seq") or 0)
            durable_rows = int(row.get("path_rows_durable") or 0)
            pos.exec_path_total = max(durable_seq, durable_rows)
            pos.exec_path_terminal_written = bool(row.get("path_has_terminal"))
            # The in-memory tail of an abnormally stopped process is gone; the
            # last durable observation is unknown, so no signature is restored
            # and the next quote is recorded rather than deduplicated away.
            pos.exec_path_last = None
            if durable_rows:
                pos.path_restored_from_seq = durable_seq
            self.positions[pos.tid] = pos
        if self.positions:
            self._safe_log("paper", f"restored {len(self.positions)} open paper positions")
        return len(self.positions)

    def invalidate_books(self, tickers):
        if self.realistic:
            for shadows in self.shadow_books.values():
                shadows.invalidate(tickers)

    def apply_book_snapshot(self, ticker, book):
        if self.realistic:
            for shadows in self.shadow_books.values():
                shadows.reset(ticker, book)

    def apply_book_delta(self, ticker, message, sequence=None):
        if self.realistic:
            for shadows in self.shadow_books.values():
                shadows.apply_delta(ticker, message, sequence)

    def _shadow_for(self, strategy):
        try:
            return self.shadow_books[strategy or "gate_a"]
        except KeyError as exc:
            raise ValueError(f"unknown paper strategy: {strategy!r}") from exc

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
        for pending in list(self.pending_entries):
            if pending.due_mono > now_mono:
                continue
            try:
                terminal = self._execute_entry(pending, live_books, now_wall)
            except Exception as exc:
                pending.attempts += 1
                pending.due_mono = now_mono + 0.1
                self._report_error("paper_entry", exc)
                self._safe_log("paper", f"entry retry {pending.signal_id}: {exc!r}")
                self._safe_broadcast({"type": "log", "text":
                                      f"paper entry retry {pending.signal_id}: {exc!r}"})
                continue
            if terminal and pending in self.pending_entries:
                self.pending_entries.remove(pending)
        for tid, order in list(self.pending_exits.items()):
            if order.due_mono > now_mono:
                continue
            try:
                self._execute_exit(tid, order, live_books, now_mono, now_wall)
            except Exception as exc:
                self._reschedule_exit(order, now_mono, now_wall)
                self._report_error("paper_exit", exc)
                self._safe_log("paper", f"exit retry {tid}: {exc!r}")
                self._safe_broadcast({"type": "log", "text":
                                      f"paper exit retry {tid}: {exc!r}"})

    def _execute_entry(self, pending, live_books, now_wall):
        if self.kill:
            return self._finalize_entry_outcome(pending, "killed", now_wall, [])
        book = live_books.get(pending.sig["ticker"])
        if book is None or not book.ok:
            return self._finalize_entry_outcome(pending, "no_book", now_wall, [])
        strategy = pending.sig.get("strategy") or "gate_a"
        shadow = self._shadow_for(strategy).ensure(pending.sig["ticker"], book)
        if not shadow.ok:
            return self._finalize_entry_outcome(pending, "no_book", now_wall, [])
        side = "yes" if pending.sig["dir"] > 0 else "no"
        arrival_book = shadow.snapshot_dict()
        fill = shadow.buy(side, config.NOTIONAL_USD, config.PRICE_CAP, consume=False)
        if fill.quantity < 1 or fill.vwap is None:
            return self._finalize_entry_outcome(
                pending, "rejected_cap", now_wall, fill.levels,
            )
        arrival_book["fill_levels"] = fill.levels
        latency_ms, order_arrival_ms, detail = self._entry_timing(pending, now_wall, fill.levels)
        fee_type = pending.meta.get("fee_type", "quadratic")
        fee_multiplier = pending.meta.get("fee_multiplier", 1.0)
        try:
            fees = level_fees(fill.levels, fee_type, fee_multiplier)
        except UnsupportedFeeSchedule:
            return self._finalize_entry_outcome(
                pending, "unsupported_fee", now_wall, fill.levels,
            )
        entry_fee = sum(level[2] for level in fees)
        trade = {
            "signal_id": pending.signal_id,
            "market": pending.sig["ticker"],
            "event": pending.meta["event"],
            "series": pending.meta["series"],
            "dir": pending.sig["dir"],
            "side": side,
            "entry_ts": now_wall,
            "entry_px": round(fill.vwap, 2),
            "size": fill.quantity,
            "cap": config.PRICE_CAP,
            "notional": config.NOTIONAL_USD,
            "book_at_entry": arrival_book,
            "fee_type": fee_type,
            "fee_multiplier": fee_multiplier,
            "strategy": strategy,
        }
        pos = Position(
            0, pending.signal_id, pending.sig["ticker"], pending.meta["event"],
            pending.meta["series"], pending.sig["dir"], side, fill.vwap,
            fill.quantity, pending.sig["ref"], pending.sig["ext"],
            fee_type, fee_multiplier, pending.sig.get("sleeve"), strategy,
        )
        pos.entry_ts = now_wall
        pos.entry_fees = entry_fee
        source = shadow.no_bids if side == "yes" else shadow.yes_bids
        before = dict(source)
        shadow.consume_buy(side, fill)
        try:
            tid = store.open_paper_trade(
                trade, detail, fees, entry_fee, latency_ms, order_arrival_ms,
            )
        except Exception:
            source.clear()
            source.update(before)
            raise
        pos.tid = tid
        self.positions[tid] = pos
        self._safe_broadcast({"type": "trade_open", "trade": self.pos_dict(pos)})
        self._safe_log(
            "trade", f"OPEN {side.upper()} {pending.sig['ticker']} "
            f"{fill.quantity:.1f}@{fill.vwap:.1f} across {len(fill.levels)} levels",
        )
        self._notify_entry(pending, "filled", detail)
        return True

    def _finalize_entry_outcome(self, pending, outcome, now_wall, levels):
        latency_ms, order_arrival_ms, detail = self._entry_timing(pending, now_wall, levels)
        store.finish_paper_signal(
            pending.signal_id, outcome, detail, latency_ms, order_arrival_ms,
        )
        self._notify_entry(pending, outcome, detail)
        return True

    @staticmethod
    def _entry_timing(pending, now_wall, levels):
        latency_ms = max(0.0, (now_wall - pending.queued_wall) * 1000.0)
        signal_ts_ms = pending.sig.get("ts_ms")
        order_arrival_ms = (
            max(0.0, now_wall * 1000.0 - signal_ts_ms)
            if isinstance(signal_ts_ms, (int, float)) else None
        )
        detail = {
            "paper_latency_ms": round(latency_ms, 3),
            "order_arrival_ms": round(order_arrival_ms, 3) if order_arrival_ms is not None else None,
            "fill_levels": levels,
            "strategy": pending.sig.get("strategy") or "gate_a",
        }
        if pending.sig.get("sleeve"):
            detail["sleeve"] = pending.sig["sleeve"]
        return latency_ms, order_arrival_ms, detail

    def _notify_entry(self, pending, outcome, detail):
        if self.entry_result is not None:
            try:
                self.entry_result(pending.signal_id, pending.sig, outcome, detail)
            except Exception as exc:
                self._report_error("paper_entry_callback", exc)
                self._safe_log("paper", f"entry notification failed: {exc!r}")

    def _report_error(self, component, error):
        if self.error_result is None:
            return
        try:
            self.error_result(component, error)
        except Exception:
            pass

    def _safe_broadcast(self, message):
        try:
            self.broadcast(message)
        except Exception:
            pass

    @staticmethod
    def _safe_log(kind, text):
        try:
            store.log_event(kind, text)
        except Exception:
            pass

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
            "strategy": sig.get("strategy") or "gate_a",
        })
        pos = Position(tid, signal_id, sig["ticker"], meta["event"], meta["series"],
                       sig["dir"], side, entry_px, size, sig["ref"], sig["ext"],
                       sleeve=sig.get("sleeve"),
                       strategy=sig.get("strategy") or "gate_a")
        self.positions[tid] = pos
        self.broadcast({"type": "trade_open", "trade": self.pos_dict(pos)})
        store.log_event("trade", f"OPEN {side.upper()} {sig['ticker']} {size:.0f}@{entry_px:.1f}")
        return "filled"

    def pos_dict(self, pos, bid=None):
        return {"id": pos.tid, "signal_id": pos.signal_id,
                "market": pos.market, "event": pos.event, "series": pos.series,
                "side": pos.side, "dir": pos.dir, "entry_px": round(pos.entry_px, 2),
                "size": round(pos.remaining if self.realistic else pos.size, 1),
                "initial_size": round(pos.initial_size, 1), "entry_ts": pos.entry_ts,
                "bid": bid, "upnl": round(pos.unrealized(bid), 2) if bid is not None else None,
                "strategy": pos.strategy,
                "max_executable_bid": pos.max_executable_bid,
                "max_executable_bid_ts": pos.max_executable_bid_ts,
                "mfe_c": pos.mfe_c,
                "high_after_entry_s": (
                    round(pos.max_executable_bid_ts - pos.entry_ts, 3)
                    if pos.max_executable_bid_ts is not None else None
                )}

    def _record_exec_path(self, pos, book, bid, now):
        """Buffer one held-side quote when it changes.  Memory only.

        A scalar high cannot say whether that high was reachable: 90c resting
        for 200ms in size 1 and 90c resting for 12s in size 500 produce an
        identical max_executable_bid.  Storing the change-log keeps time-at-price
        and depth, so exit rules can be re-tuned without re-collecting.
        """
        try:
            ladder = book.bid_ladder(pos.side)
        except Exception:
            ladder = []
        bid_size = ladder[0][1] if ladder else None
        qty = pos.remaining
        exec_px = None
        if ladder and qty and qty > 0:
            taken, weighted = 0.0, 0.0
            for price, size in ladder:
                take = min(size, qty - taken)
                if take <= 0:
                    break
                weighted += take * price
                taken += take
                if taken >= qty:
                    break
            # Only a fully fillable size gets a VWAP; a partial walk would
            # overstate what the position could actually have realized.
            if taken >= qty and taken > 0:
                exec_px = weighted / taken
        signature = (bid, bid_size, exec_px, qty)
        if signature == pos.exec_path_last:
            return
        pos.exec_path_last = signature
        # One slot is reserved so the terminal row always fits inside the cap.
        if pos.exec_path_total >= store.BID_PATH_MAX_SAMPLES - 1:
            pos.exec_path_dropped += 1
            return
        pos.exec_path_total += 1
        pos.exec_path.append({
            "kind": "position", "trade_id": pos.tid, "signal_id": pos.signal_id,
            "event": pos.event, "market": pos.market, "side": pos.side,
            "strategy": pos.strategy, "anchor_ts": pos.entry_ts,
            "dt_ms": round((now - pos.entry_ts) * 1000.0, 1),
            "bid": bid, "bid_size": bid_size, "exec_px": exec_px, "qty": qty,
            "sample_seq": pos.exec_path_total, "availability": "quote",
            "terminal": 0,
        })
        # Bound how much a crash can lose without paying a commit per quote.
        if len(pos.exec_path) >= BID_PATH_FLUSH_EVERY:
            self._flush_exec_path(pos)

    def _record_exec_gap(self, pos, now):
        """Record that the held side had no executable bid.

        Skipping these left an unexplained hole in the path, so time-at-peak
        was computed across a window in which the peak may not have been
        quotable at all.
        """
        if pos.exec_path_last is None and not pos.exec_path:
            return
        signature = (None, None, None, pos.remaining)
        if signature == pos.exec_path_last:
            return
        pos.exec_path_last = signature
        if pos.exec_path_total >= store.BID_PATH_MAX_SAMPLES - 1:
            pos.exec_path_dropped += 1
            return
        pos.exec_path_total += 1
        pos.exec_path.append({
            "kind": "position", "trade_id": pos.tid, "signal_id": pos.signal_id,
            "event": pos.event, "market": pos.market, "side": pos.side,
            "strategy": pos.strategy, "anchor_ts": pos.entry_ts,
            "dt_ms": round((now - pos.entry_ts) * 1000.0, 1),
            "bid": None, "bid_size": None, "exec_px": None, "qty": pos.remaining,
            "sample_seq": pos.exec_path_total, "availability": "gap",
            "terminal": 0,
        })

    def _record_exec_terminal(self, pos, exit_px, now):
        """Close the path at the exit so the last interval has a real end.

        Without a terminal sample the final segment had no duration and
        time-at-peak was understated whenever the peak was the last quote.
        """
        if getattr(pos, "exec_path_terminal_written", False):
            return
        pos.exec_path_terminal_written = True
        pos.exec_path_total += 1
        pos.exec_path.append({
            "kind": "position", "trade_id": pos.tid, "signal_id": pos.signal_id,
            "event": pos.event, "market": pos.market, "side": pos.side,
            "strategy": pos.strategy, "anchor_ts": pos.entry_ts,
            "dt_ms": round((now - pos.entry_ts) * 1000.0, 1),
            # The executed exit price belongs on trades.exit_px.  A terminal
            # row is only the end timestamp for availability and must never be
            # relabelled as an executable quote.
            "bid": None, "bid_size": None, "exec_px": None,
            "qty": pos.remaining,
            "sample_seq": pos.exec_path_total, "availability": "terminal",
            "terminal": 1,
        })

    def _flush_exec_path(self, pos, final=False):
        """Write the buffered path in one transaction.

        Called incrementally so a restart loses at most BID_PATH_FLUSH_EVERY
        samples, and once more at close.  dt_ms is relative to the restored
        entry_ts, so partial flushes reassemble in dt_ms order.
        """
        if pos.exec_path:
            rows = list(pos.exec_path)
            try:
                written = store.insert_bid_path(rows)
            except Exception as exc:
                # Keep the buffer.  Clearing before the write made a failed
                # write lose the rows permanently; retrying on the next flush
                # is the whole point of buffering.
                pos.exec_path_flush_failed = True
                self._report_error("bid_path", exc)
            else:
                if isinstance(written, int) and written < len(rows):
                    # An ignored row means its sequence key already exists.
                    # That is expected for a retry of rows we know are durable,
                    # and a real fault otherwise, so it is reported rather than
                    # treated as a successful write.
                    self._report_error("bid_path", RuntimeError(
                        f"trade {pos.tid}: {len(rows) - written} of {len(rows)} "
                        "path rows collided with an existing sequence key",
                    ))
                pos.exec_path = pos.exec_path[len(rows):]
                pos.exec_path_flush_failed = False
        if final:
            try:
                samples = store.bid_path_for_trade(pos.tid)
                store.set_trade_path_summary(pos.tid, store.bid_path_summary(
                    samples,
                    truncated=bool(pos.exec_path_dropped),
                    dropped_samples=pos.exec_path_dropped,
                ))
            except Exception as exc:
                self._report_error("bid_path_summary", exc)
        if final and pos.exec_path_dropped:
            self._safe_log(
                "paper",
                f"bid path for trade {pos.tid} truncated at "
                f"{store.BID_PATH_MAX_SAMPLES} samples "
                f"({pos.exec_path_dropped} dropped)",
            )

    def _observe_executable_high(self, pos, bid, now=None):
        """Record the highest executable held-side bid after entry fill."""
        if bid is None:
            return
        now = time.time() if now is None else now
        if pos.max_executable_bid is not None and bid <= pos.max_executable_bid:
            return
        candidate_bid = float(bid)
        candidate_ts = now
        candidate_mfe = max(0.0, candidate_bid - pos.entry_px)
        try:
            store.update_trade_high(pos.tid, candidate_bid, candidate_ts)
        except Exception as exc:
            # Do not advance the in-memory high on a failed write.  Doing so
            # made every later quote compare against a high the database never
            # accepted, so the write was never retried and the high was lost.
            pos.high_dirty = True
            self._report_error("trade_high", exc)
            return
        pos.max_executable_bid = candidate_bid
        pos.max_executable_bid_ts = candidate_ts
        pos.mfe_c = candidate_mfe
        pos.high_dirty = False

    def on_book(self, ticker, book):
        """Mark open positions on this market; handle target exits + shadow metrics."""
        for pos in [p for p in self.positions.values() if p.market == ticker]:
            bid = book.best_yes_bid() if pos.side == "yes" else book.best_no_bid()
            if bid is None:
                self._record_exec_gap(pos, time.time())
                continue
            self._observe_executable_high(pos, bid)
            self._record_exec_path(pos, book, bid, time.time())
            pos.best_bid = bid
            adverse = pos.entry_px - bid
            if adverse > pos.mae:
                pos.mae = adverse
            # shadow stop (recorded, not acted on unless USE_STOP)
            has_stop_inputs = pos.ext is not None and pos.ref is not None
            if has_stop_inputs:
                ext_side = pos.ext if pos.side == "yes" else 100 - pos.ext
                ref_side = pos.ref if pos.side == "yes" else 100 - pos.ref
                stop_lvl = ext_side - config.STOP_FRAC * (ext_side - ref_side)
            else:
                stop_lvl = None
            if stop_lvl is not None and bid <= stop_lvl:
                if pos.shadow_stop_hit_px is None:
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
                continue
            reason = sleeve_exit_reason(pos, bid, time.time(), fee_dollars)
            if reason:
                if self.realistic:
                    # The trigger uses the observed executable bid.  A zero floor
                    # models a taker exit after latency instead of assuming the
                    # scratch or trailing level remains available.
                    self._queue_exit(pos, reason, 0.0)
                else:
                    self.close(pos, bid, reason)

    def check_timeouts(self):
        now = time.time()
        for pos in list(self.positions.values()):
            timeout_s = config.SLEEVE_TIMEOUT_S if pos.sleeve else config.TIMEOUT_S
            if now - pos.entry_ts > timeout_s:
                reason = "sleeve_timeout" if pos.sleeve else "timeout"
                if self.realistic:
                    self._queue_exit(pos, reason, 0.0)
                else:
                    self.close(pos, pos.best_bid, reason)

    def _queue_exit(self, pos, reason, price_floor):
        priority = {
            "target": 1,
            "timeout": 2,
            "sleeve_timeout": 2,
            "sleeve_profit_lock": 3,
            "sleeve_scratch": 3,
            "sleeve_oscillation": 3,
            "stop": 4,
            "sleeve_reversal": 4,
            "flatten": 5,
            "kill": 6,
        }
        current = self.pending_exits.get(pos.tid)
        if current is not None and priority.get(current.reason, 0) >= priority.get(reason, 0):
            return
        self.pending_exits[pos.tid] = PendingExit(
            due_mono=time.monotonic() + config.PAPER_EXIT_LATENCY_MS / 1000.0,
            queued_wall=time.time(),
            reason=reason,
            price_floor=price_floor,
        )

    @staticmethod
    def _reschedule_exit(order, now_mono, now_wall):
        order.attempts += 1
        order.queued_wall = now_wall
        order.due_mono = now_mono + config.PAPER_EXIT_LATENCY_MS / 1000.0

    def _execute_exit(self, tid, order, live_books, now_mono, now_wall):
        pos = self.positions.get(tid)
        if pos is None:
            self.pending_exits.pop(tid, None)
            return
        book = live_books.get(pos.market)
        if book is None or not book.ok:
            self._reschedule_exit(order, now_mono, now_wall)
            return
        shadow = self._shadow_for(pos.strategy).ensure(pos.market, book)
        if not shadow.ok:
            self._reschedule_exit(order, now_mono, now_wall)
            return
        fill = shadow.sell(pos.side, pos.remaining, order.price_floor, consume=False)
        if fill.quantity <= 1e-9 or fill.vwap is None:
            self._reschedule_exit(order, now_mono, now_wall)
            return
        fees = level_fees(
            fill.levels, pos.fee_type, pos.fee_multiplier,
        ) if config.FEE_EXIT_TAKER else [
            (price, quantity, 0.0) for price, quantity in fill.levels
        ]
        exit_fee = sum(level[2] for level in fees)
        remaining = max(0.0, pos.remaining - fill.quantity)
        realized_gross = pos.realized_gross + (
            (fill.vwap - pos.entry_px) * fill.quantity / 100.0
        )
        exit_fees = pos.exit_fees + exit_fee
        exit_qty = pos.exit_qty + fill.quantity
        exit_vwap_num = pos.exit_vwap_num + fill.vwap * fill.quantity
        progress = {
            "remaining": remaining,
            "realized_gross": realized_gross,
            "accrued_fees": pos.entry_fees + exit_fees,
            "exit_qty": exit_qty,
            "exit_vwap_num": exit_vwap_num,
        }
        final = None
        if remaining <= 1e-9:
            final = self._final_values(
                pos, realized_gross, pos.entry_fees + exit_fees, exit_qty, exit_vwap_num,
            )
        source = shadow.yes_bids if pos.side == "yes" else shadow.no_bids
        before = dict(source)
        shadow.consume_sell(pos.side, fill)
        if final is not None:
            # Last write for this trade: the terminal row joins the same
            # transaction as the final fill, progress and closed fields.
            self._record_exec_terminal(pos, final["exit_px"], now_wall)
        try:
            store.record_paper_exit(
                pos.tid, pos.signal_id, pos.side, now_wall, order.reason, fees, progress,
                max(0.0, (now_wall - order.queued_wall) * 1000.0), final,
                path_rows=list(pos.exec_path) if final is not None else None,
                truncated=bool(pos.exec_path_dropped),
                dropped_samples=pos.exec_path_dropped,
            )
        except Exception:
            source.clear()
            source.update(before)
            if final is not None:
                self._retract_terminal(pos)
                pos.exec_path_flush_failed = True
            raise
        if final is not None:
            pos.exec_path = []
            pos.exec_path_flush_failed = False
        pos.remaining = remaining
        pos.realized_gross = realized_gross
        pos.exit_fees = exit_fees
        pos.exit_qty = exit_qty
        pos.exit_vwap_num = exit_vwap_num
        if pos.remaining > 1e-9:
            self._safe_broadcast({"type": "trade_partial", "trade": {
                "id": pos.tid, "market": pos.market, "side": pos.side,
                "filled": round(fill.quantity, 1), "remaining": round(pos.remaining, 1),
                "exit_px": round(fill.vwap, 2), "reason": order.reason,
                "strategy": pos.strategy,
            }})
            self._safe_log(
                "trade", f"PARTIAL CLOSE {pos.market} {fill.quantity:.1f}@{fill.vwap:.1f}; "
                f"{pos.remaining:.1f} remains",
            )
            self._reschedule_exit(order, now_mono, now_wall)
            return
        self._complete_realistic(pos, order.reason, final)

    def settle_market(self, ticker, result):
        """result: 'yes'|'no' in market (YES) space."""
        still_pending = []
        for pending in self.pending_entries:
            if pending.sig["ticker"] == ticker:
                self._finalize_entry_outcome(pending, "expired", time.time(), [])
            else:
                still_pending.append(pending)
        self.pending_entries = still_pending
        for pos in [p for p in self.positions.values() if p.market == ticker]:
            won = (result == "yes") == (pos.side == "yes")
            exit_px = 100.0 if won else 0.0
            if self.realistic:
                qty = pos.remaining
                realized_gross = pos.realized_gross + (exit_px - pos.entry_px) * qty / 100.0
                exit_qty = pos.exit_qty + qty
                exit_vwap_num = pos.exit_vwap_num + exit_px * qty
                progress = {
                    "remaining": 0.0,
                    "realized_gross": realized_gross,
                    "accrued_fees": pos.entry_fees + pos.exit_fees,
                    "exit_qty": exit_qty,
                    "exit_vwap_num": exit_vwap_num,
                }
                final = self._final_values(
                    pos, realized_gross, pos.entry_fees + pos.exit_fees,
                    exit_qty, exit_vwap_num,
                )
                settled_at = time.time()
                self._record_exec_terminal(pos, final["exit_px"], settled_at)
                try:
                    store.record_paper_exit(
                        pos.tid, pos.signal_id, pos.side, settled_at, "settle",
                        [(exit_px, qty, 0.0)], progress, 0.0, final,
                        path_rows=list(pos.exec_path),
                        truncated=bool(pos.exec_path_dropped),
                        dropped_samples=pos.exec_path_dropped,
                    )
                except Exception as exc:
                    # Settlement is the same all-or-nothing contract: keep the
                    # position owned rather than half-settling it.
                    self._retract_terminal(pos)
                    pos.exec_path_flush_failed = True
                    self._report_error("paper_settle", exc)
                    continue
                pos.exec_path = []
                pos.exec_path_flush_failed = False
                pos.realized_gross = realized_gross
                pos.exit_qty = exit_qty
                pos.exit_vwap_num = exit_vwap_num
                pos.remaining = 0.0
                self._complete_realistic(pos, "settle", final)
            else:
                self.close(pos, exit_px, "settle")

    @staticmethod
    def _final_values(pos, gross, fees, exit_qty, exit_vwap_num):
        exit_px = exit_vwap_num / exit_qty if exit_qty > 1e-9 else pos.entry_px
        return {
            "exit_px": round(exit_px, 2),
            "gross": round(gross, 2),
            "fees": round(fees, 2),
            "net": round(gross - fees, 2),
            "mae": round(pos.mae, 2),
            "shadow_stop_px": pos.shadow_stop_hit_px,
        }

    def _complete_realistic(self, pos, reason, final):
        # The path, summary and closed fields were already committed with the
        # final fill by record_paper_exit(); reaching here means that
        # transaction succeeded, so the position may finally be released.
        self.positions.pop(pos.tid, None)
        self.pending_exits.pop(pos.tid, None)
        self._safe_broadcast({"type": "trade_close", "trade": {
            "id": pos.tid, "market": pos.market, "series": pos.series, "side": pos.side,
            "entry_px": round(pos.entry_px, 2), "exit_px": final["exit_px"],
            "size": round(pos.initial_size, 1), "reason": reason, "net": final["net"],
            "strategy": pos.strategy,
        }})
        self._safe_log("trade", f"CLOSE {pos.market} {reason} net ${final['net']:+.2f}")

    def close(self, pos, exit_px, reason):
        exit_px = min(max(exit_px, 0.0), 100.0)
        gross = (exit_px - pos.entry_px) * pos.size / 100.0
        fees = fee_dollars(pos.size, pos.entry_px)
        if reason != "settle" and config.FEE_EXIT_TAKER:
            fees += fee_dollars(pos.size, exit_px)
        net = gross - fees
        # Terminal row first, then ONE transaction covering path, summary and
        # the closed-trade fields.  Closing before flushing the path left a
        # closed trade whose final rows had no owner when the write failed.
        self._record_exec_terminal(pos, round(exit_px, 2), time.time())
        try:
            store.close_trade(
                pos.tid, round(exit_px, 2), reason, round(gross, 2),
                round(fees, 2), round(net, 2), round(pos.mae, 2),
                pos.shadow_stop_hit_px,
                path_rows=list(pos.exec_path),
                truncated=bool(pos.exec_path_dropped),
                dropped_samples=pos.exec_path_dropped,
            )
        except Exception as exc:
            # Nothing committed: keep owning the position so a later attempt can
            # retry the same sequence keys.  No close is broadcast or logged.
            pos.exec_path_flush_failed = True
            self._retract_terminal(pos)
            self._report_error("paper_close", exc)
            return False
        pos.exec_path = []
        pos.exec_path_flush_failed = False
        self.positions.pop(pos.tid, None)
        self.broadcast({"type": "trade_close", "trade": {
            "id": pos.tid, "market": pos.market, "series": pos.series, "side": pos.side,
            "entry_px": round(pos.entry_px, 2), "exit_px": round(exit_px, 2),
            "size": round(pos.size, 1), "reason": reason, "net": round(net, 2),
            "strategy": pos.strategy}})
        store.log_event("trade", f"CLOSE {pos.market} {reason} net ${net:+.2f}")
        return True

    def _retract_terminal(self, pos):
        """Undo an unpersisted terminal row so a retry appends exactly one.

        The row stays in the buffer only if it was already durable; otherwise a
        retry would append a second terminal sample under a new sequence key.
        """
        while pos.exec_path and pos.exec_path[-1].get("terminal"):
            pos.exec_path.pop()
            pos.exec_path_total = max(0, pos.exec_path_total - 1)
        pos.exec_path_terminal_written = False

    def flatten_all(self, reason="flatten"):
        for pos in list(self.positions.values()):
            if self.realistic:
                self._queue_exit(pos, reason, 0.0)
            else:
                self.close(pos, pos.best_bid, reason)
