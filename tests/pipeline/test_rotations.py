import threading
from datetime import date
from pathlib import Path

import duckdb
import pytest

from pipeline import rotations, stats
from pipeline.output import dataset_path

FIXTURE = Path(__file__).parent.parent / "fixtures" / "playbyplay_0022500001.json.gz"
HOU = 1610612745
OKC = 1610612760
GAME = "0022500001"

HOME, AWAY = 100, 200
# five players a side, ids 1-5 and 11-15 at home, 21-25 and 31-35 away
HOME_PLAYERS = [(i, f"First{i}", f"Home{i}") for i in [*range(1, 6), *range(11, 16)]]
AWAY_PLAYERS = [(i, f"First{i}", f"Away{i}") for i in [*range(21, 26), *range(31, 36)]]


def player(pid, first, family, minutes="", pm=0):
    return {
        "personId": pid,
        "firstName": first,
        "familyName": family,
        "nameI": f"{first[0]}. {family}",
        "statistics": {"minutes": minutes, "plusMinusPoints": float(pm)},
    }


def box(home=HOME_PLAYERS, away=AWAY_PLAYERS, minutes=None):
    """a box score; `minutes` is person id -> "MM:SS" for those who played"""
    played = minutes or {}

    def team(team_id, players):
        return {
            "teamId": team_id,
            "players": [
                player(pid, first, family, played.get(pid, ""))
                for pid, first, family in players
            ],
        }

    return {
        "boxScoreTraditional": {
            "homeTeamId": HOME,
            "awayTeamId": AWAY,
            "homeTeam": team(HOME, home),
            "awayTeam": team(AWAY, away),
        }
    }


def clock(seconds_left):
    return f"PT{int(seconds_left // 60):02d}M{seconds_left % 60:05.2f}S"


class Plays:
    """builds a play-by-play, one action at a time"""

    def __init__(self):
        self.actions = []
        self.score = [0, 0]

    def add(self, period, left, team, pid, kind, description="", sub_type="", name=""):
        self.actions.append(
            {
                "actionNumber": len(self.actions) + 1,
                "period": period,
                "clock": clock(left),
                "teamId": team,
                "personId": pid,
                "playerName": name,
                "actionType": kind,
                "subType": sub_type,
                "description": description,
                "scoreHome": "",
                "scoreAway": "",
                "shotValue": 0,
            }
        )
        return self.actions[-1]

    def scores(self, period, left, team, pid, points, kind="Made Shot", sub_type=""):
        self.score[0 if team == HOME else 1] += points
        a = self.add(period, left, team, pid, kind, "shot", sub_type)
        a["scoreHome"], a["scoreAway"] = map(str, self.score)
        if kind == "Made Shot":
            a["shotValue"] = points

    def misses_free_throw(self, period, left, team, pid):
        self.add(period, left, team, pid, "Free Throw", "MISS free throw")

    def sub(self, period, left, team, out_pid, incoming, outgoing):
        self.add(
            period,
            left,
            team,
            out_pid,
            "Substitution",
            f"SUB: {incoming} FOR {outgoing}",
        )

    def starters(self, period, left=715, home=range(1, 6), away=range(21, 26)):
        """an action for each starter, so they're inferred"""
        for pid in [*home, *away]:
            self.add(period, left, HOME if pid < 20 else AWAY, pid, "Rebound")

    def game(self, box_score=None, period_boxscores=None):
        return {
            "playbyplay": {"game": {"actions": self.actions}},
            "boxscore": box_score or box(),
            "period_boxscores": period_boxscores or {},
        }


def by_player(stints):
    return {s.player_id: s for s in stints}


def derive(plays, **kw):
    stints, problems = rotations.derive(plays.game(**kw))
    assert problems == []
    return stints


def test_time():
    assert rotations.elapsed(1, "PT12M00.00S") == 0
    assert rotations.elapsed(2, "PT07M13.50S") == 720 + 286.5
    assert rotations.elapsed(5, "PT00M00.00S") == 3180
    assert rotations.period_start(6) == 3180
    assert rotations.minutes_seconds("29:18") == 1758
    assert rotations.minutes_seconds("") == 0


def test_a_game():
    plays = Plays()
    plays.starters(1)
    plays.scores(1, 700, HOME, 1, 2)
    plays.sub(1, 600, HOME, 1, "Home11", "Home1")
    plays.scores(1, 500, AWAY, 21, 3)
    plays.scores(1, 400, HOME, 11, 2)
    stints = by_player(derive(plays))
    one, eleven, two = stints[1], stints[11], stints[2]
    assert (one.start, one.end, one.pts) == (0, 120, 2)
    assert rotations.plus_minus(one, HOME) == 2
    # to the end of the period
    assert (eleven.start, eleven.end, eleven.pts) == (120, 720, 2)
    assert rotations.plus_minus(eleven, HOME) == -1
    assert rotations.plus_minus(two, HOME) == 1
    assert rotations.plus_minus(stints[21], HOME) == -1


