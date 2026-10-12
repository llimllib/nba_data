"""
Fetch barttorvik.com's season CSVs for men's college basketball, and build

    ncaam/torvik/torvik_team_results/season=<season>/data.parquet
    ncaam/torvik/torvik_four_factors/season=<season>/data.parquet

with one row per Division I team: T-Rank's adjusted efficiencies, records,
strength of schedule and projections, and the four factors (plus shooting,
assist and block rates) for and against.

usage: python -m pipeline.torvik [--out out] [--season 2026 ...] [--no-fetch]

The CSVs are at /<season>_team_results.csv and /<season>_fffinal.csv, named
by the season's end year like our seasons, from 2008 on. The current
season is refetched each run; past seasons only once. A CSV that doesn't
exist yet (the four factors before a season starts) is a 404, and is
skipped. Every CSV is kept, gzipped, under the date it was fetched:

    ncaam/raw/torvik/<season>/<team_results|four_factors>/<date>.csv.gz

and a season is built from its latest files, whose date is `as_of_date`.

Teams have no ids: the key is torvik's team name, which torvik applies to
every season (a renamed school has its current name throughout). The CSVs'
headers repeat names ("rank", "Rk") and the four factors' leaves its last
four columns unlabeled, so columns are read by position, and a header that
isn't one of the known ones writes nothing. So does a page that isn't a CSV
(torvik's "Verifying Browser" page), which is checked before it's saved.
"""

import argparse
import csv
import gzip
import http.client
import io
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Protocol

import duckdb
import pyarrow as pa

from .output import check_keys, dataset_path, write_atomic, write_parquet
from .seasons import current_season, today_eastern

BASE_URL = "https://barttorvik.com"
USER_AGENT = "nba_data (https://github.com/llimllib/nba_data)"
# torvik doesn't publish a limit; be polite
REQUEST_DELAY = 2
RAW_DIR = Path("ncaam/raw/torvik")
OUT_DIR = Path("ncaam/torvik")
FIRST_SEASON = 2008

# Division I has had 341 to 365 teams since 2008
MIN_TEAMS, MAX_TEAMS = 300, 400

# torvik's file -> our raw directory and dataset
FILES = {
    "team_results": ("team_results", "torvik_team_results"),
    "fffinal": ("four_factors", "torvik_four_factors"),
}

# Each CSV's columns in order: (header, column, kind). A column of None is
# dropped (torvik's ranks, which a query can recompute). Kinds:
#   VARCHAR, INTEGER, DOUBLE  as they are
#   record                    "37-3", split into <column>_wins, <column>_losses
#                             (just wins, losses if <column> is "")
#   pct                       a percentage (49.4), stored as a fraction (0.494)
#   per_100                   points per possession (1.16), stored per 100 (116)
TEAM_RESULTS = [
    ("rank", None, None),
    ("team", "team", "VARCHAR"),
    ("conf", "conf", "VARCHAR"),
    ("record", "", "record"),
    ("adjoe", "adj_oe", "DOUBLE"),
    ("oe Rank", None, None),
    ("adjde", "adj_de", "DOUBLE"),
    ("de Rank", None, None),
    ("barthag", "barthag", "DOUBLE"),
    ("rank", None, None),
    ("proj. W", "proj_wins", "DOUBLE"),
    ("Proj. L", "proj_losses", "DOUBLE"),
    ("Pro Con W", "proj_conf_wins", "DOUBLE"),
    ("Pro Con L", "proj_conf_losses", "DOUBLE"),
    ("Con Rec.", "conf", "record"),
    ("sos", "sos", "DOUBLE"),
    ("ncsos", "nc_sos", "DOUBLE"),
    ("consos", "conf_sos", "DOUBLE"),
    ("Proj. SOS", "proj_sos", "DOUBLE"),
    ("Proj. Noncon SOS", "proj_nc_sos", "DOUBLE"),
    ("Proj. Con SOS", "proj_conf_sos", "DOUBLE"),
    ("elite SOS", "elite_sos", "DOUBLE"),
    ("elite noncon SOS", "elite_nc_sos", "DOUBLE"),
    ("Opp OE", "opp_oe", "DOUBLE"),
    ("Opp DE", "opp_de", "DOUBLE"),
    ("Opp Proj. OE", "opp_proj_oe", "DOUBLE"),
    ("Opp Proj DE", "opp_proj_de", "DOUBLE"),
    ("Con Adj OE", "conf_adj_oe", "DOUBLE"),
    ("Con Adj DE", "conf_adj_de", "DOUBLE"),
    ("Qual O", "qual_oe", "DOUBLE"),
    ("Qual D", "qual_de", "DOUBLE"),
    ("Qual Barthag", "qual_barthag", "DOUBLE"),
    ("Qual Games", "qual_games", "INTEGER"),
    ("FUN", "fun", "DOUBLE"),
    ("ConPF", "conf_pts", "INTEGER"),
    ("ConPA", "conf_opp_pts", "INTEGER"),
    ("ConPoss", "conf_poss", "DOUBLE"),
    ("ConOE", "conf_oe", "per_100"),
    ("ConDE", "conf_de", "per_100"),
    ("ConSOSRemain", "conf_sos_remain", "DOUBLE"),
    ("Conf Win%", "conf_win_pct", "DOUBLE"),
    ("WAB", "wab", "DOUBLE"),
    ("WAB Rk", None, None),
    ("Fun Rk", None, None),
    ("adjt", "adj_tempo", "DOUBLE"),
]

