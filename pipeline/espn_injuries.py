"""
ESPN's NBA injury list, every time it's fetched (#108), built into

    nba/espn/espn_injuries/season=<season>/data.parquet

one row per fetch and listed player: their status (out, day-to-day), the
injury (body part, side, detail), ESPN's expected return date, and its
news blurb. ESPN only publishes the current list, so there's no history
but what we save: each fetch is kept at

    nba/raw/espn/<season>/injuries/<fetched_at>.json.gz

(UTC, from the response's timestamp) and the dataset is rebuilt from every
saved fetch. It's one request, made every update.yml run, so the list
closest to a game's tip-off can be found; it's the pre-game input for
predicting who plays, and player_game_status is what happened.

ESPN's players have ESPN's ids, so they're matched to NBA ids by name:
against that day's saved team rosters (pipeline.stats' team_rosters/,
the latest day on or before the fetch), on the player's team, then against
the season's player_seasons if only one player there has the name (a
player traded since the rosters were saved). Players names can't match
go in player_ids.csv (source "espn", the same ids pipeline.espn_net_pts
uses), which is checked first. A player who doesn't match is left out and
reported (output.report: the workflow publishes, then fails).

usage: python -m pipeline.espn_injuries [--out out] [--no-fetch] [--url URL]
"""

import argparse
import re
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from . import stats
from .bbref import normalize_name
from .catalog import URL
from .espn_odds import espn_team_ids
from .ncaam_espn import Fetcher, Getter, arrow, load_json, save_json
from .output import check_keys, dataset_path, report, write_parquet
from .player_ids import PLAYER_IDS_FILE, load_player_ids
from .rotations import read_stats
from .seasons import EASTERN, current_season, season_window, today_eastern

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
RAW_DIR = Path("nba/raw/espn")
OUT_DIR = Path("nba/espn")
DATASET = "espn_injuries"
KEYS = ["fetched_at", "player_id"]

# during the season a list with fewer players than this is a bad response
# (on 2026-11-12 it had 97); outside it, an empty list is enough
MIN_INJURED = 20

COLUMNS = [
    ("fetched_at", "TIMESTAMPTZ"),
    ("as_of_date", "DATE"),
    ("team_id", "VARCHAR"),
    ("player_id", "VARCHAR"),
    ("status", "VARCHAR"),
    ("fantasy_status", "VARCHAR"),
    ("injury_type", "VARCHAR"),
    ("location", "VARCHAR"),
    ("side", "VARCHAR"),
    ("detail", "VARCHAR"),
    ("return_date", "DATE"),
    ("updated_at", "TIMESTAMPTZ"),
    ("short_comment", "VARCHAR"),
    ("long_comment", "VARCHAR"),
    ("espn_athlete_id", "VARCHAR"),
    ("espn_injury_id", "VARCHAR"),
]


def raw_dir(outdir: Path, season: int) -> Path:
    return outdir / RAW_DIR / str(season) / "injuries"


def raw_path(outdir: Path, season: int, fetched_at: datetime) -> Path:
    stamp = fetched_at.astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    return raw_dir(outdir, season) / f"{stamp}.json.gz"


def fetched_at_of(path: Path) -> datetime:
    stamp = path.name.removesuffix(".json.gz")
    return datetime.strptime(stamp, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=UTC)


def saved_fetches(outdir: Path, season: int) -> list[Path]:
    return sorted(raw_dir(outdir, season).glob("*.json.gz"))


def entries(doc: dict) -> list[dict]:
    return [inj for team in doc.get("injuries", []) for inj in team["injuries"]]


def check_list(doc: dict, today: date, season: int) -> None:
    """raise if `doc` doesn't look like a whole injury list"""
    if not isinstance(doc.get("injuries"), list):
        raise TypeError("espn_injuries: the response has no injuries list")
    start, end = season_window(season)
    n = len(entries(doc))
    if start <= today <= end and n < MIN_INJURED:
        raise ValueError(
            f"espn_injuries: only {n} injured players listed; expected at "
            f"least {MIN_INJURED} during the season"
        )


def fetch(fetcher: Getter, outdir: Path, today: date, now: datetime) -> Path:
    """
    fetch the list, check it, and save it; returns where. It's saved
    under the current season, not the one ESPN labels it with, since
    update.yml downloads only the current season's saved lists: in
    September ESPN may still be on the last one
    """
    doc = fetcher.json(INJURIES_URL)
    if doc is None:
        raise ValueError(f"espn_injuries: {INJURIES_URL} is a 404")
    season = current_season(today)
    check_list(doc, today, season)
    stamp = doc.get("timestamp")
    fetched_at = datetime.fromisoformat(stamp) if stamp else now
    path = raw_path(outdir, season, fetched_at)
    save_json(path, doc)
    print(f"espn_injuries: saved {len(entries(doc))} players to {path}")
    return path


# --- matching ESPN's players to NBA ids


def espn_athlete_id(athlete: dict) -> str | None:
    """ESPN's id, which this endpoint only has in the player's links"""
    for link in athlete.get("links", []):
        m = re.search(r"/id/(\d+)", link.get("href", ""))
        if m:
            return m[1]
    return None


def manual_ids(path: Path = PLAYER_IDS_FILE) -> dict[str, str]:
    """ESPN id -> NBA id from player_ids.csv, for players names don't match"""
    return {
        p.source_id: p.player_id
        for p in load_player_ids(path).values()
        if p.source == "espn"
    }


