import argparse
from datetime import datetime, timedelta
import glob
import json
import os
from pathlib import Path
from typing import cast, Protocol

import boto3

CURRENT_SEASON = 2024


def fetch_espn_data(season, day):
    """
    Fetches NBA net points data from ESPN's S3 bucket for a specific season and
    day.

    Parameters:
        season (str): The year when the NBA season started (e.g., "2023" for
                      the 2023-2024 season)
        day (str): The specific date to retrieve data for, in 'yyyy-mm-dd'
                   format

    Returns:
        dict: JSON data containing NBA statistics for the specified day
        None: If an error occurs during the data retrieval process

    Note:
        Uses AWS Cognito for authentication with a hardcoded identity ID.
        The data is stored in the 'espnsportsanalytics.com' S3 bucket.
    """
    cognito_identity = boto3.client("cognito-identity", region_name="us-east-1")
    identity_id = "us-east-1:bf788d54-d676-c9e0-049d-ef3e67cf0372"

    try:
        # Get credentials from Cognito
        cognito_data = cognito_identity.get_credentials_for_identity(
            IdentityId=identity_id
        )

        # Configure a new session with the temporary credentials
        session = boto3.Session(
            aws_access_key_id=cognito_data["Credentials"]["AccessKeyId"],
            aws_secret_access_key=cognito_data["Credentials"]["SecretKey"],
            aws_session_token=cognito_data["Credentials"]["SessionToken"],
            region_name="us-east-1",
        )

        s3 = session.client("s3")

        # seems like the structure is
        # NBA/netpts/<year of start of season>/yyyy-mm-dd.json
        response = s3.get_object(
            Bucket="espnsportsanalytics.com", Key=f"NBA/netpts/{season}/{day}.json"
        )

        return json.loads(response["Body"].read().decode("utf-8"))

    except Exception as e:
        print(f"Error retrieving data for {day}: {str(e)}")
        return None


def daterange(start_date_str, end_date_str=None):
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")

    if end_date_str:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
    else:
        end_date = datetime.now()

    date_list = []
    current_date = start_date

    while current_date <= end_date and current_date <= datetime.now():
        date_list.append(current_date.strftime("%Y-%m-%d"))
        current_date += timedelta(days=1)

    return date_list


def create_duckdb(datadir: str):
    for file in glob.glob(f"{datadir}/*"):
        print(file)


class Options(Protocol):
    every_season: bool


def main(opts: Options):
    # Create output directory if it doesn't exist
    espn_dir = Path("__file__").parent / "data" / "espn"

    # the data on the site appears to go back to the 2021-2022 season
    for season in range(
        2021 if opts.every_season else CURRENT_SEASON, CURRENT_SEASON + 1
    ):
        output_dir = espn_dir / str(season)
        os.makedirs(output_dir, exist_ok=True)

        # - I verified that preseason games don't yield results
        # - I don't think any season started before 10-15 in this sample
        dates = daterange(f"{season}-10-15", f"{season+1}-06-30")

        print(f"Downloading data for {len(dates)} days...")

        for date in dates:
            outfile = os.path.join(output_dir, f"{date}.json")
            if os.path.isfile(outfile):
                continue

            print(f"Fetching data for {season} {date}...")
            data = fetch_espn_data(season, date)

            if data:
                with open(outfile, "w") as f:
                    json.dump(data, f, indent=2)
                print(f"Saved data for {date}")
            else:
                print(f"No data available for {date}, skipping")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="download stats from stats.nba.com")
    parser.add_argument("-e", "--every-season", const=True, action="store_const")

    args = parser.parse_args()

    # the cast tells pyright to shut up, every_season does actually exist
    main(cast(Options, args))
