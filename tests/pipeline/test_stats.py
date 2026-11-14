import gzip
import json
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest
from nba_api.stats.endpoints import (
    ISTStandings,
    LeagueDashPlayerStats,
    LeagueStandingsV3,
)

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
            ["PLAYER_ID", "TEAM_ID", "GAME_ID", "USG_PCT"],
            [[r[1], r[3], r[PGL.index("GAME_ID")], 0.25] for r in player_logs()],
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


def write_events(outdir, actions, season=2026):
    """
    write a game_events file for GAME from (period, team, person,
    action_type, sub_type, shot_result) tuples, in order
    """
    path = dataset_path(outdir / stats.OUT_DIR, stats.EVENTS, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ", ".join(
        f"('{GAME}', {i}, {period}, {team and repr(str(team))}, "
        f"{person and repr(str(person))}, '{action}', {sub and repr(sub)}, "
        f"{result and repr(result)})".replace("None", "NULL")
        for i, (period, team, person, action, sub, result) in enumerate(actions)
    )
    duckdb.sql(
        f"COPY (SELECT * FROM (VALUES {rows}) t(game_id, action_id, period, "
        "team_id, person_id, action_type, sub_type, shot_result)) TO "
        f"'{path}'"
    )


def test_team_oreb(tmp_path):
    miss = (1, BOS, 1, "Missed Shot", "Jump Shot", "Missed")
    write_events(
        tmp_path,
        [
            # a player's offensive rebound is in oreb already
            miss,
            (1, BOS, 1, "Rebound", "Unknown", None),
            # out of bounds off the defense, then a shot: counted
            miss,
            (1, BOS, None, "Rebound", "Unknown", None),
            (1, BOS, 1, "Made Shot", "Layup Shot", "Made"),
            # a shot-clock violation: counted
            (1, NYK, 2, "Missed Shot", "Jump Shot", "Missed"),
            (1, NYK, None, "Rebound", "Unknown", None),
            (1, NYK, None, "Turnover", "Shot Clock Turnover", None),
            # a dead ball between free throws isn't
            (1, BOS, 1, "Free Throw", "Free Throw 1 of 2", "Missed"),
            (1, BOS, None, "Rebound", "Normal Rebound", None),
            (1, BOS, 1, "Free Throw", "Free Throw 2 of 2", "Made"),
            # a team defensive rebound isn't
            (1, NYK, 2, "Missed Shot", "Jump Shot", "Missed"),
            (1, BOS, None, "Rebound", "Unknown", None),
            # nor is one as the period ends
            miss,
            (1, BOS, None, "Rebound", "Unknown", None),
            (2, NYK, 2, "Made Shot", "Jump Shot", "Made"),
        ],
    )
    write_raw(tmp_path)
    assert stats.build_season(tmp_path, 2026)
    rows = {(r["game_id"], r["team_id"]): r for r in read(tmp_path, "team_game_logs")}
    assert rows[(GAME, str(BOS))]["team_oreb"] == 1
    assert rows[(GAME, str(NYK))]["team_oreb"] == 1
    # no play-by-play
    assert rows[(CUP_FINAL, str(BOS))]["team_oreb"] is None
    assert types(tmp_path, "team_game_logs")["team_oreb"] == "INTEGER"


@pytest.mark.parametrize("url", [None, "http://nowhere.invalid"])
def test_team_oreb_without_events(tmp_path, url):
    # a bucket that can't be read doesn't stop the build
    write_raw(tmp_path)
    assert stats.build_season(tmp_path, 2026, url)
    assert all(r["team_oreb"] is None for r in read(tmp_path, "team_game_logs"))


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


def test_player_on_both_teams(tmp_path):
    # the NBA listed Devin Booker on both teams in a game, the extra row with
    # no minutes and a free throw
    date = "2025-10-21T00:00:00"
    phantom = player_log(1, "A", NYK, GAME, date, "NYK @ BOS", "L", 1)
    phantom[PGL.index("MIN")] = 0.0
    logs = player_logs() + [phantom]
    write_raw(
        tmp_path,
        player_game_logs_base=response(PGL, logs),
        player_game_logs_advanced=response(
            ["PLAYER_ID", "TEAM_ID", "GAME_ID", "USG_PCT"],
            [[r[1], r[3], r[PGL.index("GAME_ID")], r[3] / 1e10] for r in logs],
        ),
    )
    assert stats.build_season(tmp_path, 2026)
    rows = [r for r in read(tmp_path, "player_game_logs") if r["player_id"] == "1"]
    assert len(rows) == 1
    assert rows[0]["team_id"] == str(BOS)
    assert rows[0]["pts"] == 20
    # the advanced stats are the same team's
    assert rows[0]["usg_pct"] == BOS / 1e10


def test_unidentified_players(tmp_path):
    date = "2025-10-03T00:00:00"
    preseason = player_log(None, None, 94, "0012500001", date, "MLN vs. NYK", "L", 5)
    write_raw(
        tmp_path, player_game_logs_base=response(PGL, player_logs() + [preseason])
    )
    stats.build_season(tmp_path, 2026)
    assert len(read(tmp_path, "player_game_logs")) == 3

    regular = player_log(None, None, 94, GAME, date, "MLN vs. NYK", "L", 5)
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
        # no saved standings
        "conference": None,
        "division": None,
        "cup_group": None,
    }
    assert set(rows) == {str(BOS), str(NYK)}


