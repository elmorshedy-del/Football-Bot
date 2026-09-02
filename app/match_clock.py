"""Match-clock parsing, persistence helpers, and the narrow 88+ gate object.

This module never reads score, scorer, goal, penalty, VAR, correction,
narrative, or canonical event fields.  Current clock is taken only from
current-state provider fields; historical last-play and significant-event
times are ignored.
"""
from dataclasses import dataclass
import json
import re
import time

from . import config


CLOCK_STAMP_SCHEMA = "football.match_clock_stamp.v1"
CLOCK_PRECISION = "provider_minute_polled"
CLOCK_SOURCE = "kalshi_live_data_batch"
MAX_MINUTE = 130

# Typographic and ASCII minute marks used by Kalshi payloads.
_MINUTE_MARK = r"['’′`´]"
# Named mark groups so a candidate can be tested for an explicit minute mark.
# A bare integer inside prose ("2nd half", a "1-0" score) is NOT a clock.
_CLOCK_RE = re.compile(
    r"(?P<minute>\d{1,3})\s*(?P<mark>" + _MINUTE_MARK + r")?\s*"
    r"(?:\+\s*(?P<stoppage>\d{1,2})\s*(?P<stoppage_mark>" + _MINUTE_MARK + r")?)?"
)
_BARE_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*$")

_SKIP_CLOCK_KEYS = {
    "last_play", "lastplay",
    "home_significant_events", "awaysignificantevents",
    "home_significant_events", "away_significant_events",
    "homesignificantevents", "awaysignificantevents",
    "significant_events", "significantevents",
}

_PERIOD_FIRST = {
    "1", "1st", "first", "firsthalf", "1h", "h1", "fh", "period1",
}
_PERIOD_SECOND = {
    "2", "2nd", "second", "secondhalf", "2h", "h2", "sh", "period2",
    "stoppage", "addedtime", "extratime2",
}
_PERIOD_HALF_TIME = {"ht", "halftime", "half"}
_PERIOD_FINAL = {
    "ft", "fulltime", "full", "final", "ended", "finished", "complete",
}
_STATUS_LIVE = {
    "live", "inplay", "in_play", "inprogress", "in_progress", "playing",
    "active", "started",
}
_STATUS_SUSPENDED = {"suspended", "stopped", "interrupted"}
_STATUS_ABANDONED = {"abandoned", "postponed", "cancelled", "canceled", "void"}
_STATUS_PRE = {"pre", "prematch", "scheduled", "warmup", "notstarted", "ns"}
_STATUS_FINAL = {
    "ft", "fulltime", "full", "final", "ended", "finished", "complete",
    "closed",
}


def _compact(value):
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())


def _is_skip_key(key):
    compact = _compact(key)
    return compact in _SKIP_CLOCK_KEYS or "significantevent" in compact


def render_clock(minute, stoppage):
    if minute is None:
        return None
    if stoppage:
        return f"{minute}+{stoppage}′"
    return f"{minute}′"


def parse_clock_text(value):
    """Return (minute, stoppage, rendered) or (None, None, None)."""
    if isinstance(value, bool) or value is None:
        return None, None, None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None, None, None
        minute = int(value)
        if minute < 0 or minute > MAX_MINUTE:
            return None, None, None
        if float(value) != float(minute):
            return None, None, None
        return minute, None, render_clock(minute, None)
    text = str(value).strip()
    if not text:
        return None, None, None

    # Prefer a candidate carrying an explicit minute mark ("90+5′") or an
    # explicit stoppage ("90+5").  Scanning for the first bare integer would
    # read the period ordinal out of "2nd Half 90+5′" as minute 2, or the
    # home score out of "1-0 90+5′" as minute 1, and then persist that
    # wrong minute as a confident clock stamp.
    fallback = None
    for matched in _CLOCK_RE.finditer(text):
        minute = int(matched.group("minute"))
        if minute > MAX_MINUTE:
            continue
        stoppage = (
            int(matched.group("stoppage")) if matched.group("stoppage") else None
        )
        marked = bool(
            matched.group("mark")
            or matched.group("stoppage_mark")
            or matched.group("stoppage")
        )
        if marked:
            return minute, stoppage, render_clock(minute, stoppage)
        if fallback is None:
            fallback = (minute, stoppage)

    # An unmarked integer is only a clock when it is the entire value, which is
    # how a dedicated clock field carries a plain minute.  Inside prose it is
    # ambiguous, so it is refused rather than guessed.
    if fallback is not None and _BARE_NUMBER_RE.match(text):
        minute, stoppage = fallback
        return minute, stoppage, render_clock(minute, stoppage)
    return None, None, None


