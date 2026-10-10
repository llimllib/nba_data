"""
Fetch and parse dunksandthrees.com's public EPM tables, and build

    nba/dunksandthrees/epm/season=<season>/data.parquet

with one row per player and season type, from the season ("actual") EPM
page, /epm/actual, for the regular season and ?seasontype=4 for the
playoffs.

usage: python -m pipeline.dunksandthrees [--out out] [--no-fetch]

Only the site's current season is public: other seasons redirect to a
subscribe page. So there's no backfill, and we fetch the default pages and
take the season from their data. The regular-season page decides which
season is written; the playoffs page adds its rows only if it's for the same
season (between seasons it may still show the previous playoffs, whose file
is already final). When the site moves on to a new season, the previous
season's file stays as it was last written.

The pages are SvelteKit apps with their data embedded as a javascript
object literal, `data:{stats:[[...], ...],k:{season:0, ...}, ...}`: rows as
arrays, and `k` mapping column names to indexes. That format changes from
time to time, so the parser accepts only the literal syntax it knows, and
the rows are validated before anything is written. Raw pages are kept,
gzipped, even when parsing fails, at

    nba/raw/dunksandthrees/<season>/epm_<season_type>.html.gz
"""

import argparse
import gzip
import re
import urllib.request
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa

from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import current_season, today_eastern

BASE_URL = "https://dunksandthrees.com"
USER_AGENT = "nba_data (https://github.com/llimllib/nba_data)"
RAW_DIR = Path("nba/raw/dunksandthrees")
OUT_DIR = Path("nba/dunksandthrees")

# our season type -> the site's seasontype parameter and value in the data
SEASON_TYPES = {"regular_season": 2, "playoffs": 4}


# the javascript literal parser's constants -> their values
JS_WORDS = {
    "true": True,
    "false": False,
    "null": None,
    "void 0": None,
    "!0": True,
    "!1": False,
    "NaN": float("nan"),
    "Infinity": float("inf"),
    "-Infinity": float("-inf"),
}
JS_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
}


class JSLiteralError(ValueError):
    pass


class JSLiteral:
    """
    A parser for the subset of javascript object literals SvelteKit embeds
    (devalue's output): objects with bare or quoted keys, arrays, strings,
    numbers (`.5`, `-.5`, `1e-5`), true/false/null, `void 0` (undefined, read
    as None), `!0`/`!1`, NaN and Infinity. Anything else raises
    JSLiteralError, so a format change fails loudly
    """

    NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
    KEY = re.compile(r"[A-Za-z_$][\w$]*")

    def __init__(self, text: str, pos: int = 0):
        self.s = text
        self.i = pos

    def error(self, msg: str) -> JSLiteralError:
        return JSLiteralError(f"{msg} at {self.i}: {self.s[self.i : self.i + 40]!r}")

    def value(self):
        c = self.s[self.i : self.i + 1]
        if c == "{":
            return self.object()
        if c == "[":
            return self.array()
        if c in "\"'":
            return self.string()
        if m := self.NUMBER.match(self.s, self.i):
            self.i = m.end()
            text = m.group()
            return float(text) if any(ch in text for ch in ".eE") else int(text)
        for word, val in JS_WORDS.items():
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        raise self.error("unexpected value")

    def expect(self, c: str) -> None:
        if self.s[self.i : self.i + 1] != c:
            raise self.error(f"expected {c!r}")
        self.i += 1

    def object(self) -> dict:
        self.expect("{")
        obj = {}
        while self.s[self.i : self.i + 1] != "}":
            if obj:
                self.expect(",")
            if self.s[self.i : self.i + 1] in "\"'":
                key = self.string()
            elif m := self.KEY.match(self.s, self.i):
                key = m.group()
                self.i = m.end()
            else:
                raise self.error("expected a key")
            self.expect(":")
            obj[key] = self.value()
        self.i += 1
        return obj

    def array(self) -> list:
        self.expect("[")
        arr = []
        while self.s[self.i : self.i + 1] != "]":
            if arr:
                self.expect(",")
            arr.append(self.value())
        self.i += 1
        return arr

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out = []
        while True:
            if self.i >= len(self.s):
                raise self.error("unterminated string")
            c = self.s[self.i]
            if c == quote:
                self.i += 1
                return "".join(out)
            if c == "\\":
                e = self.s[self.i + 1 : self.i + 2]
                if e == "u":
                    if self.s[self.i + 2 : self.i + 3] == "{":
                        end = self.s.index("}", self.i)
                        out.append(chr(int(self.s[self.i + 3 : end], 16)))
                        self.i = end + 1
                        continue
                    out.append(chr(int(self.s[self.i + 2 : self.i + 6], 16)))
                    self.i += 6
                    continue
                if e == "x":
                    out.append(chr(int(self.s[self.i + 2 : self.i + 4], 16)))
                    self.i += 4
                    continue
                out.append(JS_ESCAPES.get(e, e))
                self.i += 2
                continue
            out.append(c)
            self.i += 1