def test_stints_continue_across_periods():
    plays = Plays()
    plays.starters(1)
    plays.scores(1, 100, HOME, 1, 2)
    plays.starters(2)
    plays.scores(2, 100, HOME, 1, 3)
    stints = derive(plays)
    assert len(stints) == 10
    one = by_player(stints)[1]
    assert (one.period, one.start, one.end, one.pts) == (1, 0, 1440, 5)
    assert rotations.plus_minus(one, HOME) == 5


def test_a_player_who_doesnt_start_the_next_period():
    plays = Plays()
    plays.starters(1)
    plays.starters(2, home=[11, 2, 3, 4, 5])
    stints = derive(plays)
    starts = sorted((s.player_id, s.start, s.end) for s in stints if s.team_id == HOME)
    assert (1, 0, 720) in starts
    assert (11, 720, 1440) in starts


def test_free_throws_count_for_the_players_on_for_the_foul():
    plays = Plays()
    plays.starters(1)
    plays.add(1, 600, HOME, 1, "Foul", "S.FOUL")
    plays.scores(1, 600, AWAY, 21, 1, "Free Throw")
    plays.sub(1, 600, HOME, 1, "Home11", "Home1")
    plays.scores(1, 600, AWAY, 21, 1, "Free Throw")
    plays.scores(1, 500, AWAY, 21, 2)
    stints = by_player(derive(plays))
    assert rotations.plus_minus(stints[1], HOME) == -2
    assert rotations.plus_minus(stints[11], HOME) == -2
    assert stints[1].end == stints[11].start == 120


def test_a_new_foul_starts_a_new_trip():
    plays = Plays()
    plays.starters(1)
    plays.sub(1, 600, HOME, 1, "Home11", "Home1")
    plays.add(1, 600, HOME, 11, "Foul", "AWAY.FROM.PLAY.FOUL")
    plays.scores(1, 600, AWAY, 21, 1, "Free Throw")
    stints = by_player(derive(plays))
    assert rotations.plus_minus(stints[1], HOME) == 0
    assert rotations.plus_minus(stints[11], HOME) == -1


def test_technical_free_throws_count_for_the_players_on_for_the_technical():
    # a technical before the sub: its free throw is the outgoing player's
    plays = Plays()
    plays.starters(1)
    plays.add(1, 600, HOME, 2, "Foul", "T.FOUL", "Technical")
    plays.sub(1, 600, HOME, 1, "Home11", "Home1")
    plays.scores(1, 600, AWAY, 21, 1, "Free Throw", "Free Throw Technical")
    stints = by_player(derive(plays))
    assert rotations.plus_minus(stints[1], HOME) == -1
    assert rotations.plus_minus(stints[11], HOME) == 0

    # a technical after the sub, between a trip's free throws: the
    # technical's free throw is the incoming player's, the trip's isn't
    plays = Plays()
    plays.starters(1)
    plays.add(1, 600, HOME, 1, "Foul", "P.FOUL")
    plays.misses_free_throw(1, 600, AWAY, 21)
    plays.sub(1, 600, HOME, 1, "Home11", "Home1")
    plays.add(1, 600, HOME, 2, "Foul", "T.FOUL", "Technical")
    plays.scores(1, 600, AWAY, 22, 1, "Free Throw", "Free Throw Technical")
    plays.scores(1, 600, AWAY, 21, 1, "Free Throw")
    stints = by_player(derive(plays))
    assert rotations.plus_minus(stints[1], HOME) == -1
    assert rotations.plus_minus(stints[11], HOME) == -1


def test_bench_technicals_dont_make_starters():
    plays = Plays()
    plays.starters(1)
    plays.add(1, 600, HOME, 12, "Foul", "Home12 T.FOUL", "Technical")
    stints = derive(plays)
    assert 12 not in by_player(stints)


def test_names_from_the_player_going_out():
    # renamed: the box score says Freedom, the subs say Kanter
    home = [*HOME_PLAYERS[:-1], (15, "Enes", "Freedom")]
    plays = Plays()
    plays.starters(1)
    plays.sub(1, 600, HOME, 1, "Kanter", "Home1")
    plays.sub(1, 500, HOME, 15, "Home1", "Kanter")
    stints = derive(plays, box_score=box(home=home))
    assert (by_player(stints)[15].start, by_player(stints)[15].end) == (120, 220)