def normalize_period(value):
    if value is None or isinstance(value, bool):
        return None
    compact = _compact(value)
    if not compact:
        return None
    if compact in _PERIOD_FIRST:
        return "1st"
    if compact in _PERIOD_SECOND:
        return "2nd"
    if compact in _PERIOD_HALF_TIME:
        return "half-time"
    if compact in _PERIOD_FINAL:
        return "final"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if int(value) == 1:
            return "1st"
        if int(value) == 2:
            return "2nd"
    return str(value).strip() or None


def normalize_status(value):
    if value is None or isinstance(value, bool):
        return None
    compact = _compact(value)
    if not compact:
        return None
    if compact in _STATUS_LIVE:
        return "live"
    if compact in _STATUS_SUSPENDED:
        return "suspended"
    if compact in _STATUS_ABANDONED:
        return "abandoned"
    if compact in _STATUS_FINAL:
        return "final"
    if compact in _STATUS_PRE:
        return "pre-match"
    if compact in _PERIOD_HALF_TIME:
        return "half-time"
    return str(value).strip() or None


def _direct_field(details, names):
    wanted = {_compact(name) for name in names}
    if not isinstance(details, dict):
        return None
    for key, child in details.items():
        if _is_skip_key(key):
            continue
        if _compact(key) in wanted and not isinstance(child, (dict, list, bool)):
            return child
    return None


def _clockish(value):
    minute, _stoppage, _rendered = parse_clock_text(value)
    return minute is not None


@dataclass(frozen=True)
class ParsedClock:
    provider_period: str | None
    provider_minute: int | None
    provider_stoppage: int | None
    provider_clock: str | None
    provider_status: str | None
    source_field: str | None
    raw_context: dict

    @property
    def identity(self):
        return (
            self.provider_period,
            self.provider_minute,
            self.provider_stoppage,
            self.provider_status,
        )


def _period_from_text(text):
    if not text:
        return None
    lowered = str(text).lower()
    if re.search(r"\b(2nd|second\s*half|2h)\b", lowered):
        return "2nd"
    if re.search(r"\b(1st|first\s*half|1h)\b", lowered):
        return "1st"
    if re.search(r"\b(full\s*time|full-time|\bft\b|final)\b", lowered):
        return "final"
    if re.search(r"\b(half[\s-]*time|\bht\b)\b", lowered):
        return "half-time"
    # Unrecognized prose is not a period label.  Returning the raw string here
    # ("1-0 90+5′") would reach the gate as an unusable period and decline a
    # legitimate 88+ clock; None lets the minute-based inference apply instead.
    return None


def _status_from_text(text):
    if not text:
        return None
    lowered = str(text).lower()
    if re.search(r"\b(abandon|cancel|postpon)", lowered):
        return "abandoned"
    if re.search(r"\bsuspend", lowered):
        return "suspended"
    if re.search(r"\b(full\s*time|full-time|\bft\b|final|ended|finished)\b", lowered):
        return "final"
    if re.search(r"\b(half[\s-]*time|\bht\b)\b", lowered):
        return "half-time"
    if re.search(r"\b(live|in[\s-]*play|in[\s-]*progress|playing)\b", lowered):
        return "live"
    # Unrecognized prose is not a status label; fall through to the period-based
    # inference rather than persisting raw scoreboard text as provider_status.
    return None


