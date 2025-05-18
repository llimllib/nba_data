import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import cast, Protocol
from collections import defaultdict

import boto3
import pyarrow as pa
import pyarrow.parquet as pq


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


def dump_to_parquet(base_dir: Path, output_dir: Path):
    """
    Visit each deano json file and build parquet files for four_factors, player
    stats, and team stats.

    Ouputs three files into output_dir: four_factors.parquet,
    player_box.parquet, and team_box.parquet
    """
    json_files = list(base_dir.glob("**/*.json"))

    four_factors = defaultdict(dict)
    player_boxes = defaultdict(dict)
    team_boxes = defaultdict(dict)

    for file_path in json_files:
        season = int(file_path.parts[-2])
        try:
            with open(file_path, "r") as f:
                data = json.load(f)

            # Process each item in four_factors
            for factor in data["four_factors"]:
                game_id = factor["gameId"]
                team_abbr = factor["deanAbbrev"]
                action_type = factor["actionType"]

                key = (game_id, team_abbr)

                four_factors[key] |= {
                    "gameId": game_id,
                    "team": team_abbr,
                    "season": season,
                    f"{action_type}_oScPoss": factor.get("oScPoss"),
                    f"{action_type}_oPoss": factor.get("oPoss"),
                    f"{action_type}_oPtsProd": factor.get("oPtsProd"),
                    f"{action_type}_oNetPts": factor.get("oNetPts"),
                }

            for pb in data["player_box"]:
                player_boxes[(pb["gmId"], pb["plyrID"])] = {
                    "season": season,
                    "game_id": pb["gmId"],
                    "player_id": pb["plyrID"],
                    "team": pb["tmName"],
                    "home": pb["hmTm"],
                    "name": pb["displayName"],
                    "starter": pb["starter"],
                    "dAvgPos": pb["dAvgPos"],
                    "oNetPts": pb["oNetPts"],
                    "dNetPts": pb["dNetPts"],
                    "tNetPts": pb["tNetPts"],
                    "oUsg": pb["oUsg"],
                    "dUsg": pb["dUsg"],
                    "assisted3ptShooter": pb["assisted3ptShooter"],
                    "assistedLUShooter": pb["assistedLUShooter"],
                    "assistedShooter": pb["assistedShooter"],
                    "assister": pb["assister"],
                    "assister3pt": pb["assister3pt"],
                    "assisterLU": pb["assisterLU"],
                    "blockplyr": pb["blockplyr"],
                    "dfoulplyr": pb["dfoulplyr"],
                    "drebounder": pb["drebounder"],
                    "fg3aplyr": pb["fg3aplyr"],
                    "fg3mplyr": pb["fg3mplyr"],
                    "fgaplyr": pb["fgaplyr"],
                    "fgmplyr": pb["fgmplyr"],
                    "ftaplyr": pb["ftaplyr"],
                    "ftmplyr": pb["ftmplyr"],
                    "livetov1": pb["livetov1"],
                    "luaplyr": pb["luaplyr"],
                    "lumplyr": pb["lumplyr"],
                    "ofoulplyr": pb["ofoulplyr"],
                    "orebounder": pb["orebounder"],
                    "pts": pb["pts"],
                    "rebounder": pb["rebounder"],
                    "stlr": pb["stlr"],
                    "tov1": pb["tov1"],
                    "plusMinusPoints": pb["plusMinusPoints"],
                    "minutes_played": pb["minutes_played"],
                    "played": pb["played"],
                }

            for tb in data["team_box"]:
                team_boxes[(tb["gameId"], tb["tmID"])] = {
                    "season": season,
                    "game_id": tb["gameId"],
                    "team_id": tb["tmID"],
                    "homeTm": tb["homeTm"],
                    "tmName": tb["tmName"],
                    "assisted3ptShooter": tb["assisted3ptShooter"],
                    "assistedLUShooter": tb["assistedLUShooter"],
                    "assistedShooter": tb["assistedShooter"],
                    "assister": tb["assister"],
                    "assister3pt": tb["assister3pt"],
                    "assisterLu": tb["assisterLu"],
                    "blockplyr": tb["blockplyr"],
                    "dfoulplyr": tb["dfoulplyr"],
                    "drebounder": tb["drebounder"],
                    "fg3aplyr": tb["fg3aplyr"],
                    "fg3mplyr": tb["fg3mplyr"],
                    "fgaplyr": tb["fgaplyr"],
                    "fgmplyr": tb["fgmplyr"],
                    "ftaplyr": tb["ftaplyr"],
                    "ftmplyr": tb["ftmplyr"],
                    "livetov1": tb["livetov1"],
                    "luaplyr": tb["luaplyr"],
                    "lumplyr": tb["lumplyr"],
                    "ofoulplyr": tb["ofoulplyr"],
                    "orebounder": tb["orebounder"],
                    "pts": tb["pts"],
                    "rebounder": tb["rebounder"],
                    "stlr": tb["stlr"],
                    "tov1": tb["tov1"],
                    "minutes_played": tb["minutes_played"],
                    "eFG": tb["eFG"],
                    "fg2p": tb["fg2p"],
                    "fg3p": tb["fg3p"],
                    "ftr": tb["ftr"],
                    "totPoss": tb["totPoss"],
                    "netPts2s": tb["netPts2s"],
                    "netPts3s": tb["netPts3s"],
                    "netPtsShooting": tb["netPtsShooting"],
                    "netPtsTurnover": tb["netPtsTurnover"],
                    "netPtsRebound": tb["netPtsRebound"],
                    "netPtsFreethrow": tb["netPtsFreethrow"],
                    "oppPoss": tb["oppPoss"],
                    "oppPts": tb["oppPts"],
                    "win": tb["win"],
                }

        except Exception as e:
            print(f"Error processing {file_path}: {e}")
            raise e

    # Convert to list of dictionaries for PyArrow
    pq.write_table(
        pa.Table.from_pylist(list(four_factors.values())),
        output_dir / "four_factors.parquet",
    )

    # TODO: sum player stats for each season
    pq.write_table(
        pa.Table.from_pylist(list(player_boxes.values())),
        output_dir / "player_box.parquet",
    )

    # TODO: sum team stats for each season
    pq.write_table(
        pa.Table.from_pylist(list(team_boxes.values())),
        output_dir / "team_box.parquet",
    )


