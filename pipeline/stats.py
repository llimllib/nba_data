"""
Download game logs and season stats from stats.nba.com and build per-season
parquet files.

Each run refetches the whole season, which takes 14 requests (15 with the
schedule, below). That's cheap, and it picks up the stat corrections the
NBA makes after games. Each response is kept, gzipped, at

    nba/raw/stats/<season>/<request>.json.gz

Team rosters add rostered players who haven't played (injured, say) to
player_seasons. They take 30 requests, one per team, and change slowly, so
they're refetched only when the saved ones are more than ROSTER_MAX_AGE
old, all in one file with the time they were fetched:

    nba/raw/stats/<season>/team_rosters.json.gz

That file also keeps the players on earlier rosters who aren't on these
(waived before they played, say), so a player who was on a team stays in
player_seasons, and the tables that refer to them (salaries) stay valid.
They're optional: a season without them has only the players who played.

Each fetch is also kept by the day it was fetched (US Eastern; a second
fetch that day replaces the first), and rosters_daily is built from every
saved day, so CI downloads them first:

    nba/raw/stats/<season>/team_rosters/<date>.json.gz

A team whose roster is too small or too big (an empty or partial response)
is refetched, and if it stays that way the run fails before anything is
saved.

The season's schedule, every game whether it's been played or not, is
refetched every run too (one request), for pipeline.schedule:

    nba/raw/stats/<season>/schedule.json.gz

It's optional as well: a season without it has no schedule.

The standings (one request, every run too) give each team's conference and
division that season, for team_seasons:

    nba/raw/stats/<season>/standings.json.gz

Optional too: without them, team_seasons' conference and division are NULL.

and these are built from them, one file per season:

    nba/stats/team_game_logs/season=<season>/data.parquet  (and team_oreb, from game_events)
    nba/stats/player_game_logs/season=<season>/data.parquet
    nba/stats/player_season_stats/season=<season>/data.parquet
    nba/stats/{team_seasons,player_seasons,games}/season=<season>/data.parquet  (lookups)
    nba/stats/schedule/season=<season>/data.parquet
    nba/stats/rosters_daily/season=<season>/data.parquet

Season stats are totals only; per-game, per-36 and per-100 values are
computed from them in the catalog. Everything is relative to an output
directory laid out like the basketball-data bucket; see docs/v2.md.

team_game_logs.team_oreb is counted from the play-by-play, which
pipeline.rotations fetches after this runs: the season's game_events is
read from --out if it's there, and the bucket otherwise. The update runs
this again with --no-fetch after the rotations, so the night's games have it.

The team game logs, which games is built from, can list a game hours after
the player game logs (and ESPN) do, so a game's player_game_logs rows wait
until it's in them; each run rebuilds the season, so they're added then. A
game that's final in the schedule LATE_GAME after tip-off and still isn't
in them is reported.

usage: python -m pipeline.stats [--out out] [--season 2026 ...] [--no-fetch | --rosters-only] [--url URL]
"""

import argparse
import gzip
import json
import time
import traceback
from collections.abc import Callable, Collection
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
from nba_api.stats.endpoints import (
    CommonTeamRoster,
    LeagueDashPlayerBioStats,
    LeagueDashPlayerPtShot,
    LeagueDashPlayerStats,
    LeagueStandingsV3,
    PlayerGameLogs,
    ScheduleLeagueV2,
    TeamGameLogs,
)

from . import lookups, schedule
from .catalog import URL
from .output import check_keys, dataset_path, report, write_atomic, write_parquet
from .seasons import EASTERN, current_season, season_window, today_eastern

RAW_DIR = Path("nba/raw/stats")
OUT_DIR = Path("nba/stats")

# a slow response usually means we're being rate limited, but the advanced
# player game logs take about 15 seconds to generate
TIMEOUT = 60
RETRY_DELAYS = [1, 2, 5, 10, 20, 30, 60, 60, 60]

SEASON_TYPES = {"regular_season": "Regular Season", "playoffs": "Playoffs"}

# pipeline.rotations' play-by-play, for team_game_logs.team_oreb
EVENTS = "game_events"

ROSTERS = "team_rosters"
# the update runs every 4 hours, so this refetches rosters once a day
ROSTER_MAX_AGE = timedelta(hours=20)
# between roster requests, to go easy on stats.nba.com
ROSTER_DELAY = 1
# Players on a team's roster, two-way contracts included: up to 21 in the
# offseason, and at least 13 during the season. Out of season a roster can
# be smaller, so then it only has to have someone on it
ROSTER_MAX = 21
ROSTER_MIN = 13

ROSTERS_DAILY = "rosters_daily"
ROSTERS_DAILY_KEYS = ["as_of_date", "team_id", "player_id"]

SCHEDULE = "schedule"

