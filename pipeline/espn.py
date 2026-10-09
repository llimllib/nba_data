"""
Download ESPN net points data from espnanalytics.com and build per-season
parquet files.

ESPN publishes one JSON file per game day (plus a separate player details
file) in a public S3 bucket. We keep each day's response, gzipped, at

    nba/raw/espn/<season>/<yyyy-mm-dd>.json.gz

and build these from them, one file per season:

    nba/espn/four_factors/<season>.parquet
    nba/espn/player_box/<season>.parquet
    nba/espn/team_box/<season>.parquet
    nba/espn/player_details/<season>.parquet

Everything is relative to an output directory laid out like the
basketball-data bucket; see docs/v2.md.

usage: python -m pipeline.espn [--out out] [--season 2026 ...] [--no-fetch]
"""
import argparse
from datetime import date, datetime, timedelta, UTC
import gzip
import json
import os
from pathlib import Path
import sys

import duckdb

from .seasons import current_season, season_days, season_window, today_eastern
from .teams import TEAM_ABBREVS_FILE, UnknownTeamAbbrev, load_team_abbrevs

BUCKET = "espnsportsanalytics.com"
COGNITO_IDENTITY = "us-east-1:bf788d54-d676-c9e0-049d-ef3e67cf0372"

RAW_DIR = Path("nba/raw/espn")
OUT_DIR = Path("nba/espn")

# The parts of ESPN's daily file that we read. Fields missing from a file
# (ESPN added teamId, WPA, possessions and more partway through) come back
# NULL; fields not listed here are ignored, but kept in the raw files
SOURCE_SCHEMA = {
    "four_factors": """STRUCT(
        gameId VARCHAR, deanAbbrev VARCHAR, actionType VARCHAR,
        oScPoss DOUBLE, oPoss DOUBLE, oPtsProd DOUBLE, oNetPts DOUBLE
    )[]""",
    "player_box": """STRUCT(
        gmId VARCHAR, plyrID BIGINT, teamId BIGINT, tmName VARCHAR,
        hmTm INTEGER, starter INTEGER, played INTEGER, dAvgPos DOUBLE,
        oNetPts DOUBLE, dNetPts DOUBLE, tNetPts DOUBLE,
        oUsg DOUBLE, dUsg DOUBLE,
        oPoss DOUBLE, dPoss DOUBLE, tPoss DOUBLE,
        oTmPoss DOUBLE, dTmPoss DOUBLE, tTmPoss DOUBLE,
        oWPA DOUBLE, dWPA DOUBLE, tWPA DOUBLE,
        fgmplyr INTEGER, fgaplyr INTEGER, fg3mplyr INTEGER, fg3aplyr INTEGER,
        ftmplyr INTEGER, ftaplyr INTEGER, lumplyr INTEGER, luaplyr INTEGER,
        orebounder INTEGER, drebounder INTEGER, rebounder INTEGER,
        assister INTEGER, assister3pt INTEGER, assisterLU INTEGER,
        assistedShooter INTEGER, assisted3ptShooter INTEGER,
        assistedLUShooter INTEGER,
        stlr INTEGER, blockplyr INTEGER, tov1 INTEGER, livetov1 INTEGER,
        ofoulplyr INTEGER, dfoulplyr INTEGER,
        pts INTEGER, plusMinusPoints INTEGER,
        minutes_played VARCHAR, seconds_played INTEGER
    )[]""",
    "team_box": """STRUCT(
        gameId VARCHAR, tmID BIGINT, homeTm INTEGER, win INTEGER,
        oppPts INTEGER, oppPoss DOUBLE, totPoss DOUBLE,
        eFG DOUBLE, fg2p DOUBLE, fg3p DOUBLE, ftr DOUBLE,
        netPts2s DOUBLE, netPts3s DOUBLE, netPtsShooting DOUBLE,
        netPtsTurnover DOUBLE, netPtsRebound DOUBLE, netPtsFreethrow DOUBLE,
        ptsAllwdOffLive INTEGER, nTimesPtsAllwd INTEGER,
        fgmplyr INTEGER, fgaplyr INTEGER, fg3mplyr INTEGER, fg3aplyr INTEGER,
        ftmplyr INTEGER, ftaplyr INTEGER, lumplyr INTEGER, luaplyr INTEGER,
        orebounder INTEGER, drebounder INTEGER, rebounder INTEGER,
        assister INTEGER, assister3pt INTEGER, assisterLu INTEGER,
        assistedShooter INTEGER, assisted3ptShooter INTEGER,
        assistedLUShooter INTEGER,
        stlr INTEGER, blockplyr INTEGER, tov1 INTEGER, livetov1 INTEGER,
        ofoulplyr INTEGER, dfoulplyr INTEGER,
        pts INTEGER, minutes_played VARCHAR
    )[]""",
    "player_details": """STRUCT(
        gmID VARCHAR, plyrID BIGINT, teamId BIGINT, deanAbbrev VARCHAR,
        actionType VARCHAR, oNetPts DOUBLE, dNetPts DOUBLE, tNetPts DOUBLE
    )[]""",
}