# Torvik writes 0 for what it can't compute yet: ratings and strength of
# schedule before a team has games of that kind (all of them in the
# preseason, the non-conference ones for 2021's conference-only schedules),
# the "quality" stats without quality games, and the remaining conference
# schedule once it's played. None of these can really be 0, so 0 is NULL
ZERO_IS_NULL = {
    "adj_oe", "adj_de", "barthag", "sos", "nc_sos", "conf_sos", "proj_sos",
    "proj_nc_sos", "proj_conf_sos", "elite_sos", "elite_nc_sos", "opp_oe",
    "opp_de", "opp_proj_oe", "opp_proj_de", "conf_adj_oe", "conf_adj_de",
    "qual_oe", "qual_de", "qual_barthag", "fun", "conf_poss", "conf_oe",
    "conf_de", "conf_sos_remain", "adj_tempo",
}  # fmt: skip
# and these are 0 before a team's first conference game
NULL_WITHOUT_CONF_GAMES = {"conf_pts", "conf_opp_pts", "conf_win_pct"}

FOUR_FACTORS = [
    ("TeamName", "team", "VARCHAR"),
    ("eFG%", "efg_pct", "pct"),
    ("Rk", None, None),
    ("eFG% Def", "opp_efg_pct", "pct"),
    ("Rk", None, None),
    ("FTR", "ft_rate", "pct"),
    ("Rk", None, None),
    ("FTR Def", "opp_ft_rate", "pct"),
    ("Rk", None, None),
    ("OR%", "oreb_pct", "pct"),
    ("Rk", None, None),
    # opponents' offensive rebound rate, not the team's defensive one
    ("DR%", "opp_oreb_pct", "pct"),
    ("Rk", None, None),
    ("TO%", "tov_pct", "pct"),
    ("Rk", None, None),
    ("TO% Def.", "opp_tov_pct", "pct"),
    ("Rk", None, None),
    ("3P%", "fg3_pct", "pct"),
    ("rk", None, None),
    ("3pD%", "opp_fg3_pct", "pct"),
    ("rk", None, None),
    ("2p%", "fg2_pct", "pct"),
    ("rk", None, None),
    ("2p%D", "opp_fg2_pct", "pct"),
    ("rk", None, None),
    ("ft%", "ft_pct", "pct"),
    ("rk", None, None),
    ("ft%D", "opp_ft_pct", "pct"),
    ("rk", None, None),
    ("3P rate", "fg3a_rate", "pct"),
    ("rk", None, None),
    ("3P rate D", "opp_fg3a_rate", "pct"),
    ("rk", None, None),
    ("arate", "ast_rate", "pct"),
    ("rk", None, None),
    ("arateD", "opp_ast_rate", "pct"),
    ("rk", None, None),
    # Unlabeled. The first tracks opponents' 2P% (r = -0.56, 2008-2026) and
    # not forced turnovers (0.09), so it's the team's block rate; the second,
    # its shots' blocked rate. Their ranks put a high block rate first and a
    # low blocked rate first, as for every other stat and its opponents'
    ("", "blk_pct", "pct"),
    ("", None, None),
    ("", "opp_blk_pct", "pct"),
    ("", None, None),
]

