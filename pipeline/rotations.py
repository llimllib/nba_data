"""
Build a per-season parquet file of player stints, for rotation charts:
when each player checked in and out of a game, their points, and the score
change while they were on the floor. The stints are derived from each
game's play-by-play (playbyplayv3) and box score (boxscoretraditionalv3)
from stats.nba.com.

The NBA has an endpoint for stints, gamerotation, but it takes 30 seconds a
game when it works, often returns a 500 instead, and has nothing for the
playoffs. Derived from the play-by-play, the stints match it where it's
right, and every player's stints add up to their box score plus-minus and
minutes (checked on 117 games from 2010 to 2027; see docs/v2.md).

It's two requests a game (and rarely a third), so unlike pipeline.stats
this only fetches the games it doesn't have yet, plus the last RECENT days'
games, which may still be corrected. The games come from the season's
`games` table, from --out if it's there and the bucket otherwise. Each
game's responses are kept together:

    nba/raw/stats/<season>/playbyplay/<game_id>.json.gz
    {"playbyplay": ..., "boxscore": ..., "period_boxscores": {period: ...}}

and the stints are built from them:

    nba/stats/game_rotations/season=<season>/data.parquet

along with every action in the play-by-play and the score after it, for
score-margin (worm) charts and anything else that needs the plays:

    nba/stats/game_events/season=<season>/data.parquet

Games are fetched WORKERS at a time, newest first. Games that fail are
tried again once at the end of the run, after a pause, and left for the
next run if they fail again. stats.nba.com throttles after ~600 requests, so when
half the recent games have failed the run pauses (5, 10, then 20 minutes)
and carries on; if it's still failing after that, it stops. Either way the
games it got are built and written, but a run that stopped exits with an
error. --minutes stops starting games after that long (not an error).

How the stints are derived:

- A substitution row has the id of the player going out; the player coming
  in is only a name in the description ("SUB: Harper FOR Fox"). Names are
  matched against the names the play-by-play uses for each player: as the
  one going out of other subs (which covers renames: "Kanter" is Enes
  Freedom), in their other actions, and the box score's names, including
  "Jal. Williams"-style prefixes. Failing that, a family name with a suffix
  ("Porter" for "Porter Jr."), a first name ("Cui" for Cui Yongxi, whose
  box score has his names the other way round), or the only teammate who
  played and is never named.
- The play-by-play doesn't say who starts a period. Anyone with an action
  in it, or subbed out of it, before they're subbed in was on the floor at
  its start (technicals and ejections don't count; bench players get
  those). If that finds fewer than five, someone played without doing
  anything, and a box score of just that period says who played.
- Free throws count for the players who were on the floor for the foul, so
  a sub between free throws takes effect after them, unless the player
  coming in shoots them (for an injured or ejected player). Technical free
  throws count for the players on when the technical was called, except a
  flopping technical's, which count for the players on when they're shot.
  That's how the NBA's box score plus-minus works.
- Games without a game clock (the 2025 All-Star games, played to a target
  score) have no stints.
- The score is counted from the made shots and free throws, not read
  from the rows' scores, which are sometimes wrong (the final score at the
  end of the 1st quarter, 0-0 on misses in older games, a basket listed
  out of order already in an earlier row's score). Counted, it matches the
  final score of all 461 games checked.

The build checks every game: each player's stints must add up to their
plus-minus in player_game_logs and their minutes in the box score, with
five players a side on the floor. Games that don't are kept, with
matches_box_score false, and listed; it's usually the NBA's data that's
wrong (a missed substitution, plays in the wrong period).

usage: python -m pipeline.rotations [--out out] [--season 2026 ...] [--no-fetch] [--minutes 10] [--workers 1]
"""

import argparse
import re
import time
import unicodedata
from collections import defaultdict, deque
from collections.abc import Callable, Collection
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
from nba_api.stats.endpoints import BoxScoreTraditionalV3, PlayByPlayV3

from . import stats
from .catalog import URL
from .output import check_keys, dataset_path, write_parquet
from .seasons import current_season, today_eastern

NAME = "game_rotations"
KEYS = ["game_id", "player_id", "in_time"]
EVENTS = "game_events"
EVENTS_KEYS = ["game_id", "action_id"]
# rows per row group in game_events, which has ~500 rows a game, so a
# reader skipping row groups by game_id (hyparquet's filter does) reads
# ~32 games' rows rather than half the season. Smaller groups read fewer
# rows but grow the footer, which has every group's statistics: for one
# game of 2026's worm columns, 8192 reads a 212KB footer and 24KB of rows,
# 16384 106KB and 43KB, 32768 55KB and 92KB, 65536 28KB and 180KB
EVENTS_ROW_GROUP = 16384

