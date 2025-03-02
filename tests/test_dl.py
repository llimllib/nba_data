from pathlib import Path
import tempfile
import pandas as pd
from unittest.mock import patch

from src.dl import (
    download_player_stats,
)

# TODO this test suite is completely machine-written and needs to be made with
# some thought, but it's included here as at least a start


class TestDownloadPlayerStats:

    @classmethod
    def setup_class(cls):
        """Load test fixtures once for all tests"""
        cls.fixtures_dir = Path(__file__).parent / "fixtures"

        # Load player stats fixtures
        cls.player_stats = {}
        for measure in ["Base", "Defense", "Advanced"]:
            for per in ["Totals", "PerGame", "Per36", "Per100Possessions"]:
                if measure == "Advanced" and per != "Totals":
                    continue  # Skip unnecessary combinations

                filename = f"player_stats_{measure}_{per}.parquet"
                filepath = cls.fixtures_dir / filename
                if filepath.exists():
                    cls.player_stats[(measure, per)] = pd.read_parquet(filepath)

        # Load 2pt shots fixture
        cls.shots_2pt = pd.read_parquet(cls.fixtures_dir / "2pt_shots.parquet")

        # Load bio stats fixture
        cls.bio_stats = pd.read_parquet(cls.fixtures_dir / "bio_stats.parquet")

    def get_mock_dash_player_stats(self, season, measure_type, per_mode):
        """Return fixture data for get_dash_player_stats"""
        return self.player_stats.get((measure_type, per_mode), pd.DataFrame())

    def get_mock_2pt_shots(self, season):
        """Return fixture data for get_2pt_shots"""
        return self.shots_2pt

    def get_mock_bio_stats(self, season):
        """Return fixture data for get_bio_stats"""
        return self.bio_stats

    @patch("src.dl.get_dash_player_stats")
    @patch("src.dl.get_2pt_shots")
    @patch("src.dl.get_bio_stats")
    @patch("src.dl.write_parquet")
    @patch("src.dl.fresh")
    @patch("src.dl.pd.read_parquet")
    @patch("src.dl.Path.is_file")
    @patch("src.dl.tryrm")
    def test_download_player_stats_no_redownload(
        self,
        mock_tryrm,
        mock_is_file,
        mock_read_parquet,
        mock_fresh,
        mock_write_parquet,
        mock_bio_stats,
        mock_2pt_shots,
        mock_dash_stats,
    ):
        """Test that we don't redownload data for previous seasons if files exist"""
        # Setup
        mock_is_file.return_value = True
        # Use the first player stats fixture as sample data
        sample_data = next(iter(self.player_stats.values()))
        mock_read_parquet.return_value = sample_data

        with tempfile.TemporaryDirectory() as tempdir:
            download_player_stats(Path(tempdir), 2022, 2023)

            # Verify
            # Should have read the parquet file for previous seasons
            assert mock_read_parquet.call_count == 2  # Once for each year
            # Should not have called the API for previous seasons
            assert mock_dash_stats.call_count == 0
            assert mock_2pt_shots.call_count == 0
            assert mock_bio_stats.call_count == 0
            # Should have written the combined stats
            assert mock_write_parquet.call_count == 1
            # Should have tried to remove the old file
            assert mock_tryrm.call_count == 1

    @patch("src.dl.get_dash_player_stats")
    @patch("src.dl.get_2pt_shots")
    @patch("src.dl.get_bio_stats")
    @patch("src.dl.write_parquet")
    @patch("src.dl.fresh")
    @patch("src.dl.pd.read_parquet")
    @patch("src.dl.Path.is_file")
    @patch("src.dl.join")
    @patch("src.dl.convert_i64_to_i32")
    @patch("src.dl.tryrm")
    def test_download_player_stats_current_season(
        self,
        mock_tryrm,
        mock_convert,
        mock_join,
        mock_is_file,
        mock_read_parquet,
        mock_fresh,
        mock_write_parquet,
        mock_bio_stats,
        mock_2pt_shots,
        mock_dash_stats,
    ):
        """Test downloading data for the current season"""
        # Setup
        mock_is_file.return_value = False
        mock_fresh.return_value = False

        # Use fixture data for mocks
        mock_dash_stats.side_effect = self.get_mock_dash_player_stats
        mock_2pt_shots.side_effect = self.get_mock_2pt_shots
        mock_bio_stats.side_effect = self.get_mock_bio_stats

        # Use the first player stats fixture as the result of join
        sample_data = next(iter(self.player_stats.values()))
        mock_join.return_value = sample_data

        with tempfile.TemporaryDirectory() as tempdir:
            download_player_stats(Path(tempdir), 2023, 2023)

            # Verify
            # Should call the API for the current season
            assert mock_dash_stats.call_count > 0
            assert mock_2pt_shots.call_count == 1
            assert mock_bio_stats.call_count == 1
            # Should have joined the dataframes
            assert mock_join.call_count == 1
            # Should have converted int64 to int32
            assert mock_convert.call_count == 1
            # Should have written the parquet files
            assert (
                mock_write_parquet.call_count == 2
            )  # Once for the year, once for all years
            # Should have tried to remove the old file
            assert mock_tryrm.call_count == 1

    @patch("src.dl.get_dash_player_stats")
    @patch("src.dl.get_2pt_shots")
    @patch("src.dl.get_bio_stats")
    @patch("src.dl.write_parquet")
    @patch("src.dl.fresh")
    @patch("src.dl.pd.read_parquet")
    @patch("src.dl.Path.is_file")
    @patch("src.dl.json.dump")
    @patch("src.dl.tryrm")
    def test_metadata_json_creation(
        self,
        mock_tryrm,
        mock_json_dump,
        mock_is_file,
        mock_read_parquet,
        mock_fresh,
        mock_write_parquet,
        mock_bio_stats,
        mock_2pt_shots,
        mock_dash_stats,
    ):
        """Test that metadata.json is created with the current timestamp"""
        # Setup
        mock_is_file.return_value = True
        # Use the first player stats fixture
        sample_data = next(iter(self.player_stats.values()))
        mock_read_parquet.return_value = sample_data

        with tempfile.TemporaryDirectory() as tempdir:
            # Execute
            download_player_stats(Path(tempdir), 2022, 2023)

            # Verify
            # Should have created the metadata.json file
            assert mock_json_dump.call_count == 1
            # Check that the first argument to json.dump contains an 'updated' key
            args, _ = mock_json_dump.call_args
            assert "updated" in args[0]
            # The value should be an ISO format timestamp ending with Z
            assert args[0]["updated"].endswith("Z")
