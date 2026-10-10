"""
Fetch and parse dunksandthrees.com's public EPM tables, and build

    nba/dunksandthrees/epm/season=<season>/data.parquet
    nba/dunksandthrees/epm_predictive/season=<season>/data.parquet

`epm` is season ("actual") EPM, one row per player and season type, from
/epm/actual for the regular season and ?seasontype=4 for the playoffs.
`epm_predictive` is the site's predictive EPM, one row per player, from
/epm: a prediction as of a date, recomputed daily, so the file holds the
latest one. `epm` keeps only players who played (the site also lists
rostered players with no games and no EPM); `epm_predictive` keeps everyone
in the season's player_seasons, which includes rostered players who haven't
played, read from out/ or else from the bucket.

usage: python -m pipeline.dunksandthrees [--out out] [--no-fetch]

Only the site's current season is public: other seasons redirect to a
subscribe page. So there's no backfill, and we fetch the default pages and
take the season from their data. The regular-season page decides which
season is written; the playoffs and predictive pages are only used if
they're for the same season (between seasons the playoffs page may still
show the previous playoffs, whose file is already final). When the site moves on to a new season, the previous
season's file stays as it was last written.

The pages are SvelteKit apps with their data embedded as a javascript
object literal. The season pages have `data:{stats:[[...], ...],k:{season:0,
...}, ...}`: rows as arrays, and `k` mapping column names to indexes. The
predictive page has `{type:"data",data:{date:"2026-06-13",stats:[{...},
...], season:2026, ...}}`: rows as objects. That format changes from time to
time, so the parser accepts only the literal syntax it knows, and the rows
are validated before anything is written. Raw pages are kept, gzipped, even
when parsing fails (under unparsed-<date>/), at

    nba/raw/dunksandthrees/<season>/epm_<season_type>.html.gz
    nba/raw/dunksandthrees/<season>/epm_predictive/<date>.html.gz

The predictive pages are kept for every day, so a history of the
predictions could be rebuilt.
"""

import argparse
import gzip
import re
import urllib.request
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa

from .catalog import URL
from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import current_season, today_eastern

