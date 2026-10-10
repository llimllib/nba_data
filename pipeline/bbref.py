"""
Fetch and parse basketball-reference.com pages, and build team season stats:

    nba/bbref/bbref_team_stats/season=<season>/data.parquet

from each season's league page (NBA_<season>.html). Past seasons' pages are
fetched once; the current season's is refetched each run.

usage: python -m pipeline.bbref [--out out] [--season 2026 ...] [--no-fetch]

basketball-reference blocks clients that make more than ~20 requests a
minute, for up to a day, and its robots.txt asks for 3 seconds between
requests. So we wait REQUEST_DELAY seconds between requests, stop on the
first HTTP error rather than retrying, and keep every page we fetch,
gzipped, at

    nba/raw/bbref/<season>/<page>.html.gz      season pages (league, per_game)
    nba/raw/bbref/players/<bbref_id>.html.gz   player pages

so a page is never fetched twice (except the current season's, which
changes).
"""

import argparse
import gzip
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

import duckdb
import pyarrow as pa

from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import current_season, today_eastern
from .teams import load_team_abbrevs

BASE_URL = "https://www.basketball-reference.com"
USER_AGENT = "nba_data (https://github.com/llimllib/nba_data)"
# robots.txt asks for 3; be more conservative than that
REQUEST_DELAY = 6
RAW_DIR = Path("nba/raw/bbref")


class Pages(Protocol):
    def get(self, path: str) -> str: ...


class RateLimited(Exception):
    pass