def page_data(html: str) -> dict:
    """
    The page's EPM data: the object holding `stats`, `k`, `season` and
    `seasontype`. Raises ValueError if it isn't there
    """
    start = html.find("data:{stats:[")
    if start == -1:
        raise ValueError("dunksandthrees: no `data:{stats:[` in the page")
    data = JSLiteral(html, start + len("data:")).object()
    missing = {"stats", "k", "season", "seasontype"} - data.keys()
    if missing:
        raise ValueError(f"dunksandthrees: page data has no {sorted(missing)}")
    return data


# the page's columns we keep -> our name and type. Everything else (names,
# abbreviations, the *_attr z-scores, ranks and percentiles) is left out:
# names come from the lookup tables, and ranks are a query away
COLUMNS = {
    "player_id": ("player_id", "VARCHAR"),
    "team_id": ("team_id", "VARCHAR"),
    "age": ("age", "INTEGER"),
    "pos_text": ("position", "VARCHAR"),
    "rookie_year": ("rookie_year", "INTEGER"),
    "inches": ("height_inches", "INTEGER"),
    "weight": ("weight", "INTEGER"),
    "gp": ("gp", "INTEGER"),
    "start": ("gs", "INTEGER"),
    "roster_games": ("roster_games", "INTEGER"),
    "mp": ("min", "DOUBLE"),
    "mpg": ("min_per_game", "DOUBLE"),
    "off": ("o_epm", "DOUBLE"),
    "def": ("d_epm", "DOUBLE"),
    "tot": ("epm", "DOUBLE"),
    "ewins": ("ewins", "DOUBLE"),
    "usg": ("usg_pct", "DOUBLE"),
    "tspct": ("ts_pct", "DOUBLE"),
    "efg": ("efg_pct", "DOUBLE"),
    "fgpct_rim": ("fg_pct_rim", "DOUBLE"),
    "fgpct_mid": ("fg_pct_mid", "DOUBLE"),
    "fg2pct": ("fg2_pct", "DOUBLE"),
    "fg3pct": ("fg3_pct", "DOUBLE"),
    "ftpct": ("ft_pct", "DOUBLE"),
    "orbpct": ("oreb_pct", "DOUBLE"),
    "drbpct": ("dreb_pct", "DOUBLE"),
    "astpct": ("ast_pct", "DOUBLE"),
    "topct": ("tov_pct", "DOUBLE"),
    "stlpct": ("stl_pct", "DOUBLE"),
    "blkpct": ("blk_pct", "DOUBLE"),
    "fga_rim_75": ("fga_rim_per_75", "DOUBLE"),
    "fga_mid_75": ("fga_mid_per_75", "DOUBLE"),
    "fg3a_75": ("fg3a_per_75", "DOUBLE"),
    "fta_75": ("fta_per_75", "DOUBLE"),
    "fga_75": ("fga_per_75", "DOUBLE"),
}

# the page is useless without these columns, so a missing one fails the
# build; any other missing column is NULL, with a warning. Every row needs
# a value for NOT_NULL; players with few minutes have no EPM
REQUIRED = {
    "season",
    "seasontype",
    "player_id",
    "team_id",
    "gp",
    "mp",
    "off",
    "def",
    "tot",
}
NOT_NULL = {"season", "seasontype", "player_id", "team_id", "gp", "mp"}

EPM_COLUMNS = [
    ("season", "INTEGER"),
    ("season_type", "VARCHAR"),
    *COLUMNS.values(),
]

# more rows than this means we're not reading what we think we are
MAX_ROWS = 1000


def convert(value, type_: str):
    if value is None or value == "":
        return None
    if type_ == "VARCHAR":
        # ids come as numbers; never through a float
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"{value!r} isn't an id")
            value = int(value)
        return str(value)
    if type_ == "INTEGER":
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{value!r} isn't an integer")
        return int(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"{value!r} isn't a number")


def epm_rows(data: dict, season_type: str) -> list[dict]:
    """
    The page data's rows as dicts of our columns. Raises ValueError if the
    data isn't as expected, so nothing is written from a changed page
    """
    keys: dict = data["k"]
    seasontype = SEASON_TYPES[season_type]
    if data["seasontype"] != seasontype:
        raise ValueError(
            f"dunksandthrees {season_type}: page is for seasontype "
            f"{data['seasontype']}, expected {seasontype}"
        )
    missing = REQUIRED - keys.keys()
    if missing:
        raise ValueError(f"dunksandthrees {season_type}: no {sorted(missing)} column")
    absent = sorted(c for c in COLUMNS if c not in keys)
    if absent:
        print(f"dunksandthrees: {season_type}: columns missing, left NULL: {absent}")
    if len(data["stats"]) > MAX_ROWS:
        raise ValueError(
            f"dunksandthrees {season_type}: {len(data['stats'])} rows, expected at "
            f"most {MAX_ROWS}"
        )

    rows = []
    for raw in data["stats"]:
        if not isinstance(raw, list) or len(raw) <= max(keys.values()):
            raise ValueError(
                f"dunksandthrees {season_type}: malformed row {raw!r:.200}"
            )
        get = {name: raw[i] for name, i in keys.items()}
        if (get["season"], get["seasontype"]) != (data["season"], seasontype):
            raise ValueError(
                f"dunksandthrees {season_type}: row for season {get['season']} "
                f"type {get['seasontype']} on the page for {data['season']}"
            )
        row = {"season": data["season"], "season_type": season_type}
        for col, (name, type_) in COLUMNS.items():
            try:
                row[name] = convert(get.get(col), type_)
            except ValueError as e:
                raise ValueError(
                    f"dunksandthrees {season_type} player {get['player_id']}: {col} {e}"
                ) from e
        nulls = [c for c in NOT_NULL if get[c] is None]
        if nulls:
            raise ValueError(
                f"dunksandthrees {season_type}: row with no {sorted(nulls)}: {raw!r:.200}"
            )
        rows.append(row)
    return rows