# games this recent are refetched every run, for the NBA's corrections
RECENT = timedelta(days=2)
RETRY_DELAYS = [5, 30]
TIMEOUT = 60
# games in flight at once, and the time between starting them. Two at a
# time a second apart got rate limited after ~600 requests
WORKERS = 1
REQUEST_DELAY = 2
# stats.nba.com throttles after ~600 requests, and then lets a few through,
# so a run watches the failure rate of its last WINDOW games. When at least
# THROTTLED of them (and MIN_WINDOW games) have failed, it pauses for the
# next of PAUSES seconds and carries on; after the last one, it stops. A
# healthy window after a pause resets them
WINDOW = 20
MIN_WINDOW = 10
THROTTLED = 0.5
PAUSES = [300, 600, 1200]
# the wait before trying the run's failed games again: they're mostly from a
# stretch of throttling, which has usually passed by the end of the run
RETRY_PAUSE = 60

# a player's minutes may differ from the box score's by this much, in seconds
MINUTES_TOLERANCE = 6

# actions a player can have while on the bench
OFF_FLOOR = re.compile(r"technical|t\.foul|ejection", re.IGNORECASE)
SUB = re.compile(r"SUB: (?P<incoming>.+) FOR (?P<outgoing>.+)$")


def raw_path(outdir: Path, season: int, game_id: str) -> Path:
    return outdir / stats.RAW_DIR / str(season) / "playbyplay" / f"{game_id}.json.gz"


def saved_games(outdir: Path, season: int) -> list[Path]:
    return sorted(
        (outdir / stats.RAW_DIR / str(season) / "playbyplay").glob("*.json.gz")
    )


def read_stats(
    outdir: Path, season: int, dataset: str, query: str, url: str = URL
) -> list[tuple]:
    """
    `query` (FROM t) over a season of one of pipeline.stats' datasets, from
    `outdir` if it has the file, else from the bucket at `url`. Empty if
    there's no file
    """
    local = dataset_path(outdir / stats.OUT_DIR, dataset, season)
    if local.is_file():
        source = str(local)
    else:
        source = f"{url}/{stats.OUT_DIR}/{dataset}/season={season}/data.parquet"
    # its own connection: a failed read aborts the connection's transaction
    try:
        with duckdb.connect() as con:
            return con.sql(
                f"WITH t AS (FROM read_parquet('{source}', hive_partitioning = false)) {query}"
            ).fetchall()
    except duckdb.HTTPException as e:
        if "404" in str(e):
            return []
        raise


def season_games(outdir: Path, season: int, url: str = URL) -> list[tuple[str, date]]:
    """(game_id, game_date) for a season's games; empty if there are none yet"""
    return read_stats(
        outdir,
        season,
        "games",
        "SELECT game_id, game_date FROM t ORDER BY game_date, game_id",
        url,
    )


@dataclass
class Logged:
    """a player's line in player_game_logs"""

    team_id: int
    seconds: float
    pts: int
    plus_minus: int


def game_logs(
    outdir: Path, season: int, url: str = URL
) -> dict[str, dict[int, Logged]]:
    """
    game_id -> player id -> their line, from the season's player_game_logs.
    They're for checking and for player ids, so if they can't be read it's
    empty
    """
    found = defaultdict(dict)
    try:
        rows = read_stats(
            outdir,
            season,
            "player_game_logs",
            "SELECT game_id, player_id, team_id, min::DOUBLE, pts, plus_minus FROM t",
            url,
        )
    except duckdb.IOException as e:
        print(f"rotations: checking against the box score; no game logs: {e}")
        rows = []
    for game_id, player_id, team_id, minutes, pts, pm in rows:
        found[game_id][int(player_id)] = Logged(
            int(team_id), (minutes or 0) * 60, pts or 0, pm
        )
    return found


def logged_ids(game: dict, logged: dict[int, Logged]) -> dict[int, int]:
    """
    box score id -> game log id, for players the game logs have under
    another id. The NBA gives some players on international teams in
    preseason a different id in each (Jordan Hunter of South East Melbourne
    is 196295505 in the box score and 196295493 in the game logs), and
    player_seasons comes from the game logs. A player is matched to the
    only teammate missing from the box score with their points and minutes
    """
    ids = {}
    box_ids = {p["personId"] for t in box_teams(game["boxscore"]) for p in t["players"]}
    for t in box_teams(game["boxscore"]):
        unmatched = {
            pid: line
            for pid, line in logged.items()
            if line.team_id == t["teamId"] and pid not in box_ids
        }
        for p in t["players"]:
            pid, st = p["personId"], p["statistics"]
            secs = minutes_seconds(st["minutes"])
            if pid in logged or not secs:
                continue
            same = [
                log_id
                for log_id, line in unmatched.items()
                if line.pts == (st.get("points") or 0)
                and abs(line.seconds - secs) <= MINUTES_TOLERANCE
            ]
            if len(same) == 1:
                ids[pid] = same[0]
                del unmatched[same[0]]
    return ids


# --- time


def period_start(period: int) -> int:
    """seconds from the start of the game: periods are 720s, overtimes 300s"""
    return (period - 1) * 720 if period <= 4 else 2880 + (period - 5) * 300


def elapsed(period: int, clock: str) -> float:
    """seconds from the start of the game, from a period and "PT07M13.00S" left"""
    m = re.fullmatch(r"PT(\d+)M([\d.]+)S", clock)
    if not m:
        raise ValueError(f"rotations: unexpected clock {clock!r}")
    return period_start(period + 1) - (int(m[1]) * 60 + float(m[2]))


