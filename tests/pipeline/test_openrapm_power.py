import gzip
import json
from datetime import date

import duckdb
import pytest

from pipeline import openrapm_power as p
from pipeline.output import dataset_path

TEAM_IDS = {"SAS": "1610612759", "BOS": "1610612738"}
TODAY = date(2026, 11, 12)


def team(tm="SAS", recent=(0.4, 0.31), players=((60.0, 2.0, 1.0),), g=10):
    """
    A team whose ratings are its rows' sum. `players` are (share, o, d);
    `recent` is the correction's (o, d), or None for no correction row
    """
    rows: list[list] = [[f"P{i}", 1000 + i, share, o, d, round(o + d, 2)]
            for i, (share, o, d) in enumerate(players)]  # fmt: skip
    if recent is not None:
        o, d = recent
        rows.insert(0, ["Last 10 Performance Over Expected", None, None, o, d,
                        round(o + d, 2)])  # fmt: skip
    o = sum(r[3] * (r[2] / 100 if r[2] is not None else 1) for r in rows)
    d = sum(r[4] * (r[2] / 100 if r[2] is not None else 1) for r in rows)
    return {"tm": tm, "name": tm, "g": g, "o": round(o, 2), "d": round(d, 2),
            "t": round(o + d, 2), "players": rows}  # fmt: skip


def power(*teams, through="2026-11-11", built="2026-11-12", season=2027):
    return {
        "meta": {"season": season, "through": through, "built": built,
                 "ready": True, "average_team": {"o": 0.1, "d": 0.1, "t": 0.2}},
        "teams": list(teams) or [team("SAS"), team("BOS", recent=(-0.4, -0.31))],
    }  # fmt: skip


def read(path, order="team_id"):
    return duckdb.sql(
        f"SELECT * FROM read_parquet('{path}', hive_partitioning = false) "
        f"ORDER BY {order}"
    ).fetchall()


def test_power_rows():
    rows = p.power_rows(power(team("SAS")), TEAM_IDS)
    assert rows == [
        {"season": 2027, "through_date": date(2026, 11, 11),
         "built": date(2026, 11, 12), "ready": True, "team_id": "1610612759",
         "games": 10, "o_rating": 1.6, "d_rating": 0.91, "rating": 2.51,
         "o_recent": 0.4, "d_recent": 0.31, "recent": 0.71},
    ]  # fmt: skip


def test_ready_is_optional():
    doc = power(team("SAS"))
    del doc["meta"]["ready"]
    assert p.power_rows(doc, TEAM_IDS)[0]["ready"] is None


def test_rows_that_dont_add_up_fail():
    t = team("SAS")
    t["o"] += 0.2
    with pytest.raises(ValueError, match="add up to o"):
        p.power_rows(power(t), TEAM_IDS)


def test_a_team_with_no_recent_row_fails():
    with pytest.raises(ValueError, match="no recent-games row"):
        p.power_rows(power(team("SAS", recent=None)), TEAM_IDS)


def test_two_recent_rows_fail():
    t = team("SAS")
    t["players"].append(t["players"][0])
    with pytest.raises(ValueError, match="two rows with no share"):
        p.power_rows(power(t), TEAM_IDS)


def test_an_unknown_team_fails():
    with pytest.raises(ValueError, match="unknown team 'XYZ'"):
        p.power_rows(power(team("XYZ")), TEAM_IDS)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("meta"),
        lambda d: d["meta"].pop("through"),
        lambda d: d["meta"].update(built="soon"),
        lambda d: d["meta"].update(season="2027"),
        lambda d: d["meta"].update(ready="yes"),
        lambda d: d.update(teams=[]),
        lambda d: d["teams"][0].pop("players"),
        lambda d: d["teams"][0].update(g=None),
        lambda d: d["teams"][0].update(o="1.5"),
        lambda d: d["teams"][0]["players"][1].pop(),
    ],
)
def test_malformed_files_fail(change):
    doc = power()
    change(doc)
    with pytest.raises(ValueError):
        p.power_rows(doc, TEAM_IDS)


def test_a_duplicate_team_fails(tmp_path):
    with pytest.raises(Exception, match="(?i)duplicate"):
        p.build_latest(tmp_path, power(team("SAS"), team("SAS")), TEAM_IDS)


def save(outdir, doc):
    meta = doc["meta"]
    p.save_raw(p.raw_path(outdir, meta["season"], meta["through"]), json.dumps(doc))


def test_build_daily(tmp_path):
    save(tmp_path, power(through="2026-11-10", built="2026-11-11"))
    save(tmp_path, power())
    path = p.build_daily(tmp_path, 2027, TEAM_IDS)
    assert path == dataset_path(tmp_path / p.OUT_DIR, "team_openrapm_daily", 2027)
    assert [(r[1], r[4]) for r in read(path, "through_date, team_id")] == [
        (date(2026, 11, 10), "1610612738"),
        (date(2026, 11, 10), "1610612759"),
        (date(2026, 11, 11), "1610612738"),
        (date(2026, 11, 11), "1610612759"),
    ]


def test_build_daily_with_no_days(tmp_path):
    assert p.build_daily(tmp_path, 2027, TEAM_IDS) is None


