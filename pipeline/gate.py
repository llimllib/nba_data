"""
Whether update.yml has work to do. The NBA's team game logs list a game
about 2 to 4.5 hours after it ends, so the update is dispatched every 30
minutes through the afternoon and night, and each run asks this first; most
stop here, before the exit node is set up or stats.nba.com is touched.

There's work if a game should be over (ENDED after its tip-off, not
postponed, within LOOKBACK) and isn't in `games` yet, or if a game from
the last two days is in `games` but not in `game_events` (its play-by-play
failed, or hasn't been fetched). The tables are read from the bucket (or
--out if it has them). If they can't be read, there's work: a bad gate
shouldn't stop the updates.

Prints why, and writes run=true or run=false to $GITHUB_OUTPUT if it's
set (a workflow step's outputs).

usage: python -m pipeline.gate [--out out] [--url URL]
"""

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from .catalog import URL
from .rotations import read_stats
from .seasons import EASTERN, current_season

# a game is usually over this long after tip-off
ENDED = timedelta(minutes=150)
# a game missing longer than this is pipeline.stats' late-game report's
# business (12 hours after tip-off); the gate stops waiting for it
LOOKBACK = timedelta(hours=18)
# games this recent should have play-by-play
EVENTS_DAYS = 2


def read(outdir: Path, season: int, dataset: str, query: str, url: str) -> list:
    """
    read_stats, but a file that isn't there (the bucket answers 403, not
    404) is empty: before a season's schedule is out, there's nothing to do
    """
    try:
        return read_stats(outdir, season, dataset, query, url)
    except duckdb.HTTPException as e:
        if "403" in str(e) or "404" in str(e):
            return []
        raise


def ended_missing(outdir: Path, season: int, now: datetime, url: str) -> list[str]:
    """games that should be over and aren't in `games`, oldest first"""
    ended = read(
        outdir,
        season,
        "schedule",
        f"""
        SELECT game_id FROM t
        WHERE start_time <= '{(now - ENDED).isoformat()}'
            AND start_time >= '{(now - LOOKBACK).isoformat()}'
            AND coalesce(status_text, '') NOT ILIKE '%ppd%'
            AND coalesce(status_text, '') NOT ILIKE '%postpon%'
        ORDER BY start_time, game_id
        """,
        url,
    )
    if not ended:
        return []
    have = {g for (g,) in read(outdir, season, "games", "SELECT game_id FROM t", url)}
    return [g for (g,) in ended if g not in have]


def events_missing(outdir: Path, season: int, now: datetime, url: str) -> list[str]:
    """recent games in `games` with no play-by-play in `game_events`"""
    since = now.astimezone(EASTERN).date() - timedelta(days=EVENTS_DAYS)
    recent = read(
        outdir,
        season,
        "games",
        f"SELECT game_id FROM t WHERE game_date >= '{since}' ORDER BY game_id",
        url,
    )
    if not recent:
        return []
    have = {
        g
        for (g,) in read(
            outdir, season, "game_events", "SELECT DISTINCT game_id FROM t", url
        )
    }
    return [g for (g,) in recent if g not in have]


def work(outdir: Path, now: datetime, url: str = URL) -> list[str]:
    """why there's work to do; empty if there's none"""
    season = current_season(now.astimezone(EASTERN).date())
    reasons = []
    try:
        if missing := ended_missing(outdir, season, now, url):
            reasons.append(f"games over but not in games: {', '.join(missing)}")
        if missing := events_missing(outdir, season, now, url):
            reasons.append(f"games without play-by-play: {', '.join(missing)}")
    except (duckdb.Error, OSError) as e:
        reasons.append(f"couldn't read the bucket, so updating anyway: {e}")
    return reasons


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--url", default=URL, help=f"bucket URL (default: {URL})")
    args = parser.parse_args(argv)

    reasons = work(args.out, datetime.now(UTC), args.url)
    for r in reasons:
        print(f"gate: {r}")
    if not reasons:
        print("gate: nothing to do")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as f:
            f.write(f"run={'true' if reasons else 'false'}\n")


if __name__ == "__main__":
    main()