def minutes_seconds(minutes: str) -> float:
    """seconds from the box score's "29:18" (or "" for didn't play)"""
    if not minutes:
        return 0.0
    m, s = minutes.split(":")
    return int(m) * 60 + float(s)


# --- the box score


def box_teams(box: dict) -> list[dict]:
    b = box["boxScoreTraditional"]
    return [b["homeTeam"], b["awayTeam"]]


def played(box: dict, team_id: int) -> dict[int, float]:
    """`team_id`'s players with minutes in `box`: id -> seconds"""
    return {
        p["personId"]: secs
        for t in box_teams(box)
        if t["teamId"] == team_id
        for p in t["players"]
        if (secs := minutes_seconds(p["statistics"]["minutes"]))
    }


def norm(name: str) -> str:
    """lowercase without accents: "Schröder" -> "schroder" """
    name = unicodedata.normalize("NFKD", name)
    return "".join(c for c in name if not unicodedata.combining(c)).lower().strip()


class Names:
    """the players a substitution's name could be, by team"""

    def __init__(self, box: dict, actions: list[dict]):
        self.team_of: dict[int, int] = {}
        self.names: dict[int, dict[str, set[int]]] = {}
        for t in box_teams(box):
            names = self.names[t["teamId"]] = defaultdict(set)
            for p in t["players"]:
                pid, first, family = p["personId"], p["firstName"], p["familyName"]
                self.team_of[pid] = t["teamId"]
                # "Jal. Williams": as much of the first name as tells them apart
                prefixes = [f"{first[:k]}. {family}" for k in range(1, len(first) + 1)]
                for n in (family, p["nameI"], f"{first} {family}", *prefixes):
                    names[norm(n)].add(pid)
        # the play-by-play's own names: a player's actions, and the player
        # going out of a sub ("SUB: Smart FOR Kanter" has Freedom's id)
        for a in actions:
            team = self.team_of.get(a["personId"])
            if team is None:
                continue
            if a["playerName"]:
                self.names[team][norm(a["playerName"])].add(a["personId"])
            if m := sub(a):
                self.names[team][norm(m["outgoing"])].add(a["personId"])
        self._fallbacks(box, actions)

    def _fallbacks(self, box: dict, actions: list[dict]) -> None:
        """
        For incoming names that match no one: a family name with a suffix
        ("Porter" for "Porter Jr."), or a first name ("Cui" for Cui Yongxi,
        whose box score has his names the other way round). And the players
        who played but no sub names, for incoming() to choose from by
        elimination (a player who was renamed, and never went out to teach
        us the old name)
        """
        named = set()
        for a in actions:
            if (team := self.team_of.get(a["personId"])) and (m := sub(a)):
                named.add(a["personId"])
                if len(ids := self.lookup(team, m["incoming"])) == 1:
                    named |= ids
        self.unnamed: dict[int, set[int]] = {}
        for t in box_teams(box):
            team = t["teamId"]
            players = [
                p for p in t["players"] if minutes_seconds(p["statistics"]["minutes"])
            ]
            self.unnamed[team] = {
                p["personId"] for p in players if p["personId"] not in named
            }
            for a in actions:
                if self.team_of.get(a["personId"]) != team or not (m := sub(a)):
                    continue
                n = norm(m["incoming"])
                if self.names[team].get(n):
                    continue
                suffixed = {
                    p["personId"]
                    for p in players
                    if norm(p["familyName"]).startswith(n + " ")
                }
                first = {p["personId"] for p in players if norm(p["firstName"]) == n}
                for ids in (suffixed, first):
                    if len(ids) == 1:
                        self.names[team][n] = ids
                        break

    def lookup(self, team: int, name: str) -> set[int]:
        return self.names[team].get(norm(name), set())

    def incoming(self, action: dict, on: Collection = ()) -> int | None:
        """
        the id of the player a sub brings in, if it's clear: not one in
        `on`, the players on the floor. A name that matches no one is the
        only player off the floor who played and is never named
        """
        m = sub(action)
        team = self.team_of.get(action["personId"])
        if not m or team is None:
            return None
        ids = self.lookup(team, m["incoming"])
        if not ids:
            ids = self.unnamed[team]
        if len(ids) > 1:
            ids = ids - set(on)
        return next(iter(ids)) if len(ids) == 1 else None


def sub(action: dict) -> re.Match | None:
    if action["actionType"] != "Substitution":
        return None
    return SUB.match(action["description"])


# --- deriving the stints


@dataclass
class Stint:
    team_id: int
    player_id: int
    period: int
    start: float
    end: float
    # the home team's lead when the stint started and ended
    lead_start: int
    lead_end: int = 0
    pts: int = 0