STANDINGS = "standings"

# The team game logs, which games is built from, can list a game hours after
# it ends: up to about 4 hours after a late West Coast game. Other datasets
# hold back their rows for it until then; a game that's final in the
# schedule this long after tip-off and still isn't in games is reported
LATE_GAME = timedelta(hours=12)

# Columns we don't keep. Names and abbreviations come from the lookup tables;
# ranks are a query away and would be wrong for per-mode stats
DROP = {
    "season_year",
    "player_name",
    "nickname",
    "team_abbreviation",
    "team_name",
    "player_last_team_abbreviation",
    "available_flag",
    "min_sec",
    # per-game values; season stats are totals
    "fgm_pg",
    "fga_pg",
}

KEYS = {
    "team_game_logs": ["game_id", "team_id"],
    "player_game_logs": ["game_id", "player_id"],
    "player_season_stats": ["season_type", "player_id"],
}


def season_string(season: int) -> str:
    """the NBA's name for a season: 2026 -> "2025-26" """
    return f"{season - 1}-{str(season)[2:]}"


def requests(season: int) -> dict[str, tuple[Callable, dict]]:
    """every request needed for a season: name -> (endpoint, arguments)"""
    s = season_string(season)
    # with no season type, game logs cover every game: preseason through
    # the Cup final, All-Star games and playoffs
    logs = {"league_id_nullable": "00", "season_nullable": s}
    advanced = {"measure_type_player_game_logs_nullable": "Advanced"}
    reqs: dict[str, tuple[Callable, dict]] = {
        "team_game_logs_base": (TeamGameLogs, logs),
        "team_game_logs_advanced": (TeamGameLogs, logs | advanced),
        "player_game_logs_base": (PlayerGameLogs, logs),
        "player_game_logs_advanced": (PlayerGameLogs, logs | advanced),
    }
    for name, season_type in SEASON_TYPES.items():
        dash = {
            "league_id_nullable": "00",
            "season": s,
            "season_type_all_star": season_type,
            "per_mode_detailed": "Totals",
        }
        other = {"league_id": "00", "season": s, "season_type_all_star": season_type}
        for measure in ("Base", "Defense", "Advanced"):
            reqs[f"player_stats_{name}_{measure.lower()}"] = (
                LeagueDashPlayerStats,
                dash | {"measure_type_detailed_defense": measure},
            )
        # 2-point shots, which the other measures fold into field goals
        reqs[f"player_stats_{name}_pt_shot"] = (LeagueDashPlayerPtShot, other)
        # height, weight, draft position, college and country
        reqs[f"player_stats_{name}_bio"] = (LeagueDashPlayerBioStats, other)
    return reqs


def raw_path(outdir: Path, season: int, name: str) -> Path:
    return outdir / RAW_DIR / str(season) / f"{name}.json.gz"


def fetch(
    endpoint: Callable,
    kwargs: dict,
    sleep=time.sleep,
    delays: list[int] = RETRY_DELAYS,
    timeout: int = TIMEOUT,
    check: Callable[[dict], object] | None = None,
) -> dict:
    """
    call an nba_api endpoint, retrying with backoff, and return its JSON.
    `check` raises if a response is bad, which is retried; by default it
    must have a result set
    """
    for attempt, delay in enumerate([*delays, None]):
        try:
            data = endpoint(**kwargs, timeout=timeout).get_dict()
            (check or result_set)(data)
            return data
        except Exception as exc:
            if delay is None:
                raise
            args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
            label = getattr(endpoint, "__name__", repr(endpoint))
            print(
                f"stats: {label}({args}) failed (attempt {attempt + 1}), "
                f"retrying in {delay}s: {exc!r}"
            )
            # a 500 from stats.nba.com has an empty body, which isn't JSON
            if not isinstance(
                exc, (TimeoutError, ConnectionError, OSError, json.JSONDecodeError)
            ):
                print(traceback.format_exc())
            sleep(delay)
    raise AssertionError("unreachable")


def write_raw(path: Path, data: dict) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            json.dump(data, f, separators=(",", ":"))

    write_atomic(path, write)


