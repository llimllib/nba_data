"""
Men's college basketball games and box scores from ESPN, built into

    ncaam/espn/games/season=<season>/data.parquet
    ncaam/espn/team_box/season=<season>/data.parquet
    ncaam/espn/player_box/season=<season>/data.parquet
    ncaam/espn/team_seasons/season=<season>/data.parquet
    ncaam/espn/player_seasons/season=<season>/data.parquet

keyed by ESPN's ids. games, team_seasons and player_seasons are the college
catalog's lookup tables, named like the NBA's so pipeline.integrity checks
every key the same way.

usage: python -m pipeline.ncaam_espn [--out out] [--season 2026 ...] [--no-fetch]

Past seasons (2005-2026) come from sportsdataverse's hoopR files, ESPN's
data already scraped and published as parquet on GitHub, so the backfill
puts almost no load on ESPN. They're downloaded once, as published, to

    ncaam/raw/espn/<season>/sportsdataverse/<schedule|team_box|player_box>.parquet

Their schedules list a few finished games (1,400 over 22 seasons, most
before 2008) that have no box score in their files, though ESPN has one;
those are fetched from ESPN once.

The current season comes from ESPN's public JSON API: a scoreboard of every
Division I game per day, and a summary (box score, play-by-play) per
finished game, kept gzipped at

    ncaam/raw/espn/<season>/scoreboard/<date>.json.gz
    ncaam/raw/espn/<season>/summary/<game_id>.json.gz

A day's scoreboard is fetched until it's REFETCH_DAYS old, then never again;
a game's summary once, when it's final (again if it had no box score and
it's recent). Summaries are kept without the news, videos and betting odds
ESPN includes, which are more than half of each response and don't change
the game. A season with saved scoreboards is built from them, and one
without from sportsdataverse's files.

Only finished games are kept. Players who didn't play aren't in player_box.
Teams are mapped to torvik's names in ncaam_teams.csv, so box scores join
to pipeline.torvik's ratings.
"""

import argparse
import csv
import functools
import gzip
import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import duckdb
import pyarrow as pa

from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import EASTERN, current_season, today_eastern

API = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
SDV = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
USER_AGENT = "nba_data (https://github.com/llimllib/nba_data)"
# ESPN doesn't publish a limit; one request a second is gentle for a CDN
REQUEST_DELAY = 1
RETRY_DELAY = 10
RAW_DIR = Path("ncaam/raw/espn")
OUT_DIR = Path("ncaam/espn")
TEAMS_CSV = Path(__file__).parent / "ncaam_teams.csv"

# sportsdataverse's files have 1 game for 2003 and 12 for 2004
FIRST_SEASON = 2005
# Division I, on the scoreboard; one day has had at most ~150 games
GROUPS = 50
LIMIT = 400
# a day's scoreboard is refetched until it's this old (late finishes,
# stat corrections), and a summary without a box score until its game is
REFETCH_DAYS = 2
RECENT_DAYS = 7

# the parts of a summary that are kept
SUMMARY_KEYS = ("header", "boxscore", "gameInfo", "plays", "winprobability")

# sportsdataverse's files for a season, and where they're kept
SDV_FILES = {
    "schedule": "espn_mens_college_basketball_schedules/mbb_schedule_{season}.parquet",
    "team_box": "espn_mens_college_basketball_team_boxscores/team_box_{season}.parquet",
    "player_box": "espn_mens_college_basketball_player_boxscores/player_box_{season}.parquet",
}

# ESPN's season types: 1 is the preseason, which has no games here
SEASON_TYPES = {2: "regular_season", 3: "postseason"}

GAMES = [
    ("season", "INTEGER"),
    ("game_id", "VARCHAR"),
    ("game_date", "DATE"),
    ("start_time", "TIMESTAMPTZ"),
    ("season_type", "VARCHAR"),
    ("home_team_id", "VARCHAR"),
    ("away_team_id", "VARCHAR"),
    ("home_pts", "INTEGER"),
    ("away_pts", "INTEGER"),
    ("neutral_site", "BOOLEAN"),
    ("conference_game", "BOOLEAN"),
    ("note", "VARCHAR"),
    ("venue", "VARCHAR"),
    ("attendance", "INTEGER"),
]