def points(action: dict) -> int:
    """
    the points `action` scores, for its team. The score is counted from
    these rather than read from the rows' scores, which are sometimes wrong:
    a period's start or end can have the final score, older games put 0-0 on
    rows that don't score, and a basket listed out of order can be in an
    earlier row's score
    """
    if action["actionType"] == "Made Shot":
        return action["shotValue"]
    if action["actionType"] == "Free Throw" and not action["description"].startswith(
        "MISS"
    ):
        return 1
    return 0


def period_actions(actions: list[dict]) -> dict[int, list[dict]]:
    periods = defaultdict(list)
    for a in actions:
        periods[a["period"]].append(a)
    return dict(sorted(periods.items()))


def inferred_starters(acts: list[dict], names: Names) -> dict[int, set[int]]:
    """
    team -> the players who started a period: anyone with an action, or
    subbed out, before they're subbed in
    """
    starters = {team: set() for team in names.names}
    subbed_in, out = set(), set()
    for a in acts:
        pid = a["personId"]
        team = names.team_of.get(pid)
        if team is None:
            continue
        if sub(a):
            if pid not in subbed_in:
                starters[team].add(pid)
            out.add(pid)
            # who's on as far as we know so far, for incoming()
            on = (starters[team] | subbed_in) - out
            if (incoming := names.incoming(a, on)) is not None:
                subbed_in.add(incoming)
                out.discard(incoming)
        elif pid not in subbed_in and not OFF_FLOOR.search(
            " ".join((a["actionType"], a["subType"], a["description"]))
        ):
            starters[team].add(pid)
    return starters


def missing_starters(game: dict) -> list[int]:
    """the periods where a team has fewer than five inferred starters"""
    actions = game["playbyplay"]["game"]["actions"]
    names = Names(game["boxscore"], actions)
    return [
        period
        for period, acts in period_actions(actions).items()
        if any(len(s) < 5 for s in inferred_starters(acts, names).values())
    ]


def lead_at_sub(
    acts: list[dict],
    i: int,
    lead: int,
    home: int,
    names: Names | None = None,
    incoming: int | None = None,
) -> int:
    """
    the home team's lead when the sub at acts[i], bringing in `incoming`,
    takes effect, from `lead` before it. Free throws at the same clock count
    for the players on for the foul, so the sub takes effect after them,
    unless a new foul comes first, or a player coming in at this sub or a
    later one of the team's shoots them (they weren't on for the foul;
    they're shooting for an injured or ejected player). Technical free
    throws count for the players on when the technical was called, except a
    flopping technical's, which count for the players on when they're shot
    """
    same_clock = []
    for b in acts[i + 1 :]:
        if b["clock"] != acts[i]["clock"]:
            break
        same_clock.append(b)
    before = []
    for b in reversed(acts[:i]):
        if b["clock"] != acts[i]["clock"] or b["period"] != acts[i]["period"]:
            break
        before.append(b)
    # the players coming in who weren't on for the foul: a player going out
    # and straight back in was
    went_out = {b["personId"] for b in before if sub(b)} | {acts[i]["personId"]}
    coming_in = {incoming} - went_out
    shot_by_sub = False
    for b in same_clock:
        if b["actionType"] == "Foul" and "Technical" not in b["subType"]:
            break
        if sub(b) and b["teamId"] == acts[i]["teamId"] and names is not None:
            coming_in |= {names.incoming(b)} - went_out
            went_out.add(b["personId"])
        if b["actionType"] == "Free Throw" and b["personId"] in coming_in - {None}:
            shot_by_sub = True
            break
    fouls = [b for b in before if b["actionType"] == "Foul"]
    flops = [b for b in fouls if "Flopping" in b["subType"]]
    technical_before = not flops or any(
        OFF_FLOOR.search(f"{b['subType']} {b['description']}")
        for b in fouls
        if b not in flops
    )
    for b in same_clock:
        if b["actionType"] == "Foul":
            if "Technical" not in b["subType"]:
                break
            technical_before = False
            continue
        if b["actionType"] != "Free Throw":
            continue
        technical = "Technical" in b["subType"]
        if (technical and technical_before) or (not technical and not shot_by_sub):
            lead += points(b) if b["teamId"] == home else -points(b)
    return lead


