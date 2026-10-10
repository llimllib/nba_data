"""
Download game logs and season stats from stats.nba.com and build per-season
parquet files.

Each run refetches the whole season, which takes 14 requests. That's cheap,
and it picks up the stat corrections the NBA makes after games. Each
response is kept, gzipped, at

    nba/raw/stats/<season>/<request>.json.gz

and these are built from them, one file per season:

    nba/stats/team_game_logs/<season>.parquet
    nba/stats/player_game_logs/<season>.parquet
    nba/stats/player_season_stats/<season>.parquet
    nba/stats/{team_seasons,player_seasons,games}/<season>.parquet  (lookups)

Season stats are totals only; per-game, per-36 and per-100 values are
computed from them in the catalog. Everything is relative to an output
directory laid out like the basketball-data bucket; see docs/v2.md.

usage: python -m pipeline.stats [--out out] [--season 2026 ...] [--no-fetch]
"""

import argparse
import gzip
import json
import time
import traceback
from collections.abc import Callable, Collection
from pathlib import Path

import duckdb
import pyarrow as pa
from nba_api.stats.endpoints import (
    LeagueDashPlayerBioStats,
    LeagueDashPlayerPtShot,
    LeagueDashPlayerStats,
    PlayerGameLogs,
    TeamGameLogs,
)

from . import lookups
from .output import check_keys, write_atomic, write_parquet
from .seasons import current_season, today_eastern

RAW_DIR = Path("nba/raw/stats")
OUT_DIR = Path("nba/stats")

# a slow response usually means we're being rate limited, but the advanced
# player game logs take about 15 seconds to generate
TIMEOUT = 60
RETRY_DELAYS = [1, 2, 5, 10, 20, 30, 60, 60, 60]

SEASON_TYPES = {"regular_season": "Regular Season", "playoffs": "Playoffs"}

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


def fetch(endpoint: Callable, kwargs: dict, sleep=time.sleep) -> dict:
    """call an nba_api endpoint, retrying with backoff, and return its JSON"""
    for attempt, delay in enumerate([*RETRY_DELAYS, None]):
        try:
            data = endpoint(**kwargs, timeout=TIMEOUT).get_dict()
            result_set(data)
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
            if not isinstance(exc, (TimeoutError, ConnectionError, OSError)):
                print(traceback.format_exc())
            sleep(delay)
    raise AssertionError("unreachable")


def fetch_season(
    outdir: Path, season: int, fetch: Callable[[Callable, dict], dict] = fetch
) -> None:
    for name, (endpoint, kwargs) in requests(season).items():
        data = fetch(endpoint, kwargs)

        def write(tmp, data=data):
            with gzip.open(tmp, "wt") as f:
                json.dump(data, f, separators=(",", ":"))

        write_atomic(raw_path(outdir, season, name), write)
        print(f"stats: fetched {name}")


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
            {", ".join(rest)}
        FROM tgl t
        ORDER BY game_date, game_id, team_id
        """
    )


def build_player_game_logs(con: duckdb.DuckDBPyConnection, season: int) -> None:
    join_raw(con, ["pgl_base", "pgl_advanced"], ["game_id", "player_id"], "pgl")
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
        WHERE NOT (p.player_id IS NULL AND p.game_id LIKE '001%')
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


def build_season(outdir: Path, season: int) -> bool:
    """
    Build the parquet files for `season` from its raw responses. Returns
    False if there are none. A dataset with no rows (no games played yet) is
    not written
    """
    raw = outdir / RAW_DIR / str(season)
    names = requests(season)
    if not all(raw_path(outdir, season, n).is_file() for n in names):
        if any(raw.glob("*.json.gz")):
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
        path = outdir / OUT_DIR / dataset / f"{season}.parquet"
        write_parquet(con, dataset, path)
        print(f"stats: wrote {path}")
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
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild parquet from raw files"
    )
    args = parser.parse_args(argv)

    for season in args.season or [current_season(today_eastern())]:
        if not args.no_fetch:
            fetch_season(args.out, season)
        if not build_season(args.out, season):
            raise SystemExit(f"stats: no raw data for season {season}")


if __name__ == "__main__":
    main()
