import gzip
import io
import json
import re
import urllib.error
from datetime import date
from email.message import Message

import duckdb
import pytest

from pipeline import ncaam_espn as ne
from pipeline.output import dataset_path

SEASON = 2026
TODAY = date(2025, 11, 10)


def team(team_id, location, conference_id="7"):
    return {
        "id": team_id,
        "location": location,
        "name": f"{location} Team",
        "displayName": f"{location} Team",
        "abbreviation": location[:3].upper(),
        "conferenceId": conference_id,
    }


def event(
    game_id="1",
    start="2025-11-04T00:30Z",
    completed=True,
    season_type=2,
    home=("194", "Ohio State"),
    away=("2509", "Purdue"),
    neutral=False,
    notes=(),
):
    """a scoreboard event; a 7:30pm Eastern start on Nov 3 is Nov 4 in UTC"""
    return {
        "id": game_id,
        "season": {"year": SEASON, "type": season_type},
        "status": {"type": {"completed": completed}},
        "competitions": [
            {
                "date": start,
                "neutralSite": neutral,
                "conferenceCompetition": True,
                "attendance": 15524,
                "notes": [{"headline": n} for n in notes],
                "venue": {"fullName": "Schottenstein Center"},
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "82",
                        "winner": True,
                        "team": team(*home),
                    },
                    {
                        "homeAway": "away",
                        "score": "74",
                        "winner": False,
                        # a non-Division I opponent has no conference
                        "team": team(*away, conference_id=None),
                    },
                ],
            }
        ],
    }


def scoreboard(*events):
    return {"events": list(events)}


TEAM_STATS = {
    "fieldGoalsMade-fieldGoalsAttempted": "28-63",
    "fieldGoalPct": "44",
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "11-29",
    "freeThrowsMade-freeThrowsAttempted": "7-10",
    "totalRebounds": "29",
    "offensiveRebounds": "10",
    "defensiveRebounds": "19",
    "assists": "13",
    "steals": "5",
    "blocks": "0",
    "turnovers": "6",
    "teamTurnovers": "1",
    "totalTurnovers": "7",
    "totalTechnicalFouls": "0",
    "flagrantFouls": "0",
    "turnoverPoints": "11",
    "fastBreakPoints": "4",
    "pointsInPaint": "22",
    "fouls": "17",
}
LABELS = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF"]  # fmt: skip
STATS = ["28", "19", "8-12", "0-0", "3-5", "9", "0", "1", "0", "0", "3", "6", "5"]


def athlete(player_id, **kw):
    return {
        "athlete": {
            "id": player_id,
            "displayName": f"Player {player_id}",
            "shortName": f"P. {player_id}",
            "position": {"abbreviation": "F"},
        },
        "starter": kw.get("starter", True),
        "didNotPlay": kw.get("dnp", False),
        "ejected": False,
        "stats": kw.get("stats", STATS),
    }


def summary(game_id="1", team_stats=TEAM_STATS, athletes=None, labels=LABELS):
    comp = event(game_id)["competitions"][0]
    comp = {
        "date": comp["date"],
        "boxscoreAvailable": True,
        "competitors": [
            {"id": c["team"]["id"], "homeAway": c["homeAway"], "score": c["score"],
             "winner": c["winner"]}
            for c in comp["competitors"]
        ],
    }  # fmt: skip
    if athletes is None:
        athletes = {"194": [athlete("10"), athlete("11", dnp=True, stats=[])],
                    "2509": [athlete("20")]}  # fmt: skip
    stats = [{"name": k, "displayValue": v} for k, v in team_stats.items()]
    return {
        "header": {"id": game_id, "season": {"type": 2}, "competitions": [comp]},
        "boxscore": {
            "teams": [
                {"team": {"id": t}, "statistics": stats} for t in ("194", "2509")
            ],
            "players": [
                {"team": {"id": t}, "statistics": [{"labels": labels, "athletes": a}]}
                for t, a in athletes.items()
            ],
        },
        "news": {"articles": ["dropped"]},
        "plays": [],
    }


# ---------------------------------------------------------------- parsing


def box(data, season):
    """summary_box's result, which must be there"""
    result = ne.summary_box(data, season)
    assert result is not None
    return result


def day_of(url):
    m = re.search(r"dates=(\d+)", url)
    assert m
    return m[1]


def test_scoreboard_games():
    data = scoreboard(
        event("1", notes=["Players Era Festival"], neutral=True),
        event("2", completed=False),
        event("3", season_type=1),
    )
    games, teams = ne.scoreboard_games(data, SEASON)
    # only finished games outside the preseason
    assert [g["game_id"] for g in games] == ["1"]
    g = games[0]
    assert g["game_date"] == date(2025, 11, 3)
    assert (g["home_team_id"], g["away_team_id"]) == ("194", "2509")
    assert (g["home_pts"], g["away_pts"]) == (82, 74)
    assert g["season_type"] == "regular_season"
    assert g["neutral_site"] is True
    assert g["note"] == "Players Era Festival"
    assert {t["team_id"]: t["conference_id"] for t in teams} == {
        "194": "7",
        "2509": None,
    }


