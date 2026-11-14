from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from pipeline import espn_injuries, stats
from pipeline.output import dataset_path

from .test_stats import response

ATL, BOS = "1610612737", "1610612738"
# ESPN's team ids
ESPN_ATL, ESPN_BOS = "1", "2"
DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 18, 43, 22, tzinfo=UTC)


def athlete(espn_id, name, espn_team):
    return {
        "displayName": name,
        "links": [{"href": f"https://www.espn.com/nba/player/_/id/{espn_id}/x"}],
        "team": {"id": espn_team, "abbreviation": "X"},
    }


def injury(espn_id, name, espn_team, kind="INJURY_STATUS_OUT", status="Out"):
    return {
        "id": f"9{espn_id}",
        "athlete": athlete(espn_id, name, espn_team),
        "status": status,
        "type": {"name": kind},
        "date": "2026-11-08T22:15Z",
        "shortComment": f"{name} is hurt.",
        "longComment": f"{name} is hurt, at length.",
        "details": {
            "fantasyStatus": {"abbreviation": "GTD"},
            "type": "Toe",
            "location": "Leg",
            "side": "Left",
            "detail": "Sprain",
            "returnDate": "2026-11-13",
        },
    }


def injury_list(injuries, timestamp="2026-11-12T18:43:22Z"):
    """ESPN's list: injuries [(espn team id, injury)]"""
    teams = {}
    for team, inj in injuries:
        teams.setdefault(team, []).append(inj)
    return {
        "timestamp": timestamp,
        "status": "success",
        "season": {"year": 2027, "type": 2},
        "injuries": [{"id": t, "injuries": injs} for t, injs in teams.items()],
    }


ROSTER = ["TeamID", "PLAYER", "NUM", "POSITION", "PLAYER_ID"]


def write_rosters(outdir, day, rosters, season=2027):
    """rosters: team_id -> [(player_id, name)]"""
    stats.write_raw(
        stats.daily_rosters_path(outdir, season, day),
        {
            "rosters": {
                team: response(
                    ROSTER, [[team, name, "7", "F", pid] for pid, name in players]
                )
                for team, players in rosters.items()
            }
        },
    )


def write_player_seasons(outdir, players, season=2027):
    path = dataset_path(outdir / stats.OUT_DIR, "player_seasons", season)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ", ".join(f"('{pid}', '{name}')" for pid, name in players)
    duckdb.sql(f"SELECT * FROM (VALUES {rows}) t(player_id, name)").write_parquet(
        str(path)
    )


class FakeESPN:
    def __init__(self, doc):
        self.doc = doc
        self.urls = []

    def get(self, url):
        raise AssertionError(url)

    def json(self, url):
        self.urls.append(url)
        return self.doc


def save(outdir, doc):
    return espn_injuries.fetch(FakeESPN(doc), outdir, DAY, NOW)


def rows(outdir, season=2027):
    path = dataset_path(outdir / espn_injuries.OUT_DIR, espn_injuries.DATASET, season)
    return duckdb.read_parquet(str(path), hive_partitioning=False)


PLAYER_IDS = "source,source_id,player_id,method\n"


@pytest.fixture(autouse=True)
def player_ids(tmp_path, monkeypatch):
    """an empty player_ids.csv"""
    path = tmp_path / "player_ids.csv"
    path.write_text(PLAYER_IDS)
    monkeypatch.setattr(espn_injuries, "PLAYER_IDS_FILE", path)
    monkeypatch.setattr(espn_injuries, "MIN_INJURED", 1)
    return path


def test_names_match_across_spellings():
    key = espn_injuries.normalize_name
    assert key("Kelly Oubre Jr.") == key("Kelly Oubre")
    assert key("Nikola Jokić") == key("Nikola Jokic")
    assert key("Gary Trent Jr") == key("Gary Trent Jr.")


def test_status_of():
    assert espn_injuries.status_of({"type": {"name": "INJURY_STATUS_OUT"}}) == "out"
    assert (
        espn_injuries.status_of({"type": {"name": "INJURY_STATUS_DAYTODAY"}})
        == "day_to_day"
    )
    assert espn_injuries.status_of({"status": "Day-To-Day"}) == "day_to_day"