def read_raw(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def fetch_season(
    outdir: Path, season: int, fetch: Callable[[Callable, dict], dict] = fetch
) -> None:
    for name, (endpoint, kwargs) in requests(season).items():
        write_raw(raw_path(outdir, season, name), fetch(endpoint, kwargs))
        print(f"stats: fetched {name}")


def fetch_schedule(
    outdir: Path, season: int, fetch: Callable[..., dict] = fetch
) -> None:
    data = fetch(
        ScheduleLeagueV2,
        {"league_id": "00", "season": season_string(season)},
        check=schedule.games,
    )
    write_raw(raw_path(outdir, season, SCHEDULE), data)
    print(f"stats: fetched {SCHEDULE}")


def check_standings(data: dict) -> None:
    """raise unless the standings have every NBA team's conference and division"""
    rs = result_set(data)
    missing = {"TeamID", "Conference", "Division"} - set(rs["headers"])
    if missing:
        raise ValueError(f"the standings have no {missing}")
    i, c, d = (rs["headers"].index(h) for h in ("TeamID", "Conference", "Division"))
    teams = {row[i] for row in rs["rowSet"] if row[c] and row[d]}
    if teams != set(lookups.NBA_TEAM_IDS):
        raise ValueError(
            f"the standings have {len(teams)} teams with a conference and "
            f"division, not the {len(lookups.NBA_TEAM_IDS)} in NBA_TEAM_IDS"
        )


def fetch_standings(
    outdir: Path, season: int, fetch: Callable[..., dict] = fetch
) -> None:
    data = fetch(
        LeagueStandingsV3,
        {
            "league_id": "00",
            "season": season_string(season),
            "season_type": "Regular Season",
        },
        check=check_standings,
    )
    write_raw(raw_path(outdir, season, STANDINGS), data)
    print(f"stats: fetched {STANDINGS}")


def load_standings(con: duckdb.DuckDBPyConnection, outdir: Path, season: int) -> None:
    """
    Load `standings` (team_id, conference, division) from the saved
    standings, empty if there are none
    """
    path = raw_path(outdir, season, STANDINGS)
    if path.is_file():
        check_standings(read_raw(path))
        load_raw(con, path, "standings_raw")
        query = (
            "SELECT teamid::VARCHAR AS team_id, conference::VARCHAR AS conference, "
            "division::VARCHAR AS division FROM standings_raw"
        )
    else:
        print(f"stats: no {STANDINGS} for {season}; no conferences or divisions")
        query = (
            "SELECT NULL::VARCHAR AS team_id, NULL::VARCHAR AS conference, "
            "NULL::VARCHAR AS division WHERE false"
        )
    con.execute(f"CREATE OR REPLACE TABLE standings AS {query}")


def daily_rosters_path(outdir: Path, season: int, day: date) -> Path:
    return outdir / RAW_DIR / str(season) / ROSTERS / f"{day.isoformat()}.json.gz"


def saved_roster_days(outdir: Path, season: int) -> list[tuple[date, Path]]:
    """(day, path) for every saved day's rosters, oldest first"""
    paths = (outdir / RAW_DIR / str(season) / ROSTERS).glob("*.json.gz")
    return sorted(
        (date.fromisoformat(p.name.removesuffix(".json.gz")), p) for p in paths
    )


def check_roster(data: dict, minimum: int) -> None:
    """raise if a team's roster response doesn't have minimum to ROSTER_MAX players"""
    players = len(result_set(data)["rowSet"])
    if not minimum <= players <= ROSTER_MAX:
        raise ValueError(
            f"a roster has {players} players, not {minimum} to {ROSTER_MAX}"
        )


def rosters_fetched(outdir: Path, season: int) -> datetime | None:
    """when the saved rosters were fetched, or None if there are none"""
    path = raw_path(outdir, season, ROSTERS)
    if not path.is_file():
        return None
    return datetime.fromisoformat(read_raw(path)["fetched"])


def fetch_rosters(
    outdir: Path,
    season: int,
    fetch: Callable[..., dict] = fetch,
    sleep=time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    force: bool = False,
) -> None:
    """fetch every team's roster, unless the saved ones are recent enough"""
    fetched = rosters_fetched(outdir, season)
    if fetched and not force and now() - fetched < ROSTER_MAX_AGE:
        print(f"stats: rosters for {season} are from {fetched.isoformat()}")
        return
    # players on earlier rosters but not on these (waived, say), kept so a
    # player stays in player_seasons once they've been on a team that season
    former = dict(saved_roster_players(outdir, season))
    fetched = now()
    day = fetched.astimezone(EASTERN).date()
    start, end = season_window(season)
    minimum = ROSTER_MIN if start <= day <= end else 1
    rosters = {}
    for i, team_id in enumerate(lookups.NBA_TEAM_IDS):
        if i:
            sleep(ROSTER_DELAY)
        rosters[str(team_id)] = fetch(
            CommonTeamRoster,
            {
                "team_id": str(team_id),
                "season": season_string(season),
                "league_id_nullable": "00",
            },
            check=lambda data: check_roster(data, minimum),
        )
    for pid, _ in roster_players(rosters):
        former.pop(pid, None)
    write_raw(
        daily_rosters_path(outdir, season, day),
        {"fetched": fetched.isoformat(), "rosters": rosters},
    )
    write_raw(
        raw_path(outdir, season, ROSTERS),
        {
            "fetched": fetched.isoformat(),
            "rosters": rosters,
            "former": sorted(former.items()),
        },
    )
    print(f"stats: fetched {ROSTERS}")


def roster_players(rosters: dict) -> list[tuple[str, str]]:
    """(player_id, name) on every team's roster, from team_id -> response"""
    players = []
    for team_id, data in rosters.items():
        rs = result_set(data)
        missing = {"PLAYER_ID", "PLAYER"} - set(rs["headers"])
        if missing:
            raise ValueError(f"stats: team {team_id}'s roster has no {missing}")
        pid, name = rs["headers"].index("PLAYER_ID"), rs["headers"].index("PLAYER")
        players += [(str(row[pid]), row[name]) for row in rs["rowSet"]]
    return players


def saved_roster_players(outdir: Path, season: int) -> list[tuple[str, str]]:
    """
    (player_id, name) for everyone on the saved rosters or on earlier ones,
    empty if there are none
    """
    path = raw_path(outdir, season, ROSTERS)
    if not path.is_file():
        return []
    saved = read_raw(path)
    # files from before "former" was kept don't have it
    former = [(str(pid), name) for pid, name in saved.get("former", [])]
    return roster_players(saved["rosters"]) + former


def load_rosters(con: duckdb.DuckDBPyConnection, outdir: Path, season: int) -> None:
    """
    Load everyone on the saved rosters, now or earlier in the season, into
    `rosters` (player_id, player_name), empty if there are none. That
    includes every saved day's, so rosters_daily's players are all in
    player_seasons
    """
    players = saved_roster_players(outdir, season)
    for _, path in saved_roster_days(outdir, season):
        players += roster_players(read_raw(path)["rosters"])
    if not raw_path(outdir, season, ROSTERS).is_file():
        print(
            f"stats: no rosters for {season}; player_seasons has only players who played"
        )
    con.register(
        "arrow",
        pa.table(
            {
                "player_id": pa.array([p[0] for p in players], pa.string()),
                "player_name": pa.array([p[1] for p in players], pa.string()),
            }
        ),
    )
    con.execute("CREATE OR REPLACE TABLE rosters AS SELECT * FROM arrow")
    con.unregister("arrow")


def result_set(data: dict) -> dict:
    """
    The rows of a response. Responses have one result set, sometimes as a
    list and sometimes not
    """
    rs = data["resultSets"]
    rs = rs[0] if isinstance(rs, list) else rs
    if not isinstance(rs.get("headers"), list) or not isinstance(
        rs.get("rowSet"), list
    ):
        raise TypeError(f"unexpected response shape: {str(data)[:500]}")
    return rs


def load_raw(con: duckdb.DuckDBPyConnection, path: Path, table: str) -> None:
    """load a raw response into `table`, with lowercase column names"""
    with gzip.open(path, "rt") as f:
        rs = result_set(json.load(f))
    columns = {}
    for i, h in enumerate(rs["headers"]):
        values = pa.array([row[i] for row in rs["rowSet"]])
        # with no values to infer a type from, load as text; normalized()
        # types the column by name
        if pa.types.is_null(values.type):
            values = values.cast(pa.string())
        columns[h.lower()] = values
    con.register("arrow", pa.table(columns))
    con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM arrow")
    con.unregister("arrow")


# Counting stats are INTEGER. The NBA sometimes sends them as floats (34.0),
# so types are set by name rather than inferred, which also keeps them the
# same from season to season. Other numbers are DOUBLE
COUNTS = {
    # games and possessions
    "gp", "g", "w", "l", "dd2", "td3", "team_count", "poss",
    # box score
    "pts", "plus_minus", "fgm", "fga", "fg2m", "fg2a", "fg3m", "fg3a", "ftm",
    "fta", "oreb", "dreb", "reb", "ast", "tov", "stl", "blk", "blka", "pf", "pfd",
    # defense
    "opp_pts_off_tov", "opp_pts_2nd_chance", "opp_pts_fb", "opp_pts_paint",
    # bio
    "player_height_inches", "player_weight", "draft_year", "draft_round",
    "draft_number",
}  # fmt: skip
STRINGS = {"player_height", "country", "college"}


def normalized(
    con: duckdb.DuckDBPyConnection,
    table: str,
    skip: Collection[str] = (),
    alias: str | None = None,
) -> list[str]:
    """
    Select expressions for `table` that apply the schema conventions: ids are
    strings, counting stats are INTEGER, other numbers are DOUBLE, and dropped
    columns are left out. Columns in `skip` are left for the caller to
    handle. Pass `alias` if the query refers to `table` by an alias
    """
    prefix = f"{alias}." if alias else ""
    exprs = []
    for name, type_ in con.execute(
        f"SELECT column_name, column_type FROM (DESCRIBE {table})"
    ).fetchall():
        if name in DROP or name in skip or name.endswith("_rank"):
            continue
        col = prefix + name
        if name.endswith("_id") or name in STRINGS:
            exprs.append(f"{col}::VARCHAR AS {name}")
        elif name in COUNTS:
            # bio fields are text, and use "Undrafted" for no value
            if type_ == "VARCHAR":
                exprs.append(f"TRY_CAST({col} AS INTEGER) AS {name}")
            else:
                exprs.append(
                    f"CASE WHEN {col} = round({col}) THEN {col}::INTEGER "
                    f"ELSE error('{table}.{name} is not a whole number: ' || {col}) "
                    f"END AS {name}"
                )
        else:
            # a VARCHAR here means the column had no values (see load_raw);
            # a real text column fails the cast and belongs in STRINGS
            exprs.append(f"{col}::DOUBLE AS {name}")
    return exprs


def count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


def join_raw(
    con: duckdb.DuckDBPyConnection, tables: list[str], keys: list[str], out: str
) -> None:
    """
    Left join `tables` on `keys` into `out`. A column that appears in more
    than one table is taken from the first
    """
    seen = set(keys)
    selects = [f"t0.{k}" for k in keys]
    joins = [f"{tables[0]} t0"]
    for i, table in enumerate(tables):
        cols = [
            c[0]
            for c in con.execute(
                f"SELECT column_name FROM (DESCRIBE {table})"
            ).fetchall()
        ]
        selects += [f"t{i}.{c}" for c in cols if c not in seen]
        seen.update(cols)
        if i:
            joins.append(f"LEFT JOIN {table} t{i} USING ({', '.join(keys)})")
    con.execute(
        f"CREATE OR REPLACE TABLE {out} AS SELECT {', '.join(selects)} FROM {' '.join(joins)}"
    )


def load_team_orebs(
    con: duckdb.DuckDBPyConnection, outdir: Path, season: int, url: str | None = None
) -> None:
    """
    Load `team_orebs` (game_id, team_id, team_oreb), every team in the
    season's game_events, from `outdir` if it has the file, else from the
    bucket at `url` (if given). Empty if there are none.

    A team offensive rebound is a rebound credited to the team rather than a
    player (a miss that went out of bounds off the defense, or a shot-clock
    violation) after the team's own miss, where the team's next shot, free
    throw or turnover is in the same period. The box score's oreb has only
    players' rebounds, and one as a period ends doesn't continue anything.
    The rebound after a free throw with more to come in the trip is a dead
    ball, not a rebound
    """
    local = dataset_path(outdir / OUT_DIR, EVENTS, season)
    if local.is_file():
        source = str(local)
    elif url:
        source = f"{url}/{OUT_DIR}/{EVENTS}/season={season}/data.parquet"
    else:
        source = None
    empty = "SELECT NULL::VARCHAR AS game_id, NULL::VARCHAR AS team_id, NULL::INTEGER AS team_oreb WHERE false"
    query = f"""
        WITH e AS (
            SELECT game_id, period, action_id, team_id, person_id, action_type,
                action_type = 'Missed Shot'
                    OR (action_type = 'Free Throw' AND shot_result = 'Missed') AS miss,
                action_type = 'Missed Shot' OR (
                    action_type = 'Free Throw' AND shot_result = 'Missed'
                    AND sub_type NOT LIKE '%Technical%'
                    AND regexp_extract(sub_type, '([0-9]) of ([0-9])', 1)
                        = regexp_extract(sub_type, '([0-9]) of ([0-9])', 2)) AS live_miss,
                action_type IN ('Made Shot', 'Missed Shot', 'Turnover')
                    OR (action_type = 'Free Throw'
                        AND sub_type NOT LIKE '%Technical%') AS plays
            FROM read_parquet('{source}', hive_partitioning = false)
        ), w AS (
            SELECT *,
                last_value(CASE WHEN miss THEN team_id END IGNORE NULLS) OVER (
                    PARTITION BY game_id ORDER BY action_id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS miss_team,
                last_value(CASE WHEN miss THEN live_miss END IGNORE NULLS) OVER (
                    PARTITION BY game_id ORDER BY action_id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS miss_live,
                first_value(CASE WHEN plays THEN team_id END IGNORE NULLS) OVER (
                    PARTITION BY game_id, period ORDER BY action_id
                    ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS next_team
            FROM e
        )
        SELECT game_id, team_id,
            count(*) FILTER (
                WHERE action_type = 'Rebound' AND person_id IS NULL
                AND team_id = miss_team AND miss_live AND team_id = next_team
            )::INTEGER AS team_oreb
        FROM w
        WHERE team_id IS NOT NULL
        GROUP BY ALL
    """
    try:
        con.execute(
            f"CREATE OR REPLACE TABLE team_orebs AS {query if source else empty}"
        )
    except duckdb.IOException as e:
        # the bucket's file is optional: it answers 403, not 404, for a
        # file that isn't there (a new season), and a bucket that can't be
        # read shouldn't stop the stats
        if local.is_file():
            raise
        print(f"stats: couldn't read the bucket's {EVENTS}: {str(e).splitlines()[0]}")
        con.execute(f"CREATE OR REPLACE TABLE team_orebs AS {empty}")
    if not count(con, "team_orebs"):
        print(f"stats: no {EVENTS} for {season}; team_oreb is NULL")


def build_team_game_logs(con: duckdb.DuckDBPyConnection, season: int) -> None:
    join_raw(con, ["tgl_base", "tgl_advanced"], ["game_id", "team_id"], "tgl")
    rest = normalized(
        con, "tgl", {"game_id", "team_id", "game_date", "matchup", "wl"}, alias="t"
    )
    # A team's plus-minus is its margin, but the NBA's is occasionally off in
    # preseason games (4.4 in a game won by 4), so compute it
    rest = [
        "(t.pts - (SELECT any_value(o.pts) FROM tgl o "
        "WHERE o.game_id = t.game_id AND o.team_id <> t.team_id))::INTEGER AS plus_minus"
        if e.endswith(" AS plus_minus")
        else e
        for e in rest
    ]
    # matchup is "BOS vs. NYK" for the home team and "NYK @ BOS" for the away
    # team. Neutral-site games (the Cup final) can list both teams as away,
    # so home is NULL unless exactly one team in the game is home
    con.execute(
        f"""
        CREATE OR REPLACE TABLE team_game_logs AS
        SELECT {season}::INTEGER AS season, t.game_id, t.team_id::VARCHAR AS team_id,
            (SELECT any_value(o.team_id)::VARCHAR FROM tgl o
             WHERE o.game_id = t.game_id AND o.team_id <> t.team_id) AS opp_team_id,
            t.game_date::DATE AS game_date,
            CASE WHEN count(*) FILTER (WHERE t.matchup LIKE '% vs. %')
                      OVER (PARTITION BY t.game_id) = 1
                 THEN t.matchup LIKE '% vs. %' END AS home,
            CASE t.wl WHEN 'W' THEN true WHEN 'L' THEN false END AS win,
            {", ".join(rest)},
            r.team_oreb
        FROM tgl t
        LEFT JOIN team_orebs r
            ON r.game_id = t.game_id AND r.team_id = t.team_id::VARCHAR
        ORDER BY game_date, game_id, team_id
        """
    )


def build_player_game_logs(con: duckdb.DuckDBPyConnection, season: int) -> None:
    keys = ["game_id", "player_id", "team_id"]
    join_raw(con, ["pgl_base", "pgl_advanced"], keys, "pgl")
    rest = normalized(
        con,
        "pgl",
        {"game_id", "player_id", "team_id", "game_date", "matchup", "wl"},
        alias="p",
    )
    # International teams in preseason exhibitions sometimes have players the
    # NBA never gave an id (Olimpia Milano, 2010). With no id there's nothing
    # to key on, so skip them; anywhere else, a missing id fails the build
    unidentified = count(con, "pgl WHERE player_id IS NULL AND game_id LIKE '001%'")
    if unidentified:
        print(f"stats: skipping {unidentified} preseason player rows with no player_id")
    # The NBA sometimes lists a player on both teams in a game, the extra row
    # with no minutes (Devin Booker on OKC in 0022600124, with a free throw
    # that was probably a Thunder player's). Keep the team with the most
    # minutes; a duplicate row for the same team still fails the key check
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE pgl_other_team AS
        SELECT game_id, player_id, team_id, minutes FROM (
            SELECT game_id, player_id, team_id, max(min) AS minutes
            FROM pgl WHERE player_id IS NOT NULL GROUP BY ALL)
        QUALIFY row_number() OVER (
            PARTITION BY game_id, player_id ORDER BY minutes DESC NULLS LAST) > 1
        """
    )
    extra = con.execute("SELECT * FROM pgl_other_team").fetchall()
    for game_id, player_id, team_id, minutes in extra:
        print(
            f"stats: dropping player {player_id}'s row for team {team_id} in "
            f"{game_id} ({minutes} min), they have another row in that game"
        )
    # The player game logs can have a game hours before the team game logs,
    # which games is built from (#105), so its rows wait for the team's; the
    # next run rebuilds the season and adds them
    waiting = con.execute(
        "SELECT DISTINCT game_id FROM pgl "
        "WHERE game_id NOT IN (SELECT game_id FROM team_game_logs) ORDER BY ALL"
    ).fetchall()
    if waiting:
        print(
            f"stats: holding back player_game_logs for {[g for (g,) in waiting]}, "
            "not in the team game logs yet"
        )
    # home and opponent come from the team's game log
    con.execute(
        f"""
        CREATE OR REPLACE TABLE player_game_logs AS
        SELECT {season}::INTEGER AS season, p.game_id, p.player_id::VARCHAR AS player_id,
            p.team_id::VARCHAR AS team_id, t.opp_team_id, p.game_date::DATE AS game_date,
            t.home, CASE p.wl WHEN 'W' THEN true WHEN 'L' THEN false END AS win,
            {", ".join(rest)}
        FROM pgl p
        LEFT JOIN team_game_logs t
            ON t.game_id = p.game_id AND t.team_id = p.team_id::VARCHAR
        ANTI JOIN pgl_other_team x
            ON x.game_id = p.game_id AND x.player_id = p.player_id
            AND x.team_id = p.team_id
        WHERE NOT (p.player_id IS NULL AND p.game_id LIKE '001%')
            AND p.game_id IN (SELECT game_id FROM team_game_logs)
        ORDER BY game_date, game_id, team_id, player_id
        """
    )


def build_player_season_stats(con: duckdb.DuckDBPyConnection, season: int) -> None:
    parts = []
    for name in SEASON_TYPES:
        tables = [
            f"ps_{name}_{m}" for m in ("base", "defense", "advanced", "pt_shot", "bio")
        ]
        if not count(con, tables[0]):
            continue
        joined = f"ps_{name}"
        join_raw(con, tables, ["player_id"], joined)
        rest = normalized(con, joined, {"player_id", "college"})
        parts.append(
            f"""
            SELECT {season}::INTEGER AS season, '{name}' AS season_type,
                player_id::VARCHAR AS player_id,
                {", ".join(rest)},
                NULLIF(college::VARCHAR, 'None') AS college
            FROM {joined}
            """
        )
    query = (
        " UNION ALL BY NAME ".join(parts)
        if parts
        else "SELECT NULL::INTEGER AS season, NULL AS season_type, NULL AS player_id WHERE false"
    )
    con.execute(
        f"CREATE OR REPLACE TABLE player_season_stats AS {query} ORDER BY season_type, player_id"
    )


def build_rosters_daily(
    con: duckdb.DuckDBPyConnection, outdir: Path, season: int
) -> None:
    """
    Load `rosters_daily`, every team's roster on each saved day, from the
    daily roster files. SUPPLEMENTAL_STATUS is 1 for about three players a
    team, nearly all with no salary in bbref_salaries, or one paid by a team
    that waived them: two-way contracts. Any other value fails the build,
    since it would mean something else
    """
    columns: dict[str, list] = {
        "as_of_date": [],
        "team_id": [],
        "player_id": [],
        "num": [],
        "position": [],
        "two_way": [],
        "how_acquired": [],
    }
    for day, path in saved_roster_days(outdir, season):
        for team_id, data in read_raw(path)["rosters"].items():
            rs = result_set(data)
            for values in rs["rowSet"]:
                row = dict(zip(rs["headers"], values))
                status = row.get("SUPPLEMENTAL_STATUS")
                if status not in (0, 1, None):
                    raise ValueError(
                        f"stats: {path}: player {row['PLAYER_ID']} has "
                        f"SUPPLEMENTAL_STATUS {status!r}, not 0 or 1"
                    )
                columns["as_of_date"].append(day)
                columns["team_id"].append(team_id)
                columns["player_id"].append(str(row["PLAYER_ID"]))
                columns["num"].append(row.get("NUM") or None)
                columns["position"].append(row.get("POSITION") or None)
                columns["two_way"].append(None if status is None else status == 1)
                columns["how_acquired"].append(row.get("HOW_ACQUIRED") or None)
    types = {"as_of_date": pa.date32(), "two_way": pa.bool_()}
    con.register(
        "arrow",
        pa.table(
            {
                name: pa.array(values, types.get(name, pa.string()))
                for name, values in columns.items()
            }
        ),
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {ROSTERS_DAILY} AS
        SELECT {season}::INTEGER AS season, * FROM arrow
        ORDER BY {", ".join(ROSTERS_DAILY_KEYS)}
        """
    )
    con.unregister("arrow")