def test_scoreboard_at_the_limit_fails():
    with pytest.raises(ValueError, match="limit"):
        ne.scoreboard_games(
            scoreboard(*[event(str(i)) for i in range(ne.LIMIT)]), SEASON
        )
    with pytest.raises(ValueError):
        ne.scoreboard_games({"leagues": []}, SEASON)


def test_summary_box():
    team_rows, player_rows, players = box(summary(), SEASON)
    home = next(r for r in team_rows if r["team_id"] == "194")
    assert home["opp_team_id"] == "2509"
    assert (home["home"], home["win"], home["pts"], home["opp_pts"]) == (
        True,
        True,
        82,
        74,
    )
    assert (home["fgm"], home["fga"], home["fg3m"], home["fg3a"]) == (28, 63, 11, 29)
    # the box score's turnovers include team turnovers
    assert (home["tov"], home["team_tov"]) == (7, 1)
    assert home["largest_lead"] is None
    assert home["game_date"] == date(2025, 11, 3)
    # players who didn't play are left out
    assert [r["player_id"] for r in player_rows] == ["10", "20"]
    p = player_rows[0]
    assert (p["min"], p["pts"], p["fgm"], p["fga"], p["ftm"], p["fta"]) == (
        28,
        19,
        8,
        12,
        3,
        5,
    )
    assert players[0]["name"] == "Player 10"
    assert players[0]["game_id"] == "1"


def test_summary_without_team_stats_has_players():
    # old games: player box scores, but empty team statistics
    team_rows, player_rows, _ = box(summary(team_stats={}), SEASON)
    assert team_rows == []
    assert len(player_rows) == 2


def test_summary_without_a_box():
    everyone_out = {"194": [athlete("10", dnp=True, stats=[])], "2509": []}
    assert ne.summary_box(summary(team_stats={}, athletes=everyone_out), SEASON) is None


def test_player_without_an_id_is_left_out():
    lost = athlete("12")
    lost["athlete"] = {"links": []}
    _, player_rows, _ = box(
        summary(athletes={"194": [athlete("10"), lost], "2509": []}), SEASON
    )
    assert [r["player_id"] for r in player_rows] == ["10"]


@pytest.mark.parametrize(
    "data",
    [
        summary(
            team_stats={k: v for k, v in TEAM_STATS.items() if "freeThrows" not in k}
        ),
        summary(labels=[*LABELS[:-1], "+/-"]),
        summary(athletes={"194": [athlete("10", stats=STATS[:-1])], "2509": []}),
        summary(
            athletes={"194": [athlete("10", stats=["28", "x", *STATS[2:]])], "2509": []}
        ),
        summary(team_stats=TEAM_STATS | {"fieldGoalsMade-fieldGoalsAttempted": "28"}),
    ],
)
def test_bad_summary_fails(data):
    with pytest.raises(ValueError):
        ne.summary_box(data, SEASON)


def test_trim_summary():
    assert set(ne.trim_summary(summary())) == {"header", "boxscore", "plays"}


# ---------------------------------------------------------------- fetching


class FakeESPN:
    """serves scoreboards by date and summaries by game id"""

    def __init__(self, scoreboards=None, summaries=None):
        self.scoreboards = scoreboards or {}
        self.summaries = summaries or {}
        self.urls = []

    def get(self, url):
        data = self.json(url)
        return None if data is None else json.dumps(data).encode()

    def json(self, url):
        self.urls.append(url)
        if m := re.search(r"dates=(\d{8})", url):
            day = date.fromisoformat(f"{m[1][:4]}-{m[1][4:6]}-{m[1][6:]}")
            return self.scoreboards.get(day, scoreboard())
        if m := re.search(r"event=(\d+)", url):
            return self.summaries.get(m[1])
        raise AssertionError(url)


def test_fetch_scoreboards(tmp_path):
    espn = FakeESPN({date(2025, 11, 3): scoreboard(event("1"))})
    # Nov 1 to yesterday
    assert ne.fetch_scoreboards(espn, tmp_path, SEASON, TODAY) == 9
    saved = ne.season_dir(tmp_path, SEASON) / "scoreboard"
    assert len(list(saved.glob("*.json.gz"))) == 9
    # then the days up to REFETCH_DAYS old again, and the new one
    espn.urls = []
    assert ne.fetch_scoreboards(espn, tmp_path, SEASON, date(2025, 11, 11)) == 2
    assert [day_of(u) for u in espn.urls] == [
        "20251109",
        "20251110",
    ]


