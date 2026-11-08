import json
import re
from datetime import UTC, date, datetime

import duckdb
import pytest

from pipeline import espn_odds as eo
from pipeline.output import DROPPED_ENV, dataset_path

from .test_odds import EXPECTED, core, core_item

SEASON = 2027
TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 16, tzinfo=UTC)
# NBA team ids for ESPN's 7 (DEN) and 11 (IND)
DEN, IND = "1610612743", "1610612754"


def event(event_id="401", start="2026-10-23T23:00Z", completed=True, season_type=2):
    """IND hosting DEN; 7pm Eastern on Oct 23 is 23:00 UTC"""
    return {
        "id": event_id,
        "season": {"year": SEASON, "type": season_type},
        "status": {"type": {"completed": completed}},
        "competitions": [
            {
                "date": start,
                "competitors": [
                    {"homeAway": "home", "team": {"id": "11"}},
                    {"homeAway": "away", "team": {"id": "7"}},
                ],
            }
        ],
    }


def scoreboard(*events):
    return {"events": list(events)}


class FakeESPN:
    """serves scoreboards by date and odds by event id"""

    def __init__(self, scoreboards=None, odds=None):
        self.scoreboards = scoreboards or {}
        self.odds = odds or {}
        self.urls = []

    def get(self, url):
        data = self.json(url)
        return None if data is None else json.dumps(data).encode()

    def json(self, url):
        self.urls.append(url)
        if m := re.search(r"dates=(\d{8})", url):
            day = date(int(m[1][:4]), int(m[1][4:6]), int(m[1][6:]))
            return self.scoreboards.get(day, scoreboard())
        if m := re.search(r"events/(\d+)/", url):
            return self.odds.get(m[1])
        raise AssertionError(url)


def write_team_game_logs(outdir, games):
    """games: (game_id, game_date, team ids)"""
    path = dataset_path(outdir / eo.STATS_DIR, "team_game_logs", SEASON)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = ", ".join(
        f"('{g}', '{d}'::DATE, '{t}')" for g, d, teams in games for t in teams
    )
    duckdb.sql(
        f"COPY (FROM (VALUES {values}) v(game_id, game_date, team_id)) TO '{path}'"
    )


def read(path):
    return duckdb.sql(
        f"SELECT * FROM read_parquet('{path}', hive_partitioning = false)"
    )


def fetch(espn, outdir, today=TODAY):
    eo.fetch_scoreboards(espn, outdir, SEASON, today)
    return eo.fetch_odds(espn, outdir, SEASON, today)


def test_fetch_scoreboards(tmp_path):
    # the season's window starts Oct 15
    assert eo.fetch_scoreboards(FakeESPN(), tmp_path, SEASON, TODAY) == 11
    # then only the last REFETCH_DAYS days and today
    espn = FakeESPN()
    assert eo.fetch_scoreboards(espn, tmp_path, SEASON, TODAY) == 3
    assert espn.urls[0].endswith("dates=20261023")


def test_fetch_odds_once_a_game_is_final(tmp_path):
    espn = FakeESPN(
        {date(2026, 10, 23): scoreboard(event("401"), event("402", completed=False))},
        {"401": core(core_item())},
    )
    assert fetch(espn, tmp_path) == 1
    assert eo.odds_path(tmp_path, SEASON, "401").exists()
    assert not eo.odds_path(tmp_path, SEASON, "402").exists()
    # a saved game isn't fetched again
    assert eo.fetch_odds(espn, tmp_path, SEASON, TODAY) == 0