# counting stats shared by player_box and team_box. `x` is the source row
BOX_STATS = """
    x.fgmplyr AS fgm, x.fgaplyr AS fga,
    x.fg3mplyr AS fg3m, x.fg3aplyr AS fg3a,
    x.ftmplyr AS ftm, x.ftaplyr AS fta,
    x.lumplyr AS layup_fgm, x.luaplyr AS layup_fga,
    x.orebounder AS oreb, x.drebounder AS dreb, x.rebounder AS reb,
    x.assister AS ast, x.assister3pt AS ast_fg3, {ast_layup} AS ast_layup,
    x.assistedShooter AS assisted_fgm, x.assisted3ptShooter AS assisted_fg3m,
    x.assistedLUShooter AS assisted_layup_fgm,
    x.stlr AS stl, x.blockplyr AS blk,
    x.tov1 AS tov, x.livetov1 AS live_tov,
    x.ofoulplyr AS off_fouls, x.dfoulplyr AS def_fouls,
    x.pts AS pts
"""

# "mm:ss", where mm can exceed 59. ESPN truncates fractional seconds here, so
# this is used only when its more precise seconds_played is missing
MINUTES_TO_SECONDS = """
    (split_part(x.minutes_played, ':', 1)::INTEGER * 60
     + split_part(x.minutes_played, ':', 2)::INTEGER)
"""

# Each query selects from `src`, which has the season and one unnested
# source row `x`. Team abbreviations resolve through `abbrevs`
QUERIES = {
    "four_factors": """
        SELECT season, x.gameId AS game_id, a.team_id, x.actionType AS action_type,
            x.oScPoss AS o_scoring_poss, x.oPoss AS o_poss,
            x.oPtsProd AS o_pts_produced, x.oNetPts AS o_net_pts
        FROM src
        LEFT JOIN abbrevs a ON a.abbrev = x.deanAbbrev AND season BETWEEN a.first_season AND a.last_season
        ORDER BY game_id, team_id, action_type
    """,
    "player_box": f"""
        SELECT season, x.gmId AS game_id, x.plyrID::VARCHAR AS player_id,
            coalesce(x.teamId::VARCHAR, a.team_id) AS team_id,
            x.hmTm = 1 AS home, x.starter = 1 AS starter, x.played = 1 AS played,
            coalesce(x.seconds_played, {MINUTES_TO_SECONDS}) AS seconds_played,
            x.dAvgPos AS d_avg_pos,
            x.oNetPts AS o_net_pts, x.dNetPts AS d_net_pts, x.tNetPts AS t_net_pts,
            x.oUsg AS o_usg, x.dUsg AS d_usg,
            x.oPoss AS o_poss, x.dPoss AS d_poss, x.tPoss AS t_poss,
            x.oTmPoss AS o_team_poss, x.dTmPoss AS d_team_poss, x.tTmPoss AS t_team_poss,
            x.oWPA AS o_wpa, x.dWPA AS d_wpa, x.tWPA AS t_wpa,
            {BOX_STATS.format(ast_layup="x.assisterLU")},
            x.plusMinusPoints AS plus_minus
        FROM src
        LEFT JOIN abbrevs a ON a.abbrev = x.tmName AND season BETWEEN a.first_season AND a.last_season
        ORDER BY game_id, team_id, player_id
    """,
    "team_box": f"""
        SELECT season, x.gameId AS game_id, x.tmID::VARCHAR AS team_id,
            x.homeTm = 1 AS home, x.win = 1 AS win,
            {MINUTES_TO_SECONDS} AS seconds_played,
            x.totPoss AS poss, x.oppPoss AS opp_poss, x.oppPts AS opp_pts,
            x.eFG AS efg_pct, x.fg2p AS fg2_pct, x.fg3p AS fg3_pct, x.ftr AS ft_rate,
            x.netPts2s AS fg2_net_pts, x.netPts3s AS fg3_net_pts,
            x.netPtsShooting AS shooting_net_pts, x.netPtsTurnover AS turnover_net_pts,
            x.netPtsRebound AS rebound_net_pts, x.netPtsFreethrow AS freethrow_net_pts,
            x.ptsAllwdOffLive AS pts_allowed_off_live_tov,
            x.nTimesPtsAllwd AS n_times_pts_allowed_off_live_tov,
            {BOX_STATS.format(ast_layup="x.assisterLu")}
        FROM src
        ORDER BY game_id, team_id
    """,
    "player_details": """
        SELECT season, x.gmID AS game_id, x.plyrID::VARCHAR AS player_id,
            coalesce(x.teamId::VARCHAR, a.team_id) AS team_id,
            x.actionType AS action_type,
            x.oNetPts AS o_net_pts, x.dNetPts AS d_net_pts, x.tNetPts AS t_net_pts
        FROM src
        LEFT JOIN abbrevs a ON a.abbrev = x.deanAbbrev AND season BETWEEN a.first_season AND a.last_season
        ORDER BY game_id, team_id, player_id, action_type
    """,
}