def standings(skip=()):
    """standings for every NBA team; Boston is in the Atlantic, the rest elsewhere"""
    rows = [
        ["00", t, "East", "Atlantic" if t == BOS else f"Div{t % 6}", "1-0"]
        for t in stats.lookups.NBA_TEAM_IDS
        if t not in skip
    ]
    return response(["LeagueID", "TeamID", "Conference", "Division", "Record"], rows)


def test_team_seasons_conference_and_division(tmp_path):
    write_raw(tmp_path, team_game_logs_base=melbourne("0012500001"))
    stats.write_raw(stats.raw_path(tmp_path, 2026, stats.STANDINGS), standings())
    stats.build_season(tmp_path, 2026)
    rows = {r["team_id"]: r for r in read(tmp_path, "team_seasons")}
    assert (rows[str(BOS)]["conference"], rows[str(BOS)]["division"]) == (
        "East",
        "Atlantic",
    )
    # an international team isn't in the standings
    assert (rows["15016"]["conference"], rows["15016"]["division"]) == (None, None)
    assert types(tmp_path, "team_seasons")["division"] == "VARCHAR"


def test_standings_missing_a_team_fail(tmp_path):
    write_raw(tmp_path)
    stats.write_raw(
        stats.raw_path(tmp_path, 2026, stats.STANDINGS), standings(skip={NYK})
    )
    with pytest.raises(ValueError, match="29 teams"):
        stats.build_season(tmp_path, 2026)


def test_fetch_standings(tmp_path):
    calls = []

    def fake_fetch(endpoint, kwargs, check):
        calls.append((endpoint, kwargs))
        data = standings()
        check(data)
        return data

    stats.fetch_standings(tmp_path, 2027, fetch=fake_fetch)
    assert calls == [
        (
            LeagueStandingsV3,
            {"league_id": "00", "season": "2026-27", "season_type": "Regular Season"},
        )
    ]
    saved = stats.read_raw(stats.raw_path(tmp_path, 2027, stats.STANDINGS))
    assert len(stats.result_set(saved)["rowSet"]) == 30

    # an empty response is retried, so check rejects it
    empty = response(["TeamID", "Conference", "Division"], [])
    with pytest.raises(ValueError, match="0 teams"):
        stats.check_standings(empty)


