#!/usr/bin/env python
import argparse
from collections import defaultdict
import datetime
from glob import glob
import json
from time import time
import os
from pathlib import Path
import re
import sys

from nba_api.stats.library.data import teams
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .stats import (
    get_box_score,
    get_team_gamelogs,
    get_team_stats,
    get_dash_player_stats,
    get_2pt_shots,
    get_bio_stats,
    join,
)

FIRST_SEASON = 2010
CURRENT_SEASON = 2025
TEAMS_BY_ID = {t[0]: t for t in teams}

# TODO: refactor this to create a duckdb database; Here is a javascript code
# example of how to connect to a database file stored at a URL:
# https://github.com/isalazar14/PoGoKeepTossTrade/blob/11d6f949fafc23581bdba8938456dfc312489ec0/client/src/util/duckdb_wasm_dbLoader.js#L29
#
# (or just load a few parquet files? who knows. Their docs are
# incomprehensible)


def team_id_to_abbrev(id: int) -> str:
    """
    Return a team's abbreviation from their full name

    ex: team_fullname_to_abbrev("Charlotte Hornets") -> "CHA"
    """
    return TEAMS_BY_ID[id][1]


def fresh(fname: Path) -> bool:
    """
    Return whether a file is stale and should be re-downloaded

    It should be re-downloaded if the file given by fname was modified more than
    an hour ago or does not exist
    """
    return fname.is_file() and (time() - fname.stat().st_mtime) / 60 * 60 < 1


def convert_i64_to_i32(df: pd.DataFrame) -> None:
    """
    downcast any int64 columns into int32

    the int64s are painful to deal with in javascript where they get
    represented as a bigint, which observable plot can't handle: see
    https://github.com/observablehq/plot/discussions/1099
    """
    for col in df.columns:
        column = df[col]
        assert column is not None  # pyright can be dumb sometimes. Make it believe
        if column.dtype == "int64":
            df[col] = column.astype("int32")