# what makes a row unique in each output
KEYS = {
    "four_factors": ["game_id", "team_id", "action_type"],
    "player_box": ["game_id", "player_id"],
    "team_box": ["game_id", "team_id"],
    "player_details": ["game_id", "player_id", "action_type"],
}

# source fields with a team abbreviation, so we can check that every one
# resolved and agrees with the team id when ESPN gives both
ABBREV_FIELDS = {
    "four_factors": ("deanAbbrev", None),
    "player_box": ("tmName", "teamId"),
    "player_details": ("deanAbbrev", "teamId"),
}


def get_s3_client():
    """S3 client authenticated with ESPN's public Cognito identity"""
    import boto3

    cognito = boto3.client("cognito-identity", region_name="us-east-1")
    creds = cognito.get_credentials_for_identity(IdentityId=COGNITO_IDENTITY)[
        "Credentials"
    ]
    return boto3.client(
        "s3",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
        region_name="us-east-1",
    )


def fetch_day(s3, season: int, day: date) -> dict | None:
    """
    Fetch one day's data, or None if ESPN has none for that day. Any other
    error is raised.

    ESPN's keys use the season's start year
    """
    from botocore.exceptions import ClientError

    prefix = f"NBA/netpts/{season - 1}/{day.isoformat()}"
    try:
        summary = s3.get_object(Bucket=BUCKET, Key=f"{prefix}.json")
        players = s3.get_object(Bucket=BUCKET, Key=f"{prefix}_player.json")
    except ClientError as e:
        # we can't list ESPN's bucket, so S3 reports a missing file as
        # AccessDenied rather than NoSuchKey
        if e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return None
        raise
    data = json.loads(summary["Body"].read())
    data["player_details"] = json.loads(players["Body"].read())
    return data


def raw_path(outdir: Path, season: int, day: date) -> Path:
    return outdir / RAW_DIR / str(season) / f"{day.isoformat()}.json.gz"


def write_atomic(path: Path, write) -> None:
    """call write(tmp_path), then move the result into place"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    write(tmp)
    os.replace(tmp, path)


def fetch_season(s3, outdir: Path, season: int, today: date) -> list[date]:
    """
    Download every day of `season` up to `today` that we don't already have,
    plus today and yesterday, which may still be changing. Returns the days
    that had data
    """
    recent = {today, today - timedelta(days=1)}
    fetched = []
    for day in season_days(season, today):
        path = raw_path(outdir, season, day)
        if path.is_file() and day not in recent:
            continue
        data = fetch_day(s3, season, day)
        if data is None:
            continue

        def write(tmp):
            with gzip.open(tmp, "wt") as f:
                json.dump(data, f, separators=(",", ":"))

        write_atomic(path, write)
        fetched.append(day)
        print(f"espn: fetched {day}")
    return fetched


def build_season(
    outdir: Path, season: int, abbrevs_file: Path = TEAM_ABBREVS_FILE
) -> bool:
    """
    Build the four parquet files for `season` from its raw files. Returns
    False if there are no raw files. Raises if a team abbreviation doesn't
    resolve, disagrees with ESPN's team id, or a key is duplicated
    """
    files = sorted((outdir / RAW_DIR / str(season)).glob("*.json.gz"))
    if not files:
        return False

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE abbrevs (abbrev VARCHAR, team_id VARCHAR, first_season INTEGER, last_season INTEGER)"
    )
    con.executemany(
        "INSERT INTO abbrevs VALUES (?, ?, ?, ?)",
        [
            (a.abbrev, a.team_id, a.first_season, a.last_season or 9999)
            for a in load_team_abbrevs(abbrevs_file)
            if a.source == "espn"
        ],
    )

    columns = "{" + ", ".join(f"'{k}': '{v}'" for k, v in SOURCE_SCHEMA.items()) + "}"
    con.execute(
        f"""
        CREATE TABLE raw AS
        SELECT * FROM read_json(?, columns={columns}, format='unstructured')
        """,
        [[str(f) for f in files]],
    )

    updated = datetime.now(UTC).isoformat()
    for dataset, query in QUERIES.items():
        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW src AS
            SELECT {season}::INTEGER AS season, unnest({dataset}) AS x FROM raw
            """
        )
        check_abbrevs(con, dataset)
        con.execute(f"CREATE OR REPLACE TABLE out AS {query}")
        check_unique(con, dataset)

        path = outdir / OUT_DIR / dataset / f"{season}.parquet"
        write_atomic(
            path,
            lambda tmp: con.execute(
                f"COPY out TO '{tmp}' (FORMAT parquet, KV_METADATA {{updated: '{updated}'}})"
            ),
        )
        print(f"espn: wrote {path}")
    return True


