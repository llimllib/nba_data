"""
Fetch and parse Cleaning the Glass's public league summary page, and build

    nba/ctg/ctg_team_summary/season=<season>/data.parquet

with one row per team and season type (regular season and playoffs): record,
expected wins, offensive and defensive rating with garbage time removed, and
the same for the last two weeks.

usage: python -m pipeline.ctg [--out out] [--season 2026 ...] [--no-fetch]

The page is public for every season since 2003-04, at
/stats/league/summary?season=<start year>&seasontype=regseason|playoffs
(CTG names a season by its start year: 2025 is 2025-26, our 2026). The
current season is refetched each run; past seasons only once. Every page is
kept, gzipped, under the date it was fetched:

    nba/raw/ctg/<season>/team_summary_<season_type>/<date>.html.gz

and a season is built from its latest page, whose date is `as_of_date`. The
"last 2 weeks" columns are as of that date, so for the current season the
file is a daily snapshot.

Teams are identified by CTG's team ids (1 to 30, in the hrefs of the team
links), mapped to NBA team ids in team_abbrevs.csv (source "ctg"). Columns
are found by their header text, so a changed table degrades: a missing
optional column is NULL with a warning, and a missing required one, or an
unexpected row count, writes nothing.
"""

import argparse
import gzip
import re
import time
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

import duckdb
import pyarrow as pa

from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import current_season, today_eastern
from .teams import load_team_abbrevs

BASE_URL = "https://cleaningtheglass.com"
USER_AGENT = "nba_data (https://github.com/llimllib/nba_data)"
# CTG doesn't publish a limit; be polite
REQUEST_DELAY = 3
RAW_DIR = Path("nba/raw/ctg")
OUT_DIR = Path("nba/ctg")
DATASET = "ctg_team_summary"

# our season type -> CTG's seasontype parameter
SEASON_TYPES = {"regular_season": "regseason", "playoffs": "playoffs"}
# and how the page's heading names it: <h2>2025-26 regular season</h2>
HEADINGS = {"regular_season": "regular season", "playoffs": "playoffs"}
# what the page says instead of the table before a season type starts
NO_DATA = "no data matching the filters"

# the table's headers -> our column names and types. The headers after the
# "Last 2 Weeks" section's spacer get LAST_2WK appended
HEADERS = {
    "Point Diff": ("point_diff", "DOUBLE"),
    "W": ("wins", "INTEGER"),
    "L": ("losses", "INTEGER"),
    "Win%": ("win_pct", "DOUBLE"),
    "Exp W82": ("exp_wins_82", "DOUBLE"),
    "Exp W": ("exp_wins", "DOUBLE"),
    "Win Diff": ("win_diff", "DOUBLE"),
    "Offense": ("off_rtg", "DOUBLE"),
    "Defense": ("def_rtg", "DOUBLE"),
    # only on regular-season and playoff pages
    "Spread Diff": ("spread_diff", "DOUBLE"),
}
LAST_2WK = "_last_2wk"
LAST_2WK_HEADERS = ["W", "L", "Point Diff", "Offense", "Defense", "Spread Diff"]

# a team row is useless without these, so a missing column or value fails
# the build; any other missing column is NULL, with a warning
REQUIRED = {"wins", "losses", "off_rtg", "def_rtg"}

COLUMNS = [
    ("season", "INTEGER"),
    ("season_type", "VARCHAR"),
    ("team_id", "VARCHAR"),
    ("as_of_date", "DATE"),
    *HEADERS.values(),
    *[(HEADERS[h][0] + LAST_2WK, HEADERS[h][1]) for h in LAST_2WK_HEADERS],
]
NAMES = [name for name, _ in COLUMNS]

TEAM_HREF = re.compile(r"^/stats/team/(\d+)/")


class SummaryParser(HTMLParser):
    """
    The league_summary table: its second header row's texts, and for each
    body row the team link's href and the texts of the cells whose class
    includes "value" (the others are ranks, logos and spacers)
    """

    def __init__(self):
        super().__init__()
        self.in_table = self.in_head = self.in_body = False
        self.head_rows: list[list[str]] = []
        self.rows: list[dict] = []
        self.row: dict | None = None
        self.cell: str | None = None  # "th", "team", "value" or None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "table" and a.get("id") == "league_summary":
            self.in_table = True
        elif not self.in_table:
            return
        elif tag == "thead":
            self.in_head = True
        elif tag == "tbody":
            self.in_body = True
        elif tag == "tr" and self.in_head:
            self.head_rows.append([])
        elif tag == "tr" and self.in_body:
            self.row = {"href": None, "name": None, "values": []}
        elif tag == "th" and self.in_head:
            self.cell, self.text = "th", []
        elif tag == "td" and self.row is not None:
            if "team_name" in classes:
                self.cell = "team"
            elif "value" in classes:
                self.cell = "value"
            else:
                self.cell = None
            self.text = []
        elif tag == "a" and self.cell == "team" and self.row is not None:
            self.row["href"] = a.get("href")

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag == "table":
            self.in_table = False
        elif tag == "thead":
            self.in_head = False
        elif tag == "tbody":
            self.in_body = False
        elif tag == "th" and self.cell == "th" and self.head_rows:
            self.head_rows[-1].append("".join(self.text).strip())
            self.cell = None
        elif tag == "td" and self.row is not None and self.cell:
            text = " ".join("".join(self.text).split())
            if self.cell == "team":
                self.row["name"] = text
            else:
                self.row["values"].append(text)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell:
            self.text.append(data)


