import pytest
from unittest.mock import patch, MagicMock
import pandas as pd

from src.stats import (
    get_dash_player_stats,
    get_2pt_shots,
    get_bio_stats,
    get_team_stats,
    retry,
    sleep,
)


class TestStatsAPI:
    @patch("src.stats.LeagueDashPlayerStats")
    def test_get_dash_player_stats(self, mock_league_dash):
        """Test that get_dash_player_stats calls the API correctly and returns the DataFrame"""
        # Setup
        mock_response = MagicMock()
        mock_response.get_data_frames.return_value = [pd.DataFrame({"test": [1, 2, 3]})]
        mock_league_dash.return_value = mock_response

        # Execute
        result = get_dash_player_stats("2022-23", "Base", "Totals")

        # Verify
        mock_league_dash.assert_called_once_with(
            league_id_nullable="00",
            measure_type_detailed_defense="Base",
            per_mode_detailed="Totals",
            season="2022-23",
            season_type_all_star="Regular Season",
            timeout=30,
        )
        assert isinstance(result, pd.DataFrame)
        assert list(result["test"]) == [1, 2, 3]

    @patch("src.stats.LeagueDashPlayerPtShot")
    def test_get_2pt_shots(self, mock_pt_shot):
        """Test that get_2pt_shots calls the API correctly and returns the DataFrame"""
        # Setup
        mock_response = MagicMock()
        mock_response.get_data_frames.return_value = [pd.DataFrame({"test": [1, 2, 3]})]
        mock_pt_shot.return_value = mock_response

        # Execute
        result = get_2pt_shots("2022-23")

        # Verify
        mock_pt_shot.assert_called_once_with(
            league_id="00",
            season="2022-23",
            season_type_all_star="Regular Season",
            timeout=30,
        )
        assert isinstance(result, pd.DataFrame)
        assert list(result["test"]) == [1, 2, 3]

    @patch("src.stats.LeagueDashPlayerBioStats")
    def test_get_bio_stats(self, mock_bio_stats):
        """Test that get_bio_stats calls the API correctly and returns the DataFrame"""
        # Setup
        mock_response = MagicMock()
        mock_response.get_data_frames.return_value = [pd.DataFrame({"test": [1, 2, 3]})]
        mock_bio_stats.return_value = mock_response

        # Execute
        result = get_bio_stats("2022-23")

        # Verify
        mock_bio_stats.assert_called_once_with(
            league_id="00", season="2022-23", season_type_all_star="Regular Season"
        )
        assert isinstance(result, pd.DataFrame)
        assert list(result["test"]) == [1, 2, 3]

    @patch("src.stats.sleep")
    def test_retry_mechanism(self, mock_sleep):
        """Test that retry mechanism works correctly"""
        # Setup
        mock_func = MagicMock()
        mock_func.__name__ = "mock_function"
        mock_func.side_effect = [
            Exception("API error"),
            Exception("API error"),
            "success",
        ]

        # Execute
        with patch("src.stats.retry.__defaults__", (mock_func,)):
            result = retry(mock_func, arg1="test")

        # Verify
        assert mock_func.call_count == 3
        assert mock_sleep.call_count == 2
        assert result == "success"
