"""
NBA betting lines from ESPN's public JSON API, built into

    nba/espn/betting_lines/season=<season>/data.parquet

one row per game and provider (sportsbook), with each provider's opening and
closing spread, total and moneylines (#99). ESPN's scoreboard has odds only
for games that haven't started, so the lines come from its core API, which
keeps them for each game after it's played. Saved gzipped at

    nba/raw/espn/<season>/scoreboard/<date>.json.gz    a day's games
    nba/raw/espn/<season>/odds/<event_id>.json.gz      a game's lines

A day's scoreboard is fetched until it's REFETCH_DAYS old, then never again;
a game's odds once it's final (again while it's recent if ESPN had none).

Past seasons also get the lines in the archives pipeline.odds_archives
downloads, each as its own provider: The Odds API's consensus close for
2020-2026 (keyed by ESPN event id, so matched through the scoreboards) and
sportsbookreview.com's for 2012-2022. ESPN's own odds before 2026 are
missing for about half the games, so a backfill can skip them with
--no-espn-odds, which still fetches the scoreboards the Odds API archive
needs.

ESPN's games (and sportsbookreview's) are matched to the NBA's by date and
teams in pipeline.stats' team_game_logs (in the output directory, else the
bucket), since only those games are in `games`. A game that isn't there
yet is left out until a later run, and one still unmatched a day after
tip-off is reported.

usage: python -m pipeline.espn_odds [--out out] [--season 2026 ...] [--no-fetch] [--no-espn-odds] [--url URL]
"""

import argparse
import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa

from . import odds, odds_archives
from .catalog import URL
from .ncaam_espn import TYPES, Fetcher, Getter, arrow, save_json
from .output import check_keys, dataset_path, report, write_parquet
from .seasons import EASTERN, current_season, season_days, today_eastern
from .teams import TEAM_ABBREVS_FILE, load_team_abbrevs

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE_API = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
RAW_DIR = Path("nba/raw/espn")
OUT_DIR = Path("nba/espn")
STATS_DIR = Path("nba/stats")
DATASET = "betting_lines"

# a day's scoreboard is refetched until it's this old (late finishes), and
# a game's odds, when ESPN had none, until its game is
REFETCH_DAYS = 2
RECENT_DAYS = 3
# a final game not matched to the NBA's by then is reported
LATE = timedelta(hours=24)

# ESPN's season types; 1 is the preseason, which is left out
PRESEASON = 1

COLUMNS = [
    ("season", "INTEGER"),
    ("game_id", "VARCHAR"),
    *odds.PROVIDER,
    ("home_team_id", "VARCHAR"),
    ("away_team_id", "VARCHAR"),
    *odds.LINES,
    ("espn_event_id", "VARCHAR"),
]
KEYS = ["game_id", "provider_id"]
# a game in one source, which build_season matches to an NBA game
EVENT_COLUMNS = [
    ("key", "VARCHAR"),
    ("espn_event_id", "VARCHAR"),
    ("game_date", "DATE"),
    ("start_time", "TIMESTAMPTZ"),
    ("home_team_id", "VARCHAR"),
    ("away_team_id", "VARCHAR"),
]


def season_dir(outdir: Path, season: int) -> Path:
    return outdir / RAW_DIR / str(season)


