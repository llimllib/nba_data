"""
Fetch openrapm's published ratings and build

    nba/openrapm/openrapm/season=<season>/data.parquet

OpenRAPM (https://openrapm.com, code at https://github.com/bbstats/openrapm)
is RAPM pulled toward a box-score prior, one rating per player per season,
1997 on. Its site is static, and its data is two JSON files in the repo's
docs/data/:

    ratings.json            {"meta": {"built": ...}, "rows": [{"s": season,
                            "id": NBA id, "p": possessions, "o", "d", "t"}, ...]}
    decompose_openrapm.json {"meta": {"built", "pieces"}, "rows": {season:
                            [[name, id, possessions, [offense pieces],
                            [defense pieces], [total pieces], teams], ...]}}

The decomposition has the same rows, with each side's rating split into
pieces that add up to it (box score, on court, off court, team SOS, swap,
then the rating itself). It's an extra page on the site, so it's optional:
if it's missing or doesn't match ratings.json, the pieces are NULL.

Every publish recomputes every season (a model change applies to all of
them), so when `meta.built` changes, every season is rewritten, and a run
whose `built` matches the bucket's writes nothing. Each version's raw files
are kept, under the ratings' `built` date, at

    nba/raw/openrapm/<built>/ratings.json.gz
    nba/raw/openrapm/<built>/decompose_openrapm.json.gz

Seasons before FIRST_SEASON are left out until our lookup tables cover them
(#89), and players not in their season's player_seasons are dropped, so the
integrity check passes.

usage: python -m pipeline.openrapm [--out out] [--no-fetch]
"""

import argparse
import gzip
import http.client
import json
import time
import urllib.request
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa

from .catalog import URL
from .output import check_keys, dataset_path, write_atomic, write_parquet

BASE_URL = "https://raw.githubusercontent.com/bbstats/openrapm/main/docs/data"
USER_AGENT = "nba_data (https://github.com/llimllib/nba_data)"
FILES = ("ratings", "decompose_openrapm")
RETRY_DELAYS = [5, 30]
RAW_DIR = Path("nba/raw/openrapm")
OUT_DIR = Path("nba/openrapm")
STATS_DIR = Path("nba/stats")
DATASET = "openrapm"

# our lookup tables start with 2009-10; openrapm's go back to 1996-97
FIRST_SEASON = 2010

# the decomposition's pieces, in its order -> our column suffix. The last
# piece is the rating, which ratings.json already has
PIECES = {
    "box_score": "box_score",
    "on_court": "on_court",
    "off_court": "off_court",
    "team_sos": "team_sos",
    "swap": "swap",
}
DECOMPOSE_FIELDS = ["n", "id", "p", "o", "d", "t", "tm"]

COLUMNS = [
    ("season", "INTEGER"),
    ("player_id", "VARCHAR"),
    ("built", "DATE"),
    ("possessions", "INTEGER"),
    ("o_rating", "DOUBLE"),
    ("d_rating", "DOUBLE"),
    ("rating", "DOUBLE"),
    *((f"o_{p}", "DOUBLE") for p in PIECES.values()),
    *((f"d_{p}", "DOUBLE") for p in PIECES.values()),
]
KEYS = ["player_id"]

PA_TYPES = {
    "INTEGER": pa.int32(),
    "DOUBLE": pa.float64(),
    "VARCHAR": pa.string(),
    "DATE": pa.date32(),
}

# ratings.json has every season since 1997, about 14,000 rows; far fewer
# means we're not reading what we think we are
MIN_ROWS = 5000
# the decomposition's pieces are rounded to two decimals, so their sum can
# be a few hundredths off the rating
PIECE_TOLERANCE = 0.06
# a few players a season may be missing from player_seasons; more than this
# means openrapm's ids or ours aren't what we think
MAX_DROPPED = 0.02


def built_of(doc: dict, name: str) -> date:
    try:
        return date.fromisoformat(doc["meta"]["built"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"openrapm {name}: no valid meta.built") from e


def number(value, label: str) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"openrapm: {label} {value!r} isn't a number")


def integer(value, label: str) -> int:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"openrapm: {label} {value!r} isn't an integer")


def rating_rows(doc: dict) -> list[dict]:
    """
    ratings.json's rows as dicts of our columns, without the pieces. Raises
    ValueError if the file isn't as expected, so nothing is written from a
    changed file
    """
    built = built_of(doc, "ratings")
    raw = doc.get("rows")
    if not isinstance(raw, list) or len(raw) < MIN_ROWS:
        raise ValueError(
            f"openrapm ratings: expected at least {MIN_ROWS} rows, got "
            f"{len(raw) if isinstance(raw, list) else type(raw).__name__}"
        )
    rows = []
    for r in raw:
        if not isinstance(r, dict) or {"s", "id", "p", "o", "d", "t"} - r.keys():
            raise ValueError(f"openrapm ratings: malformed row {r!r:.200}")
        label = f"ratings {r['s']} player {r['id']}"
        rows.append(
            {
                "season": integer(r["s"], f"{label} season"),
                "player_id": str(integer(r["id"], f"{label} id")),
                "built": built,
                "possessions": integer(r["p"], f"{label} possessions"),
                "o_rating": number(r["o"], f"{label} o"),
                "d_rating": number(r["d"], f"{label} d"),
                "rating": number(r["t"], f"{label} t"),
            }
        )
    return rows


