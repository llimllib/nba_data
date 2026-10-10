import gzip
import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from nba_api.stats.endpoints import LeagueDashPlayerStats

from pipeline import stats
from pipeline.output import dataset_path

BOS = 1610612738
NYK = 1610612752
GAME = "0022500001"
CUP_FINAL = "0062500001"


def response(headers, rows, as_list=True):
    rs = {"name": "x", "headers": headers, "rowSet": rows}
    return {"resource": "x", "parameters": {}, "resultSets": [rs] if as_list else rs}


TGL = ["SEASON_YEAR", "TEAM_ID", "TEAM_ABBREVIATION", "TEAM_NAME", "GAME_ID"]
TGL += ["GAME_DATE", "MATCHUP", "WL", "MIN", "PTS", "TOV", "PTS_RANK"]


def team_logs():
    date = "2025-10-21T00:00:00"
    return [
        [
            "2025-26",
            BOS,
            "BOS",
            "Boston Celtics",
            GAME,
            date,
            "BOS vs. NYK",
            "W",
            240.0,
            110,
            12.0,
            1,
        ],
        [
            "2025-26",
            NYK,
            "NYK",
            "New York Knicks",
            GAME,
            date,
            "NYK @ BOS",
            "L",
            240.0,
            100,
            15.0,
            2,
        ],
        # neutral site: both teams listed as away
        [
            "2025-26",
            BOS,
            "BOS",
            "Boston Celtics",
            CUP_FINAL,
            date,
            "BOS @ NYK",
            "L",
            240.0,
            99,
            9.0,
            3,
        ],
        [
            "2025-26",
            NYK,
            "NYK",
            "New York Knicks",
            CUP_FINAL,
            date,
            "NYK @ BOS",
            "W",
            240.0,
            101,
            11.0,
            4,
        ],
    ]


PGL = ["SEASON_YEAR", "PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "TEAM_ABBREVIATION"]
PGL += ["TEAM_NAME", "GAME_ID", "GAME_DATE", "MATCHUP", "WL", "MIN", "PTS", "MIN_SEC"]


def player_log(player_id, name, team_id, game_id, date, matchup, wl, pts):
    team = {BOS: ("BOS", "Boston Celtics"), NYK: ("NYK", "New York Knicks")}
    abbrev, team_name = team.get(team_id, ("MLN", "Milano"))
    return ["2025-26", player_id, name, team_id, abbrev, team_name, game_id] + [
        date,
        matchup,
        wl,
        30.0,
        pts,
        "30:00",
    ]


def player_logs():
    date = "2025-10-21T00:00:00"
    later = "2025-12-16T00:00:00"
    return [
        player_log(1, "A", BOS, GAME, date, "BOS vs. NYK", "W", 20),
        player_log(2, "B", NYK, GAME, date, "NYK @ BOS", "L", 8),
        # player 2 changed their name
        player_log(2, "B2", NYK, CUP_FINAL, later, "NYK @ BOS", "W", 12),
    ]


BASE = ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "GP", "MIN", "PTS", "PTS_RANK"]
DEFENSE = ["PLAYER_ID", "GP", "DEF_WS", "DEF_WS_RAW"]
ADVANCED = ["PLAYER_ID", "GP", "POSS", "FGM_PG"]
PT_SHOT = ["PLAYER_ID", "PLAYER_LAST_TEAM_ID", "PLAYER_LAST_TEAM_ABBREVIATION", "FG2M"]
BIO = ["PLAYER_ID", "PTS", "PLAYER_HEIGHT", "PLAYER_WEIGHT", "COLLEGE"]
BIO += ["DRAFT_YEAR", "DRAFT_ROUND", "DRAFT_NUMBER"]


def season_stats(players=(1, 2)):
    rows = {
        "base": [[p, "A", BOS, 10, 300.5, 200, 1] for p in players],
        "defense": [[p, 10, 0.3, 0.2987] for p in players],
        "advanced": [[p, 10, 600, 7.5] for p in players],
        # player 2 took no 2-point shots, so isn't in the shot response
        "pt_shot": [[p, BOS, "BOS", 50] for p in players if p != 2],
        "bio": [
            [p, 20.0, "6-8", "220" if p == 1 else None, "None" if p == 1 else "Duke"]
            + (
                ["Undrafted", "Undrafted", "Undrafted"]
                if p == 1
                else ["2020", "1", "3"]
            )
            for p in players
        ],
    }
    headers = {
        "base": BASE,
        "defense": DEFENSE,
        "advanced": ADVANCED,
        "pt_shot": PT_SHOT,
        "bio": BIO,
    }
    return {m: response(headers[m], r) for m, r in rows.items()}


