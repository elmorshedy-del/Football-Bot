"""Read-only Kalshi live-score versus market-arrival latency observer.

The observer intentionally has no callback into Detector or PaperDesk.  It
compares deterministic score fields from Kalshi live data, records the local
receipt boundary, and correlates that boundary with in-memory market changes.
"""
import asyncio
import json
import re
import time

from . import config, store

# Events a later correction may point back at.
_SUBSTANTIVE_FOR_REVISION = {
    "goal.observed", "penalty.scored", "score.correction",
    "goal.disallowed", "var.overturned",
}
from .match_clock import MatchClockTracker, is_persisted_id, parse_current_clock
from .match_events import (
    iter_provider_event_rows,
    normalize_match_event,
    period_status_event,
)

_SCORE_KEY = re.compile(r"score", re.IGNORECASE)


def _number(value):
    """Return a stable numeric scalar or None; booleans are not scores."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def score_signature(details):
    """Extract numeric fields at or below a key containing ``score``.

    Kalshi documents ``live_data.details`` as flexible JSON, so this accepts
    snake_case, camelCase, and nested score objects without guessing a league-
    specific schema.  Player statistics are not requested by the observer.
    """
    found = {}

    def walk(value, path=(), inside_score=False):
        scalar = _number(value)
        if scalar is not None:
            if inside_score:
                found[".".join(path)] = scalar
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key = str(key)
                walk(child, path + (key,), inside_score or bool(_SCORE_KEY.search(key)))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + (str(index),), inside_score)

    walk(details)
    return dict(sorted(found.items()))


def classify_score_change(before, after):
    """Classify a score transition without interpreting natural language."""
    keys = set(before) | set(after)
    deltas = [after.get(key, 0.0) - before.get(key, 0.0) for key in keys]
    if any(delta < 0 for delta in deltas):
        return "score_correction"
    if any(delta > 0 for delta in deltas):
        return "goal"
    return "score_schema_change"


def correlate_market_window(observations, goal_mono, before=True):
    """Return the nearest book and trade observation on one side of a goal."""
    selected = {}
    for observation in observations:
        delta = observation["mono"] - goal_mono
        if before and delta > 0:
            continue
        if not before and delta < 0:
            continue
        kind = "trade" if observation["kind"] == "trade" else "book"
        current = selected.get(kind)
        if current is None or abs(delta) < abs(current["delta_ms"] / 1000.0):
            selected[kind] = {
                **observation,
                "delta_ms": round(delta * 1000.0, 3),
            }
    return selected


class GoalLatencyObserver:
    def __init__(self, client, event_tickers, market_window, clock_tracker=None):
        self.client = client
        self.event_tickers = event_tickers
        self.market_window = market_window
        self.clock_tracker = clock_tracker or MatchClockTracker()
        self.milestones = {}            # event ticker -> milestone id
        self.events_by_milestone = {}
        self.scores = {}                # milestone id -> deterministic signature
        self.last_poll_ts = {}          # milestone id -> prior successful receipt
        self.pending = []               # rows awaiting post-goal market observations
        self.last_mapping_attempt = {}
        self.last_error = None
        self.last_poll_wall = None
        self.last_response_ms = None
        self.polls = 0
        self.scoreless_payloads = 0
        self.goals = 0
        self.corrections = 0
        self.clocks_recorded = 0
        self.provider_events_recorded = 0
        self.seen_fingerprints = {}     # event -> set of fingerprints
        # (event, fingerprint) -> newest poll metadata for an already-recorded
        # occurrence.  Held in memory and flushed in one transaction; see
        # `_flush_provider_refreshes`.
        self.pending_refreshes = {}
        self.last_refresh_flush = 0.0
        self.refresh_flushes = 0
        self.refreshes_written = 0
        self.lifecycle_state = {}       # event -> {period, status}
        # event -> fingerprint of the last substantive event, so a correction
        # arriving on a later poll still links to what it revises.
        self.last_substantive_fingerprint = {}

    async def _resolve_new_events(self):
        now = time.time()
        active = set(self.event_tickers())
        dropped = set(self.milestones) - active
        if dropped:
            # The event is leaving the watch list, so its buffered observation
            # times must reach SQLite before the in-memory state is discarded.
            await self._flush_provider_refreshes(force=True)
        for event in dropped:
            milestone_id = self.milestones.pop(event)
            self.events_by_milestone.pop(milestone_id, None)
            self.scores.pop(milestone_id, None)
            self.last_poll_ts.pop(milestone_id, None)
            self.seen_fingerprints.pop(event, None)
            for key in [k for k in self.pending_refreshes if k[0] == event]:
                self.pending_refreshes.pop(key, None)
            self.lifecycle_state.pop(event, None)
            self.last_substantive_fingerprint.pop(event, None)
            self.clock_tracker.drop_event(event)
        for event in sorted(active - set(self.milestones)):
            if now - self.last_mapping_attempt.get(event, 0.0) < 30.0:
                continue
            self.last_mapping_attempt[event] = now
            try:
                response = await self.client.get(
                    "/milestones", limit=10, related_event_ticker=event,
                )
                choices = [m for m in response.get("milestones") or []
                           if event in (m.get("related_event_tickers") or [])]
                if not choices:
                    continue
                milestone = choices[0]
                milestone_id = str(milestone["id"])
                self.milestones[event] = milestone_id
                self.events_by_milestone[milestone_id] = event
                self.clock_tracker.set_mapping(event, milestone_id)
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                self.last_error = f"milestone {event}: {type(exc).__name__}: {exc}"
                self.clock_tracker.set_mapping(event, None, error=self.last_error)

    async def _record_change(self, live_data, before, after, timing):
        milestone_id = str(live_data.get("milestone_id") or "")
        event = self.events_by_milestone.get(milestone_id)
        if not event:
            return
        change_kind = classify_score_change(before, after)
        if change_kind == "goal":
            self.goals += 1
        elif change_kind == "score_correction":
            self.corrections += 1

        recent = self.market_window(event, timing["received_mono"],
                                    config.GOAL_LATENCY_LOOKBACK_S, 0.0)
        prior = correlate_market_window(recent, timing["received_mono"], before=True)
        book = prior.get("book")
        trade = prior.get("trade")
        row = {
            "observed_ts": timing["received_wall"],
            "event": event,
            "milestone_id": milestone_id,
            "change_kind": change_kind,
            "live_type": live_data.get("type"),
            "score_before": before,
            "score_after": after,
            "previous_poll_ts": self.last_poll_ts.get(milestone_id),
            "poll_started_ts": timing["started_wall"],
            "response_ms": timing["response_ms"],
            "last_book_change_ts": book.get("wall") if book else None,
            "last_book_lead_ms": -book["delta_ms"] if book else None,
            "last_trade_ts": trade.get("wall") if trade else None,
            "last_trade_lead_ms": -trade["delta_ms"] if trade else None,
            "detail": {
                "poll_uncertainty_ms": (
                    round((timing["received_wall"] - self.last_poll_ts[milestone_id]) * 1000, 3)
                    if milestone_id in self.last_poll_ts else None
                ),
                "live_data": live_data,
                "prior_book": book,
                "prior_trade": trade,
                "market_window_before": recent,
            },
        }
        row["normalized_event"] = normalize_match_event(
            change_kind, before, after, live_data,
        )
        row_id = await asyncio.to_thread(store.insert_goal_latency, row)
        self.pending.append({
            "id": row_id,
            "event": event,
            "goal_mono": timing["received_mono"],
            "finish_mono": timing["received_mono"] + config.GOAL_LATENCY_AFTER_S,
        })
        book_text = f"{row['last_book_lead_ms']:.1f}ms" if book else "none"
        trade_text = f"{row['last_trade_lead_ms']:.1f}ms" if trade else "none"
        store.log_event(
            "goal_latency",
            f"{row['normalized_event']['canonical_type']} {event} "
            f"{row['normalized_event']['human_label']}; last_book_led={book_text}; "
            f"last_trade_led={trade_text}",
        )

    async def _record_clock(self, event, milestone_id, details, timing):
        parsed = parse_current_clock(details)
        row = self.clock_tracker.observe(event, milestone_id, parsed, {
            **timing,
            "previous_poll_ts": self.last_poll_ts.get(milestone_id),
        })
        if row is None:
            return parsed
        # Persist first, publish second.  Until this returns a positive row id
        # the previous persisted observation stays the only decision-visible
        # one, so a signal racing this await cannot read an id-less clock.
        try:
            row_id = await asyncio.to_thread(store.insert_match_clock, row)
        except Exception as exc:
            self.clock_tracker.fail_persist(event, exc)
            store.log_event(
                "match_clock",
                f"{event}: clock insert failed ({exc!r}); retrying on next poll",
            )
            return parsed
        if not is_persisted_id(row_id):
            self.clock_tracker.fail_persist(
                event, ValueError(f"insert returned non-positive id {row_id!r}"),
            )
            return parsed
        self.clock_tracker.promote(event, row_id)
        self.clocks_recorded += 1
        return parsed

    async def _record_provider_events(self, event, milestone_id, live_data, parsed, timing):
        seen = self.seen_fingerprints.setdefault(event, set())
        previous_lifecycle = self.lifecycle_state.get(event)
        current_lifecycle = {
            "period": parsed.provider_period if parsed else None,
            "status": parsed.provider_status if parsed else None,
        }
        candidates = list(iter_provider_event_rows(live_data))
        lifecycle = period_status_event(previous_lifecycle, current_lifecycle, live_data)
        if lifecycle is not None:
            candidates.append(lifecycle)
        self.lifecycle_state[event] = current_lifecycle
        # A correction usually arrives on a LATER poll than the goal it
        # corrects.  Resetting this per poll meant every such correction was
        # stored with previous_fingerprint=null and the revision chain broke.
        # In-memory state is preferred; after a restart it is empty, so the
        # link is resolved from durable same-event, same-mode history instead.
        previous_fingerprint = self.last_substantive_fingerprint.get(event)
        if previous_fingerprint is None:
            previous_fingerprint = await asyncio.to_thread(
                store.previous_substantive_fingerprint, event,
            )
            if previous_fingerprint is not None:
                self.last_substantive_fingerprint[event] = previous_fingerprint
        for item in candidates:
            fingerprint = item["fingerprint"]
            if fingerprint in seen:
                # Already recorded.  Only the poll metadata changes, so it is
                # buffered and written in one batch (see
                # `_flush_provider_refreshes`).  Writing it here cost a
                # SELECT+UPDATE+COMMIT under the writer lock per known event per
                # poll: with five mapped matches and dozens of events each, the
                # 250 ms poll measured 5.7 s p50 / 30 s max on 2026-09-04.
                self.pending_refreshes[(event, fingerprint)] = {
                    "observed_ts": timing["received_wall"],
                    "poll_started_ts": timing["started_wall"],
                    "previous_poll_ts": self.last_poll_ts.get(milestone_id),
                    "response_ms": timing["response_ms"],
                    "event": event,
                    "fingerprint": fingerprint,
                }
                continue
            seen.add(fingerprint)
            row = {
                "observed_ts": timing["received_wall"],
                "poll_started_ts": timing["started_wall"],
                "previous_poll_ts": self.last_poll_ts.get(milestone_id),
                "response_ms": timing["response_ms"],
                "event": event,
                "milestone_id": milestone_id,
                "fingerprint": fingerprint,
                "previous_fingerprint": (
                    previous_fingerprint if item["canonical_type"] in {
                        "score.correction", "goal.disallowed", "var.overturned",
                    } else None
                ),
                "canonical_type": item["canonical_type"],
                "canonical_side": item.get("side"),
                "provider_period": parsed.provider_period if parsed else None,
                "provider_minute": item.get("provider_minute"),
                "provider_stoppage": item.get("provider_stoppage"),
                "provider_clock": item.get("provider_clock"),
                "normalized_event": item,
                "raw_payload": item.get("raw") or live_data,
            }
            inserted, is_new = await asyncio.to_thread(store.upsert_provider_event, row)
            if item["canonical_type"] in _SUBSTANTIVE_FOR_REVISION:
                previous_fingerprint = fingerprint
                self.last_substantive_fingerprint[event] = fingerprint
            if is_new:
                self.provider_events_recorded += 1
                previous_fingerprint = fingerprint
            _ = inserted

    async def _flush_provider_refreshes(self, force=False, now=None):
        """Write the buffered `last_observed_ts` refreshes in one transaction.

        New fingerprints still insert immediately, because the insert carries
        the observation itself.  A repeat sighting carries nothing new except
        when it was last seen, which is exactly what can wait for a batch.

        On failure the buffer is kept and retried on the next flush, so a
        transient database problem loses no observation time.
        """
        if not self.pending_refreshes:
            return 0
        now = time.time() if now is None else now
        interval = max(float(config.PROVIDER_EVENT_FLUSH_S), 1.0)
        if not force and now - self.last_refresh_flush < interval:
            return 0
        rows = list(self.pending_refreshes.values())
        self.last_refresh_flush = now
        try:
            written = await asyncio.to_thread(store.refresh_provider_events, rows)
        except Exception as exc:  # noqa: BLE001 - retried on the next flush
            self.last_error = f"provider refresh: {type(exc).__name__}: {exc}"
            return 0
        self.pending_refreshes.clear()
        self.refresh_flushes += 1
        self.refreshes_written += int(written or 0)
        return written

    async def _poll(self):
        milestone_ids = sorted(self.events_by_milestone)
        if not milestone_ids:
            return
        started_wall = time.time()
        started_mono = time.monotonic()
        response = await self.client.get(
            "/live_data/batch", milestone_ids=milestone_ids,
            include_player_stats=False,
        )
        received_mono = time.monotonic()
        received_wall = time.time()
        timing = {
            "started_wall": started_wall,
            "received_wall": received_wall,
            "received_mono": received_mono,
            "response_ms": round((received_mono - started_mono) * 1000.0, 3),
        }
        self.polls += 1
        self.last_poll_wall = received_wall
        self.last_response_ms = timing["response_ms"]
        store.add_latency("match_response_ms", timing["response_ms"])
        for live_data in response.get("live_datas") or []:
            milestone_id = str(live_data.get("milestone_id") or "")
            if milestone_id not in self.events_by_milestone:
                continue
            event = self.events_by_milestone[milestone_id]
            details = live_data.get("details") or {}
            parsed = await self._record_clock(event, milestone_id, details, timing)
            await self._record_provider_events(
                event, milestone_id, live_data, parsed, timing,
            )
            signature = score_signature(details)
            if not signature:
                self.scoreless_payloads += 1
                self.last_poll_ts[milestone_id] = received_wall
                continue
            previous = self.scores.get(milestone_id)
            if previous is not None and previous != signature:
                await self._record_change(live_data, previous, signature, timing)
            self.scores[milestone_id] = signature
            self.last_poll_ts[milestone_id] = received_wall

    async def _finish_pending(self):
        now = time.monotonic()
        remaining = []
        for pending in self.pending:
            if now < pending["finish_mono"]:
                remaining.append(pending)
                continue
            observations = self.market_window(
                pending["event"], pending["goal_mono"], 0.0,
                config.GOAL_LATENCY_AFTER_S,
            )
            after = correlate_market_window(observations, pending["goal_mono"], before=False)
            await asyncio.to_thread(
                store.finish_goal_latency, pending["id"], after.get("book"), after.get("trade"),
            )
        self.pending = remaining

    async def mapping_task(self):
        """Resolve event -> milestone mappings on their own slower cadence.

        This used to run inside `run()`, before every poll. It makes one
        sequential REST call per unmapped event, and leagues Kalshi has no
        milestone for never resolve, so the call is retried for each of them
        every 30 s forever. That blocked the clock refresh for seconds at a
        time: measured `match_clock_age_ms` was p50 6099 ms against a poll
        interval of 250 ms, which starved the 88+ gate of fresh clocks and left
        the sleeve unable to admit a single candidate.

        Mapping changes on the discovery timescale, not the poll timescale, so
        it belongs on its own task where it cannot delay a clock confirmation.
        """
        while True:
            try:
                await self._resolve_new_events()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(max(config.CLOCK_MAPPING_INTERVAL_S, 1.0))

    async def run(self):
        delay = max(config.GOAL_LATENCY_POLL_MS, 50.0) / 1000.0
        while True:
            loop_started = time.monotonic()
            try:
                await self._poll()
                await self._finish_pending()
                self.last_error = None
                # After the poll's own error state is cleared, so a flush
                # failure is reported rather than immediately overwritten.
                await self._flush_provider_refreshes()
            # An observer failure must never terminate or affect the trading loop.
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            elapsed = time.monotonic() - loop_started
            await asyncio.sleep(max(delay - elapsed, 0.01))

    def status(self):
        return {
            "enabled": True,
            "poll_ms": config.GOAL_LATENCY_POLL_MS,
            "mapped_matches": len(self.events_by_milestone),
            "polls": self.polls,
            "scoreless_payloads": self.scoreless_payloads,
            "goals": self.goals,
            "corrections": self.corrections,
            "clocks_recorded": self.clocks_recorded,
            "provider_events_recorded": self.provider_events_recorded,
            "provider_refreshes_pending": len(self.pending_refreshes),
            "provider_refresh_flushes": self.refresh_flushes,
            "provider_refreshes_written": self.refreshes_written,
            "last_poll_ts": self.last_poll_wall,
            "last_response_ms": self.last_response_ms,
            "last_error": self.last_error,
            "clock_coverage": self.clock_tracker.coverage(),
        }