def pieces(doc: dict) -> dict[tuple[int, str], dict]:
    """
    (season, player_id) -> the pieces' columns, from the decomposition.
    Raises ValueError if it isn't as expected
    """
    meta = doc.get("meta", {})
    expected = [*PIECES, "rating"]
    if meta.get("fields") != DECOMPOSE_FIELDS or meta.get("pieces") != expected:
        raise ValueError(
            f"openrapm decomposition: fields {meta.get('fields')} and pieces "
            f"{meta.get('pieces')}, expected {DECOMPOSE_FIELDS} and {expected}"
        )
    found = {}
    for season, rows in doc.get("rows", {}).items():
        for r in rows:
            if not isinstance(r, list) or len(r) != len(DECOMPOSE_FIELDS):
                raise ValueError(f"openrapm decomposition: malformed row {r!r:.200}")
            _, player_id, _, off, def_, _, _ = r
            label = f"decomposition {season} player {player_id}"
            row = {}
            for side, values in (("o", off), ("d", def_)):
                if not isinstance(values, list) or len(values) != len(expected):
                    raise ValueError(f"openrapm {label}: {side} pieces {values!r}")
                values = [number(v, f"{label} {side}") for v in values]
                if abs(sum(values[:-1]) - values[-1]) > PIECE_TOLERANCE:
                    raise ValueError(
                        f"openrapm {label}: {side} pieces {values[:-1]} don't add "
                        f"up to {values[-1]}"
                    )
                row |= {f"{side}_{p}": v for p, v in zip(PIECES.values(), values)}
                row[f"{side}_rating"] = values[-1]
            found[(int(season), str(integer(player_id, f"{label} id")))] = row
    return found


def with_pieces(rows: list[dict], decompose: dict | None) -> list[dict]:
    """
    `rows` with the decomposition's pieces, or NULL pieces if there's no
    decomposition or it's for another version of the ratings
    """
    empty = {f"{s}_{p}": None for s in "od" for p in PIECES.values()}
    if decompose is None:
        print("openrapm: no decomposition; the pieces are NULL")
        return [r | empty for r in rows]
    built = built_of(decompose, "decomposition")
    if built != rows[0]["built"]:
        print(
            f"openrapm: the decomposition was built {built}, the ratings "
            f"{rows[0]['built']}; the pieces are NULL"
        )
        return [r | empty for r in rows]
    found = pieces(decompose)
    out, unmatched = [], 0
    for r in rows:
        p = found.get((r["season"], r["player_id"]))
        if p is None or (p["o_rating"], p["d_rating"]) != (
            r["o_rating"],
            r["d_rating"],
        ):
            unmatched += 1
            out.append(r | empty)
            continue
        out.append(r | {k: v for k, v in p.items() if k in empty})
    if unmatched:
        print(f"openrapm: {unmatched} rows with no matching pieces, left NULL")
    return out


def known_players(outdir: Path, season: int, url: str = URL) -> set[str] | None:
    """
    The player ids in a season's player_seasons: from `outdir` if it has the
    file, else from the bucket at `url` (a sources run has only its own
    files). None if neither has it
    """
    local = dataset_path(outdir / STATS_DIR, "player_seasons", season)
    source = (
        str(local)
        if local.is_file()
        else f"{url}/{STATS_DIR}/player_seasons/season={season}/data.parquet"
    )
    try:
        rows = duckdb.sql(
            f"SELECT player_id FROM read_parquet('{source}', hive_partitioning = false)"
        ).fetchall()
    except duckdb.HTTPException as e:
        # Spaces answers 403 for a file that doesn't exist
        if getattr(e, "status_code", None) in (403, 404):
            return None
        raise
    return {r[0] for r in rows}