def write_raw(outdir, season=2026, empty=False, **overrides):
    """
    write every raw response for a season; overrides replace by name. With
    `empty`, every response has its headers but no rows, as before a season
    """
    raw = {
        "team_game_logs_base": response(TGL, team_logs()),
        "team_game_logs_advanced": response(
            ["TEAM_ID", "GAME_ID", "OFF_RATING", "POSS", "MIN"],
            [[r[1], r[4], 110.5, 100, 240.0] for r in team_logs()],
            as_list=False,
        ),
        "player_game_logs_base": response(PGL, player_logs()),
        "player_game_logs_advanced": response(
            ["PLAYER_ID", "GAME_ID", "USG_PCT"],
            [[r[1], r[PGL.index("GAME_ID")], 0.25] for r in player_logs()],
        ),
    }
    for name, players in (("regular_season", (1, 2)), ("playoffs", (1,))):
        for m, resp in season_stats(players).items():
            raw[f"player_stats_{name}_{m}"] = resp
    raw |= overrides
    if empty:
        for data in raw.values():
            rs = data["resultSets"]
            (rs[0] if isinstance(rs, list) else rs)["rowSet"] = []
    for name, data in raw.items():
        path = stats.raw_path(outdir, season, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as f:
            json.dump(data, f)


def read(outdir, dataset, season=2026):
    path = dataset_path(outdir / stats.OUT_DIR, dataset, season)
    rel = duckdb.sql(f"SELECT * FROM read_parquet('{path}', hive_partitioning = false)")
    return [dict(zip(rel.columns, row)) for row in rel.fetchall()]


def types(outdir, dataset, season=2026):
    path = dataset_path(outdir / stats.OUT_DIR, dataset, season)
    return dict(
        duckdb.sql(
            f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM read_parquet('{path}', hive_partitioning = false))"
        ).fetchall()
    )


def test_season_string():
    assert stats.season_string(2026) == "2025-26"
    assert stats.season_string(2000) == "1999-00"


def test_requests():
    reqs = stats.requests(2026)
    assert len(reqs) == 14
    endpoint, kwargs = reqs["player_stats_playoffs_defense"]
    assert endpoint is LeagueDashPlayerStats
    assert kwargs["season"] == "2025-26"
    assert kwargs["season_type_all_star"] == "Playoffs"
    assert kwargs["per_mode_detailed"] == "Totals"
    assert kwargs["measure_type_detailed_defense"] == "Defense"


def test_team_game_logs(tmp_path):
    write_raw(tmp_path)
    assert stats.build_season(tmp_path, 2026)
    rows = {(r["game_id"], r["team_id"]): r for r in read(tmp_path, "team_game_logs")}

    bos = rows[(GAME, str(BOS))]
    assert bos["season"] == 2026
    assert bos["opp_team_id"] == str(NYK)
    assert bos["home"] is True
    assert bos["win"] is True
    assert bos["off_rating"] == 110.5
    assert str(bos["game_date"]) == "2025-10-21"
    assert rows[(GAME, str(NYK))]["home"] is False
    # neutral site
    assert rows[(CUP_FINAL, str(BOS))]["home"] is None
    assert rows[(CUP_FINAL, str(NYK))]["win"] is True

    t = types(tmp_path, "team_game_logs")
    assert t["game_id"] == t["team_id"] == t["opp_team_id"] == "VARCHAR"
    # sent as 12.0, but it's a count
    assert t["tov"] == "INTEGER"
    assert t["min"] == "DOUBLE"
    for dropped in ("pts_rank", "team_abbreviation", "team_name", "matchup", "wl"):
        assert dropped not in t