class Players:
    """
    NBA ids by name: each saved day's rosters, by team, and the season's
    player_seasons
    """

    def __init__(self, outdir: Path, season: int, url: str = URL):
        self.days = stats.saved_roster_days(outdir, season)
        self.rosters: dict[date, dict[tuple[str, str], str]] = {}
        self.season: dict[str, set[str]] = {}
        try:
            rows = read_stats(
                outdir, season, "player_seasons", "SELECT player_id, name FROM t", url
            )
        except duckdb.IOException as e:
            # the bucket answers 403 for a file that isn't there
            print(f"espn_injuries: matching on rosters only; no player_seasons: {e}")
            rows = []
        for pid, name in rows:
            self.season.setdefault(normalize_name(name), set()).add(str(pid))

    def roster(self, day: date) -> dict[tuple[str, str], str]:
        """(team_id, name key) -> player_id on the latest rosters on or before `day`"""
        saved = [(d, p) for d, p in self.days if d <= day] or self.days[:1]
        if not saved:
            return {}
        d, path = saved[-1]
        if d not in self.rosters:
            found = {}
            for team_id, data in stats.read_raw(path)["rosters"].items():
                rs = stats.result_set(data)
                h = rs["headers"]
                pid, name = h.index("PLAYER_ID"), h.index("PLAYER")
                for row in rs["rowSet"]:
                    found[(str(team_id), normalize_name(row[name]))] = str(row[pid])
            self.rosters[d] = found
        return self.rosters[d]

    def match(self, day: date, team_id: str | None, name: str) -> str | None:
        key = normalize_name(name)
        pid = self.roster(day).get((team_id or "", key))
        if pid:
            return pid
        candidates = self.season.get(key, set())
        return next(iter(candidates)) if len(candidates) == 1 else None


# --- building


def date_of(value: str | None) -> date | None:
    """ESPN's "2026-11-13" or "2026-11-13T00:00Z" as a date"""
    return date.fromisoformat(value[:10]) if value else None


def time_of(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def status_of(inj: dict) -> str | None:
    """INJURY_STATUS_DAYTODAY -> day_to_day; else ESPN's text, lowercased"""
    kind = (inj.get("type") or {}).get("name") or ""
    kind = kind.removeprefix("INJURY_STATUS_").lower()
    if kind == "daytoday":
        return "day_to_day"
    if kind:
        return kind
    text = inj.get("status")
    return "_".join(text.lower().replace("-", " ").split()) if text else None


def build_season(outdir: Path, season: int, url: str = URL) -> Path | None:
    """
    Build the season's dataset from every saved fetch; None if there are
    none. Players who can't be matched are left out, and reported after
    the file is written
    """
    paths = saved_fetches(outdir, season)
    if not paths:
        print(f"espn_injuries: no saved lists for {season}")
        return None
    teams = espn_team_ids(season)
    players = Players(outdir, season, url)
    exceptions = manual_ids(PLAYER_IDS_FILE)
    rows, unmatched = [], {}
    for path in paths:
        doc = load_json(path)
        fetched_at = fetched_at_of(path)
        day = fetched_at.astimezone(EASTERN).date()
        for inj in entries(doc):
            athlete = inj.get("athlete") or {}
            name = athlete.get("displayName") or ""
            espn_id = espn_athlete_id(athlete)
            espn_team = str((athlete.get("team") or {}).get("id") or "")
            team_id = teams.get(espn_team)
            player_id = exceptions.get(espn_id or "") or players.match(
                day, team_id, name
            )
            if not player_id or not team_id:
                abbrev = (athlete.get("team") or {}).get("abbreviation")
                unmatched[espn_id or name] = f"{name} ({abbrev}, ESPN id {espn_id})"
                continue
            details = inj.get("details") or {}
            rows.append(
                {
                    "fetched_at": fetched_at,
                    "as_of_date": day,
                    "team_id": team_id,
                    "player_id": player_id,
                    "status": status_of(inj),
                    "fantasy_status": (details.get("fantasyStatus") or {}).get(
                        "abbreviation"
                    ),
                    "injury_type": details.get("type"),
                    "location": details.get("location"),
                    "side": details.get("side"),
                    "detail": details.get("detail"),
                    "return_date": date_of(details.get("returnDate")),
                    "updated_at": time_of(inj.get("date")),
                    "short_comment": inj.get("shortComment"),
                    "long_comment": inj.get("longComment"),
                    "espn_athlete_id": espn_id,
                    "espn_injury_id": str(inj["id"]) if inj.get("id") else None,
                }
            )
    con = duckdb.connect()
    con.register("rows", arrow(rows, COLUMNS))
    con.execute(
        f"""
        CREATE TABLE {DATASET} AS
        SELECT {season}::INTEGER AS season, * FROM rows
        ORDER BY fetched_at, team_id, player_id
        """
    )
    check_keys(con, DATASET, KEYS, f"espn_injuries {DATASET}")
    out = dataset_path(outdir / OUT_DIR, DATASET, season)
    write_parquet(con, DATASET, out)
    print(f"espn_injuries: wrote {out} ({len(paths)} lists, {len(rows)} rows)")
    if unmatched:
        report(
            f"espn_injuries: {len(unmatched)} players on ESPN's injury list "
            "don't match an NBA player, so they're left out (add them to "
            'player_ids.csv, source "espn"): ' + "; ".join(sorted(unmatched.values()))
        )
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ESPN's NBA injury list")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help="season(s) to rebuild (default: current season)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild from saved lists"
    )
    parser.add_argument(
        "--url",
        default=URL,
        help=f"bucket URL for player_seasons if --out has none (default: {URL})",
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    if not args.no_fetch:
        fetch(Fetcher(label="espn_injuries"), args.out, today, datetime.now(UTC))
    for season in args.season or [current_season(today)]:
        build_season(args.out, season, args.url)


if __name__ == "__main__":
    main()
