"""
The NBA's official injury report (#109), built into

    nba/stats/injury_report/season=<season>/data.parquet

one row per saved report, game and listed player, for the games the report
lists that hadn't started: the player's status (Out, Doubtful,
Questionable, Probable, Available) and the reason. A prediction made at
some time uses the latest report before it (as with espn_injuries); a
game's last word is its latest report before tip-off. It's the official
pre-game input for predicting who plays; player_game_status is what
happened, and espn_injuries is ESPN's version.

The league publishes the report as a PDF several times a day, at

    https://ak-static.cms.nba.com/referee/injury/Injury-Report_<slot>.pdf

and official.nba.com lists only the current day's, so past reports are
found by guessing their slot. A slot that doesn't exist is a 403. The
slots have changed twice (see slots_before):

    2018-12-17 to the 2021 offseason   1:30, 5:30 and 8:30 PM, "_05PM"
                                       (the 2020 bubble: other hours)
    2021-22 to 2025-12-21              every hour at :30, "_05PM"
    2025-12-22 on                      every 15 minutes, "_05_00PM"

Each report lists that day's and the next day's games. For each tip-off
time in the season's schedule, the last slot before it is fetched (or
the one before that, up to MAX_STEPS, if it's missing), and kept at

    nba/raw/stats/<season>/injury_report/Injury-Report_<slot>.pdf

with the slots that were missing listed in missing.txt there, so they
aren't asked for again. The dataset is rebuilt from every saved report.
The last report before a day's first tip-off is also the one before
that day's earliest games, so it's always fetched. A team that hasn't
reported yet (NOT YET SUBMITTED) has no rows in that report; in 2019 and
2027 none of those had reported in an earlier one either.

The PDFs are parsed by word position (parse_report): wrapped cells are
split above and below their row, so their text lines don't line up.

Players are matched to NBA ids by name ("Last, First"): first among the
players on the game's box score for that team (player_game_status), then
the team's players that season, then the latest saved rosters, then a name
only one of the season's players has. Names that don't match go in
player_ids.csv, source "nba_injury", with the name as printed. A player
who's matched only in another season isn't in this season's
player_seasons (a two-way player who never appeared, say), so they're
left out and listed; a name that matches no one is reported
(output.report: the workflow publishes, then fails).

usage: python -m pipeline.injury_report [--out out] [--season 2026 ...]
           [--no-fetch] [--before-games MINUTES]
"""

import argparse
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from itertools import islice
from pathlib import Path

import duckdb
import pdfplumber

from . import schedule, stats
from .bbref import normalize_name
from .catalog import URL
from .espn_injuries import Players
from .lookups import GAME_TYPES
from .ncaam_espn import Fetcher, Getter, arrow
from .output import check_keys, dataset_path, report, write_atomic, write_parquet
from .player_ids import PLAYER_IDS_FILE, load_player_ids
from .rotations import read_stats
from .seasons import EASTERN, current_season, today_eastern

BASE_URL = "https://ak-static.cms.nba.com/referee/injury/"
DATASET = "injury_report"
KEYS = ["report_time", "game_id", "player_id"]
SOURCE = "nba_injury"
# names with no NBA id at all, whose rows are left out
NO_ID_FILE = Path(__file__).parent / "injury_report_no_id.txt"


def no_id_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    lines = (line.strip() for line in path.read_text().splitlines())
    return {line for line in lines if line and not line.startswith("#")}


MISSING_FILE = "missing.txt"

# the first report: there are none before this
FIRST_REPORT = datetime(2018, 12, 17, 17, 30, tzinfo=EASTERN)
# until the 2021-22 season, three reports a day, at these hours (:30)
HOURLY_FROM = date(2021, 7, 1)
THREE_A_DAY_HOURS = (13, 17, 20)
# but the 2020 bubble's were at other hours (11:30 AM, 5:30 PM, ...)
BUBBLE = (date(2020, 7, 22), date(2020, 10, 15))
# reports every 15 minutes from this day; before it, every hour at :30
QUARTER_HOURS_FROM = date(2025, 12, 22)
# how many slots before a tip-off to try
MAX_STEPS = 6
# and for a game the report before its tip-off doesn't list
MAX_FILL_STEPS = 12
# a game with a report saved this long before its tip-off that doesn't list
# it had no one to report, rather than a report missing
RECENT_REPORT = timedelta(hours=3)
# a slot missing this long after it's due is recorded as missing; a newer
# one may just not be published yet
PUBLISH_LAG = timedelta(hours=1)
# the league's CDN, so go gently
REQUEST_DELAY = 1.5