def load_json(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def scoreboard_url(day: date) -> str:
    return f"{SITE_API}/scoreboard?dates={day:%Y%m%d}"


def odds_url(event_id: str) -> str:
    return f"{CORE_API}/events/{event_id}/competitions/{event_id}/odds"


def odds_path(outdir: Path, season: int, event_id: str) -> Path:
    return season_dir(outdir, season) / "odds" / f"{event_id}.json.gz"


def scoreboard_events(data: dict) -> list[dict]:
    """
    The games on a scoreboard, preseason excluded. Raises ValueError if it
    isn't as expected
    """
    events = data.get("events")
    if events is None:
        raise ValueError("espn_odds: scoreboard has no events list")
    games = []
    for e in events:
        if e["season"]["type"] == PRESEASON:
            continue
        c = e["competitions"][0]
        sides = {t["homeAway"]: t for t in c["competitors"]}
        if set(sides) != {"home", "away"}:
            raise ValueError(f"espn_odds: game {e['id']} competitors {list(sides)}")
        start = datetime.fromisoformat(c["date"])
        games.append(
            {
                "event_id": str(e["id"]),
                "start_time": start,
                "game_date": start.astimezone(EASTERN).date(),
                "completed": e["status"]["type"]["completed"],
                "home_espn_id": str(sides["home"]["team"]["id"]),
                "away_espn_id": str(sides["away"]["team"]["id"]),
            }
        )
    return games


def saved_events(outdir: Path, season: int) -> list[dict]:
    """
    every game on the season's saved scoreboards. A postponed game can be on
    two days' scoreboards; the later day's is kept
    """
    events = {}
    for path in sorted((season_dir(outdir, season) / "scoreboard").glob("*.json.gz")):
        for e in scoreboard_events(load_json(path)):
            events[e["event_id"]] = e
    return list(events.values())


def fetch_scoreboards(fetcher: Getter, outdir: Path, season: int, today: date) -> int:
    """
    Fetch the season's scoreboards up to today: every day not saved, and the
    last REFETCH_DAYS days again. Returns how many were fetched
    """
    directory = season_dir(outdir, season) / "scoreboard"
    count = 0
    for day in season_days(season, today):
        path = directory / f"{day}.json.gz"
        if path.exists() and (today - day).days > REFETCH_DAYS:
            continue
        data = fetcher.json(scoreboard_url(day))
        if data is None:
            raise ValueError(f"espn_odds {season}: no scoreboard for {day}")
        scoreboard_events(data)
        save_json(path, data)
        count += 1
    return count


def fetch_odds(fetcher: Getter, outdir: Path, season: int, today: date) -> int:
    """
    Fetch the odds of each final game not saved, and of recent games saved
    without any. Returns how many were fetched
    """
    count = 0
    for e in saved_events(outdir, season):
        if not e["completed"]:
            continue
        path = odds_path(outdir, season, e["event_id"])
        if path.exists() and (
            (today - e["game_date"]).days > RECENT_DAYS or load_json(path).get("items")
        ):
            continue
        data = fetcher.json(odds_url(e["event_id"]))
        # an event ESPN doesn't know has no odds; save that, so an old game
        # isn't asked for again
        if data is None:
            data = {"count": 0, "items": []}
        odds.core_lines(data)
        save_json(path, data)
        count += 1
    return count


def espn_team_ids(season: int, path: Path = TEAM_ABBREVS_FILE) -> dict[str, str]:
    """ESPN team id -> NBA team_id"""
    return {
        a.abbrev: a.team_id
        for a in load_team_abbrevs(path)
        if a.source == "espn_team_id" and a.covers(season)
    }


def load_team_games(
    con: duckdb.DuckDBPyConnection, outdir: Path, season: int, url: str | None
) -> None:
    """
    `team_games` (game_id, game_date, teams), each NBA game's date and sorted
    team ids, from pipeline.stats' team_game_logs in `outdir` if it's there,
    else the bucket at `url`. Empty if neither can be read
    """
    local = dataset_path(outdir / STATS_DIR, "team_game_logs", season)
    if local.is_file():
        source = str(local)
    elif url:
        source = f"{url}/{STATS_DIR}/team_game_logs/season={season}/data.parquet"
    else:
        source = None
    if source:
        try:
            con.execute(
                """
                CREATE OR REPLACE TABLE team_games AS
                SELECT game_id, any_value(game_date)::DATE AS game_date,
                    list_sort(list(team_id::VARCHAR)) AS teams
                FROM read_parquet(?, hive_partitioning = false)
                GROUP BY game_id
                """,
                [source],
            )
            return
        except duckdb.IOException as e:
            # the bucket answers 403, not 404, for a season with no games yet
            if local.is_file():
                raise
            print(
                f"espn_odds: couldn't read the bucket's games: {str(e).splitlines()[0]}"
            )
    con.execute(
        "CREATE OR REPLACE TABLE team_games "
        "(game_id VARCHAR, game_date DATE, teams VARCHAR[])"
    )


def build_season(
    outdir: Path,
    season: int,
    url: str | None = None,
    now: datetime | None = None,
    abbrevs_file: Path = TEAM_ABBREVS_FILE,
) -> Path | None:
    """
    Write the season's betting_lines from its saved scoreboards and odds,
    and the archives' lines (pipeline.odds_archives) for the seasons they
    have. Returns the path, or None if there's nothing to write. Final games
    with lines not matched to an NBA game LATE after tip-off are reported,
    as are an archive's games that aren't matched at all
    """
    now = now or datetime.now(UTC)
    teams = espn_team_ids(season, abbrevs_file)
    # events are games in a source, keyed by `key`; lines refer to them
    events, lines = [], []
    for e in saved_events(outdir, season):
        if not e["completed"]:
            continue
        path = odds_path(outdir, season, e["event_id"])
        home, away = teams.get(e["home_espn_id"]), teams.get(e["away_espn_id"])
        if home is None or away is None:
            # the All-Star game's teams aren't franchises
            if path.exists():
                print(f"espn_odds: skipping game {e['event_id']}, not two NBA teams")
            continue
        events.append(
            {
                "key": e["event_id"],
                "espn_event_id": e["event_id"],
                "game_date": e["game_date"],
                "start_time": e["start_time"],
                "home_team_id": home,
                "away_team_id": away,
            }
        )
        if path.exists():
            lines += [
                r | {"key": e["event_id"], "espn_event_id": e["event_id"]}
                for r in odds.core_lines(load_json(path))
            ]

    odds_archives.require(outdir, season)
    if season in odds_archives.ODDS_API.seasons:
        lines += [
            r | {"key": r["espn_event_id"]}
            for r in odds_archives.odds_api_lines(outdir, {e["key"] for e in events})
        ]
    if season in odds_archives.SBR.seasons:
        sbr_events, sbr_lines = odds_archives.sbr_lines(outdir, season)
        events += [e | {"espn_event_id": None, "start_time": None} for e in sbr_events]
        lines += [r | {"espn_event_id": None} for r in sbr_lines]
    if not lines:
        print(f"espn_odds: no betting lines for {season} yet")
        return None

    con = duckdb.connect()
    con.register("events_arrow", arrow(events, EVENT_COLUMNS))
    line_columns = [*odds.COLUMNS, ("key", "VARCHAR"), ("espn_event_id", "VARCHAR")]
    con.register(
        "lines",
        pa.table(
            {n: pa.array([r[n] for r in lines], TYPES[t]) for n, t in line_columns}
        ),
    )
    load_team_games(con, outdir, season, url)
    # the same teams on the same date, or a day either side (a game abroad
    # can be on a different date in the two sources, and sportsbookreview
    # has some a day off); the nearest wins
    con.execute(
        """
        CREATE TABLE matched AS
        SELECT e.*, g.game_id
        FROM events_arrow e JOIN team_games g
            ON g.teams = list_sort([e.home_team_id, e.away_team_id])
            AND abs(g.game_date - e.game_date) <= 1
        QUALIFY row_number() OVER (
            PARTITION BY e.key ORDER BY abs(g.game_date - e.game_date)) = 1
        """
    )
    late = con.execute(
        """
        SELECT key FROM events_arrow
        WHERE key IN (SELECT key FROM lines)
            AND key NOT IN (SELECT key FROM matched)
            AND (start_time IS NULL OR start_time < ?)
        ORDER BY ALL
        """,
        [now - LATE],
    ).fetchall()
    if late:
        report(
            f"espn_odds: games {[e for (e,) in late]} aren't in the NBA's team "
            f"game logs (ESPN's, {LATE.total_seconds() / 3600:g} hours after "
            "tip-off); their betting lines are left out"
        )
    columns = ", ".join(name for name, _ in COLUMNS)
    con.execute(
        f"""
        CREATE TABLE out AS
        SELECT {columns} FROM (
            SELECT {season}::INTEGER AS season, m.game_id, l.* EXCLUDE (key),
                m.home_team_id, m.away_team_id
            FROM lines l JOIN matched m USING (key)
        )
        ORDER BY game_id, provider_id
        """
    )
    check_keys(con, "out", KEYS, f"espn_odds {season} {DATASET}")
    count = con.execute("SELECT count(*) FROM out").fetchone()
    if not count or not count[0]:
        print(f"espn_odds: no betting lines for {season} matched NBA games yet")
        return None
    path = dataset_path(outdir / OUT_DIR, DATASET, season)
    write_parquet(con, "out", path)
    print(f"espn_odds: wrote {path} ({count[0]} rows)")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ESPN's NBA betting lines")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help="season(s) to update, as the end year (default: current season)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild from saved files"
    )
    parser.add_argument(
        "--no-espn-odds",
        action="store_true",
        help="don't fetch each game's ESPN odds, only the archives and, for "
        "the Odds API archive's seasons, the scoreboards that match it",
    )
    parser.add_argument(
        "--url",
        default=URL,
        help=f"bucket URL for team_game_logs if --out has none (default: {URL})",
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    fetcher = None if args.no_fetch else Fetcher(label="espn_odds")
    for season in args.season or [current_season(today)]:
        if fetcher:
            odds_archives.fetch(fetcher, args.out, season)
            if not args.no_espn_odds or season in odds_archives.ODDS_API.seasons:
                n = fetch_scoreboards(fetcher, args.out, season, today)
                print(f"espn_odds: {season}: fetched {n} scoreboards")
            if not args.no_espn_odds:
                n = fetch_odds(fetcher, args.out, season, today)
                print(f"espn_odds: {season}: fetched odds for {n} games")
        build_season(args.out, season, url=args.url)


if __name__ == "__main__":
    main()
