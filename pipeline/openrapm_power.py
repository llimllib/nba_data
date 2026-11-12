"""
Fetch OpenRAPM's team power ratings and build

    nba/openrapm/team_openrapm/season=<season>/data.parquet
    nba/openrapm/team_openrapm_daily/season=<season>/data.parquet

OpenRAPM's Power Ratings page (https://openrapm.com/power.html) rates each
team by its players' ratings, weighted by the share of possessions each is
expected to play, plus a correction from the team's recent games ("Last 10
Performance Over Expected"). Its data is docs/data/power.json in the site's
repo, current season only, rewritten by a daily commit:

    {"meta": {"season", "through": the last games' date, "built", "ready",
              "average_team", ...},
     "teams": [{"tm": NBA abbreviation, "name", "g": games counted (the
                last 10), "o", "d", "t", "players": [[name, NBA id or null,
                share of possessions (of 500) or null, o, d, t], ...]}, ...]}

The row with a null share is the recent-games correction, counted as it is;
a player's row counts its share / 100 times its ratings ("Other players" is
a player row with a null id). A team's o, d and t are the sum of its rows'
counts, which the parser checks, and every team must have the correction
row. That's the method published 2026-11-09 (its first file is through
2026-11-08): the files before it had no correction row (a team was its
players alone, with its last 5 games' shares counted double), so they're
rejected rather than mixed into one history. The 30 teams' ratings average
0.

`team_openrapm` is the latest day, `team_openrapm_daily` every saved day,
current season only (a run in CI downloads only the current season's saved
days). A day is the date of the last games counted, `through`. Since every
day's file is in the repo's history, each run also saves any days since the
newest one we have from the commits that changed power.json, so a day a
run failed is filled in by the next, and the first run backfills the season
(from 2026-11-08, the first file in the current method). Raw files, one per
day (a day rebuilt later is replaced), are at

    nba/raw/openrapm/power/<season>/<through>.json.gz

and a file that doesn't parse at nba/raw/openrapm/power/unparsed/<date>.json.gz

usage: python -m pipeline.openrapm_power [--out out] [--no-fetch]
"""

import argparse
import gzip
import http.client
import json
import os
import time
import traceback
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa

from .catalog import URL
from .openrapm import BASE_URL, RETRY_DELAYS, USER_AGENT
from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import current_season, season_window, today_eastern

FILE = "docs/data/power.json"
COMMITS_URL = "https://api.github.com/repos/bbstats/openrapm/commits"
RAW_URL = "https://raw.githubusercontent.com/bbstats/openrapm"
RAW_DIR = Path("nba/raw/openrapm/power")
OUT_DIR = Path("nba/openrapm")
STATS_DIR = Path("nba/stats")

COLUMNS = [
    ("season", "INTEGER"),
    ("through_date", "DATE"),
    ("built", "DATE"),
    ("ready", "BOOLEAN"),
    ("team_id", "VARCHAR"),
    ("games", "INTEGER"),
    ("o_rating", "DOUBLE"),
    ("d_rating", "DOUBLE"),
    ("rating", "DOUBLE"),
    ("o_recent", "DOUBLE"),
    ("d_recent", "DOUBLE"),
    ("recent", "DOUBLE"),
]
PA_TYPES = {
    "INTEGER": pa.int32(),
    "DOUBLE": pa.float64(),
    "VARCHAR": pa.string(),
    "DATE": pa.date32(),
    "BOOLEAN": pa.bool_(),
}

# there have been 29 or 30 teams; more means we're not reading what we
# think we are
MAX_TEAMS = 32
# every number is rounded to two decimals, so a team's rows add up to within
# a couple hundredths of its rating; the files before the current method
# were off by up to 0.4
TOLERANCE = 0.06
# the commits API's page size; a gap longer than this many commits is filled
# with the newest ones
MAX_COMMITS = 100


def number(value, label: str) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"openrapm power: {label} {value!r} isn't a number")


