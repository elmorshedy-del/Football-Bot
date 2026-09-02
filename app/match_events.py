"""Deterministic normalization for match-feed observations.

Raw provider payloads are always retained.  This module adds a small canonical
schema for storage, joins and human-readable audit views without using natural
language interpretation in the trading path.
"""
import hashlib
import json
import re


_CLOCK_RE = re.compile(r"(?P<minute>\d+)(?:\+(?P<stoppage>\d+))?\s*['’]?")


def _score_key_matches(key, side):
    lowered = key.lower()
    compact = lowered.replace("_", "").replace(".", "")
    tail = lowered.replace("_", ".").split(".")[-1]
    return compact in {f"{side}score", f"score{side}"} or (
        tail == side and "score" in lowered
    )


def _primary_score(signature, side):
    matches = []
    priorities = (
        (f"{side}samegamescore", 0),
        (f"{side}score", 1),
        (f"score{side}", 1),
        (f"{side}aggregatescore", 2),
    )
    for key, value in signature.items():
        compact = key.lower().replace("_", "").replace(".", "")
        priority = next((rank for suffix, rank in priorities if compact.endswith(suffix)), None)
        if priority is not None:
            matches.append((priority, key.count("."), key, value))
        elif _score_key_matches(key, side):
            matches.append((3, key.count("."), key, value))
    if not matches:
        return None
    return float(min(matches)[3])


def score_pair(signature):
    return {
        "home": _primary_score(signature, "home"),
        "away": _primary_score(signature, "away"),
    }


