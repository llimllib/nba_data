"""
The NBA's schedule for a season, every game whether it's been played or
not, from stats.nba.com's scheduleleaguev2:

    schedule    game_id -> date, tip-off, teams, status and labels

pipeline.stats fetches it every run (one request) and builds it with the
rest of the season. Unlike `games`, it lists games before they're played,
and a neutral site's game keeps the home and away teams the NBA lists for
it. Its game ids match `games`' once a game is played (checked for
2009-10, 2019-20, 2020-21 and 2025-26), and the Cup's knockout games are
listed with their ids before their teams are known.
"""

from datetime import datetime

import duckdb
import pyarrow as pa

from .lookups import GAME_TYPES, NBA_TEAM_IDS

KEYS = ["game_id"]

# gameStatus
STATUSES = {1: "scheduled", 2: "in_progress", 3: "final"}

# game types only NBA teams play in. Preseason games can have international
# teams, and All-Star games their own
NBA_ONLY = {"regular_season", "playoffs", "play_in", "cup_final"}


def all_games(data: dict) -> list[dict]:
    """every game in a scheduleleaguev2 response, raising if it isn't one"""
    days = data["leagueSchedule"]["gameDates"]
    if not isinstance(days, list):
        raise TypeError(f"unexpected schedule shape: {str(data)[:500]}")
    return [g for day in days for g in day["games"]]


def skipped(g: dict) -> bool:
    """
    A game the schedule knows only the id and time of, which the game logs
    don't have either: preseason games before 2010 have status 0 and no
    teams (they're only in the gameCode, "19961010/MINMIL"), and 2004-2006
    have Team USA's Olympic and World Championship games, game ids 009...,
    with no teams
    """
    if not g["homeTeam"]["teamId"] and not g["awayTeam"]["teamId"]:
        return g["gameStatus"] == 0 or g["gameId"].startswith("009")
    return False


def games(data: dict) -> list[dict]:
    """the games in a scheduleleaguev2 response, but for skipped() ones"""
    return [g for g in all_games(data) if not skipped(g)]


def text(value: str | None) -> str | None:
    """the schedule's empty strings are NULL"""
    return value or None


def team(t: dict) -> str | None:
    """a team's id; a game whose teams aren't known yet has 0"""
    return str(t["teamId"]) if t["teamId"] else None


def row(g: dict) -> dict:
    status = STATUSES.get(g["gameStatus"])
    if status is None:
        raise ValueError(
            f"schedule: {g['gameId']} has unknown status {g['gameStatus']}"
        )
    return {
        "game_id": g["gameId"],
        # the date is US Eastern, as in the game logs
        "game_date": datetime.fromisoformat(g["gameDateEst"]).date(),
        "start_time": datetime.fromisoformat(g["gameDateTimeUTC"]),
        "home_team_id": team(g["homeTeam"]),
        "away_team_id": team(g["awayTeam"]),
        "neutral_site": g["isNeutral"],
        "status": status,
        "status_text": text(g["gameStatusText"]),
        "label": text(g["gameLabel"]),
        "sub_label": text(g["gameSubLabel"]),
        "subtype": text(g["gameSubtype"]),
        "series_text": text(g["seriesText"]),
        "if_necessary": g["ifNecessary"] == "true",
        "arena_name": text(g["arenaName"]),
        "arena_city": text(g["arenaCity"]),
        "arena_state": text(g["arenaState"]),
    }


SCHEMA = pa.schema(
    [
        ("game_id", pa.string()),
        ("game_date", pa.date32()),
        ("start_time", pa.timestamp("s", tz="UTC")),
        ("home_team_id", pa.string()),
        ("away_team_id", pa.string()),
        ("neutral_site", pa.bool_()),
        ("status", pa.string()),
        ("status_text", pa.string()),
        ("label", pa.string()),
        ("sub_label", pa.string()),
        ("subtype", pa.string()),
        ("series_text", pa.string()),
        ("if_necessary", pa.bool_()),
        ("arena_name", pa.string()),
        ("arena_city", pa.string()),
        ("arena_state", pa.string()),
    ]
)


def build_schedule(con: duckdb.DuckDBPyConnection, season: int, data: dict) -> None:
    """build `schedule` from a scheduleleaguev2 response"""
    n = sum(skipped(g) for g in all_games(data))
    if n:
        print(f"schedule: skipping {n} games with no teams")
    con.register("arrow", pa.Table.from_pylist([row(g) for g in games(data)], SCHEMA))
    types = ", ".join(f"'{k}': '{v}'" for k, v in GAME_TYPES.items())
    bracket = "left(game_id, 3) IN ('004', '005')"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE schedule AS
        SELECT {season}::INTEGER AS season, game_id, game_date, start_time,
            MAP {{{types}}}[left(game_id, 3)] AS game_type,
            CASE WHEN {bracket} THEN substr(game_id, 8, 1)::INTEGER END AS playoff_round,
            CASE WHEN {bracket} THEN substr(game_id, 9, 1)::INTEGER END AS series_number,
            CASE WHEN {bracket} THEN substr(game_id, 10, 1)::INTEGER END AS series_game,
            * EXCLUDE (game_id, game_date, start_time)
        FROM arrow
        ORDER BY start_time, game_id
        """
    )
    con.unregister("arrow")

    unknown = con.execute(
        "SELECT DISTINCT left(game_id, 3) FROM schedule WHERE game_type IS NULL"
    ).fetchall()
    if unknown:
        raise ValueError(
            f"schedule: unknown game id prefixes: {sorted(u[0] for u in unknown)}"
        )
    # integrity checks games' ids against their season, but the schedule's
    # ids aren't checked against games, since most aren't played yet
    wrong_season = con.execute(
        f"""
        SELECT game_id FROM schedule
        WHERE substr(game_id, 4, 2) <> lpad('{(season - 1) % 100}', 2, '0')
        LIMIT 5
        """
    ).fetchall()
    if wrong_season:
        raise ValueError(
            f"schedule: game ids not in {season}: {[g[0] for g in wrong_season]}"
        )
    # nor its teams against team_seasons, which has only teams that have played
    nba = ", ".join(f"'{t}'" for t in NBA_TEAM_IDS)
    outside = con.execute(
        f"""
        SELECT game_id, home_team_id, away_team_id FROM schedule
        WHERE game_type IN ({", ".join(f"'{t}'" for t in sorted(NBA_ONLY))})
            AND (home_team_id NOT IN ({nba}) OR away_team_id NOT IN ({nba}))
        LIMIT 5
        """
    ).fetchall()
    if outside:
        raise ValueError(f"schedule: games with teams outside NBA_TEAM_IDS: {outside}")
