"""Go/no-go spike: can we build an exact Clock for every MLS game we have?

The user's requirement is an exact match clock, and an approximate one was
explicitly rejected.  Everything downstream -- the minute-cutoff sweep, which
is half the question the platform exists to answer -- rests on API-Football
publishing `fixture.periods` for our games.  This measures that before three
workers build on the assumption, rather than after.

It answers three questions and nothing else:

  1. How many MLS fixtures does the provider publish for our seasons, and how
     many carry BOTH period timestamps?
  2. How many of Kalshi's 711 games can be matched to one of those fixtures?
  3. Therefore, what fraction of games get clock_source='exact_periods'?

Run: APIFOOTBALL_KEY=... python -m lab.clock_spike
"""
import collections
import datetime as dt
import json
import re
import sys
import time
import urllib.request

from lab.common import apifootball

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def kalshi_get(path):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(KALSHI + path, timeout=45) as resp:
                return json.load(resp)
        except Exception:                             # noqa: BLE001 - retried
            time.sleep(1.5 * (attempt + 1))
    return {}


def kalshi_pages(path, extra):
    rows, cursor = [], ""
    for _ in range(40):
        page = kalshi_get(f"{path}?limit=1000{extra}" + (f"&cursor={cursor}" if cursor else ""))
        batch = page.get("markets") or []
        rows.extend(batch)
        cursor = page.get("cursor") or ""
        if not cursor or not batch:
            break
    return rows


def kalshi_games():
    """The 711 games, as {event_ticker: {date, names}} -- see CONTRACT.md 3.1."""
    markets = []
    markets += kalshi_pages("/historical/markets", "&series_ticker=KXMLSGAME")
    markets += kalshi_pages("/markets", "&series_ticker=KXMLSGAME&status=settled")
    markets += kalshi_pages("/markets", "&series_ticker=KXMLSGAME&status=open")

    games = {}
    for market in {m["ticker"]: m for m in markets}.values():
        event = market.get("event_ticker")
        if not event:
            continue
        game = games.setdefault(event, {"names": set(), "close": None})
        label = (market.get("yes_sub_title") or "").strip()
        if label and label.lower() != "tie":
            game["names"].add(label)
        close = market.get("close_time")
        if close and (game["close"] is None or close < game["close"]):
            game["close"] = close
    for event, game in games.items():
        # close_time is padded past the match, but only ever by hours-to-days
        # and never backwards, so its DATE is a sound matching key once we
        # allow a +/-1 day window for the UTC/local boundary.
        game["date"] = (game["close"] or "")[:10]
    return games


_PUNCT = re.compile(r"[^a-z ]+")
# Words that appear in one source's name and not the other's carry no signal.
_NOISE = {
    "fc", "sc", "cf", "club", "the", "city", "united", "real", "new",
    "sporting", "athletic", "de", "of",
}


def tokens(name):
    words = _PUNCT.sub(" ", (name or "").lower()).split()
    return {w for w in words if w not in _NOISE and len(w) > 2}


def name_score(kalshi_names, fixture_names):
    """How many of the two teams we can corroborate across both sources."""
    matched = 0
    for k_name in kalshi_names:
        k_tokens = tokens(k_name)
        if not k_tokens:
            continue
        if any(k_tokens & tokens(f_name) for f_name in fixture_names):
            matched += 1
    return matched


def main():
    if not apifootball.available():
        print("APIFOOTBALL_KEY is not set -- cannot run the spike.")
        print("The exact Clock is unverified until it is.")
        return 2

    print("Fetching Kalshi MLS games ...")
    games = kalshi_games()
    dates = sorted(g["date"] for g in games.values() if g["date"])
    print(f"  {len(games)} games, {dates[0]} -> {dates[-1]}")
    seasons = sorted({int(d[:4]) for d in dates})
    print(f"  seasons implied: {seasons}")

    fixtures_by_date = collections.defaultdict(list)
    totals = {}
    for season in seasons:
        print(f"Fetching api-football fixtures for MLS season {season} ...")
        try:
            rows = apifootball.fixtures(season)
        except Exception as exc:                      # noqa: BLE001 - reported
            print(f"  FAILED: {exc}")
            totals[season] = (0, 0)
            continue
        with_periods = 0
        for fixture in rows:
            first, second = apifootball.periods_of(fixture)
            if first and second:
                with_periods += 1
            date = ((fixture.get("fixture") or {}).get("date") or "")[:10]
            fixtures_by_date[date].append(fixture)
        totals[season] = (len(rows), with_periods)
        pct = 100.0 * with_periods / len(rows) if rows else 0.0
        print(f"  {len(rows)} fixtures, {with_periods} with BOTH periods ({pct:.1f}%)")

    print("\nMatching Kalshi games to fixtures (date +/-1 day, both team names) ...")
    exact = ambiguous = unmatched_name = unmatched_date = 0
    by_season = collections.Counter()
    misses = []
    for event, game in sorted(games.items()):
        if not game["date"]:
            unmatched_date += 1
            continue
        day = dt.date.fromisoformat(game["date"])
        nearby = []
        for offset in (0, -1, 1):
            nearby.extend(fixtures_by_date.get((day + dt.timedelta(days=offset)).isoformat(), []))
        if not nearby:
            unmatched_date += 1
            misses.append((event, game["date"], "no fixture on nearby dates"))
            continue

        scored = []
        for fixture in nearby:
            teams = fixture.get("teams") or {}
            names = [(teams.get("home") or {}).get("name"), (teams.get("away") or {}).get("name")]
            scored.append((name_score(game["names"], names), fixture))
        best = max(s for s, _ in scored)
        winners = [f for s, f in scored if s == best]
        if best < 2:
            unmatched_name += 1
            misses.append((event, game["date"], f"best name match {best}/2 of {sorted(game['names'])}"))
        elif len(winners) > 1:
            ambiguous += 1
            misses.append((event, game["date"], f"{len(winners)} fixtures tie at 2/2"))
        else:
            first, second = apifootball.periods_of(winners[0])
            if first and second:
                exact += 1
                by_season[game["date"][:4]] += 1
            else:
                unmatched_name += 0
                misses.append((event, game["date"], "matched but periods missing"))

    total = len(games)
    print(f"\n  exact_periods available : {exact}/{total} ({100.0*exact/total:.1f}%)")
    print(f"  by season               : {dict(by_season)}")
    print(f"  ambiguous fixture match : {ambiguous}")
    print(f"  name match failed       : {unmatched_name}")
    print(f"  no fixture near date    : {unmatched_date}")
    if misses:
        print("\n  first 15 problems:")
        for event, date, why in misses[:15]:
            print(f"    {event:34s} {date}  {why}")

    print("\nVERDICT")
    share = exact / total if total else 0.0
    if share >= 0.95:
        print("  GO. Exact Clock for essentially the whole dataset.")
    elif share >= 0.60:
        print("  GO WITH A GAP. Minute-cutoff work is sound but excludes a real slice;")
        print("  the excluded count must be visible on every result.")
    else:
        print("  NO-GO as designed. Too few games carry an exact Clock; the minute-cutoff")
        print("  question cannot be answered honestly from this provider alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