def meta_of(doc: dict) -> dict:
    """
    The file's season, through date, built date and ready flag. Raises
    ValueError if they aren't there
    """
    meta = doc.get("meta") if isinstance(doc, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    try:
        through = date.fromisoformat(meta["through"])
        built = date.fromisoformat(meta["built"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(
            f"openrapm power: no valid meta.through and meta.built in {meta!r:.200}"
        ) from e
    season = meta.get("season")
    if type(season) is not int:
        raise ValueError(f"openrapm power: meta.season {season!r}")
    ready = meta.get("ready")
    if ready is not None and not isinstance(ready, bool):
        raise ValueError(f"openrapm power: meta.ready {ready!r}")
    return {"season": season, "through_date": through, "built": built, "ready": ready}


def team_row(team: dict, meta: dict, team_ids: dict[str, str]) -> dict:
    """One team's row. Raises ValueError if it isn't as expected"""
    if (
        not isinstance(team, dict)
        or {"tm", "g", "o", "d", "t", "players"} - team.keys()
    ):
        raise ValueError(f"openrapm power: malformed team {team!r:.200}")
    tm = team["tm"]
    if tm not in team_ids:
        raise ValueError(f"openrapm power: unknown team {tm!r} in {meta['season']}")
    games = team["g"]
    if not isinstance(games, int) or isinstance(games, bool) or games < 0:
        raise ValueError(f"openrapm power: {tm} games {games!r}")
    rating = {k: number(team[k], f"{tm} {k}") for k in "odt"}

    players = team["players"]
    if not isinstance(players, list) or not players:
        raise ValueError(f"openrapm power: {tm} players {players!r:.200}")
    sums = {k: 0.0 for k in "odt"}
    recent = None
    for p in players:
        if not isinstance(p, list) or len(p) != 6:
            raise ValueError(f"openrapm power: {tm} malformed row {p!r:.200}")
        name, _, share = p[:3]
        values = dict(zip("odt", (number(v, f"{tm} {name} rating") for v in p[3:])))
        if share is None:
            if recent is not None:
                raise ValueError(f"openrapm power: {tm} has two rows with no share")
            recent = values
            weight = 1.0
        else:
            weight = number(share, f"{tm} {name} share") / 100
        for k in "odt":
            sums[k] += weight * values[k]
    if recent is None:
        raise ValueError(
            f"openrapm power: {tm} has no recent-games row; the method has changed"
        )
    for k in "odt":
        if abs(sums[k] - rating[k]) > TOLERANCE:
            raise ValueError(
                f"openrapm power: {tm}'s rows add up to {k} {sums[k]:.2f}, not "
                f"{rating[k]}; the method has changed"
            )
    if abs(rating["o"] + rating["d"] - rating["t"]) > TOLERANCE:
        raise ValueError(f"openrapm power: {tm} o + d isn't t: {rating}")

    return {
        **meta,
        "team_id": team_ids[tm],
        "games": games,
        "o_rating": rating["o"],
        "d_rating": rating["d"],
        "rating": rating["t"],
        "o_recent": recent["o"],
        "d_recent": recent["d"],
        "recent": recent["t"],
    }


def power_rows(doc: dict, team_ids: dict[str, str]) -> list[dict]:
    """
    The file's teams as dicts of our columns. `team_ids` maps the season's
    abbreviations to team ids. Raises ValueError if the file isn't as
    expected, so nothing is written from a changed file
    """
    meta = meta_of(doc)
    teams = doc.get("teams")
    if not isinstance(teams, list) or not 0 < len(teams) <= MAX_TEAMS:
        raise ValueError(
            f"openrapm power: expected 1 to {MAX_TEAMS} teams, got "
            f"{len(teams) if isinstance(teams, list) else type(teams).__name__}"
        )
    return [team_row(t, meta, team_ids) for t in teams]


def team_ids_for(outdir: Path, season: int, url: str = URL) -> dict[str, str]:
    """
    The season's NBA abbreviations -> team ids, from team_seasons in
    `outdir` if it has the file, else in the bucket at `url`. Raises
    ValueError if neither has it
    """
    local = dataset_path(outdir / STATS_DIR, "team_seasons", season)
    source = (
        str(local)
        if local.is_file()
        else f"{url}/{STATS_DIR}/team_seasons/season={season}/data.parquet"
    )
    try:
        rows = duckdb.sql(
            f"SELECT nba_abbrev, team_id FROM read_parquet('{source}', "
            "hive_partitioning = false) WHERE is_nba"
        ).fetchall()
    except duckdb.HTTPException as e:
        # Spaces answers 403 for a file that doesn't exist
        if getattr(e, "status_code", None) in (403, 404):
            raise ValueError(f"openrapm power: no team_seasons for {season}") from e
        raise
    return dict(rows)


# --- building ----------------------------------------------------------------


def write_table(
    outdir: Path, dataset: str, season: int, rows: list[dict], keys: list[str]
) -> Path:
    table = pa.table(
        {name: pa.array([r[name] for r in rows], PA_TYPES[t]) for name, t in COLUMNS}
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute(
        f"CREATE TABLE {dataset} AS SELECT * FROM arrow ORDER BY {', '.join(keys)}"
    )
    check_keys(con, dataset, keys, f"openrapm power {dataset}")
    path = dataset_path(outdir / OUT_DIR, dataset, season)
    write_parquet(con, dataset, path)
    print(f"openrapm power: wrote {path} ({len(rows)} rows)")
    return path


def build_latest(outdir: Path, doc: dict, team_ids: dict[str, str]) -> Path:
    """Write the season's latest ratings, `team_openrapm`, from one file"""
    rows = power_rows(doc, team_ids)
    return write_table(outdir, "team_openrapm", rows[0]["season"], rows, ["team_id"])


def build_daily(outdir: Path, season: int, team_ids: dict[str, str]) -> Path | None:
    """
    Write `team_openrapm_daily` from every saved day of `season`. Returns
    the path, or None if there's no saved day
    """
    rows = []
    for _, path in saved_days(outdir, season):
        doc = read_raw(path)
        if meta_of(doc)["season"] != season:
            raise ValueError(f"openrapm power: {path} is for another season")
        rows += power_rows(doc, team_ids)
    if not rows:
        return None
    return write_table(
        outdir, "team_openrapm_daily", season, rows, ["through_date", "team_id"]
    )


# --- raw files ---------------------------------------------------------------


def raw_path(outdir: Path, season: int | str, day: date | str) -> Path:
    return outdir / RAW_DIR / str(season) / f"{day}.json.gz"


def saved_days(outdir: Path, season: int) -> list[tuple[date, Path]]:
    """`season`'s saved days, oldest first"""
    paths = (outdir / RAW_DIR / str(season)).glob("*.json.gz")
    return sorted((date.fromisoformat(p.name.split(".")[0]), p) for p in paths)


def save_raw(path: Path, text: str) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            f.write(text)

    write_atomic(path, write)


def read_raw(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


# --- fetching ----------------------------------------------------------------


def get(url: str, headers: dict | None = None, sleep=time.sleep) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    for attempt, delay in enumerate([*RETRY_DELAYS, None]):
        print(f"openrapm power: fetching {url}", flush=True)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read().decode("utf-8")
        # a dropped connection mid-file is an http.client.IncompleteRead
        except (OSError, http.client.HTTPException) as e:
            if delay is None:
                raise
            print(
                f"openrapm power: attempt {attempt + 1} failed, retrying in "
                f"{delay}s: {e!r}"
            )
            sleep(delay)
    raise AssertionError("unreachable")


def commits_since(since: date) -> list[str]:
    """
    The shas of the commits that changed power.json since `since`, newest
    first. Uses $GITHUB_TOKEN if it's set: the API allows 60 requests an hour
    without one, per IP, which CI's runners share
    """
    query = urllib.parse.urlencode(
        {"path": FILE, "since": f"{since}T00:00:00Z", "per_page": MAX_COMMITS}
    )
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    commits = json.loads(get(f"{COMMITS_URL}?{query}", headers))
    if not isinstance(commits, list) or not all(isinstance(c, dict) for c in commits):
        raise ValueError(f"openrapm power: commits {commits!r:.200}")
    return [c["sha"] for c in commits]


def fill_in(
    outdir: Path, season: int, team_ids: dict[str, str], fetch=get, commits=None
) -> list[date]:
    """
    Save the days of `season` since the newest one saved, from the commits
    that changed power.json. A day built more than once keeps its newest
    build; a file that doesn't parse, from before the current method, or
    for another season is skipped. Returns the days saved
    """
    commits = commits or commits_since
    saved = saved_days(outdir, season)
    # a day's commit is made the next morning, so this lists the newest
    # saved day's commit again, and anything that rebuilt it
    since = saved[-1][0] if saved else season_window(season)[0]
    seen, added = set(), []
    for sha in commits(since):
        text = fetch(f"{RAW_URL}/{sha}/{FILE}")
        try:
            doc = json.loads(text)
            rows = power_rows(doc, team_ids)
        except ValueError as e:
            print(f"openrapm power: skipping {sha[:10]}: {e}")
            continue
        day = rows[0]["through_date"]
        if rows[0]["season"] != season or day in seen:
            continue
        seen.add(day)
        save_raw(raw_path(outdir, season, day), text)
        added.append(day)
    if added:
        saved_list = ", ".join(d.isoformat() for d in sorted(added))
        print(f"openrapm power: saved {saved_list} from the repo's history")
    return added


def update(outdir: Path, today: date, fetch=get, commits=None) -> None:
    """
    Fetch today's ratings, fill in any days missing since the newest saved
    one from the repo's history, and build both datasets. The history is a
    convenience: if it fails, today's ratings are still written
    """
    text = fetch(f"{BASE_URL}/power.json")
    try:
        doc = json.loads(text)
        season = meta_of(doc)["season"]
        team_ids = team_ids_for(outdir, season)
        power_rows(doc, team_ids)
    except ValueError:
        save_raw(outdir / RAW_DIR / "unparsed" / f"{today}.json.gz", text)
        raise
    daily = season == current_season(today)
    if daily:
        try:
            fill_in(outdir, season, team_ids, fetch, commits)
        except (OSError, http.client.HTTPException, ValueError, KeyError) as e:
            traceback.print_exception(e)
            print("openrapm power: couldn't fill in from the repo's history")
    # the main branch's file is the newest build of its day
    save_raw(raw_path(outdir, season, meta_of(doc)["through_date"]), text)
    build_latest(outdir, doc, team_ids)
    if daily:
        build_daily(outdir, season, team_ids)
    else:
        print(f"openrapm power: the site is on {season}; no daily history kept")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="OpenRAPM team power ratings")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="rebuild every season from the saved files",
    )
    args = parser.parse_args(argv)

    if not args.no_fetch:
        update(args.out, today_eastern())
        return
    for season_dir in sorted((args.out / RAW_DIR).glob("[0-9][0-9][0-9][0-9]")):
        season = int(season_dir.name)
        days = saved_days(args.out, season)
        if not days:
            continue
        team_ids = team_ids_for(args.out, season)
        build_latest(args.out, read_raw(days[-1][1]), team_ids)
        build_daily(args.out, season, team_ids)


if __name__ == "__main__":
    main()