class Options(Protocol):
    every_season: bool
    espndir: Path


def main(opts: Options):
    # the data on the site appears to go back to the 2021-2022 season
    for season in range(
        2021 if opts.every_season else CURRENT_SEASON, CURRENT_SEASON + 1
    ):
        output_dir = opts.espndir / str(season)
        os.makedirs(output_dir, exist_ok=True)

        # - I verified that preseason games don't yield results
        # - I don't think any season started before 10-15 in this sample
        dates = daterange(f"{season}-10-15", f"{season+1}-06-30")

        print(f"Downloading data for {len(dates)} days...")

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        for date in dates:
            # don't re-download data unless it's todays or yesterday's
            outfile = output_dir / f"{date}.json"
            if outfile.is_file() and date not in [today, yesterday]:
                continue

            print(f"Fetching data for {season} {date}...")
            data = fetch_espn_data(season, date)

            if data:
                with open(outfile, "w") as f:
                    json.dump(data, f, indent=2)
                print(f"Saved data for {date}")
            else:
                print(f"No data available for {date}, skipping")

    dump_to_parquet(opts.espndir, opts.espndir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="download stats from stats.nba.com")
    parser.add_argument(
        "-e",
        "--every-season",
        const=True,
        action="store_const",
        help="download every season, not just this year",
    )
    parser.add_argument(
        "--parquet-only",
        const=True,
        action="store_const",
        help="only create parquet, don't download anything",
    )

    args = parser.parse_args()

    args.espndir = Path("__file__").parent / "data" / "espn"

    # the cast tells pyright to shut up, every_season does actually exist
    if not args.parquet_only:
        main(cast(Options, args))
    else:
        dump_to_parquet(args.espndir, args.espndir)
