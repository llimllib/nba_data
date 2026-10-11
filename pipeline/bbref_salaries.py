"""
Player salaries from basketball-reference's team season pages, as

    nba/bbref/bbref_salaries/season=<season>/data.parquet

one row per player per team per season, from each page's Salaries table
(#53). Pages are kept with the others at

    nba/raw/bbref/<season>/teams/<bbref abbrev>.html.gz

That's 30 pages a season, fetched through pipeline.bbref's Fetcher (6
seconds apart, no retries). Past seasons' pages are fetched once; the
current season's are refetched each run, since salaries change with
signings and trades.

usage: python -m pipeline.bbref_salaries [--out out] [--season 2026 ...] [--no-fetch]

Rows are keyed by (team_id, bbref_id), and every row is kept so team
payrolls add up, including players a team waived and still pays. player_id
is the NBA id, through player_ids.csv (bbref rows, see pipeline.player_ids)
or else a unique name in the season's player_seasons, and NULL for players
not in player_seasons that season (every player_id must be there): mostly
waived players who didn't play. More than MAX_UNPLACED of those fails the
build.
"""

import argparse
import gzip
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import duckdb
import pyarrow as pa

from . import bbref
from .bbref import normalize_name
from .catalog import URL
from .output import check_keys, dataset_path, write_parquet
from .player_ids import load_player_ids
from .seasons import current_season, today_eastern
from .teams import load_team_abbrevs

DATASET = "bbref_salaries"
STATS_DIR = Path("nba/stats")
TABLE = "salaries2"
REQUIRED = {"bbref_id", "player", "salary"}
PLAYER_HREF = re.compile(r"^/players/\w/(\w+)\.html$")
# more players without an NBA id than this in a season means something's
# wrong
MAX_UNPLACED = 60

COLUMNS = [
    ("season", "INTEGER"),
    ("team_id", "VARCHAR"),
    ("bbref_id", "VARCHAR"),
    ("player_id", "VARCHAR"),
    ("salary", "INTEGER"),
]


def team_page_path(outdir: Path, season: int, abbrev: str) -> Path:
    return outdir / bbref.RAW_DIR / str(season) / "teams" / f"{abbrev}.html.gz"


def team_page(
    fetcher: bbref.Pages | None, outdir: Path, season: int, abbrev: str, refresh: bool
) -> str | None:
    """a team's season page: saved, or fetched if `fetcher` is given"""
    raw = team_page_path(outdir, season, abbrev)
    if fetcher is None:
        if not raw.is_file():
            return None
        with gzip.open(raw, "rt") as f:
            return f.read()
    return bbref.cached(fetcher, f"/teams/{abbrev}/{season}.html", raw, refresh)


def salary(value: str) -> int | None:
    """ "$54,126,450" -> 54126450; "" (two-way contracts) -> None"""
    value = value.strip()
    if not value:
        return None
    if not re.fullmatch(r"\$[\d,]+", value):
        raise ValueError(f"{value!r} isn't a salary")
    return int(value[1:].replace(",", ""))


def team_salaries(html: str, label: str) -> list[tuple[str, str, int | None]]:
    """
    (bbref_id, name, salary) for each row of a team page's Salaries table.
    Raises ValueError if the table isn't as expected
    """
    rows = bbref.parse_table(html, TABLE)
    for r in rows:
        # before the season, the player cell has no data-append-csv, but its
        # link still has the id: /players/j/johnsja05.html
        if "bbref_id" not in r and (m := PLAYER_HREF.match(r.get("player_href", ""))):
            r["bbref_id"] = m[1]
    if not 5 <= len(rows) <= 60:
        raise ValueError(f"{label}: {len(rows)} salary rows, expected 5 to 60")
    out = []
    # players who never played in the NBA may have no bbref page, so no id
    # to key on: skip them, but not so many that the table must have changed
    unlinked = [r.get("player") for r in rows if "bbref_id" not in r]
    if unlinked:
        print(f"{label}: skipped players with no bbref id: {unlinked}")
    if len(unlinked) > 3:
        raise ValueError(f"{label}: {len(unlinked)} salary rows with no bbref id")
    for r in rows:
        if "bbref_id" not in r:
            continue
        missing = REQUIRED - r.keys()
        if missing:
            raise ValueError(f"{label}: salary row missing {sorted(missing)}: {r}")
        try:
            out.append((r["bbref_id"], r["player"], salary(r["salary"])))
        except ValueError as e:
            raise ValueError(f"{label} {r['player']}: {e}") from e
    if not any(s for _, _, s in out):
        raise ValueError(f"{label}: no salaries")
    return out