def column_names(headers: list[str]) -> list[str | None]:
    """
    The column for each value cell, from the header row: the headers after
    the empty spacer header are the last two weeks'. None for a header we
    don't know
    """
    names: list[str | None] = []
    suffix = ""
    for h in headers:
        if h == "Team":
            continue
        if not h:
            suffix = LAST_2WK
            continue
        names.append(HEADERS[h][0] + suffix if h in HEADERS else None)
    return names


def number(value: str, type_: str):
    """
    A cell's value: "+19.0", "1,234", "62.5%" (a fraction, 0.625), or "--"
    or "" for none
    """
    value = value.replace(",", "").strip()
    if value in ("", "--"):
        return None
    if value.endswith("%"):
        # rounded, so 75.6% is 0.756 and not 0.7559999999999999
        return round(float(value[:-1]) / 100, 6)
    return int(value) if type_ == "INTEGER" else float(value)


def team_summary(
    html: str, season: int, season_type: str, team_ids: dict[str, str]
) -> list[dict]:
    """
    One row per team from a summary page. `team_ids` maps CTG's team ids
    that season to NBA team ids. Teams with no games are left out (the
    playoffs page before the playoffs). Raises ValueError if the page isn't
    as expected, so nothing is written from a changed page
    """
    label = f"ctg {season} {season_type}"
    # CTG's seasons are named by their start year
    heading = f"<h2>{season - 1}-{season % 100:02d} {HEADINGS[season_type]}</h2>"
    if heading not in html:
        found = re.findall(r"<h2>[^<]*</h2>", html)[:1]
        raise ValueError(f"{label}: expected the heading {heading}, found {found}")
    parser = SummaryParser()
    parser.feed(html)
    if not parser.head_rows and NO_DATA in html:
        return []
    if len(parser.head_rows) < 2:
        raise ValueError(f"{label}: no league_summary table with two header rows")
    headers = parser.head_rows[1]
    names = column_names(headers)
    found = {n for n in names if n}
    unknown = [h for h in headers if h and h != "Team" and h not in HEADERS]
    if unknown:
        print(f"{label}: unknown columns, ignored: {unknown}")
    missing = REQUIRED - found
    if missing:
        raise ValueError(f"{label}: no {sorted(missing)} column; headers: {headers}")
    absent = [n for n in NAMES[4:] if n not in found]
    if absent:
        print(f"{label}: columns missing, left NULL: {absent}")
    if not 0 < len(parser.rows) <= 30:
        raise ValueError(f"{label}: {len(parser.rows)} team rows, expected 1 to 30")

    types = dict(COLUMNS)
    season_columns = sum(1 for n in names if not (n or "").endswith(LAST_2WK))
    rows = []
    for r in parser.rows:
        m = TEAM_HREF.match(r["href"] or "")
        if not m:
            raise ValueError(f"{label}: team row without a team link: {r}")
        if m[1] not in team_ids:
            raise ValueError(
                f"{label}: no team_id for ctg team {m[1]} ({r['name']}); "
                "add it to team_abbrevs.csv"
            )
        # a team with no games in the last two weeks (eliminated from the
        # playoffs, say) has no cells for them
        if len(r["values"]) not in (len(names), season_columns):
            raise ValueError(
                f"{label} {r['name']}: {len(r['values'])} values for "
                f"{len(names)} columns"
            )
        row = dict.fromkeys(NAMES)
        row |= {"season": season, "season_type": season_type, "team_id": team_ids[m[1]]}
        for name, value in zip(names, r["values"], strict=False):
            if name is None:
                continue
            try:
                row[name] = number(value, types[name])
            except ValueError as e:
                raise ValueError(
                    f"{label} {r['name']}: {name} {value!r} isn't a number"
                ) from e
        if not (row["wins"] or row["losses"]):
            continue
        nulls = sorted(n for n in REQUIRED if row[n] is None)
        if nulls:
            raise ValueError(f"{label} {r['name']}: no value for {nulls}")
        rows.append(row)
    if season_type == "regular_season" and rows and len(rows) != 30:
        raise ValueError(f"{label}: {len(rows)} teams have played, expected 30")
    return rows


