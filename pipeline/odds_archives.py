"""
NBA betting lines from archives others have published on GitHub, for the
seasons before ESPN's own (#99). pipeline.espn_odds adds their rows to
betting_lines, each archive as its own provider:

- The Odds API's consensus closing line (the median across 9-19 books at
  the last snapshot before tip-off), from sportsdataverse's hoopR-nba-data:
  spread and total, 2020's bubble to 2026, keyed by ESPN event id, so it's
  matched to NBA games through ESPN's scoreboards
- sportsbookreview.com's archive, as scraped by flancast90's
  sportsbookreview-scraper (MIT): opening and closing spread and total and
  closing moneylines, 2012 to 2022, by team nickname and date

Each is downloaded once, from a pinned commit, and kept at

    nba/raw/sportsdataverse/closing_lines_odds_api.parquet
    nba/raw/sbr/nba_archive_10Y.json.gz
"""

import gzip
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from .output import write_atomic


@dataclass(frozen=True)
class Archive:
    provider_id: str
    provider: str
    url: str
    path: Path
    # the seasons it has lines for
    seasons: range


ODDS_API = Archive(
    "odds_api_consensus",
    "The Odds API consensus",
    "https://raw.githubusercontent.com/sportsdataverse/hoopR-nba-data/"
    "7be25dae2ebe5f6027f5343a4ab3195caa61aee5/"
    "nba/betting_lines/closing_lines_odds_api.parquet",
    Path("nba/raw/sportsdataverse/closing_lines_odds_api.parquet"),
    range(2020, 2027),
)
SBR = Archive(
    "sbr",
    "sportsbookreview.com",
    "https://raw.githubusercontent.com/flancast90/sportsbookreview-scraper/"
    "1a820e50c6fbd0276cde073c22bfffae78930868/data/nba_archive_10Y.json",
    Path("nba/raw/sbr/nba_archive_10Y.json.gz"),
    range(2012, 2023),
)
ARCHIVES = (ODDS_API, SBR)

# sportsbookreview's nicknames (current ones throughout: the New Orleans
# Hornets are "Pelicans", the Bobcats "Hornets") -> team_id
SBR_TEAMS = {
    "Hawks": "1610612737",
    "Celtics": "1610612738",
    "Pelicans": "1610612740",
    "Bulls": "1610612741",
    "Cavaliers": "1610612739",
    "Mavericks": "1610612742",
    "Nuggets": "1610612743",
    "Pistons": "1610612765",
    "Warriors": "1610612744",
    "Golden State": "1610612744",
    "Rockets": "1610612745",
    "Pacers": "1610612754",
    "Clippers": "1610612746",
    "LA Clippers": "1610612746",
    "Lakers": "1610612747",
    "Grizzlies": "1610612763",
    "Heat": "1610612748",
    "Bucks": "1610612749",
    "Timberwolves": "1610612750",
    "Nets": "1610612751",
    "NewJersey": "1610612751",
    "Knicks": "1610612752",
    "Thunder": "1610612760",
    "Oklahoma City": "1610612760",
    "Magic": "1610612753",
    "Seventysixers": "1610612755",
    "Suns": "1610612756",
    "Trailblazers": "1610612757",
    "Kings": "1610612758",
    "Spurs": "1610612759",
    "Raptors": "1610612761",
    "Jazz": "1610612762",
    "Wizards": "1610612764",
    "Hornets": "1610612766",
}

# plausible values; sportsbookreview's sheets have typos (a 1955.5 total)
MAX_SPREAD = 40
TOTALS = (150, 300)


def fetch(fetcher, outdir: Path, season: int) -> None:
    """download each archive with lines for `season` that isn't saved"""
    for archive in ARCHIVES:
        path = outdir / archive.path
        if season not in archive.seasons or path.exists():
            continue
        body = fetcher.get(archive.url)
        if body is None:
            raise ValueError(f"odds_archives: {archive.url} is gone")
        if path.name.endswith(".gz"):
            body = gzip.compress(body)
        write_atomic(path, lambda tmp, body=body: tmp.write_bytes(body))
        print(f"odds_archives: saved {path}")