# the games the league reports on; there are none for preseason or All-Star
REPORTED_TYPES = {"regular_season", "playoffs", "play_in", "cup_final"}

STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available")
# a team with no players listed has one of these instead
NOT_SUBMITTED = "NOT YET SUBMITTED"
ALL_AVAILABLE = "ALL PLAYERS AVAILABLE"
TEAM_NOTES = (NOT_SUBMITTED, ALL_AVAILABLE)

COLUMNS = [
    ("report_time", "TIMESTAMPTZ"),
    ("game_id", "VARCHAR"),
    ("game_date", "DATE"),
    ("team_id", "VARCHAR"),
    ("player_id", "VARCHAR"),
    ("status", "VARCHAR"),
    ("reason", "VARCHAR"),
    ("detail", "VARCHAR"),
]


# --- slots


@dataclass(frozen=True)
class Slot:
    published: datetime  # US Eastern
    name: str  # "2026-11-16_06_00PM", the file name's part

    @property
    def url(self) -> str:
        return f"{BASE_URL}Injury-Report_{self.name}.pdf"

    @property
    def file_name(self) -> str:
        return f"Injury-Report_{self.name}.pdf"


def is_slot(t: datetime) -> bool:
    """whether a report is published at `t` (Eastern, on a quarter hour)"""
    if t < FIRST_REPORT:
        return False
    if t.date() >= QUARTER_HOURS_FROM:
        return t.minute % 15 == 0
    if t.date() < HOURLY_FROM and not BUBBLE[0] <= t.date() <= BUBBLE[1]:
        return t.minute == 30 and t.hour in THREE_A_DAY_HOURS
    return t.minute == 30


def slot_at(t: datetime) -> Slot:
    """the slot published at `t`, which must be one (is_slot)"""
    hour = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    if t.date() >= QUARTER_HOURS_FROM:
        name = f"{t.date()}_{hour:02d}_{t.minute:02d}{ampm}"
    else:
        # an hourly report is named for its hour, and published at :30
        name = f"{t.date()}_{hour:02d}{ampm}"
    return Slot(t, name)


def slots_before(t: datetime) -> Iterator[Slot]:
    """every slot published before `t`, newest first"""
    t = t.astimezone(EASTERN)
    c = t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)
    if c == t:
        c -= timedelta(minutes=15)
    while c >= FIRST_REPORT:
        if is_slot(c):
            yield slot_at(c)
        # step in wall-clock time, then normalize, so DST is handled
        c = (c.replace(tzinfo=None) - timedelta(minutes=15)).replace(tzinfo=EASTERN)


# --- fetching


def raw_dir(outdir: Path, season: int) -> Path:
    return outdir / stats.RAW_DIR / str(season) / DATASET


def saved_reports(outdir: Path, season: int) -> list[Path]:
    return sorted(raw_dir(outdir, season).glob("*.pdf"))


class Reports:
    """
    a season's saved reports, fetching the ones that aren't saved. A report
    is saved under the season whose games it was fetched for, not the one
    its date falls in: the 2020 bubble's playoffs ran into October
    """

    def __init__(
        self, outdir: Path, season: int, fetcher: Getter | None, now: datetime
    ):
        self.dir = raw_dir(outdir, season)
        self.fetcher = fetcher
        self.now = now
        self.missing_path = self.dir / MISSING_FILE
        names = (
            self.missing_path.read_text().split() if self.missing_path.is_file() else []
        )
        self.missing = set(names)

    def mark_missing(self, slot: Slot) -> None:
        self.missing.add(slot.name)
        text = "".join(f"{n}\n" for n in sorted(self.missing))
        write_atomic(self.missing_path, lambda p: p.write_text(text))

    def get(self, slot: Slot) -> Path | None:
        """the slot's saved report, fetched if need be; None if there's none"""
        path = self.dir / slot.file_name
        if path.is_file():
            return path
        if self.fetcher is None or slot.name in self.missing:
            return None
        body = self.fetcher.get(slot.url)
        if body is None:
            if slot.published < self.now - PUBLISH_LAG:
                self.mark_missing(slot)
            return None
        if not body.startswith(b"%PDF"):
            raise ValueError(f"injury_report: {slot.url} isn't a PDF: {body[:100]!r}")
        write_atomic(path, lambda p: p.write_bytes(body))
        print(f"injury_report: saved {path}", flush=True)
        return path

    def before(self, t: datetime) -> Path | None:
        """the last report published before `t`, trying MAX_STEPS slots"""
        for slot in islice(slots_before(t), MAX_STEPS):
            if slot.published > self.now:
                continue
            path = self.get(slot)
            if path:
                return path
        return None