def tryrm(path: str | Path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def write_parquet(df: pd.DataFrame, filename: str | Path):
    schema = pa.Schema.from_pandas(df).with_metadata(
        {"updated": datetime.datetime.now(datetime.UTC).isoformat()},
    )
    table = pa.Table.from_pandas(df, schema=schema)
    pq.write_table(table, filename)


def dump_team_summaries(season: str, year: int, outdir: Path) -> None:
    """
    Dump team summary stats for a season to team_summary_{year}.json

    Currently only used for the team diamond, but I should make it possible to
    graph team stats as well
    """
    # I wish I understood what the estimated stats were, but let's just not
    # include them bc I don't understand them. A warning pops up when you go to
    # https://www.nba.com/stats/teams/estimated-advanced that says "The
    # advanced stats on this page are derived from estimated possessions.", but
    # what are the other stats derived from? exact posssessions? Why would I
    # want estimated instead of exact? They give reasonably different answers
    # too. Strange
    #
    # there are more stats available if you leave off "Advanced", but I don't
    # currently have a need for them. add as necessary. (dump this to parquet?)
    df = get_team_stats(season, "Advanced")[
        [
            "TEAM_ID",
            "TEAM_NAME",
            "GP",
            "W",
            "L",
            "MIN",
            "OFF_RATING",
            "DEF_RATING",
            "NET_RATING",
            "AST_PCT",
            "AST_TO",
            "AST_RATIO",
            "OREB_PCT",
            "DREB_PCT",
            "REB_PCT",
            "TM_TOV_PCT",
            "EFG_PCT",
            "TS_PCT",
            "PACE",
            "PACE_PER40",
            "POSS",
        ]
    ]
    json.dump(
        {
            "updated": datetime.datetime.now(datetime.UTC).isoformat(),
            "teams": {
                team_id_to_abbrev(t["TEAM_ID"]): t
                for t in df.to_dict(orient="records")  # type: ignore
            },
        },
        open(outdir / f"team_summary_{year}.json", "w"),
    )


def write_all_team_summaries(outdir: Path) -> None:
    """
    gather all the team summary json files into a single team summary json for
    all covered years
    """
    all_summaries = {
        "updated": datetime.datetime.now(datetime.UTC).isoformat(),
        "data": {},
    }
    for f in glob(str(DIR / "team_summary_*.json")):
        data = json.load(open(f))
        year = re.findall(r"\d{4}", f)[0]
        all_summaries["data"][year] = data["teams"]
    json.dump(all_summaries, open(outdir / f"team_summary.json", "w"))


def dump_team_eff_json(df: pd.DataFrame, year: int) -> None:
    """
    write out efficiency stats to a json file for consumption by the team graph
    page
    """
    eff = df[
        [
            "game_id",
            "team_id",
            "team_abbreviation",
            "game_date",
            "matchup",
            "off_rating",
            "def_rating",
            "pts",
            "poss",
        ]
    ]
    assert isinstance(eff, pd.DataFrame)

    data = {
        "updated": datetime.datetime.now(datetime.UTC).isoformat(),
        "games": eff.to_dict(orient="records"),
    }

    # attach the opponents' points and possessions here, but doing a self-join
    # in pandas seems to be beyond my reach. Instead I'll do some chugging with
    # loops
    game_id_index = defaultdict(list)
    for g1 in data["games"]:
        game_id_index[g1["game_id"]].append(g1)

    for g1 in data["games"]:
        try:
            a, b = game_id_index[g1["game_id"]]
        except ValueError:
            print(
                f"unable to find {g1['game_id']} in index, skipping. No idea why this is happening. {year}"
            )
            continue
        g2 = a if a["team_id"] != g1["team_id"] else b
        g1["opp_pts"] = g2["pts"]
        g1["opp_poss"] = g2["poss"]

    json.dump(data, open(DIR / f"team_efficiency_{year}.json", "w"))


def download_gamelogs(outdir: Path):
    game_seasons = []
    player_seasons = []
    for year in range(FIRST_SEASON, CURRENT_SEASON + 1):
        gamelog_file = outdir / f"gamelog_{year}.parquet"
        playerlog_file = outdir / f"playerlog_{year}.parquet"
        season = f"{year-1}-{str(year)[2:]}"
        most_recent = ""
        old_games = None
        old_playerlogs = None

        # we don't need to redownload old years, (presumably?) nothing has changed
        if year != CURRENT_SEASON and gamelog_file.is_file():
            game_seasons.append(pd.read_parquet(gamelog_file))
            continue

        # If the current year's file is less than an hour old, don't re-download
        elif year == CURRENT_SEASON and fresh(gamelog_file):
            game_seasons.append(pd.read_parquet(gamelog_file))
            continue

        # TODO: download past year's game logs... will take a long time

        # If the current year's file isn't fresh, load it so we can download
        # only the more recent games
        if gamelog_file.is_file():
            old_games = pd.read_parquet(gamelog_file)
            # the date format for LeagueGameLog appears to be YYYY-MM-DD while
            # the date formate for TeamGameLogs appears to be MM/DD/YYYY. I
            # can't find any documentation for this, I did curl requests until
            # something worked
            most_recent = (
                pd.to_datetime(old_games["game_date"])
                .max()
                .to_pydatetime()
                .strftime("%m/%d/%Y")
            )

        if playerlog_file.is_file():
            old_playerlogs = pd.read_parquet(playerlog_file)

        print(f"Downloading {season} game logs from {most_recent}")

        # you can get the game logs with ortg and drtg as "advanced"
        # MeasureType at https://www.nba.com/stats/teams/boxscores-advanced
        logs = []
        playerlogs = pd.DataFrame()
        for measure in [None, "Advanced"]:
            gamelogs = get_team_gamelogs(season, most_recent, measure)
            logs.append(gamelogs)
            for _, game_id in gamelogs["GAME_ID"].items():
                assert isinstance(game_id, str), game_id
                playerlogs = pd.concat([playerlogs, get_box_score(game_id)])

        # join games by game_id and team_id, which should serve as unique keys
        games = join(logs, on=["GAME_ID", "TEAM_ID"])

        # lower case all the columns
        games.columns = games.columns.str.lower()

        # if we had games from this season, we want to append the newly
        # downloaded ones. Otherwise, we should have all the games for this
        # season in the games dataframe
        if old_games is not None:
            # using game_id and team_id as keys, drop any duplicate rows. Favor
            # newer rows.
            games = pd.concat([old_games, games]).drop_duplicates(
                subset=["game_id", "team_id"],
                keep="last",
            )

        if old_playerlogs is not None:
            playerlogs = pd.concat([old_playerlogs, playerlogs]).drop_duplicates(
                subset=["gameId", "personId"],
                keep="last",
            )

        assert isinstance(games, pd.DataFrame)
        assert isinstance(playerlogs, pd.DataFrame)

        games.reset_index(inplace=True, drop=True)
        convert_i64_to_i32(games)

        playerlogs.reset_index(inplace=True, drop=True)
        convert_i64_to_i32(playerlogs)

        dump_team_eff_json(games, year)
        dump_team_summaries(season, year, outdir)

        game_seasons.append(games)
        write_parquet(games, gamelog_file)
        player_seasons.append(playerlogs)
        write_parquet(playerlogs, playerlog_file)

    all_game_seasons = pd.concat(game_seasons)
    all_game_seasons.reset_index(drop=True, inplace=True)
    all_player_seasons = pd.concat(player_seasons)
    all_player_seasons.reset_index(drop=True, inplace=True)

    write_all_team_summaries(outdir)

    # delete the old file and overwrite with the new. pandas parquet writing
    # does not have any option to overwrite.
    tryrm(outdir / "gamelogs.parquet")
    write_parquet(all_game_seasons, outdir / "gamelogs.parquet")
    tryrm(outdir / "player_game_logs.parquet")
    write_parquet(all_game_seasons, outdir / "player_game_logs.parquet")


columns_to_suffix = [
    "MIN",
    "FGM",
    "FGA",
    "FG3M",
    "FG3A",
    "FTM",
    "FTA",
    "OREB",
    "DREB",
    "REB",
    "AST",
    "TOV",
    "STL",
    "BLK",
    "BLKA",
    "PF",
    "PFD",
    "PTS",
    "PLUS_MINUS",
    "NBA_FANTASY_PTS",
    "DEF_WS",
    "OPP_PTS_OFF_TOV",
    "OPP_PTS_2ND_CHANCE",
    "OPP_PTS_FB",  # FB = fast break
    "OPP_PTS_PAINT",
    "DEF_WS",
]


def download_player_stats(outdir: Path):
    playerstats = []
    for year in range(FIRST_SEASON, CURRENT_SEASON + 1):
        file = outdir / f"players_{year}.parquet"
        season = f"{year-1}-{str(year)[2:]}"

        # we don't need to redownload old years, (presumably?) nothing has changed
        if year != CURRENT_SEASON and Path(file).is_file():
            playerstats.append(pd.read_parquet(file))
            continue

        # If the current year's file is less than an hour old, don't re-download
        elif year == CURRENT_SEASON and fresh(Path(file)):
            playerstats.append(pd.read_parquet(file))
            continue

        print(f"Downloading {season} player stats")

        # https://www.nba.com/stats/players/traditional and inspect to see the options
        stats = []
        for per in ["Totals", "PerGame", "Per36", "Per100Possessions"]:
            for measure in ["Base", "Defense"]:
                df = get_dash_player_stats(season, measure, per)
                df["year"] = year

                # we want the PTS column (for exmample) to contain the total # of
                # points, not PTS_Totals, so only suffix the columns of the other
                # `per` values
                if per != "Totals":
                    df.rename(
                        columns={col: f"{col}_{per}" for col in columns_to_suffix},
                        inplace=True,
                    )

                stats.append(df)

        # the advanced stats don't have any differences between totals,
        # pergame, &c, so only download them once
        stats.append(get_dash_player_stats(season, "Advanced", "Totals"))

        # https://www.nba.com/stats/players/shots-general
        # this gets us 2-point shots broken out from other field goals
        # also: this defaults to totals, if I want per game I'll need to add it
        stats.append(get_2pt_shots(season))

        # get bio stats: height, place of origin, etc
        stats.append(get_bio_stats(season))

        allstats = join(stats, on=["PLAYER_ID"])
        convert_i64_to_i32(allstats)

        # lower case all the columns
        allstats.columns = allstats.columns.str.lower()

        playerstats.append(allstats)
        write_parquet(allstats, file)

    allstats = pd.concat(playerstats)
    allstats.reset_index(drop=True, inplace=True)

    # delete the old playerstats.parquet and overwrite the new.
    tryrm(outdir / "playerstats.parquet")
    write_parquet(allstats, outdir / "playerstats.parquet")

    # XXX: Until duckdb supports reading metadata out of parquet files, we will
    #      generate a metadata file
    # - https://github.com/duckdb/duckdb/issues/2534
    # update: duckdb now supports it, but the version containing support has
    #         not yet been released. see
    #         https://duckdb.org/docs/data/parquet/metadata.html#parquet-key-value-metadata
    #         for the docs; once something > 0.9.2 gets relased, we can use it
    json.dump(
        {"updated": datetime.datetime.now(datetime.UTC).isoformat() + "Z"},
        open(outdir / "metadata.json", "w"),
    )


def update_json(outdir: Path):
    for year in range(FIRST_SEASON, CURRENT_SEASON + 1):
        season = f"{year-1}-{str(year)[2:]}"
        df = pd.read_parquet(outdir / f"gamelog_{year}.parquet")
        dump_team_eff_json(df, year)
        dump_team_summaries(season, year, outdir)


if __name__ == "__main__":
    DIR = Path("__file__").parent / "data"

    parser = argparse.ArgumentParser(description="download stats from stats.nba.com")
    parser.add_argument("-g", "--gamelogs", const=True, action="store_const")
    parser.add_argument("-s", "--player-stats", const=True, action="store_const")
    parser.add_argument("--update-json-only", const=True, action="store_const")
    parser.add_argument(
        "-d",
        "--data-dir",
        type=Path,
        default=DIR,
        help="Path to data directory (default: ./data)",
    )
    args = parser.parse_args()

    # if no arguments passed, download both
    runall = not any([args.gamelogs, args.player_stats, args.update_json_only])

    if not DIR.is_dir():
        DIR.mkdir()

    if args.update_json_only:
        update_json(args.data_dir)
        sys.exit(0)
    if args.gamelogs or runall:
        download_gamelogs(args.data_dir)
    if args.player_stats or runall:
        download_player_stats(args.data_dir)