SPECS = {"team_results": TEAM_RESULTS, "fffinal": FOUR_FACTORS}


def headers(spec: list[tuple]) -> list[str]:
    """the header row a CSV with these columns has"""
    return [h for h, _, _ in spec if h]


# the headers each CSV has had. Before 2022 the team results' last header
# was the one cell "Fun Rk, adjt", over two columns of values
KNOWN_HEADERS = {
    "team_results": [
        headers(TEAM_RESULTS),
        headers(TEAM_RESULTS)[:-2] + ["Fun Rk, adjt"],
    ],
    "fffinal": [headers(FOUR_FACTORS)],
}


def columns(spec: list[tuple]) -> list[tuple[str, str]]:
    """(name, type) of each stored column, after season and as_of_date"""
    cols = []
    for _, name, kind in spec:
        if name is None:
            continue
        if kind == "record":
            prefix = f"{name}_" if name else ""
            cols += [(f"{prefix}wins", "INTEGER"), (f"{prefix}losses", "INTEGER")]
        elif kind in ("pct", "per_100"):
            cols.append((name, "DOUBLE"))
        else:
            cols.append((name, kind))
    return [("season", "INTEGER"), ("as_of_date", "DATE"), *cols]


def integer(value: str) -> int:
    """an integer, which torvik sometimes writes as a float ("26.0")"""
    x = float(value)
    if not x.is_integer():
        raise ValueError(f"{value!r} isn't a whole number")
    return int(x)


def record(value: str) -> tuple[int, int]:
    wins, sep, losses = value.partition("-")
    if not sep:
        raise ValueError(f"{value!r} isn't a record")
    return int(wins), int(losses)


def parse(text: str, file: str, season: int) -> list[dict]:
    """
    One row per team from one of torvik's CSVs (`file` is a key of FILES),
    without as_of_date. Raises ValueError if the CSV isn't as expected, so
    nothing is saved or written from a changed one
    """
    label = f"torvik {season} {file}"
    spec = SPECS[file]
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0] not in KNOWN_HEADERS[file]:
        first = text[:200].strip().replace("\n", " ")
        raise ValueError(f"{label}: unexpected header; the file starts {first!r}")
    if not MIN_TEAMS <= len(rows) - 1 <= MAX_TEAMS:
        raise ValueError(
            f"{label}: {len(rows) - 1} teams, expected {MIN_TEAMS} to {MAX_TEAMS}"
        )

    teams = []
    for r in rows[1:]:
        if len(r) != len(spec):
            raise ValueError(f"{label}: {len(r)} values for {len(spec)} columns: {r}")
        row: dict = {"season": season}
        for (_, name, kind), value in zip(spec, r, strict=True):
            if name is None:
                continue
            try:
                if kind == "VARCHAR":
                    if not value.strip():
                        raise ValueError("empty")
                    row[name] = value.strip()
                elif kind == "record":
                    prefix = f"{name}_" if name else ""
                    row[f"{prefix}wins"], row[f"{prefix}losses"] = record(value)
                elif kind == "INTEGER":
                    row[name] = integer(value)
                elif kind == "pct":
                    # rounded, so 51.800000000000004 is 0.518
                    row[name] = round(float(value) / 100, 6)
                elif kind == "per_100":
                    row[name] = float(value) * 100
                else:
                    row[name] = float(value)
            except ValueError as e:
                raise ValueError(
                    f"{label} {r[:3]}: {name or 'record'} {value!r}: {e}"
                ) from e
        if file == "team_results":
            for name in ZERO_IS_NULL:
                if row[name] == 0:
                    row[name] = None
            if row["conf_wins"] + row["conf_losses"] == 0:
                for name in NULL_WITHOUT_CONF_GAMES:
                    row[name] = None
        teams.append(row)
    return teams


def path(season: int, file: str) -> str:
    return f"/{season}_{file}.csv"


class Pages(Protocol):
    def get(self, path: str) -> str | None: ...


