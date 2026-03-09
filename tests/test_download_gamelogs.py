"""
Test that download_gamelogs fetches each box score exactly once per game.

The bug: get_team_gamelogs returns one row per team per game, so each GAME_ID
appears twice (home team + away team). The box score download loop was inside
the `for measure in [None, "Advanced"]` loop, meaning each game_id was passed
to get_box_score 2 (teams) × 2 (measures) = 4 times. Since get_box_score
returns all players from both teams, it only needs to be called once per game.
"""

from pathlib import Path
import tempfile
from unittest.mock import patch, call

import pandas as pd

from src.dl import download_gamelogs


def make_team_gamelogs():
    """
    Simulate what get_team_gamelogs returns: one row per team per game.

    A single game (0022400001) between teams 1 and 2 produces two rows.
    """
    return pd.DataFrame(
        {
            "GAME_ID": ["0022400001", "0022400001"],
            "TEAM_ID": [1, 2],
            "TEAM_ABBREVIATION": ["AAA", "BBB"],
            "GAME_DATE": ["01/01/2025", "01/01/2025"],
            "MATCHUP": ["AAA vs. BBB", "BBB @ AAA"],
        }
    )


def make_box_score():
    """Simulate what get_box_score returns: all players from both teams."""
    return pd.DataFrame(
        {
            "gameId": ["0022400001", "0022400001"],
            "personId": [101, 201],
            "firstName": ["Alice", "Bob"],
            "familyName": ["A", "B"],
        }
    )


@patch("src.dl.write_all_team_summaries")
@patch("src.dl.dump_team_summaries")
@patch("src.dl.dump_team_eff_json")
@patch("src.dl.write_parquet")
@patch("src.dl.tryrm")
@patch("src.dl.get_box_score")
@patch("src.dl.get_team_gamelogs")
@patch("src.dl.join")
@patch("src.dl.convert_i64_to_i32")
def test_get_box_score_called_once_per_game(
    _mock_convert,
    mock_join,
    mock_get_team_gamelogs,
    mock_get_box_score,
    _mock_tryrm,
    _mock_write_parquet,
    _mock_dump_team_eff,
    _mock_dump_team_summaries,
    _mock_write_all_team_summaries,
):
    """get_box_score should be called once per unique game, not once per team row."""
    gamelogs = make_team_gamelogs()
    mock_get_team_gamelogs.return_value = gamelogs
    mock_get_box_score.return_value = make_box_score()

    # join is called to merge the Base and Advanced gamelogs; return a
    # dataframe with lowercaseable columns that has the fields dump_team_eff_json needs
    joined = gamelogs.copy()
    joined.columns = joined.columns.str.lower()
    mock_join.return_value = joined

    with tempfile.TemporaryDirectory() as tmpdir:
        download_gamelogs(Path(tmpdir), first_season=2025, current_season=2025)

    # The critical assertion: get_box_score should be called exactly once for
    # the single game, NOT 4 times (2 teams × 2 measures).
    assert mock_get_box_score.call_count == 1, (
        f"get_box_score was called {mock_get_box_score.call_count} times, "
        f"expected 1. Each game_id should only be fetched once since the box "
        f"score endpoint returns players from both teams."
    )
    mock_get_box_score.assert_called_once_with("0022400001")
