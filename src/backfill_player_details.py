"""
One-time backfill script to add player_details to existing ESPN JSON files.

The player_details data exists on S3 for all seasons (2021-2024) but was never
downloaded into the local JSON files because that feature was added after the
initial data download.

Usage:
    uv run python src/backfill_player_details.py
"""

import json
from pathlib import Path

import boto3


def get_s3_client():
    """Get authenticated S3 client using ESPN's Cognito credentials."""
    cognito_identity = boto3.client("cognito-identity", region_name="us-east-1")
    identity_id = "us-east-1:bf788d54-d676-c9e0-049d-ef3e67cf0372"

    cognito_data = cognito_identity.get_credentials_for_identity(IdentityId=identity_id)

    session = boto3.Session(
        aws_access_key_id=cognito_data["Credentials"]["AccessKeyId"],
        aws_secret_access_key=cognito_data["Credentials"]["SecretKey"],
        aws_session_token=cognito_data["Credentials"]["SessionToken"],
        region_name="us-east-1",
    )

    return session.client("s3")


def backfill_player_details(espn_dir: Path, seasons: list[int] | None = None):
    """
    Backfill player_details into existing JSON files.

    Args:
        espn_dir: Path to the ESPN data directory (e.g., data/espn)
        seasons: List of seasons to backfill, or None for all (2021-2024)
    """
    if seasons is None:
        seasons = [2021, 2022, 2023, 2024]

    s3 = get_s3_client()

    for season in seasons:
        season_dir = espn_dir / str(season)
        if not season_dir.exists():
            print(f"Season directory {season_dir} does not exist, skipping")
            continue

        json_files = sorted(season_dir.glob("*.json"))
        print(f"\nProcessing season {season}: {len(json_files)} files")

        updated = 0
        skipped = 0
        errors = 0

        for json_file in json_files:
            # Extract date from filename (e.g., "2021-10-19.json" -> "2021-10-19")
            date_str = json_file.stem

            # Load existing data
            with open(json_file, "r") as f:
                data = json.load(f)

            # Skip if player_details already exists and has data
            if "player_details" in data and len(data["player_details"]) > 0:
                skipped += 1
                continue

            # Fetch player_details from S3
            key = f"NBA/netpts/{season}/{date_str}_player.json"
            try:
                response = s3.get_object(Bucket="espnsportsanalytics.com", Key=key)
                player_details = json.loads(response["Body"].read().decode("utf-8"))

                # Add to existing data
                data["player_details"] = player_details

                # Write back
                with open(json_file, "w") as f:
                    json.dump(data, f, indent=2)

                updated += 1
                if updated % 50 == 0:
                    print(f"  Updated {updated} files...")

            except s3.exceptions.NoSuchKey:
                # No player data for this date (probably no games)
                data["player_details"] = []
                with open(json_file, "w") as f:
                    json.dump(data, f, indent=2)
                skipped += 1

            except Exception as e:
                # S3 returns AccessDenied instead of NoSuchKey when file doesn't
                # exist and caller lacks ListBucket permission. Check if this is
                # a no-games day by looking at existing data.
                if "AccessDenied" in str(e) and len(data.get("four_factors", [])) == 0:
                    # No games on this date, so no player data expected
                    data["player_details"] = []
                    with open(json_file, "w") as f:
                        json.dump(data, f, indent=2)
                    skipped += 1
                else:
                    print(f"  Error processing {json_file.name}: {e}")
                    errors += 1

        print(f"  Season {season}: {updated} updated, {skipped} skipped, {errors} errors")


def main():
    espn_dir = Path(__file__).parent.parent / "data" / "espn"

    if not espn_dir.exists():
        print(f"ESPN data directory not found: {espn_dir}")
        return

    print(f"Backfilling player_details in {espn_dir}")
    backfill_player_details(espn_dir)
    print("\nDone! Run 'uv run python src/espn.py --parquet-only' to rebuild parquet files.")


if __name__ == "__main__":
    main()
