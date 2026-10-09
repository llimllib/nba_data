import gzip
import json

import duckdb
import pytest
from nba_api.stats.endpoints import LeagueDashPlayerStats

from pipeline import stats

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


PGL = ["SEASON_YEAR", "PLAYER_ID", "PLAYER_NAME", "TEAM_ID", "GAME_ID"]
PGL += ["GAME_DATE", "MATCHUP", "WL", "MIN", "PTS", "MIN_SEC"]


def player_logs():
    date = "2025-10-21T00:00:00"
    return [
        ["2025-26", 1, "A", BOS, GAME, date, "BOS vs. NYK", "W", 30.5, 20, "30:30"],
        ["2025-26", 2, "B", NYK, GAME, date, "NYK @ BOS", "L", 20.0, 8, "20:00"],
        ["2025-26", 2, "B", NYK, CUP_FINAL, date, "NYK @ BOS", "W", 25.0, 12, "25:00"],
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
            [[r[1], r[4], 0.25] for r in player_logs()],
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
    path = outdir / stats.OUT_DIR / dataset / f"{season}.parquet"
    rel = duckdb.sql(f"SELECT * FROM '{path}'")
    return [dict(zip(rel.columns, row)) for row in rel.fetchall()]


def types(outdir, dataset, season=2026):
    path = outdir / stats.OUT_DIR / dataset / f"{season}.parquet"
    return dict(
        duckdb.sql(
            f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM '{path}')"
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
    preseason = [
        "2025-26",
        None,
        None,
        94,
        "0012500001",
        date,
        "MLN vs. NYK",
        "L",
        30.0,
        5,
        "30:00",
    ]
    write_raw(
        tmp_path, player_game_logs_base=response(PGL, player_logs() + [preseason])
    )
    stats.build_season(tmp_path, 2026)
    assert len(read(tmp_path, "player_game_logs")) == 3

    regular = [*preseason[:4], "0022500099", *preseason[5:]]
    write_raw(tmp_path, player_game_logs_base=response(PGL, player_logs() + [regular]))
    with pytest.raises(ValueError, match="NULL"):
        stats.build_season(tmp_path, 2026)