class Repo:
    """openrapm's repo: main's file and the history's, newest first"""

    def __init__(self, main, history=()):
        self.main = main
        self.history = list(history)
        self.since = []

    def commits(self, since):
        self.since.append(since)
        return [sha for sha, _ in self.history]

    def fetch(self, url, headers=None):
        if url == f"{p.BASE_URL}/power.json":
            return json.dumps(self.main)
        files = {f"{p.RAW_URL}/{sha}/{p.FILE}": doc for sha, doc in self.history}
        return json.dumps(files[url]) if isinstance(files[url], dict) else files[url]


@pytest.fixture
def team_ids(monkeypatch):
    monkeypatch.setattr(p, "team_ids_for", lambda outdir, season: TEAM_IDS)


def days(outdir, season=2027):
    return [d.isoformat() for d, _ in p.saved_days(outdir, season)]


def test_update_fills_in_from_history(tmp_path, team_ids):
    today = power()
    rebuilt = power(through="2026-11-09", built="2026-11-11", season=2027)
    first = power(team("SAS", players=((60.0, 9.0, 9.0),)), team("BOS"),
                  through="2026-11-09", built="2026-11-10")  # fmt: skip
    old_method = power(team("SAS", recent=None), through="2026-11-07")
    repo = Repo(today, [
        ("c4", today), ("c3", rebuilt), ("c2", first), ("c1", old_method),
        ("c0", "not json"),
    ])  # fmt: skip
    p.update(tmp_path, TODAY, repo.fetch, repo.commits)

    # nothing saved yet: from the start of the season
    assert repo.since == [date(2026, 10, 15)]
    assert days(tmp_path) == ["2026-11-09", "2026-11-11"]
    # a day built twice keeps the newer build
    saved = p.read_raw(p.raw_path(tmp_path, 2027, "2026-11-09"))
    assert saved["meta"]["built"] == "2026-11-11"

    latest = dataset_path(tmp_path / p.OUT_DIR, "team_openrapm", 2027)
    assert {r[1] for r in read(latest)} == {date(2026, 11, 11)}
    daily = dataset_path(tmp_path / p.OUT_DIR, "team_openrapm_daily", 2027)
    assert len(read(daily)) == 4

    # the next run lists commits from the newest saved day
    repo.history = []
    p.update(tmp_path, TODAY, repo.fetch, repo.commits)
    assert repo.since[-1] == date(2026, 11, 11)


def test_update_without_history_still_writes_today(tmp_path, team_ids):
    def broken(since):
        raise OSError("rate limited")

    repo = Repo(power())
    p.update(tmp_path, TODAY, repo.fetch, broken)
    assert days(tmp_path) == ["2026-11-11"]
    assert dataset_path(tmp_path / p.OUT_DIR, "team_openrapm_daily", 2027).is_file()


def test_update_keeps_a_file_it_cant_parse(tmp_path, team_ids):
    repo = Repo(power(team("SAS", recent=None)))
    with pytest.raises(ValueError):
        p.update(tmp_path, TODAY, repo.fetch, repo.commits)
    assert (tmp_path / p.RAW_DIR / "unparsed" / "2026-11-12.json.gz").is_file()
    assert days(tmp_path) == []
    assert not (tmp_path / p.OUT_DIR).exists()


def test_update_keeps_no_history_for_a_past_season(tmp_path, team_ids):
    repo = Repo(power(season=2026, through="2026-06-10", built="2026-06-11"))
    p.update(tmp_path, TODAY, repo.fetch, repo.commits)
    assert repo.since == []
    assert dataset_path(tmp_path / p.OUT_DIR, "team_openrapm", 2026).is_file()
    assert not dataset_path(tmp_path / p.OUT_DIR, "team_openrapm_daily", 2026).exists()


def test_main_no_fetch_rebuilds(tmp_path, team_ids):
    save(tmp_path, power(through="2026-11-10", built="2026-11-11"))
    save(tmp_path, power())
    p.main(["--out", str(tmp_path), "--no-fetch"])
    latest = dataset_path(tmp_path / p.OUT_DIR, "team_openrapm", 2027)
    assert {r[1] for r in read(latest)} == {date(2026, 11, 11)}
    daily = dataset_path(tmp_path / p.OUT_DIR, "team_openrapm_daily", 2027)
    assert len(read(daily)) == 4


def test_team_ids_for_reads_out_first(tmp_path):
    path = dataset_path(tmp_path / p.STATS_DIR, "team_seasons", 2027)
    path.parent.mkdir(parents=True)
    duckdb.sql(
        "COPY (SELECT * FROM (VALUES (2027, '1610612759', 'SAS', true), "
        "(2027, '1', 'EST', false)) t(season, team_id, nba_abbrev, is_nba)) "
        f"TO '{path}' (FORMAT parquet)"
    )
    assert p.team_ids_for(tmp_path, 2027) == {"SAS": "1610612759"}


def test_saved_files_are_gzipped(tmp_path):
    save(tmp_path, power())
    with gzip.open(p.raw_path(tmp_path, 2027, "2026-11-11"), "rt") as f:
        assert json.load(f)["meta"]["through"] == "2026-11-11"
