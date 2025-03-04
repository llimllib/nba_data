from functools import reduce
from time import sleep
import traceback
from typing import Callable, TypeVar
from urllib3.exceptions import ReadTimeoutError

import pandas as pd
from nba_api.stats.endpoints import (
    BoxScoreTraditionalV3,
    BoxScoreAdvancedV3,
    LeagueDashPlayerStats,
    LeagueDashPlayerPtShot,
    LeagueDashPlayerBioStats,
    LeagueDashTeamStats,
    TeamGameLogs,
)


def join(frames: list[pd.DataFrame], on: list[str]) -> pd.DataFrame:
    """
    join a list of dataframes, adding a _DROP suffix for repeated
    columns, which we can then filter out
    """
    return reduce(
        lambda x, y: x.merge(y, on=on, suffixes=("", "_DROP")), frames
    ).filter(regex="^(?!.*_DROP$)")


# this is the timeout used for reading data from stats.nba.com. In my
# experience, delays usually indicate that you're rate-limited, not that it is
# taking a long time to fetch the data, so having a longer timeout doesn't make
# sense to me
TIMEOUT = 30

G = TypeVar("G")


# retry takes a function and its args, and if it times out, sleeps 60 seconds
# then retries until it succeeds
def retry(f: Callable[..., G], **kwargs) -> G:
    i = 0
    while 1:
        try:
            return f(**kwargs)
        except Exception as exc:
            if i > 11:
                raise Exception("retry limit exceeded")
            timeout = [1, 2, 5, 10, 15, 20, 25, 25, 25, 50, 50, 100][i]
            i += 1
            print(f"failed {f.__name__}({kwargs}), sleeping {timeout}:\n{exc}")
            # print the traceback unless it's a read timeout
            if not isinstance(exc, (ReadTimeoutError)):
                print(traceback.format_exc())
            sleep(timeout)
    raise Exception("this should never happen")


# get_box_score will return a combined traditional + advanced box score for a
# given game
def get_box_score(game_id: str) -> pd.DataFrame:
    bs = retry(
        BoxScoreTraditionalV3, game_id=game_id, timeout=TIMEOUT
    ).get_data_frames()[0]
    bsa = retry(BoxScoreAdvancedV3, game_id=game_id, timeout=TIMEOUT).get_data_frames()[
        0
    ]
    return join([bs, bsa], on=["gameId", "personId"])


def get_team_gamelogs(season: str, dt: str, measure: None | str) -> pd.DataFrame:
    return retry(
        TeamGameLogs,
        season_nullable=season,
        date_from_nullable=dt,
        measure_type_player_game_logs_nullable=measure,
        timeout=TIMEOUT,
    ).get_data_frames()[0]


def get_dash_player_stats(season: str, measure: str, per: str) -> pd.DataFrame:
    return retry(
        LeagueDashPlayerStats,
        season=season,
        measure_type_detailed_defense=measure,
        per_mode_detailed=per,
        timeout=TIMEOUT,
    ).get_data_frames()[0]


def get_2pt_shots(season: str) -> pd.DataFrame:
    return retry(
        LeagueDashPlayerPtShot,
        season=season,
        timeout=TIMEOUT,
    ).get_data_frames()[0]


def get_bio_stats(season: str) -> pd.DataFrame:
    return retry(LeagueDashPlayerBioStats, season=season).get_data_frames()[0]


def get_team_stats(season: str, measure_defense: str) -> pd.DataFrame:
    return retry(
        LeagueDashTeamStats,
        season=season,
        measure_type_detailed_defense=measure_defense,
    ).get_data_frames()[0]