def test_names_with_accents_and_prefixes():
    home = [*HOME_PLAYERS[:-2], (14, "Jalen", "Williams"), (15, "Jaylin", "Williams")]
    plays = Plays()
    plays.starters(1)
    plays.sub(1, 600, HOME, 1, "Jay. Williams", "Home1")
    plays.sub(1, 500, HOME, 2, "Jal. Williams", "Home2")
    stints = by_player(derive(plays, box_score=box(home=home)))
    assert (stints[15].start, stints[14].start) == (120, 220)

    home = [*HOME_PLAYERS[:-1], (15, "Dennis", "Schröder")]
    plays = Plays()
    plays.starters(1)
    plays.sub(1, 600, HOME, 1, "Schroder", "Home1")
    assert by_player(derive(plays, box_score=box(home=home)))[15].start == 120


def test_names_by_suffix_and_elimination():
    home = [*HOME_PLAYERS[:-2], (14, "Otto", "Porter Jr."), (15, "Enes", "Freedom")]
    minutes = {p: "12:00" for p in [1, 2, 3, 4, 5, 14, 15]}
    plays = Plays()
    plays.starters(1)
    # neither goes out again, so the subs never use their names for them
    plays.sub(1, 600, HOME, 1, "Porter", "Home1")
    plays.sub(1, 500, HOME, 2, "Kanter", "Home2")
    stints = by_player(derive(plays, box_score=box(home=home, minutes=minutes)))
    assert (stints[14].start, stints[15].start) == (120, 220)


def test_unmatched_name():
    plays = Plays()
    plays.starters(1)
    plays.sub(1, 600, HOME, 1, "Nobody", "Home1")
    _, problems = rotations.derive(plays.game())
    assert problems == ["no player for 'SUB: Nobody FOR Home1' at 120.0s"]


def test_starters_from_the_period_box_score():
    plays = Plays()
    # player 5 does nothing all period
    plays.starters(1, home=range(1, 5))
    game = plays.game()
    assert rotations.missing_starters(game) == [1]
    _, problems = rotations.derive(game)
    assert problems == [f"period 1: 4 starters for {HOME}"]
    # the period's range takes in a few seconds of the next one, where 11
    # played
    minutes = {p: "12:05" for p in [*range(1, 6), *range(21, 26)]}
    period = box(minutes={**minutes, 11: "0:05"})
    stints = by_player(derive(plays, period_boxscores={"1": period}))
    assert stints[5].end == 720
    assert 11 not in stints


def test_the_score_is_counted_not_read():
    plays = Plays()
    plays.starters(1)
    plays.scores(1, 700, HOME, 1, 2)
    # the rows' scores are wrong: 0-0 on a miss (older games), and the final
    # score on a period's end
    plays.add(1, 650, AWAY, 21, "Missed Shot", "MISS")
    plays.actions[-1]["scoreHome"] = plays.actions[-1]["scoreAway"] = "0"
    plays.scores(1, 600, AWAY, 21, 3)
    plays.misses_free_throw(1, 550, HOME, 1)
    plays.add(1, 0, 0, 0, "period", "End of 1st Period")
    plays.actions[-1]["scoreHome"], plays.actions[-1]["scoreAway"] = "100", "90"
    stints = by_player(derive(plays))
    assert rotations.plus_minus(stints[1], HOME) == -1
    assert (stints[1].pts, stints[21].pts) == (2, 3)


def test_a_basket_listed_out_of_order():
    # a free throw's row already has the score after a later basket, which
    # comes after the sub
    plays = Plays()
    plays.starters(1)
    plays.add(1, 600, HOME, 1, "Foul", "S.FOUL")
    plays.sub(1, 600, HOME, 1, "Home11", "Home1")
    plays.scores(1, 600, AWAY, 21, 1, "Free Throw")
    plays.actions[-1]["scoreAway"] = "4"
    plays.scores(1, 590, AWAY, 22, 3)
    stints = by_player(derive(plays))
    assert rotations.plus_minus(stints[1], HOME) == -1
    assert rotations.plus_minus(stints[11], HOME) == -3