def played(rows: list[dict]) -> list[dict]:
    """
    Only players who played. EPM lists rostered players who haven't (gp = 0,
    with priors for EPM), who aren't in the NBA's game logs or player_seasons
    """
    return [r for r in rows if r["gp"]]


def fetch(season_type: str) -> str:
    path = "/epm/actual"
    if season_type != "regular_season":
        path += f"?seasontype={SEASON_TYPES[season_type]}"
    print(f"dunksandthrees: fetching {path}", flush=True)
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8")


def raw_path(outdir: Path, season: int | str, season_type: str) -> Path:
    return outdir / RAW_DIR / str(season) / f"epm_{season_type}.html.gz"


def save_raw(path: Path, html: str) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            f.write(html)

    write_atomic(path, write)


def read_raw(path: Path) -> str:
    with gzip.open(path, "rt") as f:
        return f.read()


def fetch_pages(outdir: Path, today: date) -> dict[str, tuple[int, dict]]:
    """
    Fetch each season type's page, save it under its season (or under
    `unparsed/` if we can't tell its season), and parse it. Returns season
    type -> (season, page data)
    """
    pages = {}
    for season_type in SEASON_TYPES:
        html = fetch(season_type)
        try:
            data = page_data(html)
        except ValueError:
            save_raw(
                raw_path(outdir, f"unparsed-{today.isoformat()}", season_type), html
            )
            raise
        save_raw(raw_path(outdir, data["season"], season_type), html)
        pages[season_type] = (data["season"], data)
    return pages


def saved_pages(outdir: Path) -> dict[int, dict[str, dict]]:
    """season -> season type -> page data, from every saved raw page"""
    seasons: dict[int, dict[str, dict]] = {}
    for season_dir in sorted((outdir / RAW_DIR).glob("[0-9][0-9][0-9][0-9]")):
        for season_type in SEASON_TYPES:
            path = raw_path(outdir, season_dir.name, season_type)
            if path.is_file():
                data = page_data(read_raw(path))
                seasons.setdefault(data["season"], {})[season_type] = data
    return seasons


def build_epm(outdir: Path, season: int, pages: dict[str, dict]) -> Path | None:
    """
    Write a season's EPM from its pages (season type -> page data), which
    must include the regular season. Returns the path, or None if no one
    has played yet
    """
    rows = []
    for season_type, data in pages.items():
        rows += played(epm_rows(data, season_type))
    if not any(r["season_type"] == "regular_season" for r in rows):
        print(f"dunksandthrees: no regular-season games in {season} yet")
        return None

    types = {"INTEGER": pa.int32(), "DOUBLE": pa.float64(), "VARCHAR": pa.string()}
    table = pa.table(
        {name: pa.array([r[name] for r in rows], types[t]) for name, t in EPM_COLUMNS}
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute(
        "CREATE TABLE epm AS SELECT * FROM arrow ORDER BY season_type, player_id"
    )
    check_keys(con, "epm", ["season_type", "player_id"], "dunksandthrees epm")
    path = dataset_path(outdir / OUT_DIR, "epm", season)
    write_parquet(con, "epm", path)
    print(f"dunksandthrees: wrote {path} ({len(rows)} rows)")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="dunksandthrees EPM")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="rebuild every season from the saved pages",
    )
    args = parser.parse_args(argv)

    if args.no_fetch:
        for season, pages in sorted(saved_pages(args.out).items()):
            if "regular_season" in pages:
                build_epm(args.out, season, pages)
        return

    fetched = fetch_pages(args.out, today_eastern())
    season, regular = fetched["regular_season"]
    pages = {"regular_season": regular}
    playoff_season, playoffs = fetched["playoffs"]
    if playoff_season == season:
        pages["playoffs"] = playoffs
    else:
        print(
            f"dunksandthrees: the playoffs page is for {playoff_season}, "
            f"not {season}; skipping it"
        )
    if season < current_season(today_eastern()):
        print(f"dunksandthrees: the site is still on {season}")
    build_epm(args.out, season, pages)


if __name__ == "__main__":
    main()