def check_abbrevs(con, dataset: str) -> None:
    if dataset not in ABBREV_FIELDS:
        return
    abbrev, team_id = ABBREV_FIELDS[dataset]

    unknown = con.execute(
        f"""
        SELECT DISTINCT x.{abbrev} FROM src
        WHERE NOT EXISTS (
            SELECT 1 FROM abbrevs a
            WHERE a.abbrev = x.{abbrev} AND season BETWEEN a.first_season AND a.last_season)
        """
    ).fetchall()
    # player rows that carry ESPN's own team id don't need the abbreviation
    if unknown and team_id:
        unknown = con.execute(
            f"""
            SELECT DISTINCT x.{abbrev} FROM src
            WHERE x.{team_id} IS NULL AND x.{abbrev} IN (SELECT unnest(?))
            """,
            [[u[0] for u in unknown]],
        ).fetchall()
    if unknown:
        raise UnknownTeamAbbrev(
            f"espn {dataset}: no team_id for {sorted(u[0] for u in unknown)} in season "
            f"{con.execute('SELECT any_value(season) FROM src').fetchone()[0]}; "
            "add them to team_abbrevs.csv"
        )

    if team_id:
        mismatched = con.execute(
            f"""
            SELECT DISTINCT x.{abbrev}, x.{team_id}::VARCHAR, a.team_id FROM src
            JOIN abbrevs a ON a.abbrev = x.{abbrev} AND season BETWEEN a.first_season AND a.last_season
            WHERE x.{team_id}::VARCHAR <> a.team_id
            """
        ).fetchall()
        if mismatched:
            raise ValueError(
                f"espn {dataset}: team_abbrevs.csv disagrees with ESPN's team ids "
                f"(abbrev, espn team_id, csv team_id): {mismatched}"
            )


def check_unique(con, dataset: str) -> None:
    keys = ", ".join(KEYS[dataset])
    dupes = con.execute(
        f"SELECT {keys}, count(*) FROM out GROUP BY ALL HAVING count(*) > 1 LIMIT 5"
    ).fetchall()
    if dupes:
        raise ValueError(f"espn {dataset}: duplicate ({keys}): {dupes}")
    nulls = con.execute(
        f"SELECT count(*) FROM out WHERE {' OR '.join(f'{k} IS NULL' for k in KEYS[dataset])}"
    ).fetchone()[0]
    if nulls:
        raise ValueError(f"espn {dataset}: {nulls} rows with a NULL in ({keys})")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help="season(s) to update, as the end year (default: current season)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild parquet from raw files"
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    seasons = args.season or [current_season(today)]
    s3 = None if args.no_fetch else get_s3_client()
    for season in seasons:
        if s3:
            fetch_season(s3, args.out, season, today)
        if build_season(args.out, season):
            continue
        # a missing day looks the same as being denied access, so a season
        # well underway with no data at all means something is wrong
        if today - season_window(season)[0] > timedelta(days=14):
            sys.exit(f"espn: no data for season {season}; is ESPN access broken?")
        print(f"espn: no data yet for season {season}")


if __name__ == "__main__":
    main()
