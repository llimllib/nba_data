"""
Fetch ESPN's net points season aggregates and build

    nba/espn/net_pts_season/season=<season>/data.parquet

one row per player, season and game type, with offensive, defensive and
total net points and the team possessions they were on the court for.

usage: python -m pipeline.espn_net_pts [--out out] [--season 2026 ...] [--no-fetch]

ESPN publishes every season's aggregates (2019 on) in two files, totals and
per 100 possessions:

    https://nfl-player-metrics.s3.amazonaws.com/net-pts/nba_net_pts_data.json
    https://nfl-player-metrics.s3.amazonaws.com/net-pts/nba_net_pts100_data.json

They mostly equal sums of player_box, but not always: ESPN recomputes past
seasons (2022-2025 differ substantially from the daily files we saved then),
and player_box has no possessions for 2022-2025, so it can't give per-100
values for them (#66). Both files are kept, latest only, at

    nba/raw/espn/net_pts/{totals,per_100}.json.gz

Like every other pipeline, a run only rewrites the current season; pass
--season to rewrite a finished one with ESPN's latest values.

The files identify players by ESPN's ids, not the NBA's, so players are
matched to NBA ids: by player_ids.csv (source "espn") if it has them, else
by a unique normalized name in the season's player_seasons (from out/, or
the bucket). Players that don't match are dropped with a warning; if more
than MAX_UNMATCHED are, nothing is written.
"""

import argparse
import gzip
import json
import urllib.request
from collections import defaultdict
from pathlib import Path

import duckdb
import pyarrow as pa

from .bbref import normalize_name
from .catalog import URL
from .output import check_keys, dataset_path, write_atomic, write_parquet
from .player_ids import load_player_ids
from .seasons import current_season, today_eastern
from .teams import load_team_abbrevs

BASE_URL = "https://nfl-player-metrics.s3.amazonaws.com/net-pts"
FILES = {"totals": "nba_net_pts_data.json", "per_100": "nba_net_pts100_data.json"}
RAW_DIR = Path("nba/raw/espn/net_pts")
OUT_DIR = Path("nba/espn")
STATS_DIR = Path("nba/stats")
DATASET = "net_pts_season"

# ESPN's seasonType -> games.game_type
SEASON_TYPES = {
    "Regular Season": "regular_season",
    "Playoffs": "playoffs",
    "PlayIn": "play_in",
    "IST Championship": "cup_final",
}

# each file's fields we need. A missing one fails the build: the files are
# small and flat, so there's nothing to degrade to
TOTALS_FIELDS = {
    "dot_com_id",
    "full_nm",
    "tm",
    "min_season",
    "max_season",
    "seasonType",
}
TOTALS_FIELDS |= {"offense", "defense", "overall", "net_pts_games"}
PER_100_FIELDS = {"dot_com_id", "min_season", "max_season", "seasonType"}
PER_100_FIELDS |= {"oTmPoss", "dTmPoss", "tTmPoss", "totMin"}

# more unmatched players than this in a season means something's wrong
MAX_UNMATCHED = 10

COLUMNS = [
    ("season", "INTEGER"),
    ("season_type", "VARCHAR"),
    ("player_id", "VARCHAR"),
    ("team_id", "VARCHAR"),
    ("gp", "INTEGER"),
    ("min", "DOUBLE"),
    ("o_net_pts", "DOUBLE"),
    ("d_net_pts", "DOUBLE"),
    ("t_net_pts", "DOUBLE"),
    ("o_team_poss", "DOUBLE"),
    ("d_team_poss", "DOUBLE"),
    ("t_team_poss", "DOUBLE"),
]


def raw_path(outdir: Path, name: str) -> Path:
    return outdir / RAW_DIR / f"{name}.json.gz"


def fetch(outdir: Path) -> None:
    """fetch both files and save them, replacing the last ones"""
    for name, file in FILES.items():
        print(f"espn_net_pts: fetching {file}", flush=True)
        req = urllib.request.Request(f"{BASE_URL}/{file}")
        with urllib.request.urlopen(req, timeout=120) as res:
            data = json.load(res)

        def write(tmp, data=data):
            with gzip.open(tmp, "wt") as f:
                json.dump(data, f, separators=(",", ":"))

        write_atomic(raw_path(outdir, name), write)


def load(outdir: Path, name: str, fields: set[str]) -> list[dict]:
    """a saved file's rows. Raises ValueError if it isn't what we expect"""
    with gzip.open(raw_path(outdir, name), "rt") as f:
        rows = json.load(f)
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError(f"espn_net_pts {name}: expected a list of objects")
    missing = fields - rows[0].keys()
    if missing:
        raise ValueError(f"espn_net_pts {name}: no {sorted(missing)} field")
    return rows


def known_players(outdir: Path, season: int, url: str = URL) -> list[tuple[str, str]]:
    """
    (player_id, name) for a season's player_seasons, from `outdir` if it has
    the file, else from the bucket at `url`
    """
    local = dataset_path(outdir / STATS_DIR, "player_seasons", season)
    source = (
        str(local)
        if local.is_file()
        else f"{url}/{STATS_DIR}/player_seasons/season={season}/data.parquet"
    )
    return duckdb.sql(
        f"SELECT player_id, name FROM read_parquet('{source}', hive_partitioning = false) "
        "WHERE name IS NOT NULL"
    ).fetchall()