def report_late_games(con: duckdb.DuckDBPyConnection, now: datetime) -> None:
    """report games final in the schedule more than LATE_GAME ago, not in games"""
    late = con.execute(
        f"""
        SELECT game_id FROM {SCHEDULE}
        WHERE status = 'final' AND start_time < ?
            AND game_id NOT IN (SELECT game_id FROM games)
        ORDER BY ALL
        """,
        [now - LATE_GAME],
    ).fetchall()
    if late:
        report(
            f"stats: {[g for (g,) in late]} final in the schedule but not in the "
            f"team game logs {LATE_GAME.total_seconds() / 3600:g} hours after "
            "tip-off; their rows are held back"
        )


def build_season(
    outdir: Path, season: int, url: str | None = None, now: datetime | None = None
) -> bool:
    """
    Build the parquet files for `season` from its raw responses. Returns
    False if there are none. A dataset with no rows (no games played yet) is
    not written. game_events, for team_oreb, is read from the bucket at
    `url` if `outdir` doesn't have it. With `now`, when the responses were
    fetched, games missing from the team game logs long after they ended
    are reported
    """
    names = requests(season)
    if not all(raw_path(outdir, season, n).is_file() for n in names):
        # other raw files in the directory (game_rotations) don't count
        if any(raw_path(outdir, season, n).is_file() for n in names):
            missing = [n for n in names if not raw_path(outdir, season, n).is_file()]
            raise FileNotFoundError(
                f"stats: missing raw responses for {season}: {missing}"
            )
        return False

    con = duckdb.connect()
    for name in names:
        table = (
            name.replace("team_game_logs_", "tgl_")
            .replace("player_game_logs_", "pgl_")
            .replace("player_stats_", "ps_")
        )
        load_raw(con, raw_path(outdir, season, name), table)
    load_rosters(con, outdir, season)
    load_standings(con, outdir, season)
    load_team_orebs(con, outdir, season, url)

    # the lookups read team_game_logs, so they're built after it
    builders = {
        "team_game_logs": build_team_game_logs,
        "player_game_logs": build_player_game_logs,
        "player_season_stats": build_player_season_stats,
        **lookups.BUILDERS,
    }
    for dataset, build in builders.items():
        build(con, season)
        keys = KEYS.get(dataset) or lookups.KEYS[dataset]
        check_keys(con, dataset, keys, f"stats {dataset}")
        if not count(con, dataset):
            print(f"stats: no {dataset} for {season} yet")
            continue
        path = dataset_path(outdir / OUT_DIR, dataset, season)
        write_parquet(con, dataset, path)
        print(f"stats: wrote {path}")

    if raw_path(outdir, season, SCHEDULE).is_file():
        schedule.build_schedule(
            con, season, read_raw(raw_path(outdir, season, SCHEDULE))
        )
        check_keys(con, SCHEDULE, schedule.KEYS, f"stats {SCHEDULE}")
        path = dataset_path(outdir / OUT_DIR, SCHEDULE, season)
        write_parquet(con, SCHEDULE, path)
        print(f"stats: wrote {path}")

    build_rosters_daily(con, outdir, season)
    check_keys(con, ROSTERS_DAILY, ROSTERS_DAILY_KEYS, f"stats {ROSTERS_DAILY}")
    # before a team's first game it isn't in team_seasons, so its roster
    # would fail the integrity check; the saved days fill in once it has one
    missing = con.execute(
        f"SELECT DISTINCT team_id FROM {ROSTERS_DAILY} "
        "WHERE team_id NOT IN (SELECT team_id FROM team_seasons) ORDER BY ALL"
    ).fetchall()
    if not count(con, ROSTERS_DAILY):
        print(f"stats: no {ROSTERS_DAILY} for {season}")
    elif missing:
        print(
            f"stats: not writing {ROSTERS_DAILY} for {season}: "
            f"{len(missing)} teams haven't played yet"
        )
    else:
        path = dataset_path(outdir / OUT_DIR, ROSTERS_DAILY, season)
        write_parquet(con, ROSTERS_DAILY, path)
        print(f"stats: wrote {path}")

    # last, since it raises outside the workflows
    if now and raw_path(outdir, season, SCHEDULE).is_file():
        report_late_games(con, now)
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help="season(s) to update, as the end year (default: current season)",
    )
    fetching = parser.add_mutually_exclusive_group()
    fetching.add_argument(
        "--no-fetch", action="store_true", help="only rebuild parquet from raw files"
    )
    fetching.add_argument(
        "--rosters-only",
        action="store_true",
        help="fetch only the rosters, then rebuild from raw files (to backfill them)",
    )
    parser.add_argument(
        "--url",
        default=URL,
        help=f"bucket URL for game_events if --out has none (default: {URL})",
    )
    args = parser.parse_args(argv)

    for season in args.season or [current_season(today_eastern())]:
        fetched = None
        if args.rosters_only:
            fetch_rosters(args.out, season, force=True)
        elif not args.no_fetch:
            fetch_season(args.out, season)
            fetch_schedule(args.out, season)
            fetch_standings(args.out, season)
            fetch_rosters(args.out, season)
            fetched = datetime.now(UTC)
        if not build_season(args.out, season, args.url, fetched):
            raise SystemExit(f"stats: no raw data for season {season}")


if __name__ == "__main__":
    main()