def parse_current_clock(details):
    """Parse the *current* match clock from current-state fields only."""
    details = details if isinstance(details, dict) else {}
    raw_context = {}
    for key in (
        "time", "match_clock", "game_clock", "clock", "status_text",
        "status", "match_status", "game_status", "state", "period",
        "phase", "half", "period_name", "minute", "match_minute",
        "game_minute",
    ):
        value = _direct_field(details, (key,))
        if value is not None:
            raw_context[key] = value

    status_text = _direct_field(details, ("status_text",))
    status = normalize_status(_direct_field(
        details, ("status", "match_status", "game_status", "state"),
    ))
    if status is None:
        status = _status_from_text(status_text)

    period = normalize_period(_direct_field(
        details, ("period", "phase", "half", "period_name"),
    ))
    if period is None:
        period = _period_from_text(status_text)

    minute = stoppage = rendered = source_field = None
    for field_name, value in (
        ("time", _direct_field(details, ("time",))),
        ("match_clock", _direct_field(details, ("match_clock",))),
        ("game_clock", _direct_field(details, ("game_clock",))),
        ("clock", _direct_field(details, ("clock",))),
    ):
        if value is None or isinstance(value, (dict, list)):
            continue
        parsed_minute, parsed_stoppage, parsed_rendered = parse_clock_text(value)
        if parsed_minute is None:
            continue
        minute, stoppage, rendered = parsed_minute, parsed_stoppage, parsed_rendered
        source_field = field_name
        break

    if minute is None and status_text is not None:
        minute, stoppage, rendered = parse_clock_text(status_text)
        if minute is not None:
            source_field = "status_text"

    if period is None and minute is not None:
        if minute >= 46:
            period = "2nd"
        elif minute >= 0:
            period = "1st"

    if status is None:
        if period == "final":
            status = "final"
        elif period == "half-time":
            status = "half-time"
        elif period in {"1st", "2nd"}:
            status = "live"

    return ParsedClock(
        provider_period=period,
        provider_minute=minute,
        provider_stoppage=stoppage,
        provider_clock=rendered,
        provider_status=status,
        source_field=source_field,
        raw_context=raw_context,
    )


def clock_max_age_ms():
    configured = getattr(config, "MATCH_CLOCK_MAX_AGE_MS", None)
    if configured is not None:
        return float(configured)
    return max(float(config.GOAL_LATENCY_POLL_MS) * 10.0, 2500.0)