def player_ids_for(
    players: dict[str, str], known: list[tuple[str, str]], manual: dict[str, str]
) -> dict[str, str]:
    """
    ESPN id -> NBA id for `players` (ESPN id -> name): from `manual` (the
    espn rows of player_ids.csv), else a unique normalized name in `known`
    """
    by_name = defaultdict(set)
    for player_id, name in known:
        by_name[normalize_name(name)].add(player_id)
    ids = {}
    for espn_id, name in players.items():
        if espn_id in manual:
            ids[espn_id] = manual[espn_id]
        elif len(hits := by_name.get(normalize_name(name), set())) == 1:
            ids[espn_id] = next(iter(hits))
    return ids


def season_rows(
    totals: list[dict],
    per_100: list[dict],
    season: int,
    known: list[tuple[str, str]],
    manual: dict[str, str],
    team_ids: dict[str, str],
) -> list[dict]:
    """
    A season's rows, keyed by NBA ids. ESPN's seasons are named by their
    start year (min_season 2025 is our 2026). Raises ValueError if the data
    isn't as expected or too many players don't match
    """
    label = f"espn_net_pts {season}"
    rows = [r for r in totals if r["min_season"] == season - 1]
    poss = {
        (r["dot_com_id"], r["seasonType"]): r
        for r in per_100
        if r["min_season"] == season - 1
    }
    unknown = sorted({r["seasonType"] for r in rows} - SEASON_TYPES.keys())
    if unknown:
        raise ValueError(f"{label}: unknown seasonType {unknown}")
    if any(r["max_season"] != r["min_season"] for r in rows):
        raise ValueError(f"{label}: a row spans several seasons")

    ids = player_ids_for(
        {str(r["dot_com_id"]): r["full_nm"] for r in rows}, known, manual
    )
    unmatched = sorted({r["full_nm"] for r in rows if str(r["dot_com_id"]) not in ids})
    if unmatched:
        print(f"{label}: dropped players with no NBA id: {unmatched}")
    if len(unmatched) > MAX_UNMATCHED:
        raise ValueError(
            f"{label}: {len(unmatched)} players with no NBA id; add them to "
            "player_ids.csv (source espn)"
        )

    out = []
    for r in rows:
        espn_id = str(r["dot_com_id"])
        if espn_id not in ids:
            continue
        if r["tm"] not in team_ids:
            raise ValueError(f"{label}: no team_id for espn {r['tm']!r}")
        p = poss.get((r["dot_com_id"], r["seasonType"]), {})
        out.append(
            {
                "season": season,
                "season_type": SEASON_TYPES[r["seasonType"]],
                "player_id": ids[espn_id],
                "team_id": team_ids[r["tm"]],
                "gp": r["net_pts_games"],
                "min": p.get("totMin"),
                "o_net_pts": r["offense"],
                "d_net_pts": r["defense"],
                "t_net_pts": r["overall"],
                "o_team_poss": p.get("oTmPoss"),
                "d_team_poss": p.get("dTmPoss"),
                "t_team_poss": p.get("tTmPoss"),
            }
        )
    return out


def build_season(outdir: Path, season: int) -> Path | None:
    """write a season from the saved files; None if they have no rows for it"""
    totals = load(outdir, "totals", TOTALS_FIELDS)
    per_100 = load(outdir, "per_100", PER_100_FIELDS)
    if not any(r["min_season"] == season - 1 for r in totals):
        print(f"espn_net_pts: no rows for {season} yet")
        return None
    manual = {
        p.source_id: p.player_id
        for p in load_player_ids().values()
        if p.source == "espn"
    }
    team_ids = {
        a.abbrev: a.team_id
        for a in load_team_abbrevs()
        if a.source == "espn" and a.covers(season)
    }
    rows = season_rows(
        totals, per_100, season, known_players(outdir, season), manual, team_ids
    )
    types = {"INTEGER": pa.int32(), "DOUBLE": pa.float64(), "VARCHAR": pa.string()}
    table = pa.table(
        {name: pa.array([r[name] for r in rows], types[t]) for name, t in COLUMNS}
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute(
        f"CREATE TABLE {DATASET} AS SELECT * FROM arrow ORDER BY season_type, player_id"
    )
    check_keys(con, DATASET, ["season_type", "player_id"], f"espn {DATASET}")
    path = dataset_path(outdir / OUT_DIR, DATASET, season)
    write_parquet(con, DATASET, path)
    print(f"espn_net_pts: wrote {path} ({len(rows)} rows)")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ESPN net points season totals")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help="season(s) to write (default: current season)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild from the saved files"
    )
    args = parser.parse_args(argv)
    if not args.no_fetch:
        fetch(args.out)
    for season in args.season or [current_season(today_eastern())]:
        build_season(args.out, season)


if __name__ == "__main__":
    main()