def cup_standings(group=None):
    """
    Cup standings for every NBA team: Boston in East Group A, the rest in
    six groups of five. `group(team_id)` overrides each team's group
    """
    ids = sorted(stats.lookups.NBA_TEAM_IDS, key=lambda t: t != BOS)
    default = {t: f"{'East' if i < 15 else 'West'} Group {'ABC'[i // 5 % 3]}" for i, t in enumerate(ids)}  # fmt: skip
    teams = [
        {"teamId": t, "istGroup": group(t) if group else default[t], "games": []}
        for t in ids
    ]
    return {"leagueId": "00", "seasonYear": "2025-26", "teams": teams}


def test_team_seasons_cup_group(tmp_path):
    write_raw(tmp_path)
    stats.write_raw(
        stats.raw_path(tmp_path, 2026, stats.CUP_STANDINGS), cup_standings()
    )
    stats.build_season(tmp_path, 2026)
    rows = {r["team_id"]: r for r in read(tmp_path, "team_seasons")}
    assert rows[str(BOS)]["cup_group"] == "East Group A"
    assert types(tmp_path, "team_seasons")["cup_group"] == "VARCHAR"


def test_undrawn_cup_groups_are_null(tmp_path):
    write_raw(tmp_path)
    stats.write_raw(
        stats.raw_path(tmp_path, 2026, stats.CUP_STANDINGS),
        cup_standings(lambda t: ""),
    )
    stats.build_season(tmp_path, 2026)
    assert {r["cup_group"] for r in read(tmp_path, "team_seasons")} == {None}


def test_bad_cup_groups_fail():
    # one team missing its group
    with pytest.raises(ValueError, match="aren't 6 of 5"):
        stats.cup_groups(cup_standings(lambda t: "" if t == BOS else "East Group A"))
    # all in one group
    with pytest.raises(ValueError, match="aren't 6 of 5"):
        stats.cup_groups(cup_standings(lambda t: "East Group A"))
    data = cup_standings()
    data["teams"].pop()
    with pytest.raises(ValueError, match="29 teams"):
        stats.cup_groups(data)


def test_fetch_cup_standings(tmp_path):
    calls = []

    def fake_fetch(endpoint, kwargs, check):
        calls.append((endpoint, kwargs))
        data = cup_standings()
        check(data)
        return data

    # no Cup before 2023-24
    stats.fetch_cup_standings(tmp_path, 2023, fetch=fake_fetch)
    assert calls == []
    stats.fetch_cup_standings(tmp_path, 2027, fetch=fake_fetch)
    assert calls == [
        (
            ISTStandings,
            {"league_id": "00", "season": "2026-27", "section": "group"},
        )
    ]
    assert stats.raw_path(tmp_path, 2027, stats.CUP_STANDINGS).is_file()


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


ROSTER = ["TeamID", "PLAYER", "NUM", "POSITION", "PLAYER_ID", "HOW_ACQUIRED"]
ROSTER += ["SUPPLEMENTAL_STATUS"]


def full_roster(team, players=(), size=stats.ROSTER_MIN):
    """
    a team's roster response: `players` [(player_id, name)], then made-up
    players (team * 100 + i) up to `size`
    """
    filler = [(team * 100 + i, f"Filler {i}") for i in range(size - len(players))]
    players = [*players, *filler]
    return response(
        ROSTER,
        [[team, name, "7", "F", pid, "Signed on 07/01/25", 0] for pid, name in players],
    )