def fetch_season(reports: Reports, tipoffs: list[datetime]) -> None:
    """the report before each tip-off that's passed, and the latest one"""
    missing = []
    for tip in sorted(set(tipoffs)):
        if FIRST_REPORT < tip <= reports.now and not reports.before(tip):
            missing.append(tip)
    if missing:
        print(
            f"injury_report: no report found before {len(missing)} tip-offs: "
            + ", ".join(t.astimezone(EASTERN).isoformat() for t in missing[:20])
        )


# --- parsing


@dataclass
class Row:
    game_date: date
    matchup: str
    team: str
    player: str | None  # None: a team with a note instead of players
    status: str | None
    reason: str | None
    detail: str | None
    note: str | None = None  # TEAM_NOTES


@dataclass
class Report:
    time: datetime
    rows: list[Row] = field(default_factory=list)


# words on one line are within this many points of each other, vertically
LINE_TOLERANCE = 2
# a wrapped cell's lines are this close to their row's (7 points in every
# report seen); a line further from every row on its page is the end of
# the previous page's last row
WRAP_DISTANCE = 15
# header words this close together are one column's name ("Game Date")
HEADER_GAP = 12
# a word belongs to the column whose header starts at most this far right
COLUMN_SLACK = 3

TITLE = re.compile(r"Injury Report: (\d\d/\d\d/\d\d) (\d\d:\d\d [AP]M)")
FOOTER = re.compile(r"Page \d+ of \d+")


def lines_of(words: list[dict]) -> list[tuple[float, list[dict]]]:
    """(top, words left to right) for each line of `words`, top to bottom"""
    lines: list[tuple[float, list[dict]]] = []
    for w in sorted(words, key=lambda w: w["top"]):
        if lines and w["top"] - lines[-1][0] <= LINE_TOLERANCE:
            lines[-1][1].append(w)
        else:
            lines.append((w["top"], [w]))
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in lines]