# ESPN's team box statistic -> our columns. "a-b" statistics are two
TEAM_STATS = {
    "fieldGoalsMade-fieldGoalsAttempted": ("fgm", "fga"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("fg3m", "fg3a"),
    "freeThrowsMade-freeThrowsAttempted": ("ftm", "fta"),
    "offensiveRebounds": ("oreb",),
    "defensiveRebounds": ("dreb",),
    "totalRebounds": ("reb",),
    "assists": ("ast",),
    "steals": ("stl",),
    "blocks": ("blk",),
    # the box score's turnovers include team turnovers (shot clock and
    # 5-second violations), as NBA box scores do
    "totalTurnovers": ("tov",),
    "teamTurnovers": ("team_tov",),
    "fouls": ("pf",),
    "totalTechnicalFouls": ("tech_fouls",),
    "flagrantFouls": ("flagrant_fouls",),
    "turnoverPoints": ("pts_off_tov",),
    "fastBreakPoints": ("fast_break_pts",),
    "pointsInPaint": ("pts_paint",),
    "largestLead": ("largest_lead",),
}
TEAM_STAT_COLUMNS = [c for cols in TEAM_STATS.values() for c in cols]
# sportsdataverse's names for the same columns
SDV_TEAM_STATS = {
    "fgm": "field_goals_made",
    "fga": "field_goals_attempted",
    "fg3m": "three_point_field_goals_made",
    "fg3a": "three_point_field_goals_attempted",
    "ftm": "free_throws_made",
    "fta": "free_throws_attempted",
    "oreb": "offensive_rebounds",
    "dreb": "defensive_rebounds",
    "reb": "total_rebounds",
    "ast": "assists",
    "stl": "steals",
    "blk": "blocks",
    "tov": "total_turnovers",
    "team_tov": "team_turnovers",
    "pf": "fouls",
    "tech_fouls": "total_technical_fouls",
    "flagrant_fouls": "flagrant_fouls",
    "pts_off_tov": "turnover_points",
    "fast_break_pts": "fast_break_points",
    "pts_paint": "points_in_paint",
    "largest_lead": "largest_lead",
}
# a team box row is useless without these
REQUIRED_TEAM_STATS = ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta"]

TEAM_BOX = [
    ("season", "INTEGER"),
    ("game_id", "VARCHAR"),
    ("team_id", "VARCHAR"),
    ("opp_team_id", "VARCHAR"),
    ("game_date", "DATE"),
    ("season_type", "VARCHAR"),
    ("home", "BOOLEAN"),
    ("win", "BOOLEAN"),
    ("pts", "INTEGER"),
    ("opp_pts", "INTEGER"),
    *[(c, "INTEGER") for c in TEAM_STAT_COLUMNS],
]

# ESPN's player box labels -> our columns
PLAYER_STATS = {
    "MIN": ("min",),
    "PTS": ("pts",),
    "FG": ("fgm", "fga"),
    "3PT": ("fg3m", "fg3a"),
    "FT": ("ftm", "fta"),
    "OREB": ("oreb",),
    "DREB": ("dreb",),
    "REB": ("reb",),
    "AST": ("ast",),
    "STL": ("stl",),
    "BLK": ("blk",),
    "TO": ("tov",),
    "PF": ("pf",),
}
PLAYER_STAT_COLUMNS = [c for cols in PLAYER_STATS.values() for c in cols]
SDV_PLAYER_STATS = {
    "min": "minutes",
    "pts": "points",
    "fgm": "field_goals_made",
    "fga": "field_goals_attempted",
    "fg3m": "three_point_field_goals_made",
    "fg3a": "three_point_field_goals_attempted",
    "ftm": "free_throws_made",
    "fta": "free_throws_attempted",
    "oreb": "offensive_rebounds",
    "dreb": "defensive_rebounds",
    "reb": "rebounds",
    "ast": "assists",
    "stl": "steals",
    "blk": "blocks",
    "tov": "turnovers",
    "pf": "fouls",
}

PLAYER_BOX = [
    ("season", "INTEGER"),
    ("game_id", "VARCHAR"),
    ("team_id", "VARCHAR"),
    ("player_id", "VARCHAR"),
    ("game_date", "DATE"),
    ("starter", "BOOLEAN"),
    ("ejected", "BOOLEAN"),
    *[(c, "INTEGER") for c in PLAYER_STAT_COLUMNS],
]

# a team's names as of one game, collected into team_seasons
TEAM_INFO = [
    ("team_id", "VARCHAR"),
    ("location", "VARCHAR"),
    ("name", "VARCHAR"),
    ("display_name", "VARCHAR"),
    ("abbrev", "VARCHAR"),
    ("conference_id", "VARCHAR"),
    ("start_time", "TIMESTAMPTZ"),
]
PLAYER_INFO = [
    ("game_id", "VARCHAR"),
    ("player_id", "VARCHAR"),
    ("team_id", "VARCHAR"),
    ("name", "VARCHAR"),
    ("short_name", "VARCHAR"),
    ("position", "VARCHAR"),
    ("game_date", "DATE"),
]

TYPES = {
    "INTEGER": pa.int32(),
    "DOUBLE": pa.float64(),
    "VARCHAR": pa.string(),
    "DATE": pa.date32(),
    "BOOLEAN": pa.bool_(),
    "TIMESTAMPTZ": pa.timestamp("us", tz="UTC"),
}


# ---------------------------------------------------------------- parsing


def timestamp(value: str) -> datetime:
    """ESPN's "2026-03-01T18:30Z" as an aware datetime"""
    return datetime.fromisoformat(value)


def eastern_date(start: datetime) -> date:
    """a game's date: the US Eastern date it starts, as ESPN lists it"""
    return start.astimezone(EASTERN).date()


def number(value) -> int | None:
    """a box score value: "19", 19, or "--"/"" for none"""
    if value is None:
        return None
    value = str(value).strip()
    if value in ("", "-", "--"):
        return None
    return int(float(value))


def split(value, label: str) -> tuple[int | None, int | None]:
    """a made-attempted pair, "8-12" """
    value = str(value).strip()
    if value in ("", "-", "--", "--/--"):
        return None, None
    made, sep, attempted = value.partition("-")
    if not sep:
        raise ValueError(f"{label}: {value!r} isn't made-attempted")
    return int(made), int(attempted)


def note_of(notes: list[dict] | None) -> str | None:
    """the event a game is part of ("NCAA Tournament - First Round"), if any"""
    for n in notes or []:
        if n.get("headline"):
            return n["headline"]
    return None


def team_info(team: dict, start: datetime) -> dict:
    return {
        "team_id": str(team["id"]),
        "location": team.get("location"),
        "name": team.get("name"),
        "display_name": team.get("displayName"),
        "abbrev": team.get("abbreviation"),
        "conference_id": str(team["conferenceId"])
        if team.get("conferenceId")
        else None,
        "start_time": start,
    }


def scoreboard_games(data: dict, season: int) -> tuple[list[dict], list[dict]]:
    """
    The finished games on a scoreboard, and their teams' names. Raises
    ValueError if it isn't as expected
    """
    events = data.get("events")
    if events is None:
        raise ValueError(f"espn {season} scoreboard: no events list")
    if len(events) >= LIMIT:
        raise ValueError(f"espn {season} scoreboard: {len(events)} games, the limit")
    games, teams = [], []
    for e in events:
        if not e["status"]["type"]["completed"]:
            continue
        season_type = SEASON_TYPES.get(e["season"]["type"])
        if season_type is None:
            continue
        c = e["competitions"][0]
        start = timestamp(c["date"])
        sides = {t["homeAway"]: t for t in c["competitors"]}
        if set(sides) != {"home", "away"}:
            raise ValueError(f"espn {season} game {e['id']}: competitors {list(sides)}")
        games.append(
            {
                "season": season,
                "game_id": str(e["id"]),
                "game_date": eastern_date(start),
                "start_time": start,
                "season_type": season_type,
                "home_team_id": str(sides["home"]["team"]["id"]),
                "away_team_id": str(sides["away"]["team"]["id"]),
                "home_pts": number(sides["home"].get("score")),
                "away_pts": number(sides["away"].get("score")),
                "neutral_site": c.get("neutralSite"),
                "conference_game": c.get("conferenceCompetition"),
                "note": note_of(c.get("notes")),
                "venue": (c.get("venue") or {}).get("fullName"),
                "attendance": c.get("attendance") or None,
            }
        )
        teams += [team_info(t["team"], start) for t in sides.values()]
    return games, teams


def summary_box(
    data: dict, season: int
) -> tuple[list[dict], list[dict], list[dict]] | None:
    """
    A game's team box, player box and players from its summary, or None if
    it has neither. Raises ValueError if it isn't as expected.

    Some old games (mostly before 2008) have only player box scores: their
    teams' statistics are empty, so they have no team box rows. A player
    whose athlete record ESPN has lost (no id or name) can't be keyed, and
    is left out
    """
    header = data["header"]
    game_id = str(header["id"])
    label = f"espn {season} game {game_id}"
    comp = header["competitions"][0]
    box = data.get("boxscore") or {}
    teams, players = box.get("teams") or [], box.get("players") or []
    if not comp.get("boxscoreAvailable", True):
        return None
    if len(teams) != 2 or not all(t.get("statistics") for t in teams):
        teams = []

    start = timestamp(comp["date"])
    game_date = eastern_date(start)
    season_type = SEASON_TYPES.get(header["season"]["type"])
    sides = {str(t["id"]): t for t in comp["competitors"]}
    team_rows = []
    for t in teams:
        team_id = str(t["team"]["id"])
        (opp_id,) = set(sides) - {team_id}
        side, opp = sides[team_id], sides[opp_id]
        row: dict = {
            "season": season,
            "game_id": game_id,
            "team_id": team_id,
            "opp_team_id": opp_id,
            "game_date": game_date,
            "season_type": season_type,
            "home": side["homeAway"] == "home",
            "win": side.get("winner"),
            "pts": number(side.get("score")),
            "opp_pts": number(opp.get("score")),
        } | dict.fromkeys(TEAM_STAT_COLUMNS)
        for s in t["statistics"]:
            cols = TEAM_STATS.get(s["name"])
            if cols is None:
                continue
            value = s.get("displayValue")
            if len(cols) == 2:
                row[cols[0]], row[cols[1]] = split(value, f"{label} {s['name']}")
            else:
                row[cols[0]] = number(value)
        missing = [c for c in REQUIRED_TEAM_STATS if row[c] is None]
        if missing:
            raise ValueError(f"{label} team {team_id}: no {missing}")
        team_rows.append(row)

    player_rows, player_info = [], []
    for p in players:
        team_id = str(p["team"]["id"])
        for group in p.get("statistics") or []:
            labels = group.get("labels") or []
            unknown = set(labels) - set(PLAYER_STATS)
            if unknown:
                raise ValueError(
                    f"{label}: unknown player box labels {sorted(unknown)}"
                )
            for a in group.get("athletes") or []:
                stats = a.get("stats") or []
                if a.get("didNotPlay") or not stats:
                    continue
                if len(stats) != len(labels):
                    raise ValueError(
                        f"{label}: {len(stats)} stats for {len(labels)} labels"
                    )
                athlete = a["athlete"]
                if not athlete.get("id"):
                    continue
                row = {
                    "season": season,
                    "game_id": game_id,
                    "team_id": team_id,
                    "player_id": str(athlete["id"]),
                    "game_date": game_date,
                    "starter": bool(a.get("starter")),
                    "ejected": bool(a.get("ejected")),
                } | dict.fromkeys(PLAYER_STAT_COLUMNS)
                for name, value in zip(labels, stats, strict=True):
                    cols = PLAYER_STATS[name]
                    if len(cols) == 2:
                        row[cols[0]], row[cols[1]] = split(value, f"{label} {name}")
                    else:
                        row[cols[0]] = number(value)
                player_rows.append(row)
                player_info.append(
                    {
                        "game_id": game_id,
                        "player_id": row["player_id"],
                        "team_id": team_id,
                        "name": athlete.get("displayName"),
                        "short_name": athlete.get("shortName"),
                        "position": (athlete.get("position") or {}).get("abbreviation"),
                        "game_date": game_date,
                    }
                )
    if not team_rows and not player_rows:
        return None
    return team_rows, player_rows, player_info


def trim_summary(data: dict) -> dict:
    """the parts of a summary that are kept"""
    return {k: data[k] for k in SUMMARY_KEYS if k in data}


# ---------------------------------------------------------------- fetching


class Fetcher:
    """
    GETs no faster than one per REQUEST_DELAY seconds, asking for gzip. A
    404 is None. A dropped connection, truncated response or server error
    (ESPN's CDN sends the odd 504) is retried once, after RETRY_DELAY
    """

    def __init__(self, delay: float = REQUEST_DELAY, sleep=time.sleep, clock=time.time):
        self.delay = delay
        self.sleep = sleep
        self.clock = clock
        self.last = 0.0
        self.requests = 0

    def get(self, url: str) -> bytes | None:
        try:
            return self.fetch(url)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            print(f"ncaam_espn: HTTP {e.code}; retrying", flush=True)
            self.sleep(RETRY_DELAY)
            return self.fetch(url)
        except (
            urllib.error.URLError,
            http.client.IncompleteRead,
            ConnectionError,
            TimeoutError,
        ) as e:
            print(f"ncaam_espn: {e!r}; retrying", flush=True)
            return self.fetch(url)

    def fetch(self, url: str) -> bytes | None:
        wait = self.last + self.delay - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.requests += 1
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                body = res.read()
                if res.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            body = e.read()[:500].decode("utf-8", "replace")
            print(f"ncaam_espn: HTTP {e.code} for {url}: {body!r}", flush=True)
            raise
        finally:
            self.last = self.clock()

    def json(self, url: str) -> dict | None:
        body = self.get(url)
        return None if body is None else json.loads(body)


class Getter(Protocol):
    def get(self, url: str) -> bytes | None: ...

    def json(self, url: str) -> dict | None: ...


def season_dir(outdir: Path, season: int) -> Path:
    return outdir / RAW_DIR / str(season)


def save_json(path: Path, data: dict) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            json.dump(data, f, separators=(",", ":"))

    write_atomic(path, write)


def load_json(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def season_days(season: int) -> list[date]:
    """the days a season's games are in: November 1 to April 15"""
    start, end = date(season - 1, 11, 1), date(season, 4, 15)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def scoreboard_url(day: date) -> str:
    return f"{API}/scoreboard?dates={day:%Y%m%d}&groups={GROUPS}&limit={LIMIT}"


def summary_url(game_id: str) -> str:
    return f"{API}/summary?event={game_id}"


def fetch_scoreboards(fetcher: Getter, outdir: Path, season: int, today: date) -> int:
    """
    Fetch the season's scoreboards up to yesterday: every day not saved,
    and the last REFETCH_DAYS days again. Returns how many were fetched
    """
    directory = season_dir(outdir, season) / "scoreboard"
    count = 0
    for day in season_days(season):
        if day >= today:
            break
        path = directory / f"{day}.json.gz"
        if path.exists() and (today - day).days > REFETCH_DAYS:
            continue
        data = fetcher.json(scoreboard_url(day))
        if data is None:
            raise ValueError(f"espn {season}: no scoreboard for {day}")
        scoreboard_games(data, season)
        save_json(path, data)
        count += 1
    return count


def summary_path(outdir: Path, season: int, game_id: str) -> Path:
    return season_dir(outdir, season) / "summary" / f"{game_id}.json.gz"


def fetch_summaries(
    fetcher: Getter,
    outdir: Path,
    season: int,
    games: Iterable[tuple[str, date]],
    today: date,
) -> int:
    """
    Fetch the summary of each (game_id, game_date) not saved, and of recent
    games saved without a box score. Returns how many were fetched
    """
    count = 0
    for game_id, day in games:
        path = summary_path(outdir, season, game_id)
        if path.exists():
            if (today - day).days > RECENT_DAYS:
                continue
            box = summary_box(load_json(path), season)
            if box is not None and box[0]:
                continue
        data = fetcher.json(summary_url(game_id))
        if data is None:
            print(f"espn {season}: no summary for game {game_id}")
            continue
        data = trim_summary(data)
        summary_box(data, season)
        save_json(path, data)
        count += 1
    return count


def sdv_path(outdir: Path, season: int, kind: str) -> Path:
    return season_dir(outdir, season) / "sportsdataverse" / f"{kind}.parquet"


def fetch_sdv(fetcher: Getter, outdir: Path, season: int) -> None:
    """download sportsdataverse's files for a season, if they aren't saved"""
    for kind, name in SDV_FILES.items():
        path = sdv_path(outdir, season, kind)
        if path.exists():
            continue
        body = fetcher.get(f"{SDV}/{name.format(season=season)}")
        if body is None:
            raise ValueError(f"sportsdataverse has no {kind} for {season}")
        write_atomic(path, lambda tmp, body=body: tmp.write_bytes(body))
        seasons = duckdb.sql(
            f"SELECT DISTINCT season FROM read_parquet('{path}')"
        ).fetchall()
        if seasons != [(season,)]:
            path.unlink()
            raise ValueError(f"sportsdataverse {kind} {season}: seasons {seasons}")


# ---------------------------------------------------------------- building


@functools.cache
def torvik_names() -> dict[str, str]:
    """ESPN team id -> torvik's name"""
    with TEAMS_CSV.open() as f:
        return {r["espn_team_id"]: r["torvik_team"] for r in csv.DictReader(f)}


def arrow(rows: list[dict], columns: list[tuple[str, str]]) -> pa.Table:
    return pa.table(
        {name: pa.array([r[name] for r in rows], TYPES[t]) for name, t in columns}
    )


def table_sql(columns: list[tuple[str, str]]) -> str:
    return ", ".join(f"{name} {t}" for name, t in columns)


def load_sdv(con: duckdb.DuckDBPyConnection, outdir: Path, season: int) -> None:
    """sportsdataverse's files for a season, in our tables"""
    paths = {k: sdv_path(outdir, season, k) for k in SDV_FILES}
    sched, team, player = (f"read_parquet('{paths[k]}')" for k in SDV_FILES)

    def columns(source: str) -> set[str]:
        return {
            r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
        }

    def pick(source: str, names: dict[str, str]) -> str:
        have = columns(source)
        return ", ".join(
            f"TRY_CAST({src} AS INTEGER) AS {ours}"
            if src in have
            else f"NULL::INTEGER AS {ours}"
            for ours, src in names.items()
        )

    sched_cols = columns(sched)
    neutral = "neutral_site" if season >= 2008 else "NULL::BOOLEAN"
    note = "NULLIF(notes_headline, '')" if "notes_headline" in sched_cols else "NULL"
    season_type = (
        "CASE season_type WHEN 2 THEN 'regular_season' WHEN 3 THEN 'postseason' END"
    )
    con.execute(
        f"""
        INSERT INTO games
        SELECT {season}, game_id::VARCHAR, game_date, game_date_time, {season_type},
            home_id::VARCHAR, away_id::VARCHAR, home_score, away_score,
            {neutral}, conference_competition, {note}, venue_full_name,
            NULLIF(attendance, 0)
        FROM {sched} WHERE status_type_completed
        """
    )
    for side in ("home", "away"):
        con.execute(
            f"""
            INSERT INTO team_info
            SELECT {side}_id::VARCHAR, {side}_location, {side}_name,
                {side}_display_name, {side}_abbreviation,
                {side}_conference_id::VARCHAR, game_date_time
            FROM {sched} WHERE status_type_completed
            """
        )
    con.execute(
        f"""
        INSERT INTO team_box
        SELECT {season}, game_id::VARCHAR, team_id::VARCHAR,
            opponent_team_id::VARCHAR, game_date, {season_type},
            team_home_away = 'home', team_winner, team_score,
            opponent_team_score, {pick(team, SDV_TEAM_STATS)}
        FROM {team}
        """
    )
    con.execute(
        f"""
        INSERT INTO player_box
        SELECT {season}, game_id::VARCHAR, team_id::VARCHAR,
            athlete_id::VARCHAR, game_date, coalesce(starter, false),
            coalesce(ejected, false), {pick(player, SDV_PLAYER_STATS)}
        FROM {player}
        WHERE NOT coalesce(did_not_play, false) AND athlete_id IS NOT NULL
        """
    )
    con.execute(
        f"""
        INSERT INTO player_info
        SELECT game_id::VARCHAR, athlete_id::VARCHAR, team_id::VARCHAR, athlete_display_name,
            athlete_short_name, athlete_position_abbreviation, game_date
        FROM {player}
        WHERE NOT coalesce(did_not_play, false) AND athlete_id IS NOT NULL
        """
    )


def load_espn(con: duckdb.DuckDBPyConnection, outdir: Path, season: int) -> None:
    """a season's saved scoreboards and summaries, in our tables"""
    games, teams = [], []
    for path in sorted((season_dir(outdir, season) / "scoreboard").glob("*.json.gz")):
        g, t = scoreboard_games(load_json(path), season)
        games += g
        teams += t
    # a game is on one day's scoreboard, but be sure: keep its latest
    by_id = {g["game_id"]: g for g in games}
    con.register("new_games", arrow(list(by_id.values()), GAMES))
    con.execute("INSERT INTO games SELECT * FROM new_games")
    con.register("new_teams", arrow(teams, TEAM_INFO))
    con.execute("INSERT INTO team_info SELECT * FROM new_teams")


def load_summaries(con: duckdb.DuckDBPyConnection, outdir: Path, season: int) -> None:
    """
    The saved summaries' box scores, which replace any sportsdataverse has
    for the same games
    """
    team_rows, player_rows, players = [], [], []
    for path in sorted((season_dir(outdir, season) / "summary").glob("*.json.gz")):
        box = summary_box(load_json(path), season)
        if box:
            team_rows += box[0]
            player_rows += box[1]
            players += box[2]
    con.register("new_team_box", arrow(team_rows, TEAM_BOX))
    con.register("new_player_box", arrow(player_rows, PLAYER_BOX))
    con.register("new_players", arrow(players, PLAYER_INFO))
    for table in ("team_box", "player_box"):
        con.execute(
            f"DELETE FROM {table} WHERE game_id IN (SELECT game_id FROM new_{table})"
        )
        con.execute(f"INSERT INTO {table} SELECT * FROM new_{table}")
    con.execute("INSERT INTO player_info SELECT * FROM new_players")


def has_scoreboards(outdir: Path, season: int) -> bool:
    return any((season_dir(outdir, season) / "scoreboard").glob("*.json.gz"))


def load(outdir: Path, season: int) -> duckdb.DuckDBPyConnection:
    """
    A connection with a season's games and box scores from its saved
    files: its scoreboards if it has any, otherwise sportsdataverse's.
    Summaries aren't loaded
    """
    con = duckdb.connect()
    con.execute(f"CREATE TABLE games ({table_sql(GAMES)})")
    con.execute(f"CREATE TABLE team_box ({table_sql(TEAM_BOX)})")
    con.execute(f"CREATE TABLE player_box ({table_sql(PLAYER_BOX)})")
    con.execute(f"CREATE TABLE team_info ({table_sql(TEAM_INFO)})")
    con.execute(f"CREATE TABLE player_info ({table_sql(PLAYER_INFO)})")
    if has_scoreboards(outdir, season):
        load_espn(con, outdir, season)
    elif sdv_path(outdir, season, "schedule").exists():
        load_sdv(con, outdir, season)
    return con


def build_season(outdir: Path, season: int) -> list[Path]:
    """
    Write a season's datasets from its saved files: its scoreboards if it
    has any, otherwise sportsdataverse's, plus its summaries. Returns the
    paths, or [] if there's nothing to build
    """
    con = load(outdir, season)
    load_summaries(con, outdir, season)

    count = con.execute("SELECT count(*) FROM games").fetchone()
    if not count or not count[0]:
        print(f"ncaam_espn: no finished games in {season} yet")
        return []

    # box scores only for finished games ESPN lists that season
    for table in ("team_box", "player_box", "player_info"):
        con.execute(
            f"DELETE FROM {table} WHERE game_id NOT IN (SELECT game_id FROM games)"
        )
    # every team's names as of its last game, and its torvik name
    con.register(
        "torvik",
        pa.table(
            {
                "team_id": pa.array(list(torvik_names()), pa.string()),
                "torvik_team": pa.array(list(torvik_names().values()), pa.string()),
            }
        ),
    )
    con.execute(
        f"""
        CREATE TABLE team_seasons AS
        SELECT {season} AS season, t.team_id,
            arg_max(location, start_time) AS location,
            arg_max(name, start_time) AS name,
            arg_max(display_name, start_time) AS display_name,
            arg_max(abbrev, start_time) AS abbrev,
            arg_max(conference_id, start_time) FILTER (conference_id IS NOT NULL)
                AS conference_id,
            -- ESPN gives only Division I teams a conference; it agrees with
            -- torvik's teams but for a few schools moving up each season
            bool_or(conference_id IS NOT NULL) AS is_d1,
            any_value(torvik.torvik_team) AS torvik_team
        FROM team_info t LEFT JOIN torvik USING (team_id)
        GROUP BY t.team_id
        """
    )
    con.execute(
        f"""
        CREATE TABLE player_seasons AS
        SELECT {season} AS season, player_id,
            arg_max(name, game_date) AS name,
            arg_max(short_name, game_date) AS short_name,
            arg_max(position, game_date) AS position,
            arg_max(team_id, game_date) AS team_id
        FROM player_info GROUP BY player_id
        """
    )
    keys = {
        "games": ["game_id"],
        "team_box": ["game_id", "team_id"],
        "player_box": ["game_id", "player_id"],
        "team_seasons": ["team_id"],
        "player_seasons": ["player_id"],
    }
    orders = {
        "games": "game_date, game_id",
        "team_box": "game_date, game_id, team_id",
        "player_box": "game_date, game_id, team_id, player_id",
        "team_seasons": "team_id",
        "player_seasons": "player_id",
    }
    paths = []
    for dataset, key in keys.items():
        check_keys(con, dataset, key, f"ncaam_espn {season} {dataset}")
        con.execute(
            f"CREATE OR REPLACE TABLE {dataset} AS "
            f"SELECT * FROM {dataset} ORDER BY {orders[dataset]}"
        )
    for dataset in keys:
        path = dataset_path(outdir / OUT_DIR, dataset, season)
        write_parquet(con, dataset, path)
        paths.append(path)
    counts = {
        d: con.execute(f"SELECT count(*) FROM {d}").fetchall()[0][0] for d in keys
    }
    print(f"ncaam_espn: wrote {season}: {counts}")
    return paths


# ---------------------------------------------------------------- running


def missing_boxes(outdir: Path, season: int) -> list[tuple[str, date]]:
    """
    The finished games in a season's saved schedule or scoreboards with no
    team box score in sportsdataverse's files
    """
    con = load(outdir, season)
    rows = con.execute(
        """
        SELECT game_id, game_date FROM games
        WHERE game_id NOT IN (SELECT game_id FROM team_box)
        ORDER BY game_date, game_id
        """
    ).fetchall()
    return [(g, d) for g, d in rows]


def update_season(
    fetcher: Getter, outdir: Path, season: int, current: int, today: date
) -> None:
    """fetch what a season is missing"""
    if season == current or has_scoreboards(outdir, season):
        n = fetch_scoreboards(fetcher, outdir, season, today)
        print(f"ncaam_espn: {season}: fetched {n} scoreboards")
    else:
        fetch_sdv(fetcher, outdir, season)
    n = fetch_summaries(fetcher, outdir, season, missing_boxes(outdir, season), today)
    print(f"ncaam_espn: {season}: fetched {n} summaries")


def saved_seasons(outdir: Path) -> list[int]:
    return sorted(
        int(p.name)
        for p in (outdir / RAW_DIR).glob("[0-9][0-9][0-9][0-9]")
        if p.is_dir()
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ESPN men's college basketball")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help=f"default: the current season. Seasons start in {FIRST_SEASON}",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="only rebuild from saved files (every saved season, without --season)",
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    current = current_season(today)
    default = saved_seasons(args.out) if args.no_fetch else [current]
    fetcher = None if args.no_fetch else Fetcher()
    for season in args.season or default:
        if season < FIRST_SEASON:
            raise SystemExit(f"ncaam_espn: seasons start in {FIRST_SEASON}")
        if fetcher:
            update_season(fetcher, args.out, season, current, today)
        build_season(args.out, season)


if __name__ == "__main__":
    main()