def test_fetch_summaries(tmp_path):
    espn = FakeESPN(summaries={"1": summary("1"), "2": summary("2", team_stats={})})
    games = [
        ("1", date(2025, 11, 3)),
        ("2", date(2025, 11, 3)),
        ("3", date(2025, 11, 3)),
    ]
    assert ne.fetch_summaries(espn, tmp_path, SEASON, games, TODAY) == 2
    saved = ne.load_json(ne.summary_path(tmp_path, SEASON, "1"))
    assert "news" not in saved
    # a recent game without a team box is tried again; a complete one isn't
    espn.urls = []
    ne.fetch_summaries(espn, tmp_path, SEASON, games, TODAY)
    assert espn.urls == [ne.summary_url("2"), ne.summary_url("3")]
    # once it's old, never
    espn.urls = []
    ne.fetch_summaries(espn, tmp_path, SEASON, games, date(2025, 12, 1))
    assert espn.urls == [ne.summary_url("3")]


def test_a_bad_summary_is_not_saved(tmp_path):
    bad = summary(labels=[*LABELS[:-1], "+/-"])
    with pytest.raises(ValueError):
        ne.fetch_summaries(
            FakeESPN(summaries={"1": bad}), tmp_path, SEASON, [("1", TODAY)], TODAY
        )
    assert not ne.summary_path(tmp_path, SEASON, "1").exists()