def test_player_game_logs(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    rows = {
        (r["game_id"], r["player_id"]): r for r in read(tmp_path, "player_game_logs")
    }
    a = rows[(GAME, "1")]
    assert a["team_id"] == str(BOS)
    assert a["opp_team_id"] == str(NYK)
    assert a["home"] is True
    assert a["usg_pct"] == 0.25
    assert rows[(CUP_FINAL, "2")]["home"] is None
    t = types(tmp_path, "player_game_logs")
    assert t["player_id"] == "VARCHAR"
    assert "player_name" not in t and "min_sec" not in t


def test_player_season_stats(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    rows = {
        (r["season_type"], r["player_id"]): r
        for r in read(tmp_path, "player_season_stats")
    }
    assert set(rows) == {
        ("regular_season", "1"),
        ("regular_season", "2"),
        ("playoffs", "1"),
    }

    a = rows[("regular_season", "1")]
    # the bio response's PTS is per game; the first response's total wins
    assert a["pts"] == 200
    assert a["def_ws_raw"] == 0.2987
    assert a["poss"] == 600
    assert a["fg2m"] == 50
    assert a["player_last_team_id"] == str(BOS)
    assert a["player_weight"] == 220
    assert a["draft_year"] is None
    assert a["college"] is None

    # missing from the shot response, but kept
    b = rows[("regular_season", "2")]
    assert b["fg2m"] is None
    assert b["draft_year"] == 2020
    assert b["college"] == "Duke"
    assert b["player_weight"] is None

    t = types(tmp_path, "player_season_stats")
    assert t["season_type"] == "VARCHAR"
    assert t["gp"] == "INTEGER"
    for dropped in (
        "fgm_pg",
        "pts_rank",
        "player_name",
        "player_last_team_abbreviation",
    ):
        assert dropped not in t


def test_no_types_are_bigint(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    for dataset in stats.KEYS:
        assert "BIGINT" not in types(tmp_path, dataset).values(), dataset


def test_fractional_count_fails(tmp_path):
    logs = team_logs()
    logs[0][TGL.index("TOV")] = 12.5
    write_raw(tmp_path, team_game_logs_base=response(TGL, logs))
    with pytest.raises(duckdb.Error, match="tov is not a whole number"):
        stats.build_season(tmp_path, 2026)


def test_team_plus_minus_is_the_margin(tmp_path):
    # the NBA sent 4.4 for a 4-point win (2010 preseason)
    headers = [*TGL, "PLUS_MINUS"]
    logs = [[*r, 4.4] for r in team_logs()]
    write_raw(tmp_path, team_game_logs_base=response(headers, logs))
    assert stats.build_season(tmp_path, 2026)
    rows = {(r["game_id"], r["team_id"]): r for r in read(tmp_path, "team_game_logs")}
    assert rows[(GAME, str(BOS))]["plus_minus"] == 10
    assert rows[(GAME, str(NYK))]["plus_minus"] == -10
    assert types(tmp_path, "team_game_logs")["plus_minus"] == "INTEGER"


def test_before_the_season(tmp_path):
    # no games yet: nothing is written
    write_raw(tmp_path, empty=True)
    assert stats.build_season(tmp_path, 2026)
    assert not (tmp_path / stats.OUT_DIR).exists()


def test_regular_season_without_playoffs(tmp_path):
    no_playoffs = {
        f"player_stats_playoffs_{m}": response(r["resultSets"][0]["headers"], [])
        for m, r in season_stats().items()
    }
    write_raw(tmp_path, **no_playoffs)
    stats.build_season(tmp_path, 2026)
    assert {r["season_type"] for r in read(tmp_path, "player_season_stats")} == {
        "regular_season"
    }


def test_duplicate_key_fails(tmp_path):
    logs = player_logs()
    write_raw(tmp_path, player_game_logs_base=response(PGL, logs + logs[:1]))
    with pytest.raises(ValueError, match="duplicate"):
        stats.build_season(tmp_path, 2026)


def test_missing_raw_files(tmp_path):
    assert stats.build_season(tmp_path, 2026) is False
    write_raw(tmp_path)
    stats.raw_path(tmp_path, 2026, "player_stats_playoffs_bio").unlink()
    with pytest.raises(FileNotFoundError, match="player_stats_playoffs_bio"):
        stats.build_season(tmp_path, 2026)


class FakeEndpoint:
    """an nba_api endpoint that fails `failures` times before responding"""

    __name__ = "FakeEndpoint"

    def __init__(self, failures, data=None):
        self.failures = failures
        self.calls = 0
        self.data = data or response(["A"], [[1]])

    def __call__(self, **kwargs):
        self.calls += 1
        assert kwargs["timeout"] == stats.TIMEOUT
        if self.calls <= self.failures:
            raise TimeoutError("slow")
        return self

    def get_dict(self):
        return self.data


def test_fetch_retries():
    sleeps = []
    endpoint = FakeEndpoint(failures=2)
    assert stats.fetch(endpoint, {"x": 1}, sleep=sleeps.append) == endpoint.data
    assert sleeps == stats.RETRY_DELAYS[:2]


def test_fetch_gives_up():
    endpoint = FakeEndpoint(failures=100)
    with pytest.raises(TimeoutError):
        stats.fetch(endpoint, {}, sleep=lambda _: None)
    assert endpoint.calls == len(stats.RETRY_DELAYS) + 1


def test_fetch_retries_bad_responses():
    endpoint = FakeEndpoint(failures=0, data={"message": "rate limited"})
    with pytest.raises(KeyError):
        stats.fetch(endpoint, {}, sleep=lambda _: None)
    assert endpoint.calls == len(stats.RETRY_DELAYS) + 1


def test_fetch_season(tmp_path):
    seen = []

    def fake_fetch(endpoint, kwargs):
        seen.append(endpoint)
        return response(["A"], [[len(seen)]])

    stats.fetch_season(tmp_path, 2026, fetch=fake_fetch)
    assert len(seen) == 14
    path = stats.raw_path(tmp_path, 2026, "player_stats_playoffs_bio")
    with gzip.open(path, "rt") as f:
        assert json.load(f)["resultSets"][0]["rowSet"] == [[14]]


def test_unidentified_players(tmp_path):
    date = "2025-10-03T00:00:00"
    preseason = player_log(None, None, 94, "0012500001", date, "MLN vs. NYK", "L", 5)
    write_raw(
        tmp_path, player_game_logs_base=response(PGL, player_logs() + [preseason])
    )
    stats.build_season(tmp_path, 2026)
    assert len(read(tmp_path, "player_game_logs")) == 3

    regular = player_log(None, None, 94, "0022500099", date, "MLN vs. NYK", "L", 5)
    write_raw(tmp_path, player_game_logs_base=response(PGL, player_logs() + [regular]))
    with pytest.raises(ValueError, match="NULL"):
        stats.build_season(tmp_path, 2026)


def test_team_seasons(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    rows = {r["team_id"]: r for r in read(tmp_path, "team_seasons")}
    assert rows[str(BOS)] == {
        "season": 2026,
        "team_id": str(BOS),
        "nba_abbrev": "BOS",
        "full_name": "Boston Celtics",
        "is_nba": True,
    }
    assert set(rows) == {str(BOS), str(NYK)}


def melbourne(game_id):
    """team logs where Melbourne United plays the Knicks' side of `game_id`"""
    logs = team_logs()
    for r in logs:
        if r[TGL.index("TEAM_ID")] == NYK:
            r[TGL.index("TEAM_ID")] = 15016
            r[TGL.index("TEAM_ABBREVIATION")] = "MEL"
            r[TGL.index("TEAM_NAME")] = "Melbourne United"
        r[TGL.index("GAME_ID")] = r[TGL.index("GAME_ID")].replace(GAME, game_id)
    return response(TGL, logs)


def test_international_teams_are_not_nba(tmp_path):
    write_raw(tmp_path, team_game_logs_base=melbourne("0012500001"))
    stats.build_season(tmp_path, 2026)
    rows = {r["team_id"]: r for r in read(tmp_path, "team_seasons")}
    assert rows["15016"]["is_nba"] is False
    assert rows[str(BOS)]["is_nba"] is True


def test_regular_season_team_outside_nba_ids_fails(tmp_path):
    # a new franchise would get an id outside NBA_TEAM_IDS
    write_raw(tmp_path, team_game_logs_base=melbourne(GAME))
    with pytest.raises(ValueError, match="15016"):
        stats.build_season(tmp_path, 2026)


def test_player_seasons(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    names = {r["player_id"]: r["name"] for r in read(tmp_path, "player_seasons")}
    # the name from their latest game
    assert names == {"1": "A", "2": "B2"}


def test_player_seasons_includes_players_without_games(tmp_path):
    base = season_stats((1, 2, 3))["base"]
    write_raw(tmp_path, player_stats_regular_season_base=base)
    stats.build_season(tmp_path, 2026)
    names = {r["player_id"]: r["name"] for r in read(tmp_path, "player_seasons")}
    assert names["3"] == "A"


def test_games(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    games = {r["game_id"]: r for r in read(tmp_path, "games")}
    g = games[GAME]
    assert g["game_type"] == "regular_season"
    assert g["home_team_id"] == str(BOS)
    assert g["away_team_id"] == str(NYK)
    assert g["neutral_site"] is False
    assert g["playoff_round"] is None
    cup = games[CUP_FINAL]
    assert cup["game_type"] == "cup_final"
    assert cup["neutral_site"] is True
    assert cup["home_team_id"] is None
    assert cup["away_team_id"] is None


def with_game_id(game_id):
    """the first game's team logs, under another game id"""
    return response(TGL, [[*r[:4], game_id, *r[5:]] for r in team_logs()[:2]])


@pytest.mark.parametrize(
    ("game_id", "game_type", "bracket"),
    [
        ("0042500407", "playoffs", (4, 0, 7)),
        ("0042500153", "playoffs", (1, 5, 3)),
        ("0052500211", "play_in", (2, 1, 1)),
        ("0012500010", "preseason", (None, None, None)),
        ("0032500003", "all_star", (None, None, None)),
    ],
)
def test_game_id_decoding(tmp_path, game_id, game_type, bracket):
    write_raw(tmp_path, team_game_logs_base=with_game_id(game_id))
    stats.build_season(tmp_path, 2026)
    (g,) = read(tmp_path, "games")
    assert g["game_type"] == game_type
    assert (g["playoff_round"], g["series_number"], g["series_game"]) == bracket


def test_unknown_game_type_fails(tmp_path):
    write_raw(tmp_path, team_game_logs_base=with_game_id("0092500001"))
    with pytest.raises(ValueError, match="009"):
        stats.build_season(tmp_path, 2026)


def write_rosters(outdir, rosters, season=2026, fetched="2026-01-01T00:00:00+00:00"):
    """rosters: team_id -> [(player_id, name)]"""
    stats.write_raw(
        stats.raw_path(outdir, season, stats.ROSTERS),
        {
            "fetched": fetched,
            "rosters": {
                str(team): response(
                    ["TeamID", "PLAYER", "PLAYER_ID"],
                    [[team, name, pid] for pid, name in players],
                )
                for team, players in rosters.items()
            },
        },
    )


def test_player_seasons_includes_rostered_players(tmp_path):
    write_raw(tmp_path)
    # player 1 played; 9 is on the roster but hasn't
    write_rosters(tmp_path, {BOS: [(1, "Roster A"), (9, "Injured")]})
    stats.build_season(tmp_path, 2026)
    rows = {r["player_id"]: r for r in read(tmp_path, "player_seasons")}
    assert rows["9"] == {
        "season": 2026,
        "player_id": "9",
        "name": "Injured",
        "played": False,
    }
    # a name from a game wins over the roster's
    assert rows["1"]["name"] == "A"
    assert rows["1"]["played"] is True
    assert rows["2"]["played"] is True


def test_player_seasons_without_rosters(tmp_path):
    write_raw(tmp_path)
    stats.build_season(tmp_path, 2026)
    assert all(r["played"] for r in read(tmp_path, "player_seasons"))


def test_roster_without_player_ids_fails(tmp_path):
    write_raw(tmp_path)
    stats.write_raw(
        stats.raw_path(tmp_path, 2026, stats.ROSTERS),
        {
            "fetched": "2026-01-01T00:00:00+00:00",
            "rosters": {"1": response(["PLAYER"], [])},
        },
    )
    with pytest.raises(ValueError, match="PLAYER_ID"):
        stats.build_season(tmp_path, 2026)


def test_fetch_rosters(tmp_path):
    calls, sleeps = [], []
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    def fake_fetch(endpoint, kwargs):
        calls.append(kwargs)
        return response(["PLAYER", "PLAYER_ID"], [["X", int(kwargs["team_id"])]])

    def fetch_rosters(**kw):
        stats.fetch_rosters(
            tmp_path,
            2026,
            fetch=fake_fetch,
            sleep=sleeps.append,
            now=lambda: clock[0],
            **kw,
        )

    fetch_rosters()
    assert len(calls) == 30
    assert calls[0] == {
        "team_id": "1610612737",
        "season": "2025-26",
        "league_id_nullable": "00",
    }
    assert len(sleeps) == 29
    assert stats.rosters_fetched(tmp_path, 2026) == clock[0]

    # recent rosters aren't refetched, unless forced
    clock[0] += stats.ROSTER_MAX_AGE - timedelta(minutes=1)
    fetch_rosters()
    assert len(calls) == 30
    fetch_rosters(force=True)
    assert len(calls) == 60

    clock[0] += stats.ROSTER_MAX_AGE
    fetch_rosters()
    assert len(calls) == 90
