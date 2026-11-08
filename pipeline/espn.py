"""
Download ESPN net points data from espnanalytics.com and build per-season
parquet files.

ESPN publishes one JSON file per game day (plus a separate player details
file) in a public S3 bucket. We keep each day's response, gzipped, at

    nba/raw/espn/<season>/<yyyy-mm-dd>.json.gz

and build these from them, one file per season:

    nba/espn/four_factors/season=<season>/data.parquet
    nba/espn/player_box/season=<season>/data.parquet
    nba/espn/team_box/season=<season>/data.parquet
    nba/espn/player_details/season=<season>/data.parquet

Everything is relative to an output directory laid out like the
basketball-data bucket; see docs/v2.md.

ESPN has a game hours before stats.nba.com's team game logs, which `games`
is built from, can list it, so a game's rows are held back until it's in
`games`: pipeline.stats' file in the output directory if it's there, and
the bucket's otherwise. Each run rebuilds the season from every saved day,
so they're added then.

usage: python -m pipeline.espn [--out out] [--season 2026 ...] [--no-fetch] [--url URL]
"""

import argparse
import gzip
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb

from .catalog import URL
from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import current_season, season_days, season_window, today_eastern
from .teams import TEAM_ABBREVS_FILE, UnknownTeamAbbrev, load_team_abbrevs

BUCKET = "espnsportsanalytics.com"
COGNITO_IDENTITY = "us-east-1:bf788d54-d676-c9e0-049d-ef3e67cf0372"

RAW_DIR = Path("nba/raw/espn")
OUT_DIR = Path("nba/espn")
# pipeline.stats' output, for games
STATS_DIR = Path("nba/stats")

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

        def write(tmp, data=data):
            with gzip.open(tmp, "wt") as f:
                json.dump(data, f, separators=(",", ":"))

        write_atomic(path, write)
        fetched.append(day)
        print(f"espn: fetched {day}")
    return fetched


def load_games(
    con: duckdb.DuckDBPyConnection, outdir: Path, season: int, url: str | None
) -> bool:
    """
    Load `games` (game_id) for `season` from pipeline.stats' file in `outdir`
    if it's there, else from the bucket at `url`. Returns False, loading
    nothing, if there's neither. A bucket that can't be read has no games
    """
    local = dataset_path(outdir / STATS_DIR, "games", season)
    if local.is_file():
        source = str(local)
    elif url:
        source = f"{url}/{STATS_DIR}/games/season={season}/data.parquet"
    else:
        return False
    try:
        con.execute(
            "CREATE OR REPLACE TABLE games AS SELECT game_id "
            "FROM read_parquet(?, hive_partitioning = false)",
            [source],
        )
    except duckdb.IOException as e:
        # the bucket answers 403, not 404, for a season with no games yet
        if local.is_file():
            raise
        print(f"espn: couldn't read the bucket's games: {str(e).splitlines()[0]}")
        con.execute("CREATE OR REPLACE TABLE games (game_id VARCHAR)")
    return True


def build_season(
    outdir: Path,
    season: int,
    abbrevs_file: Path = TEAM_ABBREVS_FILE,
    url: str | None = None,
) -> bool:
    """
    Build the four parquet files for `season` from its raw files. Returns
    False if there are no raw files. Raises if a team abbreviation doesn't
    resolve, disagrees with ESPN's team id, or a key is duplicated.

    Rows for games not in `games` (see load_games) are held back, and a
    dataset left with no rows isn't written. With no games to read, every
    row is kept
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

    have_games = load_games(con, outdir, season, url)
    for dataset, query in QUERIES.items():
        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW src AS
            SELECT {season}::INTEGER AS season, unnest({dataset}) AS x FROM raw
            """
        )
        check_abbrevs(con, dataset)
        con.execute(f"CREATE OR REPLACE TABLE out AS {query}")
        if have_games:
            hold_back(con, dataset)
        drop_other_team(con, dataset)
        check_keys(con, "out", KEYS[dataset], f"espn {dataset}")

        rows = con.execute("SELECT count(*) FROM out").fetchone()
        if not (rows and rows[0]):
            print(f"espn: no {dataset} for {season} yet")
            continue
        path = dataset_path(outdir / OUT_DIR, dataset, season)
        write_parquet(con, "out", path)
        print(f"espn: wrote {path}")
    return True


def hold_back(con, dataset: str) -> None:
    """remove the rows of `out` for games that aren't in `games` yet"""
    held = con.execute(
        """
        DELETE FROM out WHERE game_id NOT IN (SELECT game_id FROM games)
        RETURNING game_id
        """
    ).fetchall()
    if held:
        print(
            f"espn: holding back {dataset} for {sorted({g for (g,) in held})}, "
            "not in games yet"
        )


def drop_other_team(con, dataset: str) -> None:
    """
    ESPN, like the NBA, sometimes has a player on both teams in a game:
    Devin Booker in 0022600124 had a second player_box row, also marked PHO
    with his minutes and box score but with OKC's offense (0.05 team
    possessions to the real row's 69), and player_details rows under OKC.
    Keep the player_box row with the most possessions on the floor, and the
    player_details rows for its team. Rows that don't differ in team
    possessions (a game in two days' files) are left for the key check
    """
    if dataset == "player_box":
        extra = con.execute(
            """
            DELETE FROM out WHERE rowid IN (
                SELECT rowid FROM out JOIN (
                    SELECT game_id, player_id FROM out GROUP BY ALL
                    HAVING count(*) > 1 AND count(DISTINCT t_team_poss) = count(*)
                ) USING (game_id, player_id)
                QUALIFY row_number() OVER (
                    PARTITION BY game_id, player_id ORDER BY t_team_poss DESC) > 1)
            RETURNING game_id, player_id, team_id, t_team_poss
            """
        ).fetchall()
        for game_id, player_id, team_id, poss in extra:
            print(
                f"espn: dropping player {player_id}'s player_box row for team "
                f"{team_id} in {game_id} ({poss} team possessions), they have "
                "another row in that game"
            )
        con.execute(
            "CREATE OR REPLACE TABLE player_teams AS "
            "SELECT game_id, player_id, team_id FROM out"
        )
    elif dataset == "player_details":
        extra = con.execute(
            """
            DELETE FROM out WHERE rowid IN (
                SELECT o.rowid FROM out o JOIN player_teams p USING (game_id, player_id)
                WHERE o.team_id <> p.team_id)
            RETURNING game_id, player_id, team_id
            """
        ).fetchall()
        for game_id, player_id, team_id in sorted(set(extra)):
            print(
                f"espn: dropping player {player_id}'s player_details rows for "
                f"team {team_id} in {game_id}, their player_box row is for "
                "another team"
            )


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
    parser.add_argument(
        "--url",
        default=URL,
        help=f"bucket URL for games if --out has none (default: {URL})",
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    seasons = args.season or [current_season(today)]
    s3 = None if args.no_fetch else get_s3_client()
    for season in seasons:
        if s3:
            fetch_season(s3, args.out, season, today)
        if build_season(args.out, season, url=args.url):
            continue
        # a missing day looks the same as being denied access, so a season
        # well underway with no data at all means something is wrong
        if today - season_window(season)[0] > timedelta(days=14):
            sys.exit(f"espn: no data for season {season}; is ESPN access broken?")
        print(f"espn: no data yet for season {season}")


if __name__ == "__main__":
    main()