def derive(game: dict) -> tuple[list[Stint], list[str]]:
    """
    A game's stints, from its saved responses, and any problems found:
    names that can't be matched, periods without five starters, subs of
    players who aren't on. Stints that continue across periods are joined
    """
    actions = game["playbyplay"]["game"]["actions"]
    box = game["boxscore"]
    names = Names(box, actions)
    home = box["boxScoreTraditional"]["homeTeamId"]
    period_boxes = game.get("period_boxscores", {})
    problems = []
    stints: list[Stint] = []
    last: dict[int, Stint] = {}
    # the home team's lead
    lead = 0

    def close(stint: Stint, end: float, lead: int) -> None:
        stint.end, stint.lead_end = end, lead
        stints.append(stint)

    for period, acts in period_actions(actions).items():
        starters = inferred_starters(acts, names)
        for team, players in starters.items():
            if len(players) < 5 and str(period) in period_boxes:
                # someone played the period without doing anything; the
                # period's box score says who played. Its range takes in a
                # few seconds either side, so take those who played most
                in_period = played(period_boxes[str(period)], team)
                subbed_in = {names.incoming(a) for a in acts if sub(a)}
                others = sorted(
                    set(in_period) - subbed_in - players,
                    key=lambda pid: -in_period[pid],
                )
                players |= set(others[: 5 - len(players)])
            if len(players) != 5:
                problems.append(f"period {period}: {len(players)} starters for {team}")
        start = period_start(period)
        on = {
            pid: Stint(team, pid, period, start, start, lead)
            for team, players in starters.items()
            for pid in players
        }
        for i, a in enumerate(acts):
            if pts := points(a):
                lead += pts if a["teamId"] == home else -pts
                # the scorer's points go to their stint, or their last one
                # (a free throw after they've gone out)
                pid = a["personId"]
                if (stint := on.get(pid) or last.get(pid)) is not None:
                    stint.pts += pts
            if not sub(a) or a["personId"] not in names.team_of:
                continue
            t = elapsed(period, a["clock"])
            outgoing = a["personId"]
            stint = on.pop(outgoing, None)
            incoming = names.incoming(a, on)
            at_sub = lead_at_sub(acts, i, lead, home, names, incoming)
            if stint is not None:
                close(stint, t, at_sub)
                last[outgoing] = stint
            else:
                problems.append(f"{a['description']} at {t}s: {outgoing} isn't on")
            if incoming is None:
                problems.append(f"no player for {a['description']!r} at {t}s")
            elif incoming in on:
                problems.append(f"{a['description']} at {t}s: {incoming} is already on")
            else:
                team = names.team_of[outgoing]
                on[incoming] = Stint(team, incoming, period, t, t, at_sub)
        for pid, stint in on.items():
            close(stint, period_start(period + 1), lead)
            last[pid] = stint
    return joined(stints), problems


def joined(stints: list[Stint]) -> list[Stint]:
    """
    join each player's stints that end and restart at the end of a period.
    And a stint with no time that starts when the player's next one does
    (subbed out and back in twice at one clock, around free throws: Bagley
    in 0022100968) is part of the next one, so they don't share a key
    """
    boundaries = {period_start(p) for p in range(2, 30)}
    out: list[Stint] = []
    for s in sorted(stints, key=lambda s: (s.team_id, s.player_id, s.start, s.end)):
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.player_id == s.player_id
            and prev.end == s.start
            and (
                s.start in boundaries
                or (prev.start == s.start and prev.lead_end == s.lead_start)
            )
        ):
            prev.end, prev.lead_end, prev.pts = s.end, s.lead_end, prev.pts + s.pts
            continue
        out.append(s)
    # a sub in and out at the same moment, with nothing scored, isn't a stint
    return [s for s in out if s.end > s.start or s.lead_end != s.lead_start or s.pts]


@dataclass
class Event:
    """a play-by-play action, and the score after it"""

    action: dict
    # None for a game with no game clock
    time: float | None
    pts: int
    home_score: int
    away_score: int


def events(game: dict) -> list[Event]:
    """
    A game's actions in the play-by-play's order, with the score counted
    as derive() counts it. Each is at the time it happened, so a free
    throw shot after a sub is after it here, even when derive() counts it
    for the players on before the sub
    """
    actions = game["playbyplay"]["game"]["actions"]
    home = game["boxscore"]["boxScoreTraditional"]["homeTeamId"]
    has_clock = timed(game)
    home_score = away_score = 0
    found = []
    for period, acts in period_actions(actions).items():
        for a in acts:
            if pts := points(a):
                if a["teamId"] == home:
                    home_score += pts
                else:
                    away_score += pts
            t = elapsed(period, a["clock"]) if has_clock else None
            found.append(Event(a, t, pts, home_score, away_score))
    return found


def event_ids(action: dict, teams: Collection[int]) -> tuple[int | None, int | None]:
    """
    an action's (team id, person id). Team actions (team rebounds,
    timeouts) have no teamId and the team's id as their personId
    """
    team, person = action["teamId"] or None, action["personId"] or None
    if person in teams:
        return person, None
    return team, person


def shot_result(action: dict) -> str | None:
    """Made or Missed for shots and free throws, which have no shotResult"""
    if action["actionType"] == "Free Throw":
        return "Missed" if action["description"].startswith("MISS") else "Made"
    return action["shotResult"] or None


def timed(game: dict) -> bool:
    """whether a game's play-by-play has a game clock; the 2025 All-Star
    games, played to a target score, have every play at 0:00"""
    return any(
        a["clock"] != "PT00M00.00S" for a in game["playbyplay"]["game"]["actions"]
    )


def plus_minus(stint: Stint, home: int) -> int:
    change = stint.lead_end - stint.lead_start
    return change if stint.team_id == home else -change


