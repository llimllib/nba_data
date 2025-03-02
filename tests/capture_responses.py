"""
Script to capture real API responses for testing.
Run this manually when you need to update the test fixtures.
"""

from pathlib import Path
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stats import get_dash_player_stats, get_2pt_shots, get_bio_stats


def capture_responses():
    """Capture real API responses and save them as test fixtures"""
    fixtures_dir = Path(__file__).parent / "fixtures"
    fixtures_dir.mkdir(exist_ok=True)

    # Capture a sample season
    season = "2022-23"

    # Capture player stats
    for measure in ["Base", "Defense", "Advanced"]:
        for per in ["Totals", "PerGame", "Per36", "Per100Possessions"]:
            if measure == "Advanced" and per != "Totals":
                continue  # Skip unnecessary combinations

            print(f"Capturing {measure} {per}...")
            df = get_dash_player_stats(season, measure, per)
            filename = f"player_stats_{measure}_{per}.parquet"
            df.to_parquet(fixtures_dir / filename)

    # Capture 2pt shots
    print("Capturing 2pt shots...")
    df = get_2pt_shots(season)
    df.to_parquet(fixtures_dir / "2pt_shots.parquet")

    # Capture bio stats
    print("Capturing bio stats...")
    df = get_bio_stats(season)
    df.to_parquet(fixtures_dir / "bio_stats.parquet")

    print("Done capturing responses!")


if __name__ == "__main__":
    capture_responses()