class Fetcher:
    """
    Fetches files no faster than one per REQUEST_DELAY seconds. A 404 is
    None: torvik's file for that season doesn't exist (yet). A dropped or
    truncated response is retried once
    """

    def __init__(self, delay: float = REQUEST_DELAY, sleep=time.sleep, clock=time.time):
        self.delay = delay
        self.sleep = sleep
        self.clock = clock
        self.last = 0.0

    def get(self, path: str) -> str | None:
        try:
            return self.fetch(path)
        except (http.client.IncompleteRead, urllib.error.URLError) as e:
            if isinstance(e, urllib.error.HTTPError):
                raise
            print(f"torvik: {e!r}; retrying", flush=True)
            return self.fetch(path)

    def fetch(self, path: str) -> str | None:
        wait = self.last + self.delay - self.clock()
        if wait > 0:
            self.sleep(wait)
        print(f"torvik: fetching {path}", flush=True)
        req = urllib.request.Request(
            BASE_URL + path, headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        finally:
            self.last = self.clock()


def raw_dir(outdir: Path, season: int, file: str) -> Path:
    return outdir / RAW_DIR / str(season) / FILES[file][0]


def save_raw(path: Path, text: str) -> None:
    def write(tmp):
        with gzip.open(tmp, "wt") as f:
            f.write(text)

    write_atomic(path, write)


def latest_file(outdir: Path, season: int, file: str) -> tuple[date, str] | None:
    """the newest saved CSV for a season, and the date it was fetched"""
    saved = sorted(raw_dir(outdir, season, file).glob("*.csv.gz"))
    if not saved:
        return None
    with gzip.open(saved[-1], "rt") as f:
        return date.fromisoformat(saved[-1].name.removesuffix(".csv.gz")), f.read()


def season_files(
    fetcher: Pages | None, outdir: Path, season: int, today: date, refresh: bool
) -> dict[str, tuple[date, str]]:
    """
    file -> (date fetched, CSV). Fetches a file if `refresh` or if none is
    saved, unless `fetcher` is None. A fetched file is only saved if it
    parses, so a bad one never replaces a good one
    """
    files = {}
    for file in FILES:
        saved = latest_file(outdir, season, file)
        if fetcher and (refresh or saved is None):
            text = fetcher.get(path(season, file))
            if text is None:
                print(f"torvik: no {file} for {season} yet")
            else:
                parse(text, file, season)
                save_raw(raw_dir(outdir, season, file) / f"{today}.csv.gz", text)
                saved = (today, text)
        if saved:
            files[file] = saved
    return files


TYPES = {
    "INTEGER": pa.int32(),
    "DOUBLE": pa.float64(),
    "VARCHAR": pa.string(),
    "DATE": pa.date32(),
}


def build_season(
    outdir: Path, season: int, files: dict[str, tuple[date, str]]
) -> list[Path]:
    """write a season's datasets from its CSVs, returning their paths"""
    paths = []
    for file, (fetched, text) in files.items():
        dataset = FILES[file][1]
        rows = [r | {"as_of_date": fetched} for r in parse(text, file, season)]
        cols = columns(SPECS[file])
        table = pa.table(
            {name: pa.array([r[name] for r in rows], TYPES[t]) for name, t in cols}
        )
        con = duckdb.connect()
        con.register("arrow", table)
        con.execute(f"CREATE TABLE {dataset} AS SELECT * FROM arrow ORDER BY team")
        check_keys(con, dataset, ["team"], f"torvik {season} {dataset}")
        out = dataset_path(outdir / OUT_DIR, dataset, season)
        write_parquet(con, dataset, out)
        print(f"torvik: wrote {out} ({len(rows)} rows)")
        paths.append(out)
    return paths


def saved_seasons(outdir: Path) -> list[int]:
    return sorted(
        int(p.name)
        for p in (outdir / RAW_DIR).glob("[0-9][0-9][0-9][0-9]")
        if p.is_dir()
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="barttorvik.com team stats")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help=f"default: the current season. Seasons start in {FIRST_SEASON}",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="only rebuild from saved CSVs (every saved season, without --season)",
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    # college seasons start in November, after the NBA's, so the NBA's
    # September cutover works for them too
    current = current_season(today)
    fetcher = None if args.no_fetch else Fetcher()
    default = saved_seasons(args.out) if args.no_fetch else [current]
    for season in args.season or default:
        files = season_files(fetcher, args.out, season, today, season == current)
        if not files:
            raise SystemExit(f"torvik: no saved CSVs for {season}")
        build_season(args.out, season, files)


if __name__ == "__main__":
    main()