BASE_URL = "https://dunksandthrees.com"
USER_AGENT = "nba_data (https://github.com/llimllib/nba_data)"
RAW_DIR = Path("nba/raw/dunksandthrees")
OUT_DIR = Path("nba/dunksandthrees")
STATS_DIR = Path("nba/stats")

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
    Only players who played. The season pages also list rostered players
    who haven't (gp = 0), who have no season EPM
    """
    return [r for r in rows if r["gp"]]


# --- predictive EPM ----------------------------------------------------------

PREDICTIVE_START = '{type:"data",data:{date'

# the predictive page's columns we keep -> our name and type. Every stat is
# the site's prediction, so the names have no prefix
PREDICTIVE_COLUMNS = {
    "player_id": ("player_id", "VARCHAR"),
    "team_id": ("team_id", "VARCHAR"),
    "age": ("age", "INTEGER"),
    "position": ("position", "VARCHAR"),
    "rookie_year": ("rookie_year", "INTEGER"),
    "inches": ("height_inches", "INTEGER"),
    "weight": ("weight", "INTEGER"),
    "off": ("o_epm", "DOUBLE"),
    "def": ("d_epm", "DOUBLE"),
    "tot": ("epm", "DOUBLE"),
    "tot_change": ("epm_change", "DOUBLE"),
    "p_pct_start": ("start_pct", "DOUBLE"),
    "p_t_poss_48": ("poss_per_48", "DOUBLE"),
    "p_mp_48": ("min_per_48", "DOUBLE"),
    "p_usg": ("usg_pct", "DOUBLE"),
    "p_pts_100": ("pts_per_100", "DOUBLE"),
    "p_tspct": ("ts_pct", "DOUBLE"),
    "p_efg": ("efg_pct", "DOUBLE"),
    "p_fga_rim_100": ("fga_rim_per_100", "DOUBLE"),
    "p_fga_mid_100": ("fga_mid_per_100", "DOUBLE"),
    "p_fg2a_100": ("fg2a_per_100", "DOUBLE"),
    "p_fg3a_100": ("fg3a_per_100", "DOUBLE"),
    "p_fta_100": ("fta_per_100", "DOUBLE"),
    "p_fgpct_rim": ("fg_pct_rim", "DOUBLE"),
    "p_fgpct_mid": ("fg_pct_mid", "DOUBLE"),
    "p_fg2pct": ("fg2_pct", "DOUBLE"),
    "p_fg3pct": ("fg3_pct", "DOUBLE"),
    "p_ftpct": ("ft_pct", "DOUBLE"),
    "p_ast_100": ("ast_per_100", "DOUBLE"),
    "p_tov_100": ("tov_per_100", "DOUBLE"),
    "p_orb_100": ("oreb_per_100", "DOUBLE"),
    "p_drb_100": ("dreb_per_100", "DOUBLE"),
    "p_stl_100": ("stl_per_100", "DOUBLE"),
    "p_blk_100": ("blk_per_100", "DOUBLE"),
}
PREDICTIVE_REQUIRED = {"season", "player_id", "team_id", "off", "def", "tot", "p_mp_48"}
PREDICTIVE_NOT_NULL = {"season", "player_id", "team_id"}

PREDICTIVE_EPM_COLUMNS = [
    ("season", "INTEGER"),
    ("as_of_date", "DATE"),
    *PREDICTIVE_COLUMNS.values(),
]


def predictive_data(html: str) -> dict:
    """
    The predictive page's data: the object holding `date`, `season` and
    `stats`. Raises ValueError if it isn't there
    """
    start = html.find(PREDICTIVE_START)
    if start == -1:
        raise ValueError(f"dunksandthrees: no `{PREDICTIVE_START}` in the page")
    data = JSLiteral(html, start + len('{type:"data",data:')).object()
    missing = {"date", "season", "stats"} - data.keys()
    if missing:
        raise ValueError(f"dunksandthrees: predictive data has no {sorted(missing)}")
    try:
        date.fromisoformat(data["date"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"dunksandthrees: predictive date {data['date']!r}") from e
    return data


def predictive_rows(data: dict) -> list[dict]:
    """
    The predictive page's rows as dicts of our columns. Raises ValueError if
    the data isn't as expected
    """
    stats = data["stats"]
    if not isinstance(stats, list) or len(stats) > MAX_ROWS:
        raise ValueError(
            f"dunksandthrees predictive: expected at most {MAX_ROWS} rows, got "
            f"{len(stats) if isinstance(stats, list) else type(stats).__name__}"
        )
    absent = sorted(c for c in PREDICTIVE_COLUMNS if not any(c in r for r in stats))
    if stats and absent:
        print(f"dunksandthrees: predictive: columns missing, left NULL: {absent}")

    malformed = [r for r in stats if not isinstance(r, dict)]
    if malformed:
        raise ValueError(
            f"dunksandthrees predictive: malformed row {malformed[0]!r:.200}"
        )

    as_of = date.fromisoformat(data["date"])
    rows = []
    for raw in stats:
        missing = PREDICTIVE_REQUIRED - raw.keys()
        if missing:
            raise ValueError(f"dunksandthrees predictive: no {sorted(missing)} column")
        nulls = [c for c in PREDICTIVE_NOT_NULL if raw[c] is None]
        if nulls:
            raise ValueError(
                f"dunksandthrees predictive: row with no {sorted(nulls)}: {raw!r:.200}"
            )
        if raw["season"] != data["season"]:
            raise ValueError(
                f"dunksandthrees predictive: row for season {raw['season']} on the "
                f"page for {data['season']}"
            )
        row = {"season": data["season"], "as_of_date": as_of}
        for col, (name, type_) in PREDICTIVE_COLUMNS.items():
            try:
                row[name] = convert(raw.get(col), type_)
            except ValueError as e:
                raise ValueError(
                    f"dunksandthrees predictive player {raw['player_id']}: {col} {e}"
                ) from e
        rows.append(row)
    return rows


# --- fetching and building ---------------------------------------------------

PAGES = {
    "regular_season": "/epm/actual",
    "playoffs": f"/epm/actual?seasontype={SEASON_TYPES['playoffs']}",
    "predictive": "/epm",
}

PA_TYPES = {
    "INTEGER": pa.int32(),
    "DOUBLE": pa.float64(),
    "VARCHAR": pa.string(),
    "DATE": pa.date32(),
}


def fetch(page: str) -> str:
    path = PAGES[page]
    print(f"dunksandthrees: fetching {path}", flush=True)
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8")


def raw_path(outdir: Path, season: int | str, page: str) -> Path:
    return outdir / RAW_DIR / str(season) / f"epm_{page}.html.gz"


def predictive_raw_path(outdir: Path, season: int, as_of: str) -> Path:
    """one per day, so a history of the predictions could be rebuilt"""
    return outdir / RAW_DIR / str(season) / "epm_predictive" / f"{as_of}.html.gz"


def save_raw(path: Path, html: str) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            f.write(html)

    write_atomic(path, write)


def read_raw(path: Path) -> str:
    with gzip.open(path, "rt") as f:
        return f.read()


def fetch_page(outdir: Path, page: str, today: date, parse) -> dict:
    """
    Fetch a page and parse it with `parse`. A page we can't parse is saved
    under `unparsed-<today>/`, since we can't tell its season, before the
    error is raised; the caller saves the others
    """
    html = fetch(page)
    try:
        data = parse(html)
    except ValueError:
        save_raw(raw_path(outdir, f"unparsed-{today.isoformat()}", page), html)
        raise
    data["html"] = html
    return data


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


def latest_predictive(outdir: Path, season: int) -> dict | None:
    """the newest saved predictive page's data for `season`, if any"""
    saved = sorted(predictive_raw_path(outdir, season, "x").parent.glob("*.html.gz"))
    return predictive_data(read_raw(saved[-1])) if saved else None