def box_differences(
    game: dict, stints: list[Stint], logged: dict[int, int] | None = None
) -> list[str]:
    """
    players whose stints don't add up to their box score plus-minus and
    minutes, and teams without five players on the floor the whole game.
    The NBA's two box scores sometimes disagree about plus-minus, so a
    player matches if they match either this one or `logged`, the player
    game logs' (player id -> plus-minus)
    """
    box = game["boxscore"]
    home = box["boxScoreTraditional"]["homeTeamId"]
    pm, secs = defaultdict(int), defaultdict(float)
    per_team = defaultdict(float)
    for s in stints:
        pm[s.player_id] += plus_minus(s, home)
        secs[s.player_id] += s.end - s.start
        per_team[s.team_id] += s.end - s.start
    found = []
    for t in box_teams(box):
        for p in t["players"]:
            pid, st = p["personId"], p["statistics"]
            box_pm = round(st["plusMinusPoints"] or 0)
            accepted = {box_pm}
            if logged:
                accepted.add(logged.get(pid, 0))
            if pm.get(pid, 0) not in accepted:
                found.append(f"{pid} plus-minus {pm.get(pid, 0)}, box {box_pm}")
            if (
                abs(secs.get(pid, 0) - minutes_seconds(st["minutes"]))
                > MINUTES_TOLERANCE
            ):
                found.append(
                    f"{pid} minutes {secs.get(pid, 0) / 60:.1f}, box {st['minutes'] or 0}"
                )
    length = max((s.end for s in stints), default=0)
    for team, total in per_team.items():
        if abs(total - 5 * length) > MINUTES_TOLERANCE:
            found.append(f"{team} has {total / length:.2f} players on the floor")
    return found


# --- fetching


def check_playbyplay(data: dict) -> None:
    if not isinstance(data["game"]["actions"], list):
        raise TypeError(f"unexpected play-by-play: {str(data)[:500]}")


def check_boxscore(data: dict) -> None:
    box_teams(data)


def fetch_game(game_id: str, sleep=time.sleep) -> dict:
    """a game's play-by-play and box score, and box scores of periods the
    play-by-play can't tell the starters of"""

    def get(endpoint, check, **kwargs):
        return stats.fetch(
            endpoint,
            {"game_id": game_id, **kwargs},
            sleep=sleep,
            delays=RETRY_DELAYS,
            timeout=TIMEOUT,
            check=check,
        )

    game = {
        "playbyplay": get(PlayByPlayV3, check_playbyplay),
        "boxscore": get(BoxScoreTraditionalV3, check_boxscore),
        "period_boxscores": {},
    }
    for period in missing_starters(game):
        # StartPeriod is ignored; a range (in tenths of a second) works
        game["period_boxscores"][str(period)] = get(
            BoxScoreTraditionalV3,
            check_boxscore,
            range_type="2",
            start_range=str(period_start(period) * 10),
            end_range=str(period_start(period + 1) * 10),
        )
    return game


@dataclass
class FetchResult:
    fetched: int
    # games that failed, or were left for lack of time or after stopping
    left: list[str]
    # whether the run gave up because the endpoint kept failing
    stopped: bool = False


def fetch_season(
    outdir: Path,
    season: int,
    games: list[tuple[str, date]],
    today: date,
    fetch: Callable[[str], dict] = fetch_game,
    sleep=time.sleep,
    clock: Callable[[], float] = time.monotonic,
    budget: float | None = None,
    workers: int = WORKERS,
) -> FetchResult:
    """
    Fetch the games in `games` that aren't saved or are recent, newest
    first and `workers` at a time, starting new ones for up to `budget`
    seconds. Each game is saved as it arrives. When too many recent games
    have failed, pause and carry on (see PAUSES); a pause that wouldn't fit
    in the budget, or one after the last, stops the run. Games that failed
    are tried again after RETRY_PAUSE, if the run got through every game
    and has time
    """
    todo = [
        g
        for g, d in sorted(games, key=lambda g: (g[1], g[0]), reverse=True)
        if not raw_path(outdir, season, g).is_file() or d >= today - RECENT
    ]
    start = clock()
    result = FetchResult(0, [])
    failed: list[str] = []
    # the last WINDOW games' outcomes, True for a failure
    window: deque[bool] = deque(maxlen=WINDOW)
    pauses = 0
    pending: dict[Future, str] = {}

    def finish(done: set[Future]) -> None:
        nonlocal pauses
        for future in done:
            game_id = pending.pop(future)
            try:
                stats.write_raw(raw_path(outdir, season, game_id), future.result())
            # network errors, 500s (an empty body isn't JSON), bad responses
            except (OSError, ValueError, TypeError, KeyError) as e:
                print(f"rotations: {game_id} failed: {e!r}")
                failed.append(game_id)
                window.append(True)
                continue
            window.append(False)
            if len(window) == WINDOW and not throttled():
                pauses = 0
            result.fetched += 1
            if result.fetched % 100 == 0:
                print(f"rotations: fetched {result.fetched} of {len(todo)} games")

    def throttled() -> bool:
        return len(window) >= MIN_WINDOW and sum(window) >= THROTTLED * len(window)

    def run(games: list[str]) -> bool:
        """fetch `games`; False if the run stopped or ran out of time"""
        nonlocal pauses
        complete = True
        for i, game_id in enumerate(games):
            if len(pending) >= workers:
                finish(wait(pending, return_when=FIRST_COMPLETED).done)
            if throttled():
                failures = f"{sum(window)} of the last {len(window)} games failed"
                pause = PAUSES[pauses] if pauses < len(PAUSES) else None
                if pause is None or (
                    budget is not None and clock() - start + pause > budget
                ):
                    print(f"rotations: {failures}; stopping")
                    result.left += games[i:]
                    result.stopped = True
                    complete = False
                    break
                print(f"rotations: {failures}; pausing {pause // 60} minutes")
                sleep(pause)
                pauses += 1
                window.clear()
            if budget is not None and clock() - start > budget:
                print(f"rotations: out of time, leaving {len(games) - i} games")
                result.left += games[i:]
                complete = False
                break
            if i:
                sleep(REQUEST_DELAY)
            pending[executor.submit(fetch, game_id)] = game_id
        finish(wait(pending).done)
        return complete

    executor = ThreadPoolExecutor(workers)
    try:
        if (
            run(todo)
            and failed
            and (budget is None or clock() - start + RETRY_PAUSE <= budget)
        ):
            retry = failed.copy()
            failed.clear()
            print(
                f"rotations: retrying {len(retry)} failed games "
                f"in {RETRY_PAUSE} seconds"
            )
            sleep(RETRY_PAUSE)
            window.clear()
            run(retry)
        result.left = failed + result.left
    finally:
        # don't wait for requests still in flight after an interrupt
        executor.shutdown(wait=False, cancel_futures=True)
    print(
        f"rotations: fetched {result.fetched} games for {season}, "
        f"{len(result.left)} left"
    )
    return result