def test_box_differences():
    plays = Plays()
    plays.starters(1)
    plays.scores(1, 700, HOME, 1, 2)
    game = plays.game()
    stints, _ = rotations.derive(game)
    played = {p: "12:00" for p in [*range(1, 6), *range(21, 26)]}
    game["boxscore"] = box(minutes=played)
    found = rotations.box_differences(game, stints)
    # the box score has no plus-minus
    assert len(found) == 10 and found[0] == "1 plus-minus 2, box 0"
    logged = {p: 2 if p < 20 else -2 for p in played}
    assert rotations.box_differences(game, stints, logged) == []
    game["boxscore"] = box(minutes={**played, 1: "10:00"})
    assert rotations.box_differences(game, stints, logged) == [
        "1 minutes 12.0, box 10:00"
    ]


def test_a_real_game():
    """
    Opening night 2025-26, HOU at OKC: overtime, a player with nothing in
    the play-by-play in overtime (so a period box score), and the NBA's
    gamerotation stints to compare with
    """
    game = stats.read_raw(FIXTURE)
    stints, problems = rotations.derive(game)
    assert problems == []
    assert rotations.box_differences(game, stints) == []
    durant = [
        (s.start, s.end, rotations.plus_minus(s, OKC), s.pts)
        for s in stints
        if s.player_id == 201142
    ]
    # gamerotation: 0-720 +3 8pts, 949-1439 +4 6pts, 1440-2160 -2 3pts,
    # 2585-3478 -5 6pts
    assert durant == [
        (0, 720, 3, 8),
        (949, 1439.3, 4, 6),
        (1440, 2160, -2, 3),
        (2585, 3477.7, -5, 6),
    ]


def save_fixture(tmp_path, season=2026, game_id=GAME):
    path = rotations.raw_path(tmp_path, season, game_id)
    stats.write_raw(path, stats.read_raw(FIXTURE))


def write_parquet(tmp_path, dataset, season, sql):
    path = dataset_path(tmp_path / stats.OUT_DIR, dataset, season)
    path.parent.mkdir(parents=True)
    duckdb.sql(sql).write_parquet(str(path))


def test_build(tmp_path):
    save_fixture(tmp_path)
    assert rotations.build_season(tmp_path, 2026, url="http://nowhere.invalid")
    path = dataset_path(tmp_path / stats.OUT_DIR, rotations.NAME, 2026)
    table = duckdb.read_parquet(str(path), hive_partitioning=False)
    types = dict(zip(table.columns, map(str, table.types)))
    assert types == {
        "season": "INTEGER",
        "game_id": "VARCHAR",
        "team_id": "VARCHAR",
        "player_id": "VARCHAR",
        "period": "INTEGER",
        "in_time": "DOUBLE",
        "out_time": "DOUBLE",
        "pts": "INTEGER",
        "plus_minus": "INTEGER",
        "matches_box_score": "BOOLEAN",
    }
    assert table.count("*").fetchone() == (94,)
    assert table.filter("NOT matches_box_score").count("*").fetchone() == (0,)
    row = table.filter("player_id = '201142' AND in_time = 949").fetchone()
    assert row == (2026, GAME, str(HOU), "201142", 2, 949.0, 1439.3, 6, 4, True)


def durant_matches(tmp_path, logged, box):
    """
    build the fixture with Durant's plus-minus in the game logs and the box
    score set to these, and return the game's matches_box_score
    """
    game = stats.read_raw(FIXTURE)
    for t in rotations.box_teams(game["boxscore"]):
        for p in t["players"]:
            if p["personId"] == 201142:
                p["statistics"]["plusMinusPoints"] = float(box)
    stats.write_raw(rotations.raw_path(tmp_path, 2026, GAME), game)
    logs = dataset_path(tmp_path / stats.OUT_DIR, "player_game_logs", 2026)
    logs.parent.mkdir(parents=True, exist_ok=True)
    duckdb.sql(
        f"SELECT '{GAME}' AS game_id, '201142' AS player_id, {logged} AS plus_minus"
    ).write_parquet(str(logs))
    assert rotations.build_season(tmp_path, 2026)
    path = dataset_path(tmp_path / stats.OUT_DIR, rotations.NAME, 2026)
    flags = duckdb.sql(f"SELECT DISTINCT matches_box_score FROM '{path}'").fetchall()
    assert len(flags) == 1
    return flags[0][0]


def test_build_checks_against_the_box_scores(tmp_path, capsys):
    # the NBA's two box scores sometimes disagree; matching either is enough
    assert durant_matches(tmp_path, logged=99, box=0)
    assert durant_matches(tmp_path, logged=0, box=99)
    capsys.readouterr()
    assert not durant_matches(tmp_path, logged=99, box=99)
    assert "1 of 1 games' stints don't match" in capsys.readouterr().out


def test_no_games(tmp_path):
    assert not rotations.build_season(tmp_path, 2026)