def test_fetch_saves_the_list_by_its_timestamp(tmp_path):
    doc = injury_list([(ESPN_ATL, injury(10, "Asa Newell", ESPN_ATL))])
    fetcher = FakeESPN(doc)
    path = espn_injuries.fetch(fetcher, tmp_path, DAY, NOW)
    assert fetcher.urls == [espn_injuries.INJURIES_URL]
    assert path == tmp_path / "nba/raw/espn/2027/injuries/2026-11-12T184322Z.json.gz"
    assert espn_injuries.load_json(path) == doc
    assert espn_injuries.fetched_at_of(path) == NOW


def test_fetch_rejects_a_short_list_in_season(tmp_path, monkeypatch):
    monkeypatch.setattr(espn_injuries, "MIN_INJURED", 2)
    doc = injury_list([(ESPN_ATL, injury(10, "Asa Newell", ESPN_ATL))])
    with pytest.raises(ValueError, match="only 1 injured players"):
        save(tmp_path, doc)
    assert not espn_injuries.saved_fetches(tmp_path, 2027)
    # in the summer a short list is fine
    espn_injuries.fetch(FakeESPN(doc), tmp_path, date(2027, 8, 1), NOW)
    with pytest.raises(TypeError, match="no injuries list"):
        save(tmp_path, {"status": "error"})


def test_build(tmp_path):
    write_rosters(
        tmp_path,
        DAY,
        {ATL: [("1642854", "Asa Newell")], BOS: [("1628369", "Jayson Tatum")]},
    )
    write_player_seasons(
        tmp_path, [("1642854", "Asa Newell"), ("1628369", "Jayson Tatum")]
    )
    save(
        tmp_path,
        injury_list(
            [
                (ESPN_ATL, injury(10, "Asa Newell", ESPN_ATL)),
                (
                    ESPN_BOS,
                    injury(
                        20,
                        "Jayson Tatum",
                        ESPN_BOS,
                        "INJURY_STATUS_DAYTODAY",
                        "Day-To-Day",
                    ),
                ),
            ]
        ),
    )
    espn_injuries.build_season(tmp_path, 2027)
    table = rows(tmp_path)
    types = dict(zip(table.columns, map(str, table.types)))
    assert types == {
        "season": "INTEGER",
        "fetched_at": "TIMESTAMP WITH TIME ZONE",
        "as_of_date": "DATE",
        "team_id": "VARCHAR",
        "player_id": "VARCHAR",
        "status": "VARCHAR",
        "fantasy_status": "VARCHAR",
        "injury_type": "VARCHAR",
        "location": "VARCHAR",
        "side": "VARCHAR",
        "detail": "VARCHAR",
        "return_date": "DATE",
        "updated_at": "TIMESTAMP WITH TIME ZONE",
        "short_comment": "VARCHAR",
        "long_comment": "VARCHAR",
        "espn_athlete_id": "VARCHAR",
        "espn_injury_id": "VARCHAR",
    }
    got = table.project(
        "team_id, player_id, status, as_of_date, injury_type, side, detail, "
        "return_date, espn_athlete_id, espn_injury_id"
    ).fetchall()
    assert got == [
        (
            ATL,
            "1642854",
            "out",
            DAY,
            "Toe",
            "Left",
            "Sprain",
            date(2026, 11, 13),
            "10",
            "910",
        ),
        (
            BOS,
            "1628369",
            "day_to_day",
            DAY,
            "Toe",
            "Left",
            "Sprain",
            date(2026, 11, 13),
            "20",
            "920",
        ),
    ]


def test_build_keeps_every_fetch(tmp_path):
    write_rosters(tmp_path, DAY, {ATL: [("1642854", "Asa Newell")]})
    write_player_seasons(tmp_path, [("1642854", "Asa Newell")])
    newell = [(ESPN_ATL, injury(10, "Asa Newell", ESPN_ATL))]
    save(tmp_path, injury_list(newell, "2026-11-12T12:00:00Z"))
    # after midnight UTC, but still the 12th in the East
    save(tmp_path, injury_list(newell, "2026-11-13T03:00:00Z"))
    espn_injuries.build_season(tmp_path, 2027)
    assert rows(tmp_path).project("fetched_at, as_of_date").fetchall() == [
        (datetime(2026, 11, 12, 12, tzinfo=UTC), DAY),
        (datetime(2026, 11, 13, 3, tzinfo=UTC), DAY),
    ]