def _first_field(value, names):
    wanted = {name.lower().replace("_", "") for name in names}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("_", "")
            if normalized in wanted and not isinstance(child, bool) and \
                    isinstance(child, (str, int, float)):
                return child
        for child in value.values():
            found = _first_field(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_field(child, names)
            if found is not None:
                return found
    return None


def _score_text(pair):
    if pair["home"] is None or pair["away"] is None:
        return None
    return f"{int(pair['home'])}–{int(pair['away'])}"


def _clock_parts(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None, None, None
    matched = _CLOCK_RE.search(str(value))
    if not matched:
        return None, None, None
    minute = int(matched.group("minute"))
    stoppage = int(matched.group("stoppage")) if matched.group("stoppage") else None
    rendered = f"{minute}+{stoppage}'" if stoppage is not None else f"{minute}'"
    return minute, stoppage, rendered


def _display_person(value):
    text = str(value or "").strip()
    if "," not in text:
        return text or None
    family, given = (part.strip() for part in text.split(",", 1))
    return " ".join(part for part in (given, family) if part) or None


def _latest_score_event(details, side):
    candidates = []
    sides = (side,) if side in {"home", "away"} else ("home", "away")
    for event_side in sides:
        rows = details.get(f"{event_side}_significant_events") or []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("event_type") != "score_change":
                continue
            minute, stoppage, rendered = _clock_parts(row.get("time"))
            clock_value = (minute or -1) * 1000 + (stoppage or 0)
            candidates.append((clock_value, index, event_side, row, minute, stoppage, rendered))
    if not candidates:
        return None
    _clock_value, _index, event_side, row, minute, stoppage, rendered = max(candidates)
    return {
        "side": event_side,
        "minute": minute,
        "stoppage": stoppage,
        "clock": rendered,
        "scorer_raw": row.get("player"),
        "scorer": _display_person(row.get("player")),
    }


def normalize_match_event(change_kind, before, after, live_data=None):
    """Return a stable canonical event object from numeric score changes."""
    live_data = live_data or {}
    before_pair, after_pair = score_pair(before), score_pair(after)
    home_delta = ((after_pair["home"] or 0.0) - (before_pair["home"] or 0.0)
                  if after_pair["home"] is not None or before_pair["home"] is not None else 0.0)
    away_delta = ((after_pair["away"] or 0.0) - (before_pair["away"] or 0.0)
                  if after_pair["away"] is not None or before_pair["away"] is not None else 0.0)
    if home_delta and not away_delta:
        side = "home"
    elif away_delta and not home_delta:
        side = "away"
    else:
        side = "unknown"

    if change_kind == "goal":
        canonical_type = f"goal_observed.{side}"
        action = f"{side.title()} goal observed" if side != "unknown" else "Goal observed"
    elif change_kind == "score_correction":
        canonical_type = f"score_correction.{side}"
        action = (f"{side.title()} score correction" if side != "unknown"
                  else "Score correction")
    else:
        canonical_type = "score_schema_change.unknown"
        action = "Score schema changed"

    before_text, after_text = _score_text(before_pair), _score_text(after_pair)
    transition = f"{before_text} → {after_text}" if before_text and after_text else None
    human_label = f"{action} · {transition}" if transition else action
    details = live_data.get("details") or {}
    minute = _first_field(details, ("minute", "match_minute", "game_minute"))
    stoppage = _first_field(details, ("stoppage", "stoppage_time", "added_time"))
    period = _first_field(details, ("period", "phase", "half"))
    clock = _first_field(details, ("match_clock", "game_clock", "clock"))
    score_event = _latest_score_event(details, side)
    if score_event:
        minute = score_event["minute"] if minute is None else minute
        stoppage = score_event["stoppage"] if stoppage is None else stoppage
        clock = score_event["clock"] if clock is None else clock
    last_play = details.get("last_play") or {}
    description = last_play.get("description") if isinstance(last_play, dict) else None
    penalty = bool(isinstance(description, str) and re.search(
        r"\bpenalt(?:y|ies)\b|\bfrom the spot\b", description, re.IGNORECASE,
    ))
    if change_kind == "goal" and penalty:
        action = f"{side.title()} penalty scored" if side != "unknown" else "Penalty scored"
        human_label = f"{action} · {transition}" if transition else action
    return {
        "schema": "football.match_event.v1",
        "canonical_type": canonical_type,
        "side": side,
        "score_before": before_pair,
        "score_after": after_pair,
        "score_transition": transition,
        "provider_minute": minute,
        "provider_stoppage": stoppage,
        "provider_period": period,
        "provider_clock": clock,
        "event_method": "penalty" if penalty else "unspecified",
        "scorer": (score_event or {}).get("scorer"),
        "scorer_raw": (score_event or {}).get("scorer_raw"),
        "provider_description": description,
        "human_label": human_label,
    }


PROVIDER_EVENT_SCHEMA = "football.provider_match_event.v1"
SUBSTANTIVE_EVENT_TYPES = frozenset({
    "goal.observed", "goal.disallowed", "score.correction",
    "penalty.awarded", "penalty.scored", "penalty.missed",
    "var.review", "var.overturned",
})

_EVENT_TYPE_MAP = (
    (("penalty_scored", "penaltyscored", "spotkickscored"), "penalty.scored"),
    (("penalty_missed", "penaltymissed", "missedpenalty"), "penalty.missed"),
    (("penalty_awarded", "penaltyawarded", "penalty"), "penalty.awarded"),
    (("goal_disallowed", "disallowed", "goalnogoal"), "goal.disallowed"),
    (("score_correction", "correction", "scorechangeundone"), "score.correction"),
    (("var_overturned", "varoverturned"), "var.overturned"),
    (("var", "varreview", "videoreview"), "var.review"),
    (("red_card", "redcard", "red"), "card.red"),
    (("yellow_card", "yellowcard", "yellow"), "card.yellow"),
    (("substitution", "sub"), "substitution"),
    (("kickoff", "matchstart", "matchstarted", "start"), "match.started"),
    (("periodstart", "secondhalfstart", "halfstart"), "period.started"),
    (("periodend", "halftime", "halfend"), "period.ended"),
    (("fulltime", "matchend", "matchended", "finished"), "match.ended"),
    (("suspended", "abandoned"), "match.suspended"),
    (("score_change", "scorechange", "goal"), "goal.observed"),
)


def _compact_type(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def canonical_provider_type(event_type, description=""):
    compact = _compact_type(event_type)
    text = f"{event_type or ''} {description or ''}".lower()
    if re.search(r"\bpenalt(?:y|ies)\b|\bfrom the spot\b", text) and \
            compact in {"scorechange", "score_change", "goal", ""}:
        if re.search(r"\bmiss(?:ed)?\b|\bsaved\b|\bdenied\b", text):
            return "penalty.missed"
        if re.search(r"\bawarded\b|\bgiven\b", text) and "score" not in text:
            return "penalty.awarded"
        if re.search(r"\bscore", text) or compact in {"scorechange", "goal"}:
            return "penalty.scored"
    for aliases, canonical in _EVENT_TYPE_MAP:
        if compact in {_compact_type(alias) for alias in aliases}:
            return canonical
    if not compact:
        return "provider.unknown"
    return "provider.unknown"


PROVIDER_OCCURRENCE_PRECEDENCE = (
    ("raw.occurence_ts", ("occurence_ts",)),
    ("raw.occurrence_ts", ("occurrence_ts",)),
    ("raw.details.last_play.occurence_ts", ("details", "last_play", "occurence_ts")),
    ("raw.details.last_play.occurrence_ts", ("details", "last_play", "occurrence_ts")),
)


def _finite_timestamp(value):
    """Accept only a finite, non-negative int/float provider timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if value < 0:
        return None
    return float(value)


def _dig(payload, path):
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def provider_occurrence(payload):
    """Return (ts, source, unavailable_reason) for a provider event payload.

    The provider misspells the key as `occurence_ts` and may carry it either on
    the individual significant-event row or nested under the full payload's
    `details.last_play`.  Precedence is fixed so the same payload always
    resolves the same way.

    Receipt time is never substituted: an absent or unusable value stays null
    with an explicit reason, because a fabricated occurrence would read as
    provider-timed causality that was never observed.
    """
    if not isinstance(payload, dict):
        return None, None, "provider_field_absent"
    seen_field = False
    for source, path in PROVIDER_OCCURRENCE_PRECEDENCE:
        raw = _dig(payload, path)
        if raw is None:
            continue
        seen_field = True
        value = _finite_timestamp(raw)
        if value is not None:
            return value, source, None
    reason = "provider_field_invalid" if seen_field else "provider_field_absent"
    return None, None, reason


def event_fingerprint(side, row, last_play=False):
    payload = row if isinstance(row, dict) else {"value": row}
    material = {
        "side": side,
        "last_play": bool(last_play),
        "event_type": payload.get("event_type") or payload.get("type"),
        "player": payload.get("player") or payload.get("scorer"),
        "time": payload.get("time") or payload.get("clock"),
        "description": payload.get("description") or payload.get("text"),
        "id": payload.get("id") or payload.get("event_id"),
    }
    encoded = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return digest, material


def _clock_from_row(row):
    minute, stoppage, rendered = _clock_parts(
        (row or {}).get("time") or (row or {}).get("clock"),
    )
    return minute, stoppage, rendered


def canonicalize_significant_row(side, row, live_data=None, last_play=False):
    row = row if isinstance(row, dict) else {}
    description = row.get("description")
    event_type = row.get("event_type") or row.get("type")
    canonical_type = canonical_provider_type(event_type, description or "")
    minute, stoppage, rendered = _clock_from_row(row)
    fingerprint, material = event_fingerprint(side, row, last_play=last_play)
    human = {
        "goal.observed": f"{side or 'Unknown'} goal observed",
        "goal.disallowed": f"{side or 'Unknown'} goal disallowed",
        "score.correction": f"{side or 'Unknown'} score correction",
        "penalty.awarded": f"{side or 'Unknown'} penalty awarded",
        "penalty.scored": f"{side or 'Unknown'} penalty scored",
        "penalty.missed": f"{side or 'Unknown'} penalty missed",
        "var.review": "VAR review",
        "var.overturned": "VAR overturned",
        "card.red": f"{side or 'Unknown'} red card",
        "card.yellow": f"{side or 'Unknown'} yellow card",
        "substitution": f"{side or 'Unknown'} substitution",
        "match.started": "Match started",
        "period.started": "Period started",
        "period.ended": "Period ended",
        "match.ended": "Match ended",
        "match.suspended": "Match suspended",
    }.get(canonical_type, "Provider event")
    if description:
        human = f"{human} · {description}" if human != "Provider event" else description
    return {
        "schema": PROVIDER_EVENT_SCHEMA,
        "fingerprint": fingerprint,
        "canonical_type": canonical_type,
        "side": side or "unknown",
        "provider_minute": minute,
        "provider_stoppage": stoppage,
        "provider_clock": rendered,
        "scorer": _display_person(row.get("player") or row.get("scorer")),
        "scorer_raw": row.get("player") or row.get("scorer"),
        "provider_description": description,
        "human_label": human,
        "material": material,
        "last_play": last_play,
        "raw": row,
    }


def iter_provider_event_rows(live_data):
    """Yield canonicalized significant-event and last-play rows without scores."""
    details = (live_data or {}).get("details") if isinstance(live_data, dict) else {}
    details = details if isinstance(details, dict) else {}
    for side in ("home", "away"):
        rows = details.get(f"{side}_significant_events") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                yield canonicalize_significant_row(side, row, live_data)
    last_play = details.get("last_play")
    if isinstance(last_play, dict) and (
            last_play.get("description") or last_play.get("event_type") or
            last_play.get("type")):
        yield canonicalize_significant_row(
            last_play.get("side") or "unknown", last_play, live_data, last_play=True,
        )


def period_status_event(previous, current, live_data=None):
    """Emit a lifecycle event when period or match status changes."""
    prev_period = (previous or {}).get("period")
    prev_status = (previous or {}).get("status")
    period = (current or {}).get("period")
    status = (current or {}).get("status")
    if period == prev_period and status == prev_status:
        return None
    if status == "suspended" and prev_status != "suspended":
        canonical = "match.suspended"
    elif status == "final" and prev_status != "final":
        canonical = "match.ended"
    elif prev_period is None and period in {"1st", "2nd"} and status == "live":
        canonical = "match.started" if period == "1st" else "period.started"
    elif prev_period != period and period in {"1st", "2nd"}:
        canonical = "period.started"
    elif prev_period != period and period in {"half-time", "final"}:
        canonical = "period.ended"
    else:
        canonical = "provider.unknown"
    fingerprint = hashlib.sha256(
        f"lifecycle|{prev_period}|{period}|{prev_status}|{status}".encode()
    ).hexdigest()[:24]
    return {
        "schema": PROVIDER_EVENT_SCHEMA,
        "fingerprint": fingerprint,
        "canonical_type": canonical,
        "side": "unknown",
        "provider_minute": None,
        "provider_stoppage": None,
        "provider_clock": None,
        "scorer": None,
        "scorer_raw": None,
        "provider_description": None,
        "human_label": canonical.replace(".", " ").replace("_", " ").title(),
        "material": {
            "previous_period": prev_period, "period": period,
            "previous_status": prev_status, "status": status,
        },
        "last_play": False,
        "raw": {"period": period, "status": status},
    }


def event_consistency(inferred_state, normalized):
    """Describe logical agreement without claiming that the feed caused a trade."""
    canonical = (normalized or {}).get("canonical_type", "")
    after = (normalized or {}).get("score_after") or {}
    home, away = after.get("home"), after.get("away")
    if canonical.startswith("score_correction") or canonical in {
        "score.correction", "goal.disallowed", "var.overturned",
    }:
        return "correction_or_reversal"
    is_goal = (
        canonical.startswith("goal_observed") or canonical in {
            "goal.observed", "penalty.scored",
        }
    )
    if not is_goal:
        return "time_match_only"
    if inferred_state == "equal_score_0" and home is not None and home == away:
        return "equalizer_consistent"
    if inferred_state == "one_goal_lead_+1" and home is not None and away is not None \
            and abs(home - away) == 1:
        return "one_goal_lead_consistent"
    if inferred_state in {"equal_score_0", "one_goal_lead_+1"} and \
            home is not None and away is not None:
        return "state_mismatch"
    return "goal_consistent_state_unknown"


def association_class(consistency, matched):
    """Spec association labels; never `caused_by`."""
    if not matched:
        return "no_nearby_same_match_event"
    if consistency in {"equalizer_consistent", "one_goal_lead_consistent",
                       "goal_consistent_state_unknown"}:
        return "state_consistent"
    if consistency == "state_mismatch":
        return "state_mismatch"
    return "temporally_associated"