class Fetcher:
    """Fetches pages no faster than one per REQUEST_DELAY seconds"""

    def __init__(self, delay: float = REQUEST_DELAY, sleep=time.sleep, clock=time.time):
        self.delay = delay
        self.sleep = sleep
        self.clock = clock
        self.last = 0.0
        self.count = 0

    def get(self, path: str) -> str:
        wait = self.last + self.delay - self.clock()
        if wait > 0:
            self.sleep(wait)
        print(f"bbref: fetching {path}", flush=True)
        req = urllib.request.Request(
            BASE_URL + path, headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # never retry: retrying a 429 is how you get blocked for a day
            if e.code == 429:
                raise RateLimited(
                    f"bbref rate limited us on {path}; don't retry for at least an "
                    f"hour (Retry-After: {e.headers.get('Retry-After')})"
                ) from e
            raise
        finally:
            self.last = self.clock()
            self.count += 1


def cached(fetcher: Pages, path: str, raw: Path, refresh: bool = False) -> str:
    """the page at `path`, from `raw` if we have it, else fetched and saved there"""
    if raw.is_file() and not refresh:
        with gzip.open(raw, "rt") as f:
            return f.read()
    html = fetcher.get(path)

    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            f.write(html)

    write_atomic(raw, write)
    return html


def season_page_path(outdir: Path, season: int, page: str) -> Path:
    return outdir / RAW_DIR / str(season) / f"{page}.html.gz"


def player_page_path(outdir: Path, bbref_id: str) -> Path:
    return outdir / RAW_DIR / "players" / f"{bbref_id}.html.gz"


def per_game_page(fetcher: Fetcher, outdir: Path, season: int, refresh=False) -> str:
    return cached(
        fetcher,
        f"/leagues/NBA_{season}_per_game.html",
        season_page_path(outdir, season, "per_game"),
        refresh,
    )


def player_page(fetcher: Fetcher, outdir: Path, bbref_id: str) -> str:
    return cached(
        fetcher,
        f"/players/{bbref_id[0]}/{bbref_id}.html",
        player_page_path(outdir, bbref_id),
    )


class TableParser(HTMLParser):
    """
    The body rows of the table with id `table_id`, as dicts keyed by each
    cell's data-stat attribute, holding the cell's text. The player cell's
    data-append-csv attribute, basketball-reference's player id, is stored
    as bbref_id, and the first link in a cell as <stat>_href. Tables hidden in HTML comments (bbref reveals some with
    javascript) are parsed too
    """

    def __init__(self, table_id: str):
        super().__init__()
        self.table_id = table_id
        self.in_table = self.in_body = False
        self.rows: list[dict[str, str]] = []
        self.row: dict[str, str] | None = None
        self.stat: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == self.table_id:
            self.in_table = True
        elif not self.in_table:
            return
        elif tag == "tbody":
            self.in_body = True
        elif tag == "tr" and self.in_body:
            # bbref repeats header rows inside the body
            self.row = None if "thead" in (a.get("class") or "") else {}
        elif tag in ("td", "th") and self.row is not None:
            self.stat = a.get("data-stat")
            self.text = []
            if self.stat and a.get("data-append-csv"):
                self.row["bbref_id"] = a["data-append-csv"] or ""
        elif tag == "a" and self.row is not None and self.stat:
            self.row.setdefault(f"{self.stat}_href", a.get("href") or "")

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag == "table":
            self.in_table = self.in_body = False
        elif tag in ("td", "th") and self.row is not None and self.stat:
            self.row[self.stat] = "".join(self.text).strip()
            self.stat = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.stat:
            self.text.append(data)

    def handle_comment(self, data):
        if f'id="{self.table_id}"' in data:
            inner = TableParser(self.table_id)
            inner.feed(data)
            self.rows.extend(inner.rows)


def parse_table(html: str, table_id: str) -> list[dict[str, str]]:
    parser = TableParser(table_id)
    parser.feed(html)
    return parser.rows


@dataclass(frozen=True)
class SeasonPlayer:
    """a player on a team in a season, as bbref lists them"""

    bbref_id: str
    name: str
    team: str  # bbref's abbreviation that season


# bbref's rows for a traded player's totals: "2TM", "3TM", ... (older pages
# use "TOT")
TOTAL_ROW = re.compile(r"^(\dTM|TOT)$")

# the tables that list players on a season page, and their required columns
PER_GAME_TABLES = ["per_game_stats", "per_game_stats_post"]
REQUIRED = {"bbref_id", "name_display", "team_name_abbr"}


def season_players(html: str) -> list[SeasonPlayer]:
    """
    Every (player, team) in a season's per-game page, regular season and
    playoffs. Raises ValueError if the page doesn't look as expected, so a
    markup change fails loudly rather than producing an empty mapping
    """
    rows = parse_table(html, PER_GAME_TABLES[0])
    if len(rows) < 300:
        raise ValueError(f"bbref per_game_stats: only {len(rows)} rows")
    rows += parse_table(html, PER_GAME_TABLES[1])
    players = set()
    for r in rows:
        missing = REQUIRED - r.keys()
        if missing:
            # the league-average row at the bottom has no player
            if "bbref_id" in missing and "League Average" in r.get("name_display", ""):
                continue
            raise ValueError(f"bbref per-game row missing {sorted(missing)}: {r}")
        if TOTAL_ROW.match(r["team_name_abbr"]):
            continue
        players.add(SeasonPlayer(r["bbref_id"], r["name_display"], r["team_name_abbr"]))
    return sorted(players, key=lambda p: (p.bbref_id, p.team))


def nba_id_from_player_page(html: str) -> str | None:
    """
    The NBA player id a bbref player page links to (its stats.nba.com
    link), or None if there isn't one, as for players who never played in
    the NBA
    """
    ids = set(re.findall(r"(?:stats\.)?nba\.com/(?:stats/)?player/(\d+)", html))
    if len(ids) > 1:
        raise ValueError(f"bbref player page links to several NBA ids: {sorted(ids)}")
    return ids.pop() if ids else None


def normalize_name(name: str) -> str:
    """
    A player's name for matching across sources: no accents, punctuation or
    generational suffixes, lowercase.

    normalize_name("Nikola Jokić") -> "nikola jokic"
    normalize_name("Kevin Porter Jr.") -> "kevin porter"
    """
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c)).lower()
    name = re.sub(r"[.'’,]", "", name).replace("-", " ")
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)$", "", name.strip())
    return " ".join(name.split())


# --- team season stats -------------------------------------------------------

OUT_DIR = Path("nba/bbref")

