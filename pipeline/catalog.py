"""
Build nba/nba.duckdb, a catalog of views over the per-season parquet files.
ATTACH it to query every dataset by name:

    ATTACH 'https://basketball-data.sfo3.cdn.digitaloceanspaces.com/nba/nba.duckdb' AS nba;
    SELECT * FROM nba.games LIMIT 5;

It holds no data, only:

    <dataset>                   one view per dataset, over every season's file
    players                     each player's latest name
    player_season_stats_per_*   per game, per 36 and per 100 possessions
    metadata                    a table: seasons and update time per dataset

A view lists its files explicitly, since globs don't work over HTTPS, so the
catalog is rebuilt every run to pick up new seasons. DuckDB reads a view's
files when it's created, so with --bucket the parquet files must already be
uploaded: the files are listed from the bucket and the views read them
through the CDN. Without it, the views read the files in --out directly, and
the catalog goes to <out>/nba.local.duckdb so it's never uploaded.

Before the catalog is written, pipeline.integrity checks every key against
the lookup tables; a bad key fails the build and keeps the previous catalog.

With --check, only the integrity check runs, on the files in --out. The
update workflow runs it before uploading, so bad keys are never published.

usage: python -m pipeline.catalog [--out out] [--bucket basketball-data | --check]
"""

import argparse
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import integrity
from .output import write_atomic

URL = "https://basketball-data.sfo3.cdn.digitaloceanspaces.com"
CATALOG = Path("nba/nba.duckdb")
LOCAL_CATALOG = Path("nba.local.duckdb")

# nba/<source>/<dataset>/<season>.parquet; raw files don't match
KEY = re.compile(r"nba/(?P<source>\w+)/(?P<dataset>\w+)/(?P<season>\d{4})\.parquet")

# Season totals that the per-mode views scale. Rates, ratings and gp, w, l,
# dd2 and td3 stay as they are, as in the NBA's per-mode stats. Verified
# against the NBA's values in #42
PER_MODE = [
    "fgm", "fga", "fg2m", "fg2a", "fg3m", "fg3a", "ftm", "fta",
    "oreb", "dreb", "reb", "ast", "tov", "stl", "blk", "blka", "pf", "pfd",
    "pts", "plus_minus", "nba_fantasy_pts", "wnba_fantasy_pts",
    "opp_pts_off_tov", "opp_pts_2nd_chance", "opp_pts_fb", "opp_pts_paint",
    "def_ws_raw",
]  # fmt: skip

# view suffix -> (denominator, factor). The NBA's per-36 and per-100 stats
# keep total minutes in `min`, so only per game scales it
MODES = {
    "per_game": ("gp", 1),
    "per_36": ("min", 36),
    "per_100": ("poss", 100),
}


@dataclass(frozen=True)
class File:
    key: str
    source: str
    dataset: str
    season: int
    modified: datetime


def parse(key: str, modified: datetime) -> File | None:
    """the File for a bucket key, or None if it isn't a dataset's parquet file"""
    m = KEY.fullmatch(key)
    if not m:
        return None
    return File(key, m["source"], m["dataset"], int(m["season"]), modified)


def list_dir(outdir: Path) -> list[File]:
    files = []
    for path in sorted((outdir / "nba").glob("*/*/*.parquet")):
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if f := parse(path.relative_to(outdir).as_posix(), modified):
            files.append(f)
    return files


def list_bucket(bucket: str, client=None) -> list[File]:
    """
    List the bucket's parquet files. Credentials and the endpoint come from
    the usual AWS environment variables (AWS_ENDPOINT_URL for Spaces)
    """
    if client is None:
        import boto3

        client = boto3.client("s3")
    files = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix="nba/"
    ):
        for obj in page.get("Contents", []):
            if f := parse(obj["Key"], obj["LastModified"]):
                files.append(f)
    return files


def by_dataset(files: Iterable[File]) -> dict[str, list[File]]:
    """group files by dataset, sorted by season. Dataset names must be unique"""
    datasets: dict[str, list[File]] = {}
    for f in sorted(files, key=lambda f: (f.dataset, f.season)):
        group = datasets.setdefault(f.dataset, [])
        if group and group[0].source != f.source:
            raise ValueError(
                f"catalog: dataset {f.dataset} is in both {group[0].source} and {f.source}"
            )
        group.append(f)
    return datasets


def quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build(path: Path, files: list[File], location: str) -> None:
    """
    Write the catalog to `path`, with views reading each file at
    `location`/key. `location` is a URL or a local directory
    """
    datasets = by_dataset(files)
    if not datasets:
        raise ValueError("catalog: no parquet files found")

    # DuckDB's default 256KB blocks would make the file ~1MB, mostly empty
    def write(tmp: Path) -> None:
        tmp.unlink(missing_ok=True)
        with duckdb.connect() as con:
            con.execute(f"ATTACH {quote(str(tmp))} AS catalog (BLOCK_SIZE 16384)")
            con.execute("USE catalog")
            create(con, datasets, location.rstrip("/"))
            integrity.check(con, list(datasets))

    write_atomic(path, write)
    print(f"catalog: wrote {path} ({len(datasets)} datasets)")


def check(files: list[File], location: str) -> None:
    """
    Run the integrity check on `files` without writing a catalog. Keys are
    checked within a season, so a run's own files (the current season) can
    be checked before they're uploaded
    """
    datasets = by_dataset(files)
    if not datasets:
        print("catalog: no parquet files to check")
        return
    with duckdb.connect() as con:
        create(con, datasets, location.rstrip("/"))
        integrity.check(con, list(datasets))
    print(f"catalog: integrity check passed ({len(datasets)} datasets)")


def create(
    con: duckdb.DuckDBPyConnection, datasets: dict[str, list[File]], location: str
) -> None:
    for dataset, group in datasets.items():
        urls = ", ".join(quote(f"{location}/{f.key}") for f in group)
        con.execute(
            f"CREATE VIEW {dataset} AS "
            f"SELECT * FROM read_parquet([{urls}], union_by_name = true)"
        )

    if "player_seasons" in datasets:
        # arg_max skips NULL names, so a player's name is the latest one known
        con.execute(
            """
            CREATE VIEW players AS
            SELECT player_id, arg_max(name, season) AS name,
                min(season) AS first_season, max(season) AS last_season
            FROM player_seasons
            GROUP BY player_id
            """
        )

    if "player_season_stats" in datasets:
        for mode, (column, factor) in MODES.items():
            # dividing by 0.0 minutes gives NaN or infinity, not NULL
            denominator = f"NULLIF({column}, 0)"
            scaled = [f"{c} / {denominator} * {factor} AS {c}" for c in PER_MODE]
            if mode == "per_game":
                scaled.append("min / gp AS min")
            # def_ws is rounded to 2 decimals, which distorts per-mode values
            scaled.append(
                f"coalesce(def_ws_raw, def_ws) / {denominator} * {factor} AS def_ws"
            )
            con.execute(
                f"CREATE VIEW player_season_stats_{mode} AS "
                f"SELECT * REPLACE ({', '.join(scaled)}) FROM player_season_stats"
            )

    con.execute(
        """
        CREATE TABLE metadata (
            dataset VARCHAR PRIMARY KEY, source VARCHAR,
            first_season INTEGER, last_season INTEGER, seasons INTEGER,
            updated TIMESTAMPTZ
        )
        """
    )
    con.executemany(
        "INSERT INTO metadata VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                dataset,
                group[0].source,
                group[0].season,
                group[-1].season,
                len(group),
                max(f.modified for f in group),
            )
            for dataset, group in datasets.items()
        ],
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--bucket",
        help="list this bucket's files, read them from --url, and write <out>/nba/nba.duckdb",
    )
    parser.add_argument("--url", default=URL, help=f"bucket URL (default: {URL})")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only run the integrity check on the files in --out; write nothing",
    )
    args = parser.parse_args(argv)

    if args.check:
        check(list_dir(args.out), str(args.out.resolve()))
    elif args.bucket:
        build(args.out / CATALOG, list_bucket(args.bucket), args.url)
    else:
        build(args.out / LOCAL_CATALOG, list_dir(args.out), str(args.out.resolve()))


if __name__ == "__main__":
    main()
