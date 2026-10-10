import gzip
import json

import duckdb
import pytest

from pipeline import espn_net_pts as n
from pipeline.output import dataset_path

SEASON = 2026
TEAM_IDS = {"BOS": "1610612738", "NOR": "1610612740"}


def total(espn_id, name, season_type="Regular Season", tm="BOS", season=SEASON):
    return {
        "dot_com_id": espn_id, "full_nm": name, "tm": tm,
        "team_name": "x", "position": "G", "min_season": season - 1,
        "max_season": season - 1, "seasonType": season_type, "offense": 10.5,
        "defense": -2.5, "overall": 8.0, "net_pts_games": 12, "min_net_pts_games": 0,
    }  # fmt: skip


def per_100(espn_id, season_type="Regular Season", season=SEASON):
    return {
        "dot_com_id": espn_id, "min_season": season - 1, "max_season": season - 1,
        "seasonType": season_type, "oTmPoss": 500.0, "dTmPoss": 490.0,
        "tTmPoss": 990.0, "totMin": 300, "oNet100": 2.1, "dNet100": -0.5,
        "tNet100": 1.6,
    }  # fmt: skip


KNOWN = [("1", "Jayson Tatum"), ("2", "Nikola Jokić"), ("3", "Twin"), ("4", "Twin")]


def rows(totals, per100=None, manual=None, known=KNOWN):
    if per100 is None:
        per100 = [per_100(t["dot_com_id"], t["seasonType"]) for t in totals]
    return n.season_rows(totals, per100, SEASON, known, manual or {}, TEAM_IDS)


def test_season_rows():
    [row] = rows([total(100, "Jayson Tatum")])
    assert row == {
        "season": SEASON,
        "season_type": "regular_season",
        "player_id": "1",
        "team_id": "1610612738",
        "gp": 12,
        "min": 300,
        "o_net_pts": 10.5,
        "d_net_pts": -2.5,
        "t_net_pts": 8.0,
        "o_team_poss": 500.0,
        "d_team_poss": 490.0,
        "t_team_poss": 990.0,
    }


def test_names_match_without_accents():
    [row] = rows([total(200, "Nikola Jokic")])
    assert row["player_id"] == "2"


def test_season_types():
    result = rows(
        [
            total(100, "Jayson Tatum", t)
            for t in ("Playoffs", "PlayIn", "IST Championship")
        ]
    )
    assert [r["season_type"] for r in result] == ["playoffs", "play_in", "cup_final"]


def test_other_seasons_are_left_out():
    assert rows([total(100, "Jayson Tatum", season=2025)]) == []


def test_manual_ids_win():
    [row] = rows([total(300, "Twin")], manual={"300": "4"})
    assert row["player_id"] == "4"


def test_ambiguous_and_unknown_players_are_dropped(capsys):
    assert rows([total(300, "Twin"), total(400, "Nobody")]) == []
    assert (
        "dropped players with no NBA id: ['Nobody', 'Twin']" in capsys.readouterr().out
    )


def test_too_many_unmatched_fails():
    unknown = [total(i, f"Unknown {i}") for i in range(n.MAX_UNMATCHED + 1)]
    with pytest.raises(ValueError, match="11 players with no NBA id"):
        rows(unknown)


def test_missing_possessions_are_null():
    [row] = rows([total(100, "Jayson Tatum")], per100=[])
    assert row["t_team_poss"] is None and row["t_net_pts"] == 8.0


def test_unknown_season_type_fails():
    with pytest.raises(ValueError, match="unknown seasonType"):
        rows([total(100, "Jayson Tatum", "Summer League")])


def test_unknown_team_fails():
    with pytest.raises(ValueError, match="no team_id for espn 'XXX'"):
        rows([total(100, "Jayson Tatum", tm="XXX")])


def save(outdir, totals, per100):
    for name, data in (("totals", totals), ("per_100", per100)):
        path = n.raw_path(outdir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as f:
            json.dump(data, f)


def test_missing_field_fails(tmp_path):
    bad = total(100, "Jayson Tatum")
    del bad["overall"]
    save(tmp_path, [bad], [per_100(100)])
    with pytest.raises(ValueError, match=r"no \['overall'\] field"):
        n.build_season(tmp_path, SEASON)


def test_build_season(tmp_path):
    save(tmp_path, [total(100, "Jayson Tatum")], [per_100(100)])
    ps = dataset_path(tmp_path / n.STATS_DIR, "player_seasons", SEASON)
    ps.parent.mkdir(parents=True)
    duckdb.sql(
        f"COPY (SELECT {SEASON} AS season, '1' AS player_id, 'Jayson Tatum' AS name) "
        f"TO '{ps}' (FORMAT parquet)"
    )
    path = n.build_season(tmp_path, SEASON)
    assert path == dataset_path(tmp_path / n.OUT_DIR, n.DATASET, SEASON)
    rel = duckdb.sql(f"SELECT * FROM read_parquet('{path}', hive_partitioning = false)")
    types = {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
    assert types["season"] == "INTEGER" and types["gp"] == "INTEGER"
    assert types["player_id"] == "VARCHAR" and types["t_net_pts"] == "DOUBLE"
    assert rel.select("player_id, t_net_pts").fetchall() == [("1", 8.0)]

    # nothing for a season the files don't have yet
    assert n.build_season(tmp_path, SEASON + 1) is None