# --- building


def write_table(
    outdir: Path,
    season: int,
    name: str,
    keys: list[str],
    columns: dict[str, pa.Array],
    order: str,
    row_group_size: int | None = None,
) -> Path:
    """write a season of `name`, with a season column first, sorted by `order`"""
    con = duckdb.connect()
    con.register("arrow", pa.table(columns))
    con.execute(
        f"""
        CREATE TABLE {name} AS
        SELECT {season}::INTEGER AS season, * FROM arrow
        ORDER BY {order}
        """
    )
    check_keys(con, name, keys, f"rotations {name}")
    path = dataset_path(outdir / stats.OUT_DIR, name, season)
    write_parquet(con, name, path, row_group_size)
    return path


def build_season(outdir: Path, season: int, url: str = URL) -> bool:
    """
    Build the season's parquet files, the stints and the events, from its
    saved games. Returns False if there are none. Games whose stints don't
    match their box score are kept, and listed
    """
    paths = saved_games(outdir, season)
    if not paths:
        return False
    logs = game_logs(outdir, season, url)
    rows: dict[str, list] = defaultdict(list)
    evs: dict[str, list] = defaultdict(list)
    flagged, untimed = [], []
    for path in paths:
        game = stats.read_raw(path)
        game_id = path.name.removesuffix(".json.gz")
        logged = logs.get(game_id, {})
        ids = logged_ids(game, logged)
        box = game["boxscore"]["boxScoreTraditional"]
        home = box["homeTeamId"]
        # a game with no clock has no stints, so nothing to check
        problems: list[str] | None = None
        stints: list[Stint] = []
        if timed(game):
            stints, problems = derive(game)
            # the game logs' plus-minus, by box score id
            logged_pm = {pid: line.plus_minus for pid, line in logged.items()}
            logged_pm |= {pid: logged[log_id].plus_minus for pid, log_id in ids.items()}
            problems += box_differences(game, stints, logged_pm)
            if problems:
                flagged.append((game_id, problems))
        else:
            untimed.append(game_id)
        teams = {home, box["awayTeamId"]}
        for ev in events(game):
            a = ev.action
            team, person = event_ids(a, teams)
            shot = bool(a["isFieldGoal"])
            evs["game_id"].append(game_id)
            evs["action_id"].append(a["actionId"])
            evs["action_number"].append(a["actionNumber"])
            evs["period"].append(a["period"])
            evs["time"].append(None if ev.time is None else round(ev.time, 1))
            evs["team_id"].append(None if team is None else str(team))
            evs["person_id"].append(
                None if person is None else str(ids.get(person, person))
            )
            evs["action_type"].append(a["actionType"] or None)
            evs["sub_type"].append(a["subType"] or None)
            evs["description"].append(a["description"] or None)
            evs["shot_result"].append(shot_result(a))
            evs["is_field_goal"].append(shot)
            evs["shot_value"].append(a["shotValue"] if shot else None)
            evs["shot_distance"].append(a["shotDistance"] if shot else None)
            evs["x"].append(a["xLegacy"] if shot else None)
            evs["y"].append(a["yLegacy"] if shot else None)
            evs["video_available"].append(bool(a["videoAvailable"]))
            evs["pts"].append(ev.pts)
            evs["home_team_id"].append(str(home))
            evs["away_team_id"].append(str(box["awayTeamId"]))
            evs["home_score"].append(ev.home_score)
            evs["away_score"].append(ev.away_score)
            evs["matches_box_score"].append(None if problems is None else not problems)
        for s in stints:
            # players with no NBA id (international teams in preseason)
            # can't be keyed
            if not s.player_id:
                continue
            rows["game_id"].append(game_id)
            rows["team_id"].append(str(s.team_id))
            rows["player_id"].append(str(ids.get(s.player_id, s.player_id)))
            rows["period"].append(s.period)
            rows["in_time"].append(round(s.start, 1))
            rows["out_time"].append(round(s.end, 1))
            rows["pts"].append(s.pts)
            rows["plus_minus"].append(plus_minus(s, home))
            rows["matches_box_score"].append(not problems)
    if not rows:
        return False

    path = write_table(
        outdir,
        season,
        NAME,
        KEYS,
        {
            "game_id": pa.array(rows["game_id"], pa.string()),
            "team_id": pa.array(rows["team_id"], pa.string()),
            "player_id": pa.array(rows["player_id"], pa.string()),
            "period": pa.array(rows["period"], pa.int32()),
            "in_time": pa.array(rows["in_time"], pa.float64()),
            "out_time": pa.array(rows["out_time"], pa.float64()),
            "pts": pa.array(rows["pts"], pa.int32()),
            "plus_minus": pa.array(rows["plus_minus"], pa.int32()),
            "matches_box_score": pa.array(rows["matches_box_score"], pa.bool_()),
        },
        "game_id, team_id, in_time, player_id",
    )
    built = len(paths) - len(untimed)
    print(f"rotations: wrote {path} ({built} games)")
    path = write_table(
        outdir,
        season,
        EVENTS,
        EVENTS_KEYS,
        {
            "game_id": pa.array(evs["game_id"], pa.string()),
            "action_id": pa.array(evs["action_id"], pa.int32()),
            "action_number": pa.array(evs["action_number"], pa.int32()),
            "period": pa.array(evs["period"], pa.int32()),
            "time": pa.array(evs["time"], pa.float64()),
            "team_id": pa.array(evs["team_id"], pa.string()),
            "person_id": pa.array(evs["person_id"], pa.string()),
            "action_type": pa.array(evs["action_type"], pa.string()),
            "sub_type": pa.array(evs["sub_type"], pa.string()),
            "description": pa.array(evs["description"], pa.string()),
            "shot_result": pa.array(evs["shot_result"], pa.string()),
            "is_field_goal": pa.array(evs["is_field_goal"], pa.bool_()),
            "shot_value": pa.array(evs["shot_value"], pa.int32()),
            "shot_distance": pa.array(evs["shot_distance"], pa.int32()),
            "x": pa.array(evs["x"], pa.int32()),
            "y": pa.array(evs["y"], pa.int32()),
            "video_available": pa.array(evs["video_available"], pa.bool_()),
            "pts": pa.array(evs["pts"], pa.int32()),
            "home_team_id": pa.array(evs["home_team_id"], pa.string()),
            "away_team_id": pa.array(evs["away_team_id"], pa.string()),
            "home_score": pa.array(evs["home_score"], pa.int32()),
            "away_score": pa.array(evs["away_score"], pa.int32()),
            "matches_box_score": pa.array(evs["matches_box_score"], pa.bool_()),
        },
        "game_id, action_id",
        EVENTS_ROW_GROUP,
    )
    print(f"rotations: wrote {path}")
    if untimed:
        print(f"rotations: no game clock, so no stints, for {', '.join(untimed)}")
    if flagged:
        print(
            f"rotations: {len(flagged)} of {built} games' stints don't match "
            "their box score:"
        )
        for game_id, problems in flagged[:20]:
            print(f"  {game_id}: {'; '.join(problems[:3])}")
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season",
        type=int,
        action="append",
        help="season(s) to update, as the end year (default: current season)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="only rebuild parquet from raw files"
    )
    parser.add_argument(
        "--minutes",
        type=float,
        help="stop fetching after this many minutes (default: no limit)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=WORKERS,
        help=f"games to fetch at once (default: {WORKERS})",
    )
    parser.add_argument(
        "--url", default=URL, help=f"bucket URL for the games table (default: {URL})"
    )
    args = parser.parse_args(argv)

    today = today_eastern()
    start = time.monotonic()
    stopped = []
    for season in args.season or [current_season(today)]:
        if not args.no_fetch:
            games = season_games(args.out, season, args.url)
            if not games:
                print(f"rotations: no games for {season} yet")
                continue
            # --minutes is for the whole run, not each season
            budget = None
            if args.minutes is not None:
                budget = args.minutes * 60 - (time.monotonic() - start)
            result = fetch_season(
                args.out, season, games, today, budget=budget, workers=args.workers
            )
            if result.stopped:
                stopped.append(season)
        if not build_season(args.out, season, args.url):
            print(f"rotations: no rotations for {season}")
    # the games fetched are built and written either way, so a run that gave
    # up still publishes them; it fails after, so the gap is noticed
    if stopped:
        raise SystemExit(
            f"rotations: stopped with games left for {stopped}: stats.nba.com "
            "kept failing; run again later"
        )


if __name__ == "__main__":
    main()