def write_season(outdir: Path, season: int, rows: list[dict]) -> Path:
    table = pa.table(
        {name: pa.array([r[name] for r in rows], PA_TYPES[t]) for name, t in COLUMNS}
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute(f"CREATE TABLE {DATASET} AS SELECT * FROM arrow ORDER BY player_id")
    check_keys(con, DATASET, KEYS, f"openrapm {season}")
    path = dataset_path(outdir / OUT_DIR, DATASET, season)
    write_parquet(con, DATASET, path)
    return path


def build(
    outdir: Path,
    ratings: dict,
    decompose: dict | None,
    known=None,
) -> list[Path]:
    """
    Write every season from FIRST_SEASON on, keeping the players in each
    season's player_seasons (`known(outdir, season)`). A season with no
    player_seasons yet is skipped. Returns the paths written
    """
    known = known or known_players
    rows = with_pieces(rating_rows(ratings), decompose)
    by_season: dict[int, list[dict]] = {}
    for r in rows:
        if r["season"] >= FIRST_SEASON:
            by_season.setdefault(r["season"], []).append(r)

    # check every season before writing any, so a bad file writes nothing
    kept_by_season = {}
    for season, season_rows in sorted(by_season.items()):
        players = known(outdir, season)
        if players is None:
            print(f"openrapm: no player_seasons for {season}; skipping it")
            continue
        kept = [r for r in season_rows if r["player_id"] in players]
        dropped = sorted(r["player_id"] for r in season_rows if r not in kept)
        if dropped:
            print(
                f"openrapm: {season}: dropped {len(dropped)} players not in "
                f"player_seasons: {dropped}"
            )
        if len(dropped) > MAX_DROPPED * len(season_rows):
            raise ValueError(
                f"openrapm {season}: {len(dropped)} of {len(season_rows)} players "
                "aren't in player_seasons"
            )
        kept_by_season[season] = kept

    paths = [write_season(outdir, s, r) for s, r in kept_by_season.items()]
    built = rows[0]["built"]
    print(f"openrapm: wrote {len(paths)} seasons of the ratings built {built}")
    return paths


# --- fetching ----------------------------------------------------------------


def fetch(name: str, sleep=time.sleep) -> str:
    url = f"{BASE_URL}/{name}.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt, delay in enumerate([*RETRY_DELAYS, None]):
        print(f"openrapm: fetching {url}", flush=True)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read().decode("utf-8")
        # a dropped connection mid-file is an http.client.IncompleteRead
        except (OSError, http.client.HTTPException) as e:
            if delay is None:
                raise
            print(
                f"openrapm: attempt {attempt + 1} failed, retrying in {delay}s: {e!r}"
            )
            sleep(delay)
    raise AssertionError("unreachable")


def raw_path(outdir: Path, built: date | str, name: str) -> Path:
    return outdir / RAW_DIR / str(built) / f"{name}.json.gz"


def save_raw(path: Path, text: str) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            f.write(text)

    write_atomic(path, write)


def latest_raw(outdir: Path) -> dict[str, dict]:
    """name -> document, from the newest saved version"""
    versions = sorted((outdir / RAW_DIR).glob("[0-9][0-9][0-9][0-9]-*"))
    if not versions:
        raise FileNotFoundError(f"no saved openrapm files in {outdir / RAW_DIR}")
    docs = {}
    for name in FILES:
        path = raw_path(outdir, versions[-1].name, name)
        if path.is_file():
            with gzip.open(path, "rt") as f:
                docs[name] = json.load(f)
    return docs


def published_built(outdir: Path, season: int, url: str = URL) -> date | None:
    """
    The `built` date of a season's file: from `outdir` if it has the file,
    else from the bucket at `url`. None if neither has it
    """
    local = dataset_path(outdir / OUT_DIR, DATASET, season)
    source = (
        str(local)
        if local.is_file()
        else f"{url}/{OUT_DIR}/{DATASET}/season={season}/data.parquet"
    )
    try:
        row = duckdb.sql(
            f"SELECT max(built) FROM read_parquet('{source}', hive_partitioning = false)"
        ).fetchone()
    except duckdb.HTTPException as e:
        if getattr(e, "status_code", None) in (403, 404):
            return None
        raise
    return row[0] if row else None


def update(outdir: Path) -> None:
    """
    Fetch the ratings, and if they're a version we haven't published, the
    decomposition, then build every season
    """
    text = fetch("ratings")
    ratings = json.loads(text)
    built = built_of(ratings, "ratings")
    latest = max(r["season"] for r in rating_rows(ratings))
    if published_built(outdir, latest) == built:
        print(f"openrapm: the ratings built {built} are already published")
        return
    save_raw(raw_path(outdir, built, "ratings"), text)

    decompose = None
    try:
        text = fetch("decompose_openrapm")
        decompose = json.loads(text)
        save_raw(raw_path(outdir, built, "decompose_openrapm"), text)
    except (OSError, ValueError) as e:
        print(f"openrapm: couldn't get the decomposition: {e!r}")
    build(outdir, ratings, decompose)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="openrapm ratings")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="rebuild every season from the newest saved files",
    )
    args = parser.parse_args(argv)

    if not args.no_fetch:
        update(args.out)
        return
    docs = latest_raw(args.out)
    if "ratings" not in docs:
        raise FileNotFoundError("the newest saved openrapm version has no ratings")
    build(args.out, docs["ratings"], docs.get("decompose_openrapm"))


if __name__ == "__main__":
    main()