# the league page's tables, and the columns kept from each (bbref's names).
# Every season has the same columns, which keeps the catalog's views pruning
TEAM_TABLES = {
    "totals-team": [
        "g", "mp", "fg", "fga", "fg_pct", "fg3", "fg3a", "fg3_pct", "fg2", "fg2a",
        "fg2_pct", "ft", "fta", "ft_pct", "orb", "drb", "trb", "ast", "stl", "blk",
        "tov", "pf", "pts",
    ],
    "totals-opponent": [
        "opp_fg", "opp_fga", "opp_fg_pct", "opp_fg3", "opp_fg3a", "opp_fg3_pct",
        "opp_fg2", "opp_fg2a", "opp_fg2_pct", "opp_ft", "opp_fta", "opp_ft_pct",
        "opp_orb", "opp_drb", "opp_trb", "opp_ast", "opp_stl", "opp_blk", "opp_tov",
        "opp_pf", "opp_pts",
    ],
    "advanced-team": [
        "age", "wins", "losses", "wins_pyth", "losses_pyth", "mov", "sos", "srs",
        "off_rtg", "def_rtg", "net_rtg", "pace", "fta_per_fga_pct", "fg3a_per_fga_pct",
        "ts_pct", "efg_pct", "tov_pct", "orb_pct", "ft_rate", "opp_efg_pct",
        "opp_tov_pct", "drb_pct", "opp_ft_rate", "arena_name", "attendance",
        "attendance_per_g",
    ],
    "shooting-team": [
        "avg_dist", "pct_fga_fg2a", "pct_fga_00_03", "pct_fga_03_10", "pct_fga_10_16",
        "pct_fga_16_xx", "pct_fga_fg3a", "fg_pct_fg2a", "fg_pct_00_03", "fg_pct_03_10",
        "fg_pct_10_16", "fg_pct_16_xx", "fg_pct_fg3a", "pct_ast_fg2", "pct_ast_fg3",
        "pct_fga_dunk", "fg_dunk", "pct_fga_layup", "fg_layup", "pct_fg3a_corner",
        "fg3_pct_corner", "fg3a_heave", "fg3_heave",
    ],
    "shooting-opponent": [
        "opp_avg_dist", "opp_pct_fga_fg2a", "opp_pct_fga_00_03", "opp_pct_fga_03_10",
        "opp_pct_fga_10_16", "opp_pct_fga_16_xx", "opp_pct_fga_fg3a",
        "opp_fg_pct_fg2a", "opp_fg_pct_00_03", "opp_fg_pct_03_10", "opp_fg_pct_10_16",
        "opp_fg_pct_16_xx", "opp_fg_pct_fg3a", "opp_pct_ast_fg2", "opp_pct_ast_fg3",
        "opp_pct_fga_dunk", "opp_fg_dunk", "opp_pct_fga_layup", "opp_fg_layup",
        "opp_pct_fg3a_corner", "opp_fg3_pct_corner",
    ],
}  # fmt: skip

# a team row is useless without these, so a missing one fails the build;
# any other missing column is NULL, with a warning
REQUIRED_STATS = {"g", "pts", "opp_pts", "wins", "losses", "off_rtg", "def_rtg", "pace"}

# bbref's counting stat names -> the NBA's, used by every other dataset
RENAME = {
    "mp": "min", "fg": "fgm", "fg3": "fg3m", "fg2": "fg2m", "ft": "ftm",
    "orb": "oreb", "drb": "dreb", "trb": "reb",
}  # fmt: skip
COUNT_STATS = {
    "g", "fgm", "fga", "fg3m", "fg3a", "fg2m", "fg2a", "ftm", "fta", "oreb", "dreb",
    "reb", "ast", "stl", "blk", "tov", "pf", "pts", "wins", "losses", "wins_pyth",
    "losses_pyth", "attendance", "attendance_per_g", "fg_dunk", "fg_layup",
    "fg3a_heave", "fg3_heave",
}  # fmt: skip
TEXT_STATS = {"arena_name"}


def column_name(stat: str) -> str:
    opp = stat.startswith("opp_")
    base = stat.removeprefix("opp_")
    return ("opp_" if opp else "") + RENAME.get(base, base)


def column_type(name: str) -> str:
    base = name.removeprefix("opp_")
    if name in TEXT_STATS:
        return "VARCHAR"
    return "INTEGER" if base in COUNT_STATS else "DOUBLE"


TEAM_STATS_COLUMNS = [
    ("season", "INTEGER"),
    ("team_id", "VARCHAR"),
    ("made_playoffs", "BOOLEAN"),
    *[
        (column_name(s), column_type(column_name(s)))
        for cols in TEAM_TABLES.values()
        for s in cols
    ],
]


def number(value: str, type_: str):
    value = value.replace(",", "").strip()
    if not value:
        return None
    return int(value) if type_ == "INTEGER" else float(value)


TEAM_HREF = re.compile(r"/teams/(\w+)/")


