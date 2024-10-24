#!/usr/bin/env python
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from functools import reduce
import json
from time import time
import os
from pathlib import Path
import sys
from typing import List

from nba_api.stats.endpoints import (
    LeagueDashPlayerStats,
    LeagueDashPlayerPtShot,
    LeagueDashPlayerBioStats,
    LeagueDashTeamStats,
    TeamGameLogs,
)
from nba_api.stats.library.data import teams
import pandas as pd

FIRST_SEASON = 2010
CURRENT_SEASON = 2025
DIR = Path("data")
TIMEOUT = 60
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


def join(frames: List[pd.DataFrame], on: List[str]) -> pd.DataFrame:
    """
    join a list of dataframes, adding a _DROP suffix for repeated
    columns, which we can then filter out
    """
    return reduce(
        lambda x, y: x.merge(y, on=on, suffixes=("", "_DROP")), frames
    ).filter(regex="^(?!.*_DROP$)")


def tryrm(path: str | Path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def dump_team_summaries(season: str, year: int) -> None:
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
    df = LeagueDashTeamStats(
        season=season, measure_type_detailed_defense="Advanced"
    ).get_data_frames()[0][
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
            "updated": datetime.now(timezone.utc).isoformat(),
            "teams": {
                team_id_to_abbrev(t["TEAM_ID"]): t for t in df.to_dict(orient="records")
            },
        },
        open(DIR / f"team_summary_{year}.json", "w"),
    )


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
        "updated": datetime.now(timezone.utc).isoformat(),
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


def download_gamelogs():
    seasons = []
    for year in range(FIRST_SEASON, CURRENT_SEASON + 1):
        file = DIR / f"gamelog_{year}.parquet"
        season = f"{year-1}-{str(year)[2:]}"
        most_recent = ""
        old_games = None

        # we don't need to redownload old years, (presumably?) nothing has changed
        if year != CURRENT_SEASON and file.is_file():
            seasons.append(pd.read_parquet(file))
            continue

        # If the current year's file is less than an hour old, don't re-download
        elif year == CURRENT_SEASON and fresh(file):
            seasons.append(pd.read_parquet(file))
            continue

        # If the current year's file isn't fresh, load it so we can download
        # only the more recent games
        elif file.is_file():
            old_games = pd.read_parquet(file)
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

        print(f"Downloading {season} game logs from {most_recent}")

        # you can get the game logs with ortg and drtg as "advanced"
        # MeasureType at https://www.nba.com/stats/teams/boxscores-advanced
        logs = []
        for measure in [None, "Advanced"]:
            logs.append(
                TeamGameLogs(
                    date_from_nullable=most_recent,
                    season_nullable=season,
                    measure_type_player_game_logs_nullable=measure,
                    timeout=TIMEOUT,
                ).get_data_frames()[0]
            )

        # join games by game_id and team_id, which should serve as unique keys
        games = join(logs, on=["GAME_ID", "TEAM_ID"])

        # lower case all the columns
        games.columns = games.columns.str.lower()

        # if we had games from this season, we want to append the newly
        # downloaded ones. Otherwise, we should have all the games for this
        # season in the games dataframe
        if old_games is not None:
            # drop all our created columns; we'll recreate them in a second.
            old_games.drop(
                columns=["game_n"],
                inplace=True,
            )
            # using game_id and team_id as keys, drop any duplicate rows. Favor
            # newer rows.
            games = pd.concat([old_games, games]).drop_duplicates(
                subset=["game_id", "team_id"],
                keep="last",
            )

        assert isinstance(games, pd.DataFrame)

        games.reset_index(inplace=True)
        games.rename(columns={"index": "game_n"}, inplace=True)
        convert_i64_to_i32(games)

        dump_team_eff_json(games, year)
        dump_team_summaries(season, year)

        seasons.append(games)
        games.to_parquet(file)

    allseasons = pd.concat(seasons)
    allseasons.reset_index(drop=True, inplace=True)

    # delete the old file and overwrite with the new. pandas parquet writing
    # does not have any option to overwrite.
    tryrm(DIR / "gamelogs.parquet")
    allseasons.to_parquet(DIR / "gamelogs.parquet")


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


def download_player_stats():
    playerstats = []
    for year in range(FIRST_SEASON, CURRENT_SEASON + 1):
        file = DIR / f"players_{year}.parquet"
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
                df = LeagueDashPlayerStats(
                    season=season,
                    measure_type_detailed_defense=measure,
                    per_mode_detailed=per,
                    timeout=TIMEOUT,
                ).get_data_frames()[0]
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
        stats.append(
            LeagueDashPlayerStats(
                season=season,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="Totals",
                timeout=TIMEOUT,
            ).get_data_frames()[0]
        )

        # https://www.nba.com/stats/players/shots-general
        # this gets us 2-point shots broken out from other field goals
        # also: this defaults to totals, if I want per game I'll need to add it
        stats.append(
            LeagueDashPlayerPtShot(
                season=season,
                timeout=TIMEOUT,
            ).get_data_frames()[0]
        )

        # get bio stats: height, place of origin, etc
        stats.append(LeagueDashPlayerBioStats(season=season).get_data_frames()[0])

        allstats = join(stats, on=["PLAYER_ID"])
        convert_i64_to_i32(allstats)

        # lower case all the columns
        allstats.columns = allstats.columns.str.lower()

        playerstats.append(allstats)
        allstats.to_parquet(file)

    allstats = pd.concat(playerstats)
    allstats.reset_index(drop=True, inplace=True)

    # delete the old playerstats.parquet and overwrite the new.
    tryrm(DIR / "playerstats.parquet")
    allstats.to_parquet(DIR / "playerstats.parquet")

    # XXX: Until duckdb supports reading metadata out of parquet files, we will
    #      generate a metadata file
    # - https://github.com/duckdb/duckdb/issues/2534
    # update: duckdb now supports it, but the version containing support has
    #         not yet been released. see
    #         https://duckdb.org/docs/data/parquet/metadata.html#parquet-key-value-metadata
    #         for the docs; once something > 0.9.2 gets relased, we can use it
    json.dump(
        {"updated": datetime.utcnow().isoformat() + "Z"},
        open(DIR / "metadata.json", "w"),
    )


def update_json():
    for year in range(FIRST_SEASON, CURRENT_SEASON + 1):
        season = f"{year-1}-{str(year)[2:]}"
        df = pd.read_parquet(DIR / f"gamelog_{year}.parquet")
        dump_team_eff_json(df, year)
        dump_team_summaries(season, year)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="download stats from stats.nba.com")
    parser.add_argument("-g", "--gamelogs", const=True, action="store_const")
    parser.add_argument("-s", "--player-stats", const=True, action="store_const")
    parser.add_argument("--update-json-only", const=True, action="store_const")
    args = parser.parse_args()

    # if no arguments passed, download both
    runall = not any([args.gamelogs, args.player_stats, args.update_json_only])

    if not DIR.is_dir():
        DIR.mkdir()

    if args.update_json_only:
        update_json()
        sys.exit(0)
    if args.gamelogs or runall:
        download_gamelogs()
    if args.player_stats or runall:
        download_player_stats()