def test_season_games(tmp_path):
    write_parquet(
        tmp_path,
        "games",
        2026,
        "SELECT * FROM (VALUES ('0022500002', DATE '2026-01-02'), "
        "('0022500001', DATE '2026-01-01')) t(game_id, game_date)",
    )
    assert rotations.season_games(tmp_path, 2026) == [
        ("0022500001", date(2026, 1, 1)),
        ("0022500002", date(2026, 1, 2)),
    ]


def test_stats_ignores_the_play_by_play(tmp_path):
    save_fixture(tmp_path)
    assert not stats.build_season(tmp_path, 2026)


TODAY = date(2026, 1, 10)
GAMES = [
    ("0022500001", date(2026, 1, 1)),
    ("0022500002", date(2026, 1, 2)),
    ("0022500003", date(2026, 1, 8)),
    ("0022500004", date(2026, 1, 9)),
]


def fetch_season(tmp_path, fetch, **kw):
    """fetch GAMES, one at a time unless `workers` says otherwise"""
    kw.setdefault("workers", 1)
    return rotations.fetch_season(
        tmp_path, 2026, GAMES, TODAY, fetch=fetch, sleep=lambda _: None, **kw
    )


def fake_game(game_id):
    return {"game_id": game_id}


def saved(tmp_path):
    return [
        p.name.removesuffix(".json.gz") for p in rotations.saved_games(tmp_path, 2026)
    ]


def test_fetch_season_fetches_new_and_recent_games(tmp_path):
    for game_id in ("0022500001", "0022500003"):
        stats.write_raw(rotations.raw_path(tmp_path, 2026, game_id), {})
    seen = []

    def fetch(game_id):
        seen.append(game_id)
        return fake_game(game_id)

    assert fetch_season(tmp_path, fetch) == (3, [])
    # newest first. 0022500001 is saved and old; 0022500003 is saved but recent
    assert seen == ["0022500004", "0022500003", "0022500002"]
    assert saved(tmp_path) == [g for g, _ in GAMES]
    path = rotations.raw_path(tmp_path, 2026, "0022500003")
    assert stats.read_raw(path) == fake_game("0022500003")


def test_fetch_season_fetches_games_at_once(tmp_path):
    # each fetch waits until two are in flight, so this only finishes if
    # they overlap
    barrier = threading.Barrier(2, timeout=5)

    def fetch(game_id):
        barrier.wait()
        return fake_game(game_id)

    assert fetch_season(tmp_path, fetch, workers=2) == (4, [])
    assert saved(tmp_path) == [g for g, _ in GAMES]


def test_fetch_season_leaves_failures(tmp_path):
    def fetch(game_id):
        if game_id == "0022500002":
            raise TimeoutError
        return fake_game(game_id)

    assert fetch_season(tmp_path, fetch) == (3, ["0022500002"])
    assert "0022500002" not in saved(tmp_path)


def test_fetch_season_stops_after_consecutive_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(rotations, "MAX_CONSECUTIVE_FAILURES", 2)
    calls = []

    def fetch(game_id):
        calls.append(game_id)
        raise TimeoutError

    assert fetch_season(tmp_path, fetch) == (0, [g for g, _ in reversed(GAMES)])
    assert len(calls) == 2


def test_fetch_season_budget(tmp_path):
    clock = [0.0]

    def fetch(game_id):
        clock[0] += 60
        return fake_game(game_id)

    fetched, left = fetch_season(tmp_path, fetch, clock=lambda: clock[0], budget=90)
    assert (fetched, left) == (2, ["0022500002", "0022500001"])
    assert saved(tmp_path) == ["0022500003", "0022500004"]


def test_fetch_season_saves_each_game(tmp_path):
    def fetch(game_id):
        if game_id == "0022500002":
            raise KeyboardInterrupt
        return fake_game(game_id)

    with pytest.raises(KeyboardInterrupt):
        fetch_season(tmp_path, fetch)
    assert saved(tmp_path) == ["0022500003", "0022500004"]


def test_fetch_game_fetches_period_box_scores(monkeypatch):
    game = stats.read_raw(FIXTURE)
    calls = []

    def fake_fetch(endpoint, kwargs, **_):
        calls.append(kwargs)
        if endpoint is rotations.PlayByPlayV3:
            return game["playbyplay"]
        if kwargs.get("range_type") == "2":
            return game["period_boxscores"]["5"]
        return game["boxscore"]

    monkeypatch.setattr(stats, "fetch", fake_fetch)
    assert rotations.fetch_game(GAME) == game
    assert calls[2] == {
        "game_id": GAME,
        "range_type": "2",
        "start_range": "28800",
        "end_range": "31800",
    }