def is_persisted_id(value):
    """True only for a positive integer database row id.

    The 88+ gate must fail closed on anything else.  A candidate clock that has
    not yet been written -- or whose insert failed -- has no lineage, so an
    accepted signal could never be reconciled against SQLite afterwards.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value > 0


def evaluate_clock_gate(parsed, age_ms, mapped=True):
    """Return (accepted, outcome, usable, unusable_reason).

    Outcomes are the exact 88-gate labels used by signals and the API.
    """
    if not mapped:
        return False, "clock_unmapped", False, "unmapped"
    if parsed is None:
        return False, "clock_malformed", False, "malformed"
    status = parsed.provider_status
    if status in {"final", "abandoned", "suspended", "half-time", "pre-match"}:
        outcome = {
            "final": "clock_final",
            "abandoned": "clock_abandoned",
            "suspended": "clock_suspended",
            "half-time": "clock_half_time",
            "pre-match": "clock_pre_match",
        }[status]
        return False, outcome, False, status
    if parsed.provider_minute is None:
        return False, "clock_missing", False, "missing_clock"
    if status not in {None, "live"}:
        return False, "clock_not_live", False, f"status_{status}"
    if status != "live":
        return False, "clock_not_live", False, "status_missing"
    if parsed.provider_period == "1st":
        return False, "clock_first_half", False, "first_half"
    if parsed.provider_period not in {"2nd", None}:
        if parsed.provider_period == "final":
            return False, "clock_final", False, "final"
        return False, "clock_period_unusable", False, "period_unusable"
    if parsed.provider_period is None and parsed.provider_minute < 46:
        return False, "clock_first_half", False, "first_half"
    if age_ms is not None and age_ms < 0:
        # A receipt later than the signal cannot have informed it.  Coercing the
        # age to zero here would let a clock from the future look perfectly
        # fresh, so the reading is refused and the negative age is preserved.
        return False, "clock_future", False, "future_timestamp"
    if age_ms is None or age_ms > clock_max_age_ms():
        return False, "clock_stale", False, "stale"
    if parsed.provider_minute < 88:
        return False, "clock_pre_88", False, "pre_88"
    return True, "clock_88_plus", True, None


def unusable_stamp(event, signal_local_ts, reason, **extra):
    stamp = {
        "schema": CLOCK_STAMP_SCHEMA,
        "observation_id": None,
        "event": event,
        "provider_period": None,
        "provider_minute": None,
        "provider_stoppage": None,
        "provider_clock": None,
        "provider_status": None,
        "observed_ts": None,
        "confirmed_ts": None,
        "confirmation_previous_poll_ts": None,
        "signal_local_ts": signal_local_ts,
        "age_ms": None,
        "established_age_ms": None,
        "poll_uncertainty_ms": None,
        "source": CLOCK_SOURCE,
        "precision": CLOCK_PRECISION,
        "usable_for_88_gate": False,
        "unusable_reason": reason,
        "gate_outcome": extra.get("gate_outcome") or f"clock_{reason}",
    }
    stamp.update({key: value for key, value in extra.items() if key != "gate_outcome"})
    if "gate_outcome" in extra:
        stamp["gate_outcome"] = extra["gate_outcome"]
    return stamp


def stamp_from_observation(observation, event, signal_local_ts, mapped=True):
    """Build the immutable signal clock stamp from a persisted observation."""
    if observation is None:
        reason = "unmapped" if not mapped else "missing_clock"
        return unusable_stamp(
            event, signal_local_ts, reason,
            gate_outcome="clock_unmapped" if reason == "unmapped" else "clock_missing",
        )
    if not mapped:
        return unusable_stamp(
            event, signal_local_ts, "unmapped", gate_outcome="clock_unmapped",
        )
    observation_id = observation.get("id")
    if not is_persisted_id(observation_id):
        # Fail closed before any minute/status logic: without a row id this
        # reading cannot be reconciled to SQLite, so it is not evidence.
        return unusable_stamp(
            event, signal_local_ts, "unpersisted", gate_outcome="clock_unpersisted",
        )
    parsed = ParsedClock(
        provider_period=observation.get("provider_period"),
        provider_minute=observation.get("provider_minute"),
        provider_stoppage=observation.get("provider_stoppage"),
        provider_clock=observation.get("provider_clock"),
        provider_status=observation.get("provider_status"),
        source_field=None,
        raw_context={},
    )
    observed_ts = observation.get("observed_ts")
    # observed_ts anchors lineage: it is the receipt time of the persisted row.
    # confirmed_ts is the most recent poll that re-confirmed the same reading,
    # and is what freshness must be measured against — otherwise a clock the
    # provider is still actively confirming would age out on its own.
    confirmed_ts = observation.get("confirmed_ts")
    if not isinstance(confirmed_ts, (int, float)):
        confirmed_ts = observed_ts
    age_ms = None
    if isinstance(confirmed_ts, (int, float)) and isinstance(signal_local_ts, (int, float)):
        age_ms = round((signal_local_ts - confirmed_ts) * 1000.0, 3)
    established_age_ms = None
    if isinstance(observed_ts, (int, float)) and isinstance(signal_local_ts, (int, float)):
        established_age_ms = round((signal_local_ts - observed_ts) * 1000.0, 3)
    gate_age_ms = age_ms
    if (
        isinstance(established_age_ms, (int, float))
        and established_age_ms < 0
        and (gate_age_ms is None or gate_age_ms >= 0)
    ):
        # The persisted receipt itself is in the future; fail closed on that too.
        gate_age_ms = established_age_ms
    accepted, outcome, usable, reason = evaluate_clock_gate(
        parsed, gate_age_ms, mapped=mapped,
    )
    # Confirmation uncertainty describes the poll interval that established the
    # CURRENT confirmation, so both endpoints must come from that confirmation.
    # Measuring from the original observed_ts went negative as soon as the row
    # was re-confirmed by a later poll.
    confirmation_previous = observation.get("confirmation_previous_poll_ts")
    if confirmation_previous is None and confirmed_ts == observed_ts:
        confirmation_previous = observation.get("previous_poll_ts")
    poll_uncertainty_ms = None
    if isinstance(confirmed_ts, (int, float)) and isinstance(
        confirmation_previous, (int, float),
    ):
        interval_ms = round((confirmed_ts - confirmation_previous) * 1000.0, 3)
        if interval_ms >= 0:
            poll_uncertainty_ms = interval_ms
    return {
        "schema": CLOCK_STAMP_SCHEMA,
        "observation_id": observation_id,
        "event": event,
        "provider_period": parsed.provider_period,
        "provider_minute": parsed.provider_minute,
        "provider_stoppage": parsed.provider_stoppage,
        "provider_clock": parsed.provider_clock,
        "provider_status": parsed.provider_status,
        "observed_ts": observed_ts,
        "confirmed_ts": confirmed_ts,
        "confirmation_previous_poll_ts": confirmation_previous,
        "signal_local_ts": signal_local_ts,
        "age_ms": age_ms,
        "established_age_ms": established_age_ms,
        "poll_uncertainty_ms": poll_uncertainty_ms,
        "source": observation.get("source") or CLOCK_SOURCE,
        "precision": observation.get("precision") or CLOCK_PRECISION,
        "usable_for_88_gate": bool(accepted and usable),
        "unusable_reason": None if usable else reason,
        "gate_outcome": outcome,
    }


class MatchClockTracker:
    """In-memory latest clock per event plus coverage counters."""

    def __init__(self):
        self.latest = {}
        self.mapped = {}
        self.last_identity = {}
        self.faults = {}
        self.clock_present = set()
        # Last confirmation time per event.  Freshness is derived from this at
        # query time, never cached as a boolean, so a clock cannot report fresh
        # long after the provider stopped confirming it.
        self.last_confirmed = {}
        self.clock_gate_candidate_misses = 0
        self.mapping_errors = {}
        # Candidates awaiting their database id.  Nothing in here is
        # decision-visible: `latest` still holds the previous persisted row.
        self.pending = {}
        self.persistence_faults = {}
        # Current provider status per event, even when no minute parsed.  A
        # live match with no clock is a fault; a pre-match one is just waiting.
        self.provider_status = {}
        self.candidate_active = set()

    def set_mapping(self, event, milestone_id, error=None):
        if milestone_id:
            self.mapped[event] = str(milestone_id)
            self.mapping_errors.pop(event, None)
        if error:
            self.mapping_errors[event] = str(error)

    def drop_event(self, event):
        self.latest.pop(event, None)
        self.mapped.pop(event, None)
        self.last_identity.pop(event, None)
        self.clock_present.discard(event)
        self.last_confirmed.pop(event, None)
        self.faults.pop(event, None)
        self.mapping_errors.pop(event, None)
        self.pending.pop(event, None)
        self.persistence_faults.pop(event, None)
        self.provider_status.pop(event, None)
        self.candidate_active.discard(event)

    def mark_candidate_active(self, event, active=True):
        """Record whether a price-only candidate is currently live on this event."""
        if active:
            self.candidate_active.add(event)
        else:
            self.candidate_active.discard(event)

    def promote(self, event, row_id):
        """Publish the pending candidate now that its row id exists.

        This is the only path that makes a new reading decision-visible.
        """
        if not is_persisted_id(row_id):
            raise ValueError(f"refusing to promote non-positive row id {row_id!r}")
        pending = self.pending.pop(event, None)
        if pending is None:
            return None
        row = dict(pending["row"])
        row["id"] = row_id
        self.latest[event] = row
        self.last_identity[event] = pending["identity"]
        self.persistence_faults.pop(event, None)
        self._mark_presence(event, pending["parsed"], row.get("confirmed_ts"))
        return row

    def fail_persist(self, event, error):
        """Record a failed insert.

        The candidate is discarded and the identity was never committed, so the
        next identical poll builds a fresh candidate and retries.  The previous
        persisted observation stays visible in the meantime.
        """
        self.pending.pop(event, None)
        self.persistence_faults[event] = str(error) or "clock_persistence_failed"

    def observe(self, event, milestone_id, parsed, timing):
        """Build a candidate row to persist, or None when nothing new is needed.

        A new identity is NOT published here: it goes to `pending` and only
        becomes decision-visible through `promote()` once SQLite has given it a
        positive row id.
        """
        self.set_mapping(event, milestone_id)
        identity = parsed.identity if parsed else (None, None, None, None)
        previous = self.last_identity.get(event)
        self.provider_status[event] = parsed.provider_status if parsed else None
        observation = {
            "observed_ts": timing["received_wall"],
            "poll_started_ts": timing["started_wall"],
            "previous_poll_ts": timing.get("previous_poll_ts"),
            "response_ms": timing["response_ms"],
            "event": event,
            "milestone_id": str(milestone_id),
            "provider_period": parsed.provider_period if parsed else None,
            "provider_minute": parsed.provider_minute if parsed else None,
            "provider_stoppage": parsed.provider_stoppage if parsed else None,
            "provider_clock": parsed.provider_clock if parsed else None,
            "provider_status": parsed.provider_status if parsed else None,
            "precision": CLOCK_PRECISION,
            "source": CLOCK_SOURCE,
            "raw_context": parsed.raw_context if parsed else {},
        }
        confirmed_ts = timing["received_wall"]
        cached = self.latest.get(event)
        if previous == identity and cached is not None:
            # An unchanged poll confirms the existing reading; it does not create
            # a new one.  Replacing the cache here would drop the persisted row
            # id and advance observed_ts to a time with no database row, so the
            # 88 gate would accept a clock with no lineage.  Only freshness moves.
            #
            # The original previous_poll_ts stays untouched: it belongs to the
            # receipt that established the row.  The confirmation interval is
            # tracked separately, otherwise uncertainty measured from the
            # original observed_ts goes negative on the second reconfirmation.
            cached["confirmed_ts"] = confirmed_ts
            cached["confirmation_previous_poll_ts"] = timing.get("previous_poll_ts")
            cached["response_ms"] = timing["response_ms"]
            self._mark_presence(event, parsed, confirmed_ts)
            return None
        in_flight = self.pending.get(event)
        if in_flight is not None and in_flight["identity"] == identity:
            # An insert for this exact reading is already in flight; a second
            # candidate would duplicate the row.  A failed insert clears pending,
            # so the retry path stays open.
            return None
        observation["confirmed_ts"] = confirmed_ts
        observation["confirmation_previous_poll_ts"] = timing.get("previous_poll_ts")
        self.pending[event] = {
            "row": observation, "identity": identity, "parsed": parsed,
        }
        return observation

    def _mark_presence(self, event, parsed, confirmed_ts):
        """Track presence and the last confirmation time.

        Freshness is deliberately NOT decided here.  Whether a clock is fresh
        depends on how long ago it was confirmed, which is only knowable at
        query time, and it must not be conflated with 88+ eligibility: a
        perfectly fresh minute-70 clock is fresh and simply not yet eligible.
        """
        if parsed and parsed.provider_minute is not None:
            self.clock_present.add(event)
        else:
            self.clock_present.discard(event)
        self.last_confirmed[event] = confirmed_ts
        # Current fault reasons are derived in `coverage()` from live state, not
        # latched here: a latched reason outlived the condition that caused it
        # and made recovery to a green banner impossible.
        self.faults.pop(event, None)

    def ingest_synthetic(self, event, milestone_id, parsed, observed_ts, source):
        """Demo/test helper: install a clock without a live poll."""
        timing = {
            "received_wall": observed_ts,
            "started_wall": observed_ts,
            "previous_poll_ts": observed_ts - 0.25,
            "response_ms": 1.0,
        }
        row = self.observe(event, milestone_id, parsed, timing)
        if row is not None:
            row["source"] = source
            self.pending[event]["row"]["source"] = source
        elif event in self.latest:
            self.latest[event]["source"] = source
        return row

    def stamp(self, event, signal_local_ts, persist_id=None):
        """Stamp from the decision-visible observation only.

        `pending` is never consulted: a candidate without a row id has no
        lineage and must not reach the 88+ gate.
        """
        mapped = event in self.mapped
        observation = self.latest.get(event)
        if observation is not None and persist_id is not None:
            observation = dict(observation)
            observation["id"] = persist_id
        if observation is None and event in self.pending:
            return unusable_stamp(
                event, signal_local_ts, "unpersisted",
                gate_outcome="clock_unpersisted",
            )
        return stamp_from_observation(
            observation, event, signal_local_ts, mapped=mapped,
        )

    def coverage(self, watched_events=None, now=None):
        """Coverage as of `now`.

        `clock_fresh` means the provider confirmed this clock recently enough to
        still be trusted.  It is deliberately independent of the 88+ gate: a
        minute-70 clock polled a moment ago is fresh and simply not yet
        eligible.  Conflating the two reported a healthy feed as unhealthy and
        a decayed clock as fresh.
        """
        now = time.time() if now is None else now
        max_age_s = clock_max_age_ms() / 1000.0
        watched = set(watched_events or [])
        scope = watched or (
            set(self.mapped) | set(self.latest) | set(self.pending)
            | set(self.provider_status) | set(self.last_confirmed)
        )

        events = []
        for event in sorted(scope):
            observation = self.latest.get(event)
            observation_id = observation.get("id") if observation else None
            persisted = is_persisted_id(observation_id)
            minute = observation.get("provider_minute") if persisted else None
            confirmed = self.last_confirmed.get(event)
            present = bool(persisted and minute is not None)
            fresh = bool(
                present and isinstance(confirmed, (int, float))
                and 0 <= (now - confirmed) <= max_age_s
            )
            status = self.provider_status.get(event)
            candidate = event in self.candidate_active
            mapped = event in self.mapped
            # A live match or an active candidate needs a usable clock right
            # now.  A mapped fixture that has not kicked off is simply waiting,
            # which is healthy and must not redden the banner.
            needs_clock = candidate or status in {"live", "suspended"}
            fault = self.faults.get(event)
            if fault is None:
                if not mapped:
                    fault = "unmapped"
                elif event in self.mapping_errors:
                    fault = "mapping_error"
                elif event in self.persistence_faults:
                    fault = "clock_persistence_failed"
                elif present and not fresh:
                    fault = "stale"
                elif not present and needs_clock:
                    in_flight = self.pending.get(event)
                    in_flight_parsed = in_flight.get("parsed") if in_flight else None
                    if in_flight_parsed is not None and (
                        in_flight_parsed.provider_minute is not None
                    ):
                        # A real minute is parsed but not yet written.
                        fault = "unpersisted"
                    elif observation is not None or status is not None:
                        fault = "missing_clock"
                    else:
                        fault = "malformed"
            if fault:
                state = "fault"
            elif present:
                state = "observing"
            else:
                state = "waiting"
            events.append({
                "event": event,
                "mapped": mapped,
                "provider_status": status,
                "observation_id": observation_id if persisted else None,
                "clock_present": present,
                "clock_fresh": fresh,
                "last_confirmed_ts": confirmed,
                "candidate_active": candidate,
                "current_fault": fault,
                "state": state,
            })

        faulted = [row for row in events if row["current_fault"]]
        return {
            "watched": len(watched) if watched_events is not None else len(self.mapped),
            "mapped": sum(1 for row in events if row["mapped"]),
            "clock_present": sum(1 for row in events if row["clock_present"]),
            "clock_fresh": sum(1 for row in events if row["clock_fresh"]),
            "clock_stale": sum(
                1 for row in events
                if row["clock_present"] and not row["clock_fresh"]
            ),
            "clock_waiting": sum(1 for row in events if row["state"] == "waiting"),
            # Cumulative evidence, kept for audit.  It is deliberately NOT a
            # current fault: one historical miss must not block recovery forever.
            "clock_gate_candidate_misses_total": self.clock_gate_candidate_misses,
            "clock_gate_candidate_misses": self.clock_gate_candidate_misses,
            "events": events,
            "faults": [
                {"event": row["event"], "reason": row["current_fault"]}
                for row in faulted
            ],
            "mapping_errors": [
                {"event": event, "error": error}
                for event, error in sorted(self.mapping_errors.items())
                if not watched or event in watched
            ],
        }


class MatchClockGate:
    """The only match-feed object the price-only path may import.

    Fields are clock/status/age/source IDs.  No score or event content.
    """

    __slots__ = (
        "event", "observation_id", "provider_period", "provider_minute",
        "provider_stoppage", "provider_clock", "provider_status",
        "age_ms", "source", "precision", "mapped",
        "declared_outcome", "declared_reason",
    )

    def __init__(self, stamp, mapped=None):
        stamp = stamp or {}
        self.declared_outcome = stamp.get("gate_outcome")
        self.declared_reason = stamp.get("unusable_reason")
        self.event = stamp.get("event")
        self.observation_id = stamp.get("observation_id")
        self.provider_period = stamp.get("provider_period")
        self.provider_minute = stamp.get("provider_minute")
        self.provider_stoppage = stamp.get("provider_stoppage")
        self.provider_clock = stamp.get("provider_clock")
        self.provider_status = stamp.get("provider_status")
        self.age_ms = stamp.get("age_ms")
        self.source = stamp.get("source")
        self.precision = stamp.get("precision")
        if mapped is None:
            self.mapped = stamp.get("unusable_reason") != "unmapped"
        else:
            self.mapped = mapped

    def _result(self, accepted, outcome, usable, reason):
        return {
            "accepted": accepted,
            "outcome": outcome,
            "usable_for_88_gate": usable,
            "unusable_reason": reason,
            "provider_clock": self.provider_clock,
            "provider_minute": self.provider_minute,
            "provider_stoppage": self.provider_stoppage,
            "provider_period": self.provider_period,
            "provider_status": self.provider_status,
            "age_ms": self.age_ms,
            "observation_id": self.observation_id,
            "source": self.source,
        }

    def evaluate(self):
        if self.declared_reason and self.declared_outcome:
            # The stamp already failed closed upstream.  Re-deriving here would
            # relabel a specific refusal (unmapped, stale, unpersisted) as
            # whatever the null fields happen to evaluate to.
            return self._result(False, self.declared_outcome, False, self.declared_reason)
        if not is_persisted_id(self.observation_id):
            # Fail closed before minute/status logic.  A caller that hands in a
            # raw dict must not be able to bypass the lineage requirement.
            return self._result(False, "clock_unpersisted", False, "unpersisted")
        parsed = ParsedClock(
            provider_period=self.provider_period,
            provider_minute=self.provider_minute,
            provider_stoppage=self.provider_stoppage,
            provider_clock=self.provider_clock,
            provider_status=self.provider_status,
            source_field=None,
            raw_context={},
        )
        mapped = bool(self.mapped or self.observation_id)
        accepted, outcome, usable, reason = evaluate_clock_gate(
            parsed, self.age_ms, mapped=mapped,
        )
        return {
            "accepted": accepted,
            "outcome": outcome,
            "usable_for_88_gate": usable,
            "unusable_reason": reason,
            "provider_clock": self.provider_clock,
            "provider_minute": self.provider_minute,
            "provider_stoppage": self.provider_stoppage,
            "provider_period": self.provider_period,
            "provider_status": self.provider_status,
            "age_ms": self.age_ms,
            "observation_id": self.observation_id,
            "source": self.source,
        }


def parse_stored_stamp(value):
    if isinstance(value, dict):
        stamp = value
    elif isinstance(value, str) and value:
        try:
            stamp = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            stamp = {}
    else:
        stamp = {}
    if not stamp:
        return unusable_stamp(
            None, None, "legacy_signal_recorded_before_clock_stamps",
            gate_outcome="clock_legacy",
        )
    stamp.setdefault("schema", CLOCK_STAMP_SCHEMA)
    stamp.setdefault("precision", CLOCK_PRECISION)
    return stamp
