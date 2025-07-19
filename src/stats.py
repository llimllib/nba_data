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
from nba_api.stats.library.parameters import LeagueIDNullable, SeasonTypeAllStar
from requests.exceptions import ReadTimeout


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


class RetryLimitExceeded(Exception):
    """Exception raised when a function retry limit is exceeded."""

    def __init__(self, func_name, attempts, kwargs, exception=None):
        self.func_name = func_name
        self.attempts = attempts
        self.kwargs = kwargs
        self.last_exception = exception
        super().__init__(
            f"Retry limit exceeded after {attempts} attempts for function {func_name} with {exception.__class__.__name__}"
        )


def retry(f: Callable[..., G], **kwargs) -> G:
    """
    Retry a function call with backoff and prints out the exceptions raised
    unless they are a timeout.
    """
    i = 0
    while 1:
        try:
            return f(**kwargs)
        except Exception as exc:
            if i > 11:
                raise RetryLimitExceeded(f.__name__, i, kwargs, exc)
            timeout = [1, 2, 5, 10, 15, 20, 25, 25, 25, 50, 50, 100][i]
            i += 1
            print(f"failed {f.__name__}({kwargs}), sleeping {timeout}:\n{exc}")
            # print the traceback unless it's a read timeout
            if not isinstance(exc, (ReadTimeout, ReadTimeoutError, TimeoutError)):
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
        league_id_nullable=LeagueIDNullable.nba,
        season_nullable=season,
        date_from_nullable=dt,
        measure_type_player_game_logs_nullable=measure,
        timeout=TIMEOUT,
    ).get_data_frames()[0]


def get_dash_player_stats(
    season: str, measure: str, per: str, season_type: str = SeasonTypeAllStar.regular
) -> pd.DataFrame:
    return retry(
        LeagueDashPlayerStats,
        league_id_nullable=LeagueIDNullable.nba,
        measure_type_detailed_defense=measure,
        per_mode_detailed=per,
        season=season,
        season_type_all_star=season_type,
        timeout=TIMEOUT,
    ).get_data_frames()[0]


def get_2pt_shots(
    season: str, season_type: str = SeasonTypeAllStar.regular
) -> pd.DataFrame:
    return retry(
        LeagueDashPlayerPtShot,
        league_id=LeagueIDNullable.nba,
        season=season,
        season_type_all_star=season_type,
        timeout=TIMEOUT,
    ).get_data_frames()[0]


def get_bio_stats(
    season: str, season_type: str = SeasonTypeAllStar.regular
) -> pd.DataFrame:
    return retry(
        LeagueDashPlayerBioStats,
        league_id=LeagueIDNullable.nba,
        season=season,
        season_type_all_star=season_type,
    ).get_data_frames()[0]


def get_team_stats(season: str, measure_defense: str) -> pd.DataFrame:
    return retry(
        LeagueDashTeamStats,
        league_id_nullable=LeagueIDNullable.nba,
        season=season,
        measure_type_detailed_defense=measure_defense,
    ).get_data_frames()[0]
