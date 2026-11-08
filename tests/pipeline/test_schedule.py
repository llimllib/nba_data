import gzip
import json
from datetime import UTC, date, datetime

import duckdb
import pytest
from nba_api.stats.endpoints import ScheduleLeagueV2

from pipeline import schedule, stats
from pipeline.output import DROPPED_ENV
from tests.pipeline.test_stats import read, write_raw

BOS = 1610612738
NYK = 1610612752
MACCABI = 93


def game(game_id, home=BOS, away=NYK, status=1, **fields):
    """a game as scheduleleaguev2 lists it, with the fields we read"""
    team = {"teamName": "", "teamCity": "", "teamTricode": "", "score": 0}
    g = {
        "gameId": game_id,
        "gameStatus": status,
        "gameStatusText": "7:30 pm ET" if status == 1 else "Final",
        "gameDateEst": "2026-11-04T00:00:00Z",
        "gameDateTimeUTC": "2026-11-05T00:30:00Z",
        "homeTeam": team | {"teamId": home},
        "awayTeam": team | {"teamId": away},
        "isNeutral": False,
        "gameLabel": "",
        "gameSubLabel": "",
        "gameSubtype": "",
        "seriesText": "",
        "ifNecessary": "false",
        "arenaName": "TD Garden",
        "arenaCity": "Boston",
        "arenaState": "MA",
    }
    return g | fields


def response(*games):
    return {
        "meta": {},
        "leagueSchedule": {
            "seasonYear": "2026-27",
            "gameDates": [{"gameDate": "11/04/2026 00:00:00", "games": list(games)}],
        },
    }


def build(*games, season=2027):
    con = duckdb.connect()
    schedule.build_schedule(con, season, response(*games))
    rel = con.sql("FROM schedule")
    return [dict(zip(rel.columns, row)) for row in rel.fetchall()]


def test_schedule():
    rows = build(
        game("0022600175"),
        game(
            "0012600001",
            home=MACCABI,
            status=3,
            gameDateEst="2026-10-04T00:00:00Z",
            gameDateTimeUTC="2026-10-04T17:00:00Z",
        ),
    )
    preseason, upcoming = rows
    assert preseason["status"] == "final"
    assert preseason["home_team_id"] == str(MACCABI)
    assert upcoming == {
        "season": 2027,
        "game_id": "0022600175",
        "game_date": date(2026, 11, 4),
        "start_time": datetime(2026, 11, 5, 0, 30, tzinfo=UTC),
        "game_type": "regular_season",
        "playoff_round": None,
        "series_number": None,
        "series_game": None,
        "home_team_id": str(BOS),
        "away_team_id": str(NYK),
        "neutral_site": False,
        "status": "scheduled",
        "status_text": "7:30 pm ET",
        "label": None,
        "sub_label": None,
        "subtype": None,
        "series_text": None,
        "if_necessary": False,
        "arena_name": "TD Garden",
        "arena_city": "Boston",
        "arena_state": "MA",
    }


def test_teams_not_known_yet():
    # the Cup's knockout games are listed before their teams are known
    (row,) = build(
        game(
            "0062600001",
            home=0,
            away=0,
            isNeutral=True,
            gameLabel="Emirates NBA Cup",
            gameSubLabel="Championship",
            gameSubtype="in-season-knockout",
        )
    )
    assert row["home_team_id"] is None
    assert row["away_team_id"] is None
    assert row["neutral_site"] is True
    assert row["game_type"] == "cup_final"
    assert row["sub_label"] == "Championship"


def test_playoff_series():
    (row,) = build(
        game(
            "0042600406",
            gameLabel="NBA Finals",
            gameSubLabel="Game 6",
            ifNecessary="true",
        )
    )
    assert (row["playoff_round"], row["series_number"], row["series_game"]) == (4, 0, 6)
    assert row["if_necessary"] is True


def test_unknown_status_fails():
    with pytest.raises(ValueError, match="unknown status 9"):
        build(game("0022600175", status=9))


def test_unknown_game_type_fails():
    with pytest.raises(ValueError, match="prefixes: \\['009'\\]"):
        build(game("0092600001"))


def test_game_from_another_season_fails():
    with pytest.raises(ValueError, match="not in 2027"):
        build(game("0022500175"))


def test_international_team_in_a_regular_season_game_fails():
    with pytest.raises(ValueError, match="outside NBA_TEAM_IDS"):
        build(game("0022600175", away=MACCABI))


def test_bad_response_fails():
    with pytest.raises(KeyError):
        schedule.games({"resultSets": []})


def test_fetch_schedule(tmp_path):
    calls = []

    def fake_fetch(endpoint, kwargs, check):
        calls.append((endpoint, kwargs))
        data = response(game("0022600175"))
        check(data)
        return data

    stats.fetch_schedule(tmp_path, 2027, fetch=fake_fetch)
    assert calls == [(ScheduleLeagueV2, {"league_id": "00", "season": "2026-27"})]
    with gzip.open(stats.raw_path(tmp_path, 2027, stats.SCHEDULE), "rt") as f:
        assert schedule.games(json.load(f))[0]["gameId"] == "0022600175"


def test_build_season_writes_the_schedule(tmp_path):
    write_raw(tmp_path, season=2027)
    stats.write_raw(
        stats.raw_path(tmp_path, 2027, stats.SCHEDULE), response(game("0022600175"))
    )
    stats.build_season(tmp_path, 2027)
    assert [r["game_id"] for r in read(tmp_path, "schedule", 2027)] == ["0022600175"]


def test_build_season_without_a_schedule(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    assert not (tmp_path / stats.OUT_DIR / "schedule").exists()


def write_late_game(tmp_path):
    """a season whose schedule has a final game the team game logs don't"""
    write_raw(tmp_path)
    stats.write_raw(
        stats.raw_path(tmp_path, 2026, stats.SCHEDULE),
        response(
            game("0022500001", status=3, gameDateTimeUTC="2025-10-22T00:00:00Z"),
            game("0022500002", status=3, gameDateTimeUTC="2025-10-23T00:00:00Z"),
            # not played yet
            game("0022500003", gameDateTimeUTC="2025-10-22T00:00:00Z"),
        ),
    )


def test_late_game_reported(tmp_path, monkeypatch):
    monkeypatch.delenv(DROPPED_ENV, raising=False)
    write_late_game(tmp_path)
    now = datetime(2025, 10, 23, 13, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"\['0022500002'\] final"):
        stats.build_season(tmp_path, 2026, now=now)
    dropped = tmp_path / "dropped.txt"
    monkeypatch.setenv(DROPPED_ENV, str(dropped))
    assert stats.build_season(tmp_path, 2026, now=now)
    assert "0022500002" in dropped.read_text()


def test_late_game_not_reported_yet(tmp_path, monkeypatch):
    monkeypatch.delenv(DROPPED_ENV, raising=False)
    write_late_game(tmp_path)
    # within LATE_GAME of tip-off
    assert stats.build_season(
        tmp_path, 2026, now=datetime(2025, 10, 23, 11, tzinfo=UTC)
    )
    # a rebuild from saved responses doesn't know when they were fetched
    assert stats.build_season(tmp_path, 2026)
