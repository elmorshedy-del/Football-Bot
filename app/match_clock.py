"""Match-clock parsing, persistence helpers, and the narrow 88+ gate object.

This module never reads score, scorer, goal, penalty, VAR, correction,
narrative, or canonical event fields.  Current clock is taken only from
current-state provider fields; historical last-play and significant-event
times are ignored.
"""
from dataclasses import dataclass
import json
import re

from . import config


CLOCK_STAMP_SCHEMA = "football.match_clock_stamp.v1"
CLOCK_PRECISION = "provider_minute_polled"
CLOCK_SOURCE = "kalshi_live_data_batch"
MAX_MINUTE = 130

# Typographic and ASCII minute marks used by Kalshi payloads.
_MINUTE_MARK = r"['’′`´]"
_CLOCK_RE = re.compile(
    r"(?P<minute>\d{1,3})\s*(?:" + _MINUTE_MARK + r")?\s*"
    r"(?:\+\s*(?P<stoppage>\d{1,2})\s*(?:" + _MINUTE_MARK + r")?)?"
)

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
    matched = _CLOCK_RE.search(text)
    if not matched:
        return None, None, None
    minute = int(matched.group("minute"))
    if minute > MAX_MINUTE:
        return None, None, None
    stoppage = int(matched.group("stoppage")) if matched.group("stoppage") else None
    return minute, stoppage, render_clock(minute, stoppage)


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
    return normalize_period(text)


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
    return normalize_status(text)


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
        ("time", details.get("time") if "time" in details else None),
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
    if age_ms is None or age_ms < 0 or age_ms > clock_max_age_ms():
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
        "signal_local_ts": signal_local_ts,
        "age_ms": None,
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
    age_ms = None
    if isinstance(observed_ts, (int, float)) and isinstance(signal_local_ts, (int, float)):
        age_ms = round((signal_local_ts - observed_ts) * 1000.0, 3)
    accepted, outcome, usable, reason = evaluate_clock_gate(
        parsed, age_ms, mapped=mapped,
    )
    previous = observation.get("previous_poll_ts")
    poll_uncertainty_ms = None
    if isinstance(observed_ts, (int, float)) and isinstance(previous, (int, float)):
        poll_uncertainty_ms = round((observed_ts - previous) * 1000.0, 3)
    return {
        "schema": CLOCK_STAMP_SCHEMA,
        "observation_id": observation.get("id"),
        "event": event,
        "provider_period": parsed.provider_period,
        "provider_minute": parsed.provider_minute,
        "provider_stoppage": parsed.provider_stoppage,
        "provider_clock": parsed.provider_clock,
        "provider_status": parsed.provider_status,
        "observed_ts": observed_ts,
        "signal_local_ts": signal_local_ts,
        "age_ms": age_ms,
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
        self.clock_fresh = set()
        self.clock_gate_candidate_misses = 0
        self.mapping_errors = {}

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
        self.clock_fresh.discard(event)
        self.faults.pop(event, None)
        self.mapping_errors.pop(event, None)

    def observe(self, event, milestone_id, parsed, timing):
        """Update cache. Return a row to persist if identity changed, else None."""
        self.set_mapping(event, milestone_id)
        identity = parsed.identity if parsed else (None, None, None, None)
        previous = self.last_identity.get(event)
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
        self.latest[event] = dict(observation)
        if parsed and parsed.provider_minute is not None:
            self.clock_present.add(event)
        else:
            self.clock_present.discard(event)
        age_ms = 0.0
        _accepted, _outcome, usable, reason = evaluate_clock_gate(
            parsed, age_ms, mapped=event in self.mapped,
        )
        if usable:
            self.clock_fresh.add(event)
            self.faults.pop(event, None)
        else:
            self.clock_fresh.discard(event)
            if reason in {"stale", "unmapped", "malformed", "missing_clock"}:
                self.faults[event] = reason
        if previous == identity:
            return None
        self.last_identity[event] = identity
        return observation

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
            self.latest[event]["source"] = source
        elif event in self.latest:
            self.latest[event]["source"] = source
        return row

    def stamp(self, event, signal_local_ts, persist_id=None):
        mapped = event in self.mapped
        observation = self.latest.get(event)
        if observation is not None and persist_id is not None:
            observation = dict(observation)
            observation["id"] = persist_id
        result = stamp_from_observation(observation, event, signal_local_ts, mapped=mapped)
        if observation is not None and observation.get("id") is None and persist_id is None:
            # Stamp can still carry in-memory fields before SQLite assigns id.
            result["observation_id"] = observation.get("id")
        if not result["usable_for_88_gate"]:
            reason = result.get("unusable_reason")
            if reason in {"unmapped", "stale", "malformed", "missing_clock"}:
                self.clock_gate_candidate_misses += 1
                self.faults[event] = reason
        return result

    def coverage(self, watched_events=None):
        watched = set(watched_events or [])
        mapped = set(self.mapped) & watched if watched else set(self.mapped)
        present = self.clock_present & (watched or self.clock_present)
        fresh = self.clock_fresh & (watched or self.clock_fresh)
        return {
            "watched": len(watched) if watched_events is not None else len(self.mapped),
            "mapped": len(mapped),
            "clock_present": len(present),
            "clock_fresh": len(fresh),
            "clock_gate_candidate_misses": self.clock_gate_candidate_misses,
            "faults": [
                {"event": event, "reason": reason}
                for event, reason in sorted(self.faults.items())
                if not watched or event in watched
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
    )

    def __init__(self, stamp, mapped=None):
        stamp = stamp or {}
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

    def evaluate(self):
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