def test_build_matches_traded_players_and_player_ids(tmp_path, player_ids):
    """
    A player on another team's roster matches by name in player_seasons if
    only one player has it; player_ids.csv covers names that don't match
    """
    # the rosters are a day old: Tatum was traded to Atlanta since
    write_rosters(tmp_path, date(2026, 11, 11), {BOS: [("1628369", "Jayson Tatum")]})
    write_player_seasons(
        tmp_path,
        [("1628369", "Jayson Tatum"), ("1", "Cui Yongxi"), ("2", "Jalen Williams")],
    )
    player_ids.write_text(PLAYER_IDS + "espn,30,1,manual\n")
    save(
        tmp_path,
        injury_list(
            [
                (ESPN_ATL, injury(20, "Jayson Tatum", ESPN_ATL)),
                (ESPN_ATL, injury(30, "Yongxi Cui", ESPN_ATL)),
            ]
        ),
    )
    espn_injuries.build_season(tmp_path, 2027)
    assert rows(tmp_path).project("team_id, player_id").fetchall() == [
        (ATL, "1"),
        (ATL, "1628369"),
    ]


def test_build_reports_players_it_cant_match(tmp_path):
    write_rosters(tmp_path, DAY, {ATL: [("1642854", "Asa Newell")]})
    write_player_seasons(
        tmp_path,
        [("1642854", "Asa Newell"), ("2", "Jalen Williams"), ("3", "Jalen Williams")],
    )
    save(
        tmp_path,
        injury_list(
            [
                (ESPN_ATL, injury(10, "Asa Newell", ESPN_ATL)),
                # two players with his name: no telling which
                (ESPN_ATL, injury(40, "Jalen Williams", ESPN_ATL)),
                (ESPN_ATL, injury(50, "Nobody Atall", ESPN_ATL)),
            ]
        ),
    )
    with pytest.raises(ValueError, match="2 players") as e:
        espn_injuries.build_season(tmp_path, 2027)
    assert "Jalen Williams" in str(e.value) and "Nobody Atall" in str(e.value)
    # written first, without them
    assert rows(tmp_path).project("player_id").fetchall() == [("1642854",)]


def test_no_saved_lists(tmp_path):
    assert espn_injuries.build_season(tmp_path, 2027) is None


def test_fetch_saves_under_the_current_season(tmp_path):
    """
    update.yml only downloads the current season's lists, so a list ESPN
    still labels with last season (in September) goes in this one's
    """
    doc = injury_list([(ESPN_ATL, injury(10, "Asa Newell", ESPN_ATL))])
    doc["season"] = {"year": 2026, "type": 4}
    path = espn_injuries.fetch(FakeESPN(doc), tmp_path, date(2026, 9, 15), NOW)
    assert path.parent == espn_injuries.raw_dir(tmp_path, 2027)


def write_schedule(outdir, starts, season=2027):
    path = dataset_path(outdir / stats.OUT_DIR, "schedule", season)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ", ".join(f"('{i}', TIMESTAMPTZ '{s}')" for i, s in enumerate(starts))
    duckdb.sql(f"SELECT * FROM (VALUES {rows}) t(game_id, start_time)").write_parquet(
        str(path)
    )


def test_next_tipoff(tmp_path):
    write_schedule(
        tmp_path,
        ["2026-11-12 23:00:00+00", "2026-11-13 00:00:00+00", "2026-11-13 00:30:00+00"],
    )
    now = datetime(2026, 11, 12, 23, 30, tzinfo=UTC)
    tip = espn_injuries.next_tipoff(tmp_path, 2027, now, url="http://nowhere.invalid")
    assert tip == datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
    later = datetime(2026, 11, 14, tzinfo=UTC)
    assert espn_injuries.next_tipoff(tmp_path, 2027, later) is None


def test_before_games_skips_when_no_game_is_soon(tmp_path, monkeypatch, capsys):
    soon = datetime.now(UTC) + timedelta(minutes=30)
    write_schedule(
        tmp_path,
        [soon.isoformat()],
        season=espn_injuries.current_season(espn_injuries.today_eastern()),
    )

    def no_fetch(*a, **kw):
        raise AssertionError("fetched")

    monkeypatch.setattr(espn_injuries, "fetch", no_fetch)
    monkeypatch.setattr(espn_injuries, "build_season", no_fetch)
    espn_injuries.main(["--out", str(tmp_path), "--before-games", "20"])
    assert "not fetching" in capsys.readouterr().out
    fetched = []
    monkeypatch.setattr(espn_injuries, "fetch", lambda *a, **kw: fetched.append(1))
    monkeypatch.setattr(espn_injuries, "build_season", lambda *a, **kw: None)
    espn_injuries.main(["--out", str(tmp_path), "--before-games", "45"])
    assert fetched == [1]