class Response:
    def __init__(self, body, encoding=None):
        self.body = body
        self.headers = {"Content-Encoding": encoding} if encoding else {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def fetcher_with(monkeypatch, *responses):
    queue = list(responses)
    requests = []

    def urlopen(req, timeout):
        requests.append(req)
        r = queue.pop(0)
        if isinstance(r, urllib.error.HTTPError):
            raise r
        return r

    monkeypatch.setattr(ne.urllib.request, "urlopen", urlopen)
    fetcher = ne.Fetcher(delay=0, sleep=lambda s: None, clock=lambda: 0.0)
    return fetcher, requests


def test_fetcher_asks_for_gzip(monkeypatch):
    body = gzip.compress(b'{"events": []}')
    fetcher, requests = fetcher_with(monkeypatch, Response(body, "gzip"))
    assert fetcher.json("https://espn.test/u") == {"events": []}
    assert requests[0].get_header("Accept-encoding") == "gzip"


def test_fetcher_404_and_retry(monkeypatch):
    not_found = urllib.error.HTTPError("u", 404, "x", Message(), io.BytesIO(b""))
    assert fetcher_with(monkeypatch, not_found)[0].get("https://espn.test/u") is None
    dropped = Response(ConnectionResetError())
    assert (
        fetcher_with(monkeypatch, dropped, Response(b"ok"))[0].get(
            "https://espn.test/u"
        )
        == b"ok"
    )


# ---------------------------------------------------------------- building


def read(path):
    return duckdb.sql(
        f"SELECT * FROM read_parquet('{path}', hive_partitioning = false)"
    )


def test_build_season_from_espn(tmp_path):
    espn = FakeESPN(
        {date(2025, 11, 3): scoreboard(event("1"), event("2", completed=False))},
        {"1": summary("1")},
    )
    ne.update_season(espn, tmp_path, SEASON, SEASON, TODAY)
    paths = ne.build_season(tmp_path, SEASON)
    names = ["games", "team_box", "player_box", "team_seasons", "player_seasons"]
    assert paths == [dataset_path(tmp_path / "ncaam/espn", n, SEASON) for n in names]
    games, team_box, player_box, team_seasons, player_seasons = map(read, paths)
    assert games.count("*").fetchone() == (1,)
    assert team_box.count("*").fetchone() == (2,)
    assert player_box.select("player_id").order("1").fetchall() == [("10",), ("20",)]
    assert team_seasons.select("team_id, is_d1, torvik_team").order("1").fetchall() == [
        ("194", True, "Ohio St."),
        ("2509", False, "Purdue"),
    ]
    assert player_seasons.select("player_id, name, team_id").order("1").fetchall() == [
        ("10", "Player 10", "194"),
        ("20", "Player 20", "2509"),
    ]
    types = {c: str(t) for c, t in zip(team_box.columns, team_box.types, strict=True)}
    assert (types["season"], types["game_id"], types["game_date"]) == (
        "INTEGER", "VARCHAR", "DATE",
    )  # fmt: skip
    assert str(games.types[games.columns.index("start_time")]).startswith("TIMESTAMP")


def test_nothing_to_build_before_the_season(tmp_path):
    ne.update_season(FakeESPN(), tmp_path, 2027, 2027, date(2026, 10, 12))
    assert ne.build_season(tmp_path, 2027) == []
    assert not (tmp_path / "ncaam" / "espn").exists()


def write_sdv(outdir, season=2010, box_games=(1, 2)):
    """sportsdataverse's three files for a season of two games"""
    games = [
        (1, "2009-11-09", 194, 2509, True),
        (2, "2009-11-10", 2509, 194, True),
        # never marked final
        (3, "2009-11-11", 194, 2509, False),
    ]
    paths = {k: ne.sdv_path(outdir, season, k) for k in ne.SDV_FILES}

    def conf(team_id):
        # 2509 is a non-Division I team here
        return "7" if team_id == 194 else "NULL::INTEGER"

    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    rows = ", ".join(
        f"({g}, {season}, 2, DATE '{d}', TIMESTAMPTZ '{d} 19:00:00-05', {h}, "
        f"'H{h}', 'Home', 'HM', 'Home {h}', {conf(h)}, {a}, 'A{a}', 'Away', 'AW', "
        f"'Away {a}', {conf(a)}, 70, 60, true, false, '', 'Arena', 1000, {f})"
        for g, d, h, a, f in games
    )
    duckdb.sql(
        f"""COPY (SELECT * FROM (VALUES {rows}) v(game_id, season, season_type,
            game_date, game_date_time, home_id, home_location, home_name,
            home_abbreviation, home_display_name, home_conference_id, away_id,
            away_location, away_name, away_abbreviation, away_display_name,
            away_conference_id, home_score, away_score, neutral_site,
            conference_competition, notes_headline, venue_full_name,
            attendance, status_type_completed))
        TO '{paths["schedule"]}' (FORMAT parquet)"""
    )
    stats = ", ".join(
        f"10 AS {c}" for c in ne.SDV_TEAM_STATS.values() if c != "largest_lead"
    )
    box = []
    for g in box_games:
        for t, o in ((194, 2509), (2509, 194)):
            box.append(
                f"SELECT {g} AS game_id, {season} AS season, 2 AS season_type, "
                f"DATE '2009-11-09' + {g - 1} AS game_date, {t} AS team_id, {o} AS opponent_team_id, "
                f"'home' AS team_home_away, true AS team_winner, 70 AS team_score, "
                f"60 AS opponent_team_score, {stats}"
            )
    duckdb.sql(
        f"COPY ({' UNION ALL '.join(box)}) TO '{paths['team_box']}' (FORMAT parquet)"
    )
    pstats = ", ".join(f"5 AS {c}" for c in ne.SDV_PLAYER_STATS.values())
    duckdb.sql(
        f"""COPY (
            SELECT g AS game_id, {season} AS season, DATE '2009-11-09' AS game_date,
                194 AS team_id, a AS athlete_id, 'P' || a AS athlete_display_name,
                'P.' AS athlete_short_name, 'G' AS athlete_position_abbreviation,
                true AS starter, false AS ejected, dnp AS did_not_play, {pstats}
            FROM (VALUES (1, 10, false), (1, 11, true), (1, NULL, false), (3, 12, false))
                v(g, a, dnp)
        ) TO '{paths["player_box"]}' (FORMAT parquet)"""
    )


def test_build_season_from_sportsdataverse(tmp_path):
    write_sdv(tmp_path)
    paths = ne.build_season(tmp_path, 2010)
    games, team_box, player_box, team_seasons, player_seasons = map(read, paths)
    # the unfinished game, and players who didn't play or have no id, are out
    assert games.select("game_id").order("1").fetchall() == [("1",), ("2",)]
    assert team_box.count("*").fetchone() == (4,)
    assert player_box.select("player_id").fetchall() == [("10",)]
    assert player_seasons.select("player_id").fetchall() == [("10",)]
    row = team_box.filter("game_id = '1' AND team_id = '194'")
    assert row.select("fgm, tov, pf, largest_lead, home").fetchone() == (
        10,
        10,
        10,
        None,
        True,
    )
    assert team_seasons.select("team_id, is_d1").order("1").fetchall() == [
        ("194", True),
        ("2509", False),
    ]


def test_missing_boxes_are_filled_from_espn(tmp_path):
    write_sdv(tmp_path, box_games=(1,))
    assert ne.missing_boxes(tmp_path, 2010) == [("2", date(2009, 11, 10))]
    espn = FakeESPN(summaries={"2": summary("2")})
    ne.fetch_summaries(espn, tmp_path, 2010, ne.missing_boxes(tmp_path, 2010), TODAY)
    team_box = read(ne.build_season(tmp_path, 2010)[1])
    assert team_box.filter("game_id = '2'").count("*").fetchone() == (2,)


def test_teams_csv():
    names = ne.torvik_names()
    assert names["41"] == "Connecticut"
    assert len(set(names.values())) == len(names)
