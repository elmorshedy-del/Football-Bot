"""API-Football client -- the only source of an exact match Clock.

Why this exists at all: Kalshi publishes no usable match timing.  Its
`expected_expiration_time` is a schedule estimate that was measured 47 minutes
wrong on KXMLSGAME-26SEP05ATXSJ (it read 03:30 for a game whose last print was
02:43), and `close_time` is padded by days.  See lab/CONTRACT.md section 5.

What makes this provider the answer is one field: `fixture.periods`, which
carries `first` and `second` as unix timestamps of when each half ACTUALLY
kicked off -- as distinct from `fixture.timestamp`, the scheduled time.  Two
real anchors per game turn any tape microsecond into an exact match second,
with the halftime break falling out of the arithmetic rather than having to be
estimated.

Cost note that shapes the call pattern: `/fixtures` returns `periods` for every
fixture of a season in one paginated call, so the Clock costs ~4 requests for
our whole date range.  It is `/fixtures/events` -- the VAR and penalty ground
truth -- that costs one request per game.  Fetch the cheap thing eagerly and
the expensive thing incrementally.
"""
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "https://v3.football.api-sports.io"
MLS_LEAGUE_ID = 253

# Free plan is 10 requests/minute; paid plans are far higher.  Pacing at 6/s
# stays inside every plan's per-minute ceiling without a plan lookup.
_MIN_INTERVAL_S = 1.0 / 6.0
_last_call = [0.0]


class ApiFootballUnavailable(RuntimeError):
    """No key configured.  Callers degrade to a non-exact Clock, never guess."""


def api_key():
    return os.environ.get("APIFOOTBALL_KEY") or ""


def available():
    return bool(api_key())


def _get(path, **params):
    key = api_key()
    if not key:
        raise ApiFootballUnavailable(
            "APIFOOTBALL_KEY is not set; an exact Clock is unavailable"
        )
    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-apisports-key": key})
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                _last_call[0] = time.monotonic()
                payload = json.load(resp)
            break
        except Exception as exc:                      # noqa: BLE001 - retried below
            last = exc
            time.sleep(min(2 ** attempt, 20))
    else:
        raise RuntimeError(f"api-football {path} failed after 5 attempts: {last!r}")

    # The API answers 200 with an `errors` body rather than an HTTP error code,
    # so a quota or plan problem looks like success unless it is checked here.
    errors = payload.get("errors")
    if errors and errors not in ([], {}):
        raise RuntimeError(f"api-football {path} returned errors: {errors}")
    return payload


def fixtures(season, league=MLS_LEAGUE_ID):
    """Every fixture of a season, with `periods`.  Paginated; ~1-2 requests."""
    out = []
    page = 1
    while True:
        payload = _get("/fixtures", league=league, season=season, page=page)
        out.extend(payload.get("response") or [])
        paging = payload.get("paging") or {}
        if page >= int(paging.get("total") or 1):
            break
        page += 1
    return out


def fixture_events(fixture_id):
    """Goals, cards, substitutions and VAR decisions for one fixture.

    This is where `Goal cancelled` and `Penalty confirmed` live.  Kalshi's own
    feed has no VAR or penalty event type at all -- verified across 29 games,
    it carries only score_change, yellow_card and red_card -- so an overturned
    goal is invisible there.  One request per game, so call it incrementally.
    """
    return _get("/fixtures/events", fixture=fixture_id).get("response") or []


def periods_of(fixture):
    """(first, second) unix seconds, or (None, None) when not published."""
    periods = (fixture.get("fixture") or {}).get("periods") or {}
    return periods.get("first"), periods.get("second")