def text_of(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def header_columns(words: list[dict]) -> list[tuple[str, float]]:
    """(name, x0) of each column, from the header line's words"""
    cols: list[list] = []
    for w in words:
        if cols and w["x0"] - cols[-1][2] < HEADER_GAP:
            cols[-1][0] += " " + w["text"]
            cols[-1][2] = w["x1"]
        else:
            cols.append([w["text"], w["x0"], w["x1"]])
    return [(name, x0) for name, x0, _ in cols]


def column_of(cols: list[tuple[str, float]], x: float) -> str:
    name = cols[0][0]
    for n, x0 in cols:
        if x >= x0 - COLUMN_SLACK:
            name = n
    return name


def join_words(words: list[str]) -> str:
    """a cell's words; a word broken at a hyphen ("Non-" "Displaced") is rejoined"""
    out = ""
    for w in words:
        if out.endswith("-") and len(out) > 1 and out[-2].isalpha():
            out += w
        else:
            out = f"{out} {w}" if out else w
    return out


def report_time(title: str) -> datetime:
    m = TITLE.search(title)
    if not m:
        raise ValueError(f"injury_report: no report time in {title!r}")
    return datetime.strptime(f"{m[1]} {m[2]}", "%m/%d/%y %I:%M %p").replace(
        tzinfo=EASTERN
    )


def date_of(text: str) -> date:
    """the report's 01/10/2019"""
    month, day, year = text.split("/")
    return date(int(year), int(month), int(day))


def is_anchor(cols: list[tuple[str, float]], ws: list[dict]) -> bool:
    """whether a line is a row's own (its status, or NOT YET SUBMITTED)"""
    text = text_of(ws)
    return any(n in text for n in TEAM_NOTES) or any(
        w["text"] in STATUSES and column_of(cols, w["x0"]) == "Current Status"
        for w in ws
    )


def cells_of(path: Path) -> tuple[datetime, list[dict[str, str]]]:
    """the report's time, and each row's cells: column name -> text"""
    time = None
    cols: list[tuple[str, float]] = []
    rows: list[dict[str, list[tuple[int, float, float, str]]]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            body = []
            for top, ws in lines_of(page.extract_words(x_tolerance=1.5)):
                text = text_of(ws)
                if TITLE.search(text):
                    time = time or report_time(text)
                elif any(w["text"] == "Matchup" for w in ws):
                    # later pages repeat it before 2024, and since then don't
                    cols = header_columns(ws)
                elif not FOOTER.fullmatch(text):
                    body.append((top, ws))
            if not body:
                continue
            if not cols:
                raise ValueError(f"injury_report: {path.name} has no header")

            anchors = [top for top, ws in body if is_anchor(cols, ws)]
            first = len(rows)
            rows += [{} for _ in anchors]
            for top, ws in body:
                if anchors:
                    i = min(range(len(anchors)), key=lambda i: abs(anchors[i] - top))
                    far = abs(anchors[i] - top) > WRAP_DISTANCE
                else:
                    i, far = 0, True
                if far and top < (anchors[0] if anchors else float("inf")):
                    # the end of the previous page's last row
                    if first == 0:
                        raise ValueError(
                            f"injury_report: {path.name}: text before the "
                            f"first row: {text_of(ws)!r}"
                        )
                    row = rows[first - 1]
                else:
                    row = rows[first + i]
                for w in ws:
                    cell = row.setdefault(column_of(cols, w["x0"]), [])
                    # in reading order, pages after the first after it
                    cell.append((page.page_number, top, w["x0"], w["text"]))
    if time is None:
        raise ValueError(f"injury_report: {path.name} has no report time")
    return time, [
        {k: join_words([t for *_, t in sorted(v)]) for k, v in row.items()}
        for row in rows
    ]


def reason_of(cells: dict[str, str]) -> tuple[str | None, str | None]:
    """
    (reason, detail): the 2019 layout's Category and Reason columns, or
    later layouts' "Category - detail" split at the first " - "
    """
    if "Category" in cells:
        reason, detail = cells["Category"], cells.get("Reason")
    else:
        text = cells.get("Reason") or ""
        reason, _, detail = text.partition(" - ")

    def text(s: str | None) -> str | None:
        s = (s or "").strip()
        return None if s in ("", "-") else s

    return text(reason), text(detail)


@cache
def parse_report(path: Path) -> Report:
    """a saved report's rows, with the date, matchup and team carried down"""
    time, cells = cells_of(path)
    out = Report(time)
    game_date = matchup = team = None
    for c in cells:
        if c.get("Game Date"):
            game_date = date_of(c["Game Date"])
        matchup = c.get("Matchup") or matchup
        team = c.get("Team") or team
        if not (game_date and matchup and team):
            raise ValueError(f"injury_report: {path.name}: a row with no game: {c}")
        note = next((n for n in TEAM_NOTES if any(n in v for v in c.values())), None)
        if note:
            out.rows.append(Row(game_date, matchup, team, None, None, None, None, note))
            continue
        status = c.get("Current Status")
        if status not in STATUSES:
            raise ValueError(f"injury_report: {path.name}: unknown status: {c}")
        player = c.get("Player Name")
        if not player:
            raise ValueError(f"injury_report: {path.name}: a row with no player: {c}")
        reason, detail = reason_of(c)
        out.rows.append(Row(game_date, matchup, team, player, status, reason, detail))
    if not out.rows:
        raise ValueError(f"injury_report: {path.name} has no rows")
    return out


# --- matching


def flip_name(name: str) -> str:
    """the report's "Last, First" as "First Last": "Udeh, Jr., Ernest" -> "Ernest Udeh, Jr." """
    last, sep, first = name.rpartition(", ")
    return f"{first} {last}" if sep else name


def manual_ids(path: Path = PLAYER_IDS_FILE) -> dict[str, str]:
    """printed name -> NBA id from player_ids.csv"""
    return {
        p.source_id: p.player_id
        for p in load_player_ids(path).values()
        if p.source == SOURCE
    }


def unique(ids: set[str] | None) -> str | None:
    return next(iter(ids)) if ids and len(ids) == 1 else None


class Matcher:
    """NBA ids for the report's names"""

    def __init__(self, outdir: Path, season: int, url: str = URL):
        self.outdir, self.season, self.url = outdir, season, url
        self.exceptions = manual_ids(PLAYER_IDS_FILE)
        self.players = Players(outdir, season, url)
        # (game_id, team_id) and team_id -> name key -> player ids
        self.games: dict[tuple[str, str], dict[str, set[str]]] = {}
        self.teams: dict[str, dict[str, set[str]]] = {}
        names = {pid: key for key, pids in self.players.season.items() for pid in pids}
        try:
            rows = read_stats(
                outdir,
                season,
                "player_game_status",
                "SELECT DISTINCT game_id, team_id, player_id FROM t",
                url,
            )
        except duckdb.IOException as e:
            print(f"injury_report: no player_game_status for {season}: {e}")
            rows = []
        for game_id, team_id, pid in rows:
            key = names.get(str(pid))
            if key:
                g = self.games.setdefault((str(game_id), str(team_id)), {})
                g.setdefault(key, set()).add(str(pid))
                self.teams.setdefault(str(team_id), {}).setdefault(key, set()).add(
                    str(pid)
                )
        self.others: dict[str, set[str]] | None = None
        self.season_ids = {p for pids in self.players.season.values() for p in pids}
        self.no_id = no_id_names(NO_ID_FILE)

    def match(self, name: str, game_id: str, team_id: str, day: date) -> str | None:
        if name in self.exceptions:
            return self.exceptions[name]
        key = normalize_name(flip_name(name))
        return (
            unique(self.games.get((game_id, team_id), {}).get(key))
            or unique(self.teams.get(team_id, {}).get(key))
            or self.players.match(day, team_id, flip_name(name))
        )

    def in_other_season(self, name: str) -> bool:
        """whether a player with the name is in another season's player_seasons"""
        if self.others is None:
            self.others = {}
            for s in range(2010, current_season(today_eastern()) + 1):
                if s == self.season:
                    continue
                try:
                    rows = read_stats(
                        self.outdir, s, "player_seasons", "SELECT name FROM t", self.url
                    )
                except duckdb.IOException:
                    rows = []
                for (n,) in rows:
                    if n:
                        self.others.setdefault(normalize_name(n), set()).add(str(s))
        return normalize_name(flip_name(name)) in self.others


# --- building


@dataclass
class Game:
    game_id: str
    game_date: date
    start_time: datetime
    home_team_id: str | None
    away_team_id: str | None
    final: bool


def season_games(outdir: Path, season: int, url: str = URL) -> list[Game]:
    """
    the season's reported games (REPORTED_TYPES), from the saved schedule
    response if there is one, else the schedule dataset (in `outdir`, or
    the bucket); empty if there's neither
    """
    raw = stats.raw_path(outdir, season, stats.SCHEDULE)
    if raw.is_file():
        rows = [schedule.row(g) for g in schedule.games(stats.read_raw(raw))]
        found = [
            (
                r["game_id"],
                r["game_date"],
                r["start_time"],
                r["home_team_id"],
                r["away_team_id"],
                r["status"],
            )
            for r in rows
        ]
    else:
        try:
            found = read_stats(
                outdir,
                season,
                "schedule",
                "SELECT game_id, game_date, start_time, home_team_id, "
                "away_team_id, status FROM t",
                url,
            )
        except duckdb.IOException as e:
            print(f"injury_report: no schedule for {season}: {e}")
            found = []
    return [
        Game(gid, day, start, home, away, status == "final")
        for gid, day, start, home, away, status in found
        if GAME_TYPES.get(gid[:3]) in REPORTED_TYPES
    ]


def team_ids(outdir: Path, season: int, url: str = URL) -> dict[str, str]:
    """the report's team names -> team_id"""
    rows = read_stats(
        outdir,
        season,
        "team_seasons",
        "SELECT full_name, team_id FROM t WHERE is_nba",
        url,
    )
    if not rows:
        raise ValueError(f"injury_report: no team_seasons for {season}")
    return {name: str(tid) for name, tid in rows}


def build_season(
    outdir: Path, season: int, now: datetime, url: str = URL
) -> Path | None:
    """
    Build the season's dataset from every saved report; None if there are
    none. Players who can't be matched are left out, and reported after
    the file is written
    """
    paths = saved_reports(outdir, season)
    if not paths:
        print(f"injury_report: no saved reports for {season}")
        return None
    games = season_games(outdir, season, url)
    if not games:
        raise ValueError(f"injury_report: no schedule for {season}")
    by_team = {}
    for g in games:
        for team in (g.home_team_id, g.away_team_id):
            if team:
                by_team[(g.game_date, team)] = g
    teams = team_ids(outdir, season, url)

    # every report's rows for the games it lists that haven't started, and
    # each team's note (NOT YET SUBMITTED) in the last report before its game
    listed: list[tuple[Report, Game, str, Row]] = []
    last_note: dict[tuple[str, str], tuple[datetime, str | None, str]] = {}
    unknown_games = set()
    for path in paths:
        rep = parse_report(path)
        for row in rep.rows:
            # All-Star weekend's teams aren't NBA teams
            team_id = teams.get(row.team, "")
            game = by_team.get((row.game_date, team_id))
            if game is None:
                # postponed, or the schedule doesn't have it
                unknown_games.add(f"{row.game_date} {row.matchup}")
                continue
            if rep.time >= game.start_time:
                continue
            listed.append((rep, game, team_id, row))
            key = (game.game_id, team_id)
            if key not in last_note or last_note[key][0] <= rep.time:
                last_note[key] = (rep.time, row.note, row.team)
    reported = {game_id for game_id, _ in last_note}
    if unknown_games:
        print(
            f"injury_report: {len(unknown_games)} games in the reports aren't "
            f"in the schedule: {sorted(unknown_games)[:10]}"
        )

    matcher = Matcher(outdir, season, url)
    # a player is listed in many reports; match each name once per game
    matched: dict[tuple[str, str, str], str | None] = {}
    rows, unmatched, elsewhere = [], {}, set()
    for rep, game, team_id, row in listed:
        if row.player is None:
            continue
        key = (row.player, game.game_id, team_id)
        if key not in matched:
            matched[key] = matcher.match(
                row.player, game.game_id, team_id, game.game_date
            )
        pid = matched[key]
        label = f"{row.player} ({row.team})"
        if pid is None:
            if row.player in matcher.no_id or matcher.in_other_season(row.player):
                elsewhere.add(label)
            else:
                unmatched[row.player] = label
            continue
        if pid not in matcher.season_ids:
            # the integrity check would reject it
            elsewhere.add(label)
            continue
        rows.append(
            {
                "report_time": rep.time,
                "game_id": game.game_id,
                "game_date": game.game_date,
                "team_id": team_id,
                "player_id": pid,
                "status": row.status,
                "reason": row.reason,
                "detail": row.detail,
            }
        )
    # teams that still hadn't reported just before games that have started
    game_of = {g.game_id: g for g in games}
    not_submitted = sorted(
        f"{game_of[game_id].game_date} {team}"
        for (game_id, _), (_, note, team) in last_note.items()
        if note == NOT_SUBMITTED and game_of[game_id].start_time <= now
    )

    con = duckdb.connect()
    con.register("rows", arrow(rows, COLUMNS))
    con.execute(
        f"""
        CREATE TABLE {DATASET} AS
        SELECT {season}::INTEGER AS season, * FROM rows
        ORDER BY report_time, game_date, game_id, team_id, player_id
        """
    )
    check_keys(con, DATASET, KEYS, f"injury_report {DATASET}")
    out = dataset_path(outdir / stats.OUT_DIR, DATASET, season)
    write_parquet(con, DATASET, out)
    print(
        f"injury_report: wrote {out} ({len(paths)} reports, {len(reported)} "
        f"games, {len(rows)} rows)"
    )
    if not_submitted:
        print(
            f"injury_report: {len(not_submitted)} teams hadn't submitted "
            f"before tip-off: {not_submitted[:10]}"
        )
    if elsewhere:
        print(
            f"injury_report: {len(elsewhere)} players aren't in {season}'s "
            f"player_seasons, so they're left out: {sorted(elsewhere)}"
        )

    # a played game with no saved report shortly before it: a missing slot.
    # One that's saved but doesn't list the game had nobody to report: since
    # 2021-22 a game with no one listed is left off (before, its teams were
    # "ALL PLAYERS AVAILABLE"). Nothing's expected before the first report
    times = sorted({parse_report(p).time for p in paths})
    missed, unlisted = [], []
    for g in missed_games(games, reported, now):
        recent = any(g.start_time - RECENT_REPORT <= t < g.start_time for t in times)
        (unlisted if recent else missed).append(f"{g.game_date} {g.game_id}")
    if unlisted:
        print(
            f"injury_report: {len(unlisted)} played games aren't on the report "
            f"before them, so no one was listed: {unlisted[:10]}"
        )
    problems = []
    if missed:
        problems.append(
            f"{len(missed)} played games have no report before tip-off: "
            + ", ".join(sorted(missed)[:20])
        )
    if unmatched:
        problems.append(
            f"{len(unmatched)} players on the injury report don't match an "
            f'NBA player, so they\'re left out (add them to player_ids.csv, source "{SOURCE}"): '
            + "; ".join(sorted(unmatched.values()))
        )
    if problems:
        report("injury_report: " + "; ".join(problems))
    return out


def missed_games(games: list[Game], reported: set[str], now: datetime) -> list[Game]:
    """played games with no saved report before tip-off that lists them"""
    return [
        g
        for g in games
        if g.final and FIRST_REPORT < g.start_time <= now and g.game_id not in reported
    ]


def lists(rep: Report, game: Game, teams: dict[str, str]) -> bool:
    """whether a report lists `game`, before it starts"""
    sides = {game.home_team_id, game.away_team_id}
    return rep.time < game.start_time and any(
        r.game_date == game.game_date and teams.get(r.team) in sides for r in rep.rows
    )


def fill_missed(
    reports: Reports, outdir: Path, season: int, games: list[Game], url: str = URL
) -> None:
    """
    For a played game the report before its tip-off doesn't list (close to
    a playoff tip-off the league drops the game from the report, and it
    sometimes leaves a game out), step back further, to the last report
    that does
    """
    teams = team_ids(outdir, season, url)
    reps = [parse_report(p) for p in saved_reports(outdir, season)]
    reported = {g.game_id for g in games for rep in reps if lists(rep, g, teams)}
    for game in missed_games(games, reported, reports.now):
        for slot in islice(slots_before(game.start_time), MAX_FILL_STEPS):
            if slot.published > reports.now:
                continue
            path = reports.get(slot)
            if path and lists(parse_report(path), game, teams):
                print(f"injury_report: {game.game_id} is in {path.name}")
                break


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="The NBA's injury report")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help="season(s) to update (default: current season)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild from saved reports"
    )
    parser.add_argument(
        "--url",
        default=URL,
        help=f"bucket URL for the stats datasets if --out has none (default: {URL})",
    )
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    fetcher = (
        None
        if args.no_fetch
        else Fetcher(
            delay=REQUEST_DELAY,
            label="injury_report",
            not_found=(403, 404),
            # a PDF is ~100KB; a connection that stalls is retried sooner
            timeout=30,
        )
    )
    for season in args.season or [current_season(today_eastern())]:
        if fetcher:
            reports = Reports(args.out, season, fetcher, now)
            games = season_games(args.out, season, args.url)
            if not games and season != current_season(today_eastern()):
                # past seasons' schedules are fetched for their tip-off times
                stats.fetch_schedule(args.out, season)
                games = season_games(args.out, season, args.url)
            fetch_season(reports, [g.start_time for g in games])
            if season == current_season(today_eastern()):
                # the newest, for the games still to come
                reports.before(now)
            fill_missed(reports, args.out, season, games, args.url)
        build_season(args.out, season, now, args.url)


if __name__ == "__main__":
    main()