def write_daily_rosters(outdir, day, rosters, season=2026):
    """rosters: team_id -> response"""
    stats.write_raw(
        stats.daily_rosters_path(outdir, season, day),
        {
            "fetched": f"{day.isoformat()}T12:00:00+00:00",
            "rosters": {str(team): data for team, data in rosters.items()},
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

    def fake_fetch(endpoint, kwargs, check):
        calls.append(kwargs)
        data = full_roster(int(kwargs["team_id"]))
        check(data)
        return data

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


def test_fetch_rosters_keeps_former_players(tmp_path):
    write_raw(tmp_path)
    # 9 was on a roster; 8 was on one before that (and the file is old
    # enough to have no "former")
    write_rosters(tmp_path, {BOS: [(1, "Roster A"), (9, "Waived")]})
    path = stats.raw_path(tmp_path, 2026, stats.ROSTERS)
    saved = stats.read_raw(path)
    saved["former"] = [["8", "Long Gone"]]
    stats.write_raw(path, saved)

    # now 1 is the only player on a roster (with 12 more)
    def fake_fetch(endpoint, kwargs, check):
        if kwargs["team_id"] == str(BOS):
            return full_roster(BOS, [(1, "Roster A")])
        return full_roster(int(kwargs["team_id"]))

    stats.fetch_rosters(tmp_path, 2026, fetch=fake_fetch, sleep=lambda _: None)
    assert stats.read_raw(path)["former"] == [["8", "Long Gone"], ["9", "Waived"]]

    stats.build_season(tmp_path, 2026)
    rows = {r["player_id"]: r for r in read(tmp_path, "player_seasons")}
    assert rows["9"]["name"] == "Waived"
    assert rows["9"]["played"] is False
    assert rows["8"]["name"] == "Long Gone"

    # a former player back on a roster leaves the former list
    def rehired(endpoint, kwargs, check):
        if kwargs["team_id"] == str(BOS):
            return full_roster(BOS, [(9, "Waived")])
        return full_roster(int(kwargs["team_id"]))

    stats.fetch_rosters(tmp_path, 2026, fetch=rehired, sleep=lambda _: None, force=True)
    assert stats.read_raw(path)["former"] == [["1", "Roster A"], ["8", "Long Gone"]]


def test_fetch_rosters_saves_each_day(tmp_path):
    def fake_fetch(endpoint, kwargs, check):
        return full_roster(int(kwargs["team_id"]))

    def fetch_at(when):
        stats.fetch_rosters(
            tmp_path,
            2026,
            fetch=fake_fetch,
            sleep=lambda _: None,
            now=lambda: when,
            force=True,
        )

    # 3am UTC on the 2nd is still the 1st in the east
    fetch_at(datetime(2026, 1, 2, 3, tzinfo=UTC))
    fetch_at(datetime(2026, 1, 2, 18, tzinfo=UTC))
    # a second fetch the same day replaces the first
    fetch_at(datetime(2026, 1, 2, 22, tzinfo=UTC))
    days = stats.saved_roster_days(tmp_path, 2026)
    assert [d for d, _ in days] == [date(2026, 1, 1), date(2026, 1, 2)]
    saved = stats.read_raw(days[1][1])
    assert saved["fetched"] == "2026-01-02T22:00:00+00:00"
    assert len(saved["rosters"]) == 30


@pytest.mark.parametrize(
    "when, size, ok",
    [
        (datetime(2026, 1, 2, 18, tzinfo=UTC), stats.ROSTER_MIN, True),
        (datetime(2026, 1, 2, 18, tzinfo=UTC), stats.ROSTER_MIN - 1, False),
        (datetime(2026, 1, 2, 18, tzinfo=UTC), stats.ROSTER_MAX + 1, False),
        # in the offseason a roster can be small, but not empty
        (datetime(2026, 7, 20, 18, tzinfo=UTC), 5, True),
        (datetime(2026, 7, 20, 18, tzinfo=UTC), 0, False),
    ],
)
def test_fetch_rosters_checks_their_size(tmp_path, when, size, ok):
    def fetch(endpoint, kwargs, check):
        team = int(kwargs["team_id"])
        data = full_roster(team, size=size if team == BOS else stats.ROSTER_MIN)
        check(data)
        return data

    def fetch_rosters():
        stats.fetch_rosters(
            tmp_path, 2026, fetch=fetch, sleep=lambda _: None, now=lambda: when
        )

    if ok:
        fetch_rosters()
        assert stats.rosters_fetched(tmp_path, 2026) == when
    else:
        with pytest.raises(ValueError, match="players, not"):
            fetch_rosters()
        # nothing is saved
        assert stats.rosters_fetched(tmp_path, 2026) is None
        assert stats.saved_roster_days(tmp_path, 2026) == []


def test_rosters_daily(tmp_path):
    write_raw(tmp_path)
    first = full_roster(BOS, [(9, "Injured")])
    first["resultSets"][0]["rowSet"][0][2:4] = [None, ""]
    first["resultSets"][0]["rowSet"][0][5:7] = ["#3 Pick in 2025 Draft", 1]
    write_daily_rosters(
        tmp_path, date(2026, 1, 1), {BOS: first, NYK: full_roster(NYK, [(8, "C")])}
    )
    # 8 moves from New York to Boston
    write_daily_rosters(
        tmp_path,
        date(2026, 1, 2),
        {BOS: full_roster(BOS, [(9, "Injured"), (8, "C")]), NYK: full_roster(NYK)},
    )
    stats.build_season(tmp_path, 2026)
    rows = read(tmp_path, stats.ROSTERS_DAILY)
    assert len(rows) == 4 * stats.ROSTER_MIN
    assert rows[0] == {
        "season": 2026,
        "as_of_date": date(2026, 1, 1),
        "team_id": str(BOS),
        "player_id": str(BOS * 100),
        "num": "7",
        "position": "F",
        "two_way": False,
        "how_acquired": "Signed on 07/01/25",
    }
    injured = [r for r in rows if r["player_id"] == "9"]
    assert injured[0] | {"as_of_date": None} == {
        "season": 2026,
        "as_of_date": None,
        "team_id": str(BOS),
        "player_id": "9",
        "num": None,
        "position": None,
        "two_way": True,
        "how_acquired": "#3 Pick in 2025 Draft",
    }
    assert [(r["as_of_date"], r["team_id"]) for r in rows if r["player_id"] == "8"] == [
        (date(2026, 1, 1), str(NYK)),
        (date(2026, 1, 2), str(BOS)),
    ]
    assert types(tmp_path, stats.ROSTERS_DAILY) == {
        "season": "INTEGER",
        "as_of_date": "DATE",
        "team_id": "VARCHAR",
        "player_id": "VARCHAR",
        "num": "VARCHAR",
        "position": "VARCHAR",
        "two_way": "BOOLEAN",
        "how_acquired": "VARCHAR",
    }
    # every rostered player is in player_seasons, with no team_rosters.json.gz
    players = {r["player_id"] for r in read(tmp_path, "player_seasons")}
    assert {r["player_id"] for r in rows} <= players


def test_rosters_daily_waits_for_teams_to_play(tmp_path):
    write_raw(tmp_path)
    write_daily_rosters(
        tmp_path,
        date(2026, 1, 1),
        {BOS: full_roster(BOS), 1610612737: full_roster(1610612737)},
    )
    stats.build_season(tmp_path, 2026)
    path = dataset_path(tmp_path / stats.OUT_DIR, stats.ROSTERS_DAILY, 2026)
    assert not path.exists()


def test_rosters_daily_unknown_supplemental_status_fails(tmp_path):
    write_raw(tmp_path)
    data = full_roster(BOS)
    data["resultSets"][0]["rowSet"][0][6] = 2
    write_daily_rosters(tmp_path, date(2026, 1, 1), {BOS: data})
    with pytest.raises(ValueError, match="SUPPLEMENTAL_STATUS 2"):
        stats.build_season(tmp_path, 2026)


def test_player_game_logs_wait_for_the_team_game_logs(tmp_path):
    # the player game logs can have a game before the team game logs do
    late = "0022500002"
    logs = player_logs() + [
        player_log(1, "A", BOS, late, "2025-10-23T00:00:00", "BOS vs. NYK", "W", 30)
    ]
    write_raw(tmp_path, player_game_logs_base=response(PGL, logs))
    assert stats.build_season(tmp_path, 2026)
    games = {r["game_id"] for r in read(tmp_path, "player_game_logs")}
    assert games == {GAME, CUP_FINAL}