def write_table(
    outdir: Path, dataset: str, columns: list, rows: list[dict], keys: list[str]
) -> Path:
    table = pa.table(
        {name: pa.array([r[name] for r in rows], PA_TYPES[t]) for name, t in columns}
    )
    con = duckdb.connect()
    con.register("arrow", table)
    con.execute(
        f"CREATE TABLE {dataset} AS SELECT * FROM arrow ORDER BY {', '.join(keys)}"
    )
    check_keys(con, dataset, keys, f"dunksandthrees {dataset}")
    path = dataset_path(outdir / OUT_DIR, dataset, rows[0]["season"])
    write_parquet(con, dataset, path)
    print(f"dunksandthrees: wrote {path} ({len(rows)} rows)")
    return path


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
    return write_table(outdir, "epm", EPM_COLUMNS, rows, ["season_type", "player_id"])


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


def build_predictive(
    outdir: Path, season: int, data: dict, known: set[str] | None
) -> Path | None:
    """
    Write a season's predictive EPM from the predictive page's data. Only
    players in `known`, the season's player_seasons, are kept, so the
    integrity check passes: that's everyone who played or is on a roster,
    but not players who left a team without playing. Returns the path, or
    None if there's no player_seasons for the season yet
    """
    if data["season"] != season:
        raise ValueError(
            f"dunksandthrees: predictive page is for {data['season']}, not {season}"
        )
    rows = predictive_rows(data)
    if not known:
        print(f"dunksandthrees: no player_seasons for {season} yet")
        return None
    kept = [r for r in rows if r["player_id"] in known]
    if len(kept) < len(rows):
        names = {str(r.get("player_id")): r.get("player_name") for r in data["stats"]}
        dropped = sorted(names[r["player_id"]] or "" for r in rows if r not in kept)
        print(
            f"dunksandthrees: predictive {data['date']}: dropped {len(dropped)} "
            f"players not in player_seasons: {dropped}"
        )
    # nearly every player is in player_seasons; fewer means one of the two
    # isn't what we think it is
    if len(kept) < len(rows) / 2:
        raise ValueError(
            f"dunksandthrees predictive: only {len(kept)} of {len(rows)} players "
            "are in player_seasons"
        )
    return write_table(
        outdir, "epm_predictive", PREDICTIVE_EPM_COLUMNS, kept, ["player_id"]
    )


def update(outdir: Path, today: date) -> None:
    """
    Fetch every page and build both datasets for the season the site is on.
    The season pages come first: they decide the season, and a failure on
    the predictive page leaves the season's EPM written
    """
    pages = {}
    for season_type in SEASON_TYPES:
        data = fetch_page(outdir, season_type, today, page_data)
        save_raw(raw_path(outdir, data["season"], season_type), data.pop("html"))
        pages[season_type] = data
    season = pages["regular_season"]["season"]
    if pages["playoffs"]["season"] != season:
        print(
            f"dunksandthrees: the playoffs page is for {pages['playoffs']['season']}, "
            f"not {season}; skipping it"
        )
        del pages["playoffs"]
    if season < current_season(today):
        print(f"dunksandthrees: the site is still on {season}")
    build_epm(outdir, season, pages)

    predictive = fetch_page(outdir, "predictive", today, predictive_data)
    save_raw(
        predictive_raw_path(outdir, predictive["season"], predictive["date"]),
        predictive.pop("html"),
    )
    if predictive["season"] != season:
        print(
            f"dunksandthrees: the predictive page is for {predictive['season']}, "
            f"not {season}; skipping it"
        )
        return
    build_predictive(outdir, season, predictive, known_players(outdir, season))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="dunksandthrees EPM")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="rebuild every season from the saved pages",
    )
    args = parser.parse_args(argv)

    if not args.no_fetch:
        update(args.out, today_eastern())
        return
    for season, pages in sorted(saved_pages(args.out).items()):
        if "regular_season" not in pages:
            continue
        build_epm(args.out, season, pages)
        if predictive := latest_predictive(args.out, season):
            build_predictive(
                args.out, season, predictive, known_players(args.out, season)
            )


if __name__ == "__main__":
    main()