def page_path(season: int, season_type: str) -> str:
    return (
        f"/stats/league/summary?season={season - 1}"
        f"&seasontype={SEASON_TYPES[season_type]}"
    )


class Pages(Protocol):
    def get(self, path: str) -> str: ...


class Fetcher:
    """Fetches pages no faster than one per REQUEST_DELAY seconds"""

    def __init__(self, delay: float = REQUEST_DELAY, sleep=time.sleep, clock=time.time):
        self.delay = delay
        self.sleep = sleep
        self.clock = clock
        self.last = 0.0

    def get(self, path: str) -> str:
        wait = self.last + self.delay - self.clock()
        if wait > 0:
            self.sleep(wait)
        print(f"ctg: fetching {path}", flush=True)
        req = urllib.request.Request(
            BASE_URL + path, headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read().decode("utf-8")
        finally:
            self.last = self.clock()


def raw_dir(outdir: Path, season: int, season_type: str) -> Path:
    return outdir / RAW_DIR / str(season) / f"team_summary_{season_type}"


def save_raw(path: Path, html: str) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            f.write(html)

    write_atomic(path, write)


def latest_page(outdir: Path, season: int, season_type: str) -> tuple[date, str] | None:
    """the newest saved page for a season type, and the date it was fetched"""
    saved = sorted(raw_dir(outdir, season, season_type).glob("*.html.gz"))
    if not saved:
        return None
    with gzip.open(saved[-1], "rt") as f:
        return date.fromisoformat(saved[-1].name.removesuffix(".html.gz")), f.read()


def season_pages(
    fetcher: Pages | None, outdir: Path, season: int, today: date, refresh: bool
) -> dict[str, tuple[date, str]]:
    """
    season type -> (date fetched, page). Fetches a page if `refresh` or if
    none is saved, unless `fetcher` is None
    """
    pages = {}
    for season_type in SEASON_TYPES:
        saved = latest_page(outdir, season, season_type)
        if fetcher and (refresh or saved is None):
            html = fetcher.get(page_path(season, season_type))
            save_raw(raw_dir(outdir, season, season_type) / f"{today}.html.gz", html)
            saved = (today, html)
        if saved:
            pages[season_type] = saved
    return pages


def build_season(
    outdir: Path, season: int, pages: dict[str, tuple[date, str]]
) -> Path | None:
    """
    Write a season's team summary from its pages. Returns the path, or None
    if no regular-season games have been played
    """
    team_ids = {
        a.abbrev: a.team_id
        for a in load_team_abbrevs()
        if a.source == "ctg" and a.covers(season)
    }
    rows = []
    for season_type, (fetched, html) in pages.items():
        for row in team_summary(html, season, season_type, team_ids):
            rows.append(row | {"as_of_date": fetched})
    if not any(r["season_type"] == "regular_season" for r in rows):
        print(f"ctg: no regular-season games in {season} yet")
        return None

    types = {
        "INTEGER": pa.int32(),
        "DOUBLE": pa.float64(),
        "VARCHAR": pa.string(),
        "DATE": pa.date32(),
    }
    table = pa.table(
        {name: pa.array([r[name] for r in rows], types[t]) for name, t in COLUMNS}
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute(
        f"CREATE TABLE {DATASET} AS SELECT * FROM arrow ORDER BY season_type, team_id"
    )
    check_keys(con, DATASET, ["season_type", "team_id"], f"ctg {DATASET}")
    path = dataset_path(outdir / OUT_DIR, DATASET, season)
    write_parquet(con, DATASET, path)
    print(f"ctg: wrote {path} ({len(rows)} rows)")
    return path


def saved_seasons(outdir: Path) -> list[int]:
    return sorted(
        int(p.name)
        for p in (outdir / RAW_DIR).glob("[0-9][0-9][0-9][0-9]")
        if p.is_dir()
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Cleaning the Glass team summary")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season", type=int, action="append", help="default: current season"
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="only rebuild from saved pages (every saved season, without --season)",
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    current = current_season(today)
    fetcher = None if args.no_fetch else Fetcher()
    default = saved_seasons(args.out) if args.no_fetch else [current]
    for season in args.season or default:
        pages = season_pages(fetcher, args.out, season, today, season == current)
        if "regular_season" not in pages:
            raise SystemExit(f"ctg: no saved pages for {season}")
        build_season(args.out, season, pages)


if __name__ == "__main__":
    main()