def team_stats(html: str, season: int, team_ids: dict[str, str]) -> list[dict]:
    """
    One row per team from a season's league page. `team_ids` maps bbref's
    abbreviations that season to team ids. Raises ValueError if the page
    isn't as expected, so nothing is written from a changed page
    """
    teams: dict[str, dict] = {}
    warnings = []
    for table, stats in TEAM_TABLES.items():
        rows = [
            r
            for r in parse_table(html, table)
            if TEAM_HREF.search(r.get("team_href", ""))
        ]
        if len(rows) != 30:
            raise ValueError(
                f"bbref {season} {table}: {len(rows)} team rows, expected 30"
            )
        for r in rows:
            abbrev = TEAM_HREF.search(r["team_href"])[1]  # ty: ignore[not-subscriptable]
            if abbrev not in team_ids:
                raise ValueError(
                    f"bbref {season}: no team_id for {abbrev}; add it to team_abbrevs.csv"
                )
            row = teams.setdefault(
                team_ids[abbrev], {"season": season, "team_id": team_ids[abbrev]}
            )
            if table == "advanced-team":
                # bbref marks playoff teams with an asterisk
                row["made_playoffs"] = r.get("team", "").endswith("*")
            for stat in stats:
                name = column_name(stat)
                if stat not in r:
                    if stat in REQUIRED_STATS:
                        raise ValueError(f"bbref {season} {table}: no {stat} column")
                    warnings.append(stat)
                    row[name] = None
                    continue
                type_ = column_type(name)
                try:
                    row[name] = (
                        r[stat].strip() or None
                        if type_ == "VARCHAR"
                        else number(r[stat], type_)
                    )
                except ValueError as e:
                    raise ValueError(
                        f"bbref {season} {table}.{stat}: {r[stat]!r} isn't a number"
                    ) from e
    if len(teams) != 30:
        raise ValueError(
            f"bbref {season}: the tables cover {len(teams)} teams, expected 30"
        )
    for row in teams.values():
        missing = [s for s in REQUIRED_STATS if row.get(column_name(s)) is None]
        if missing:
            raise ValueError(
                f"bbref {season} team {row['team_id']}: no value for {missing}"
            )
    if warnings:
        print(
            f"bbref: {season}: columns missing from the page, left NULL: {sorted(set(warnings))}"
        )
    return sorted(teams.values(), key=lambda r: r["team_id"])


def league_page(
    fetcher: Fetcher | None, outdir: Path, season: int, refresh: bool
) -> str | None:
    raw = season_page_path(outdir, season, "league")
    if fetcher is None:
        if not raw.is_file():
            return None
        with gzip.open(raw, "rt") as f:
            return f.read()
    return cached(fetcher, f"/leagues/NBA_{season}.html", raw, refresh)


def build_team_stats(outdir: Path, html: str, season: int) -> Path:

    team_ids = {
        a.abbrev: a.team_id
        for a in load_team_abbrevs()
        if a.source == "bbref" and a.covers(season)
    }
    rows = team_stats(html, season, team_ids)
    types = {
        "INTEGER": pa.int32(),
        "DOUBLE": pa.float64(),
        "VARCHAR": pa.string(),
        "BOOLEAN": pa.bool_(),
    }
    table = pa.table(
        {
            name: pa.array([r.get(name) for r in rows], types[t])
            for name, t in TEAM_STATS_COLUMNS
        }
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute("CREATE TABLE bbref_team_stats AS SELECT * FROM arrow")
    check_keys(con, "bbref_team_stats", ["team_id"], "bbref bbref_team_stats")
    path = dataset_path(outdir / OUT_DIR, "bbref_team_stats", season)
    write_parquet(con, "bbref_team_stats", path)
    print(f"bbref: wrote {path}")
    return path


def main(argv: list[str] | None = None) -> None:

    parser = argparse.ArgumentParser(
        description="basketball-reference team season stats"
    )
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season", type=int, action="append", help="default: current season"
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild from saved pages"
    )
    args = parser.parse_args(argv)

    current = current_season(today_eastern())
    fetcher = None if args.no_fetch else Fetcher()
    for season in args.season or [current]:
        # past seasons' pages are fetched once; the current one changes daily
        html = league_page(fetcher, args.out, season, refresh=season == current)
        if html is None:
            raise SystemExit(f"bbref: no saved league page for {season}")
        if season == current and "totals-team" not in html:
            print(f"bbref: no team stats for {season} yet")
            continue
        build_team_stats(args.out, html, season)


if __name__ == "__main__":
    main()