def test_a_game_without_odds_is_refetched_while_recent(tmp_path):
    espn = FakeESPN({date(2026, 10, 23): scoreboard(event("401"))})
    # ESPN doesn't know the game: saved as having none
    assert fetch(espn, tmp_path) == 1
    espn.odds["401"] = core(core_item())
    assert eo.fetch_odds(espn, tmp_path, SEASON, TODAY) == 1
    assert eo.load_json(eo.odds_path(tmp_path, SEASON, "401"))["items"]

    # but not once it's old
    espn = FakeESPN({date(2026, 10, 23): scoreboard(event("402"))})
    fetch(espn, tmp_path)
    espn.odds["402"] = core(core_item())
    assert eo.fetch_odds(espn, tmp_path, SEASON, date(2026, 11, 20)) == 0


def test_preseason_games_are_left_out(tmp_path):
    espn = FakeESPN(
        {date(2026, 10, 16): scoreboard(event("401", season_type=1))},
        {"401": core(core_item())},
    )
    assert fetch(espn, tmp_path) == 0


def test_build_season(tmp_path):
    espn = FakeESPN(
        {date(2026, 10, 23): scoreboard(event("401"))}, {"401": core(core_item())}
    )
    fetch(espn, tmp_path)
    write_team_game_logs(tmp_path, [("0022600010", "2026-10-23", [IND, DEN])])
    path = eo.build_season(tmp_path, SEASON, now=NOW)
    assert path == dataset_path(tmp_path / eo.OUT_DIR, "betting_lines", SEASON)
    lines = read(path)
    assert lines.columns == [c for c, _ in eo.COLUMNS]
    (row,) = lines.fetchall()
    assert dict(zip(lines.columns, row, strict=True)) == EXPECTED | {
        "season": SEASON,
        "game_id": "0022600010",
        "home_team_id": IND,
        "away_team_id": DEN,
        "espn_event_id": "401",
    }


def test_a_game_abroad_can_be_a_day_off(tmp_path):
    # tip-off in Europe, listed on the day before by the NBA
    espn = FakeESPN(
        {date(2026, 10, 23): scoreboard(event("401"))}, {"401": core(core_item())}
    )
    fetch(espn, tmp_path)
    write_team_game_logs(
        tmp_path,
        [
            ("0022600010", "2026-10-22", [IND, DEN]),
            ("0022600020", "2026-10-26", [IND, DEN]),
        ],
    )
    path = eo.build_season(tmp_path, SEASON, now=NOW)
    assert read(path).select("game_id").fetchall() == [("0022600010",)]


def test_games_not_in_the_game_logs_are_held_back(tmp_path, monkeypatch):
    espn = FakeESPN(
        {date(2026, 10, 23): scoreboard(event("401"))}, {"401": core(core_item())}
    )
    fetch(espn, tmp_path)
    write_team_game_logs(tmp_path, [("0022600099", "2026-10-23", [IND, "1"])])
    # the morning after, quietly
    soon = datetime(2026, 10, 24, 12, tzinfo=UTC)
    assert eo.build_season(tmp_path, SEASON, now=soon) is None
    # a day later it's reported
    later = datetime(2026, 10, 25, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="401"):
        eo.build_season(tmp_path, SEASON, now=later)
    dropped = tmp_path / "dropped.txt"
    monkeypatch.setenv(DROPPED_ENV, str(dropped))
    assert eo.build_season(tmp_path, SEASON, now=later) is None
    assert "401" in dropped.read_text()


def test_teams_that_arent_franchises_are_skipped(tmp_path):
    allstar = event("401")
    for c in allstar["competitions"][0]["competitors"]:
        c["team"]["id"] = "31"
    espn = FakeESPN(
        {date(2026, 10, 23): scoreboard(allstar)}, {"401": core(core_item())}
    )
    fetch(espn, tmp_path)
    write_team_game_logs(tmp_path, [("0022600010", "2026-10-23", [IND, DEN])])
    assert eo.build_season(tmp_path, SEASON, now=NOW) is None


def test_espn_team_ids():
    teams = eo.espn_team_ids(SEASON)
    assert len(teams) == 30
    assert len(set(teams.values())) == 30
    assert (teams["7"], teams["11"]) == (DEN, IND)
