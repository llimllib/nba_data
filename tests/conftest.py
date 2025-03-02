import pytest
import pandas as pd
import datetime
from pathlib import Path
import json
from unittest.mock import MagicMock, patch


# Sample data for mocking responses
@pytest.fixture
def sample_player_stats_df():
    """Return a sample DataFrame that mimics player stats"""
    return pd.DataFrame(
        {
            "PLAYER_ID": [1, 2, 3],
            "PLAYER_NAME": ["Player A", "Player B", "Player C"],
            "TEAM_ID": [100, 101, 102],
            "PTS": [20.5, 15.3, 25.7],
            "REB": [5.2, 10.1, 7.8],
            "AST": [8.3, 2.5, 4.1],
        }
    )


@pytest.fixture
def sample_bio_stats_df():
    """Return a sample DataFrame that mimics bio stats"""
    return pd.DataFrame(
        {
            "PLAYER_ID": [1, 2, 3],
            "PLAYER_NAME": ["Player A", "Player B", "Player C"],
            "HEIGHT": ["6-6", "6-10", "6-3"],
            "WEIGHT": [220, 245, 195],
            "COUNTRY": ["USA", "Serbia", "France"],
        }
    )


@pytest.fixture
def sample_2pt_shots_df():
    """Return a sample DataFrame that mimics 2pt shot stats"""
    return pd.DataFrame(
        {
            "PLAYER_ID": [1, 2, 3],
            "PLAYER_NAME": ["Player A", "Player B", "Player C"],
            "FG2_PCT": [0.52, 0.48, 0.45],
            "FG2A": [350, 420, 380],
            "FG2M": [182, 202, 171],
        }
    )


@pytest.fixture
def mock_dir(tmp_path):
    """Create a temporary directory for test data"""
    return tmp_path / "data"
