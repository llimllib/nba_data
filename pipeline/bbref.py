"""
Fetch and parse basketball-reference.com pages.

basketball-reference blocks clients that make more than ~20 requests a
minute, for up to a day, and its robots.txt asks for 3 seconds between
requests. So we wait REQUEST_DELAY seconds between requests, stop on the
first HTTP error rather than retrying, and keep every page we fetch,
gzipped, at

    nba/raw/bbref/<season>/<page>.html.gz      season pages
    nba/raw/bbref/players/<bbref_id>.html.gz   player pages

so a page is never fetched twice (except the current season's, which
changes).
"""

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

from .output import write_atomic

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
    as bbref_id. Tables hidden in HTML comments (bbref reveals some with
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
