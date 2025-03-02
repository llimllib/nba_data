from pathlib import Path
import tempfile
from unittest.mock import patch

from src.dl import (
    download_player_stats,
)


class TestDownloadPlayerStats:

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
        sample_player_stats_df,
    ):
        """Test that we don't redownload data for previous seasons if files exist"""
        # Setup
        mock_is_file.return_value = True
        mock_read_parquet.return_value = sample_player_stats_df

        with tempfile.TemporaryDirectory() as tempdir:
            with patch("src.dl.CURRENT_SEASON", 2023):
                with patch("src.dl.FIRST_SEASON", 2022):
                    download_player_stats(Path(tempdir))

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
        sample_player_stats_df,
        sample_bio_stats_df,
        sample_2pt_shots_df,
        mock_dir,
    ):
        """Test downloading data for the current season"""
        # Setup
        mock_is_file.return_value = False
        mock_fresh.return_value = False
        mock_dash_stats.return_value = sample_player_stats_df
        mock_2pt_shots.return_value = sample_2pt_shots_df
        mock_bio_stats.return_value = sample_bio_stats_df
        mock_join.return_value = sample_player_stats_df

        with tempfile.TemporaryDirectory() as tempdir:
            with patch("src.dl.CURRENT_SEASON", 2023):
                with patch("src.dl.FIRST_SEASON", 2023):
                    download_player_stats(Path(tempdir))

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
        sample_player_stats_df,
        mock_dir,
    ):
        """Test that metadata.json is created with the current timestamp"""
        # Setup
        mock_is_file.return_value = True
        mock_read_parquet.return_value = sample_player_stats_df

        with tempfile.TemporaryDirectory() as tempdir:
            with patch("src.dl.CURRENT_SEASON", 2023):
                with patch("src.dl.FIRST_SEASON", 2022):
                    # Execute
                    download_player_stats(Path(tempdir))

                    # Verify
                    # Should have created the metadata.json file
                    assert mock_json_dump.call_count == 1
                    # Check that the first argument to json.dump contains an 'updated' key
                    args, _ = mock_json_dump.call_args
                    assert "updated" in args[0]
                    # The value should be an ISO format timestamp ending with Z
                    assert args[0]["updated"].endswith("Z")