def season_players(outdir: Path, season: int, url: str = URL) -> list[tuple[str, str]]:
    """(player_id, name) in a season's player_seasons, from out/ or the bucket"""
    local = dataset_path(outdir / STATS_DIR, "player_seasons", season)
    source = (
        str(local)
        if local.is_file()
        else f"{url}/{STATS_DIR}/player_seasons/season={season}/data.parquet"
    )
    return duckdb.sql(
        f"SELECT player_id, name FROM read_parquet('{source}', hive_partitioning = false)"
    ).fetchall()


def resolve(
    salaries: Sequence[tuple[str, str, str, int | None]],
    known: list[tuple[str, str]],
    mapped: dict[str, str],
    label: str,
) -> list[dict]:
    """
    `salaries` as (team_id, bbref_id, name, salary) -> rows with NBA player
    ids, NULL for players we can't place in `known` (the season's
    player_seasons). `mapped` is bbref_id -> NBA id from player_ids.csv
    """
    in_season = {pid for pid, _ in known}
    by_name = defaultdict(set)
    for pid, name in known:
        if name:
            by_name[normalize_name(name)].add(pid)
    rows, unplaced = {}, set()
    for team_id, bbref_id, name, pay in salaries:
        pid = mapped.get(bbref_id)
        if pid is None and len(hits := by_name.get(normalize_name(name), set())) == 1:
            pid = next(iter(hits))
        if pid not in in_season:
            unplaced.add(f"{name} ({bbref_id})")
            pid = None
        # a player listed twice for a team keeps the larger salary
        key = (team_id, bbref_id)
        if key not in rows or (pay or 0) > (rows[key]["salary"] or 0):
            rows[key] = {
                "team_id": team_id,
                "bbref_id": bbref_id,
                "player_id": pid,
                "salary": pay,
            }
    if unplaced:
        print(f"{label}: not in player_seasons, player_id NULL: {sorted(unplaced)}")
    if len(unplaced) > MAX_UNPLACED:
        raise ValueError(f"{label}: {len(unplaced)} players not in player_seasons")
    return list(rows.values())


def build_season(
    fetcher: bbref.Pages | None, outdir: Path, season: int, refresh: bool
) -> Path | None:
    """
    Write a season's salaries. Returns None (and writes nothing) if any team's
    page is missing without a fetcher
    """
    teams = {
        a.abbrev: a.team_id
        for a in load_team_abbrevs()
        if a.source == "bbref" and a.covers(season)
    }
    salaries = []
    for abbrev, team_id in sorted(teams.items()):
        html = team_page(fetcher, outdir, season, abbrev, refresh)
        if html is None:
            print(f"bbref_salaries: no saved page for {abbrev} {season}")
            return None
        for bbref_id, name, pay in team_salaries(html, f"bbref {season} {abbrev}"):
            salaries.append((team_id, bbref_id, name, pay))

    mapped = {
        p.source_id: p.player_id
        for p in load_player_ids().values()
        if p.source == "bbref"
    }
    rows = resolve(
        salaries, season_players(outdir, season), mapped, f"bbref_salaries {season}"
    )
    types = {"INTEGER": pa.int32(), "VARCHAR": pa.string()}
    table = pa.table(
        {
            "season": pa.array([season] * len(rows), pa.int32()),
            **{
                name: pa.array([r[name] for r in rows], types[t])
                for name, t in COLUMNS[1:]
            },
        }
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute(
        f"CREATE TABLE {DATASET} AS SELECT * FROM arrow ORDER BY team_id, salary DESC NULLS LAST, player_id"
    )
    check_keys(con, DATASET, ["team_id", "bbref_id"], f"bbref {DATASET}")
    path = dataset_path(outdir / bbref.OUT_DIR, DATASET, season)
    write_parquet(con, DATASET, path)
    print(f"bbref_salaries: wrote {path} ({len(rows)} rows)")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="basketball-reference salaries")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season", type=int, action="append", help="default: current season"
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild from saved pages"
    )
    args = parser.parse_args(argv)
    current = current_season(today_eastern())
    fetcher = None if args.no_fetch else bbref.Fetcher()
    for season in args.season or [current]:
        build_season(fetcher, args.out, season, refresh=season == current)


if __name__ == "__main__":
    main()