def require(outdir: Path, season: int) -> None:
    """raise if an archive with lines for `season` isn't saved"""
    for archive in ARCHIVES:
        if season in archive.seasons and not (outdir / archive.path).exists():
            raise FileNotFoundError(
                f"odds_archives: {season} needs {archive.path}; "
                "run pipeline.espn_odds without --no-fetch to download it"
            )


def blank(archive: Archive) -> dict:
    """a row with only the provider set"""
    from .odds import LINES

    return {
        "provider_id": archive.provider_id,
        "provider": archive.provider,
        **dict.fromkeys(name for name, _ in LINES),
    }


def odds_api_lines(outdir: Path, event_ids: set[str]) -> list[dict]:
    """The Odds API consensus close for each of `event_ids` it has"""
    rows = duckdb.execute(
        """
        SELECT game_id::VARCHAR, home_point::DOUBLE, over_under::DOUBLE
        FROM read_parquet(?)
        WHERE spread_source = 'odds_api_consensus_close'
        """,
        [str(outdir / ODDS_API.path)],
    ).fetchall()
    return [
        blank(ODDS_API)
        | {"espn_event_id": event_id, "close_spread": spread, "close_total": total}
        for event_id, spread, total in rows
        if event_id in event_ids
    ]


def spread(value) -> float | None:
    return float(value) if abs(value) <= MAX_SPREAD else None


def total(value) -> float | None:
    return float(value) if TOTALS[0] <= value <= TOTALS[1] else None


def moneyline(value) -> int | None:
    return int(value) or None


def sbr_date(r: dict) -> date:
    """
    The game's date. The archive dates 2019-20's bubble games (August to
    October 2020) a year early
    """
    d = str(int(r["date"]))
    day = date(int(d[:4]), int(d[4:6]), int(d[6:]))
    if r["season"] == 2019 and date(2019, 8, 1) <= day <= date(2019, 10, 15):
        day = day.replace(year=2020)
    return day


def sbr_close(r: dict) -> tuple[float | None, float | None]:
    """
    The closing spread and total. In about 7% of rows the two are swapped,
    the total in the spread column and the spread's size in the total
    column, and the spread's sign is lost; the moneylines say which team
    was favored
    """
    s, t = r["home_close_spread"], r["close_over_under"]
    if abs(s) <= MAX_SPREAD:
        return spread(s), total(t)
    if abs(t) <= MAX_SPREAD:
        # the total sometimes has the favorite's sign
        home, away = r["home_close_ml"], r["away_close_ml"]
        if t == 0:
            return 0.0, total(abs(s))
        if home and away and home != away:
            return (-abs(t) if home < away else abs(t)), total(abs(s))
        return None, total(abs(s))
    return None, None


def sbr_lines(outdir: Path, season: int) -> tuple[list[dict], list[dict]]:
    """
    A season's games and lines from the sportsbookreview archive, as
    (events, lines). The archive's seasons are start years. Each season's
    last Finals game is left out: it has no home team, and its lines are in
    the wrong columns
    """
    with gzip.open(outdir / SBR.path, "rt") as f:
        data = json.load(f)
    events, lines = [], []
    for r in data:
        if r["season"] != season - 1 or r["home_team"] == 0:
            continue
        home, away = SBR_TEAMS.get(r["home_team"]), SBR_TEAMS.get(r["away_team"])
        if home is None or away is None:
            raise ValueError(
                f"odds_archives: unknown sportsbookreview team in {r['date']}: "
                f"{r['home_team']!r} or {r['away_team']!r}; add it to SBR_TEAMS"
            )
        day = sbr_date(r)
        key = f"sbr-{day}-{r['home_team']}-{r['away_team']}"
        events.append(
            {
                "key": key,
                "game_date": day,
                "home_team_id": home,
                "away_team_id": away,
            }
        )
        close_spread, close_total = sbr_close(r)
        lines.append(
            blank(SBR)
            | {
                "key": key,
                "open_spread": spread(r["home_open_spread"]),
                "open_total": total(r["open_over_under"]),
                "close_spread": close_spread,
                "close_total": close_total,
                "close_home_moneyline": moneyline(r["home_close_ml"]),
                "close_away_moneyline": moneyline(r["away_close_ml"]),
            }
        )
    return events, lines
