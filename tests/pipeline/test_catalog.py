from datetime import UTC, datetime

import duckdb
import pytest

from pipeline import catalog

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def write(outdir, dataset, season, query, source="stats"):
    path = outdir / "nba" / source / dataset / f"{season}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.sql(f"COPY ({query}) TO '{path}' (FORMAT parquet)")


def season_stats(season, gp, min, poss, value, def_ws_raw="NULL"):
    """a player_season_stats row with every per-mode total set to `value`"""
    totals = ", ".join(f"{value} AS {c}" for c in catalog.PER_MODE if c != "def_ws_raw")
    return f"""
        SELECT {season} AS season, 'regular_season' AS season_type, '1' AS player_id,
            {gp} AS gp, {min}::DOUBLE AS min, {poss} AS poss, 0.5 AS ts_pct,
            {totals}, 0.25 AS def_ws, {def_ws_raw}::DOUBLE AS def_ws_raw
    """


def build(outdir):
    path = outdir / catalog.LOCAL_CATALOG
    catalog.build(path, catalog.list_dir(outdir), str(outdir))
    con = duckdb.connect()
    con.execute(f"ATTACH '{path}' AS nba (READ_ONLY)")
    return con


def test_parse():
    f = catalog.parse("nba/espn/team_box/2026.parquet", T0)
    assert f == catalog.File(
        "nba/espn/team_box/2026.parquet", "espn", "team_box", 2026, T0
    )
    assert catalog.parse("nba/raw/stats/2026/team_game_logs_base.json.gz", T0) is None
    assert catalog.parse("nba/nba.duckdb", T0) is None
    assert catalog.parse("nba/stats/games/.2026.parquet.tmp", T0) is None


def test_dataset_views_union_seasons(tmp_path):
    write(tmp_path, "games", 2025, "SELECT 2025 AS season, '0022400001' AS game_id")
    write(tmp_path, "games", 2026, "SELECT 2026 AS season, '0022500001' AS game_id")
    con = build(tmp_path)
    assert con.sql("SELECT season, game_id FROM nba.games ORDER BY 1").fetchall() == [
        (2025, "0022400001"),
        (2026, "0022500001"),
    ]


def test_columns_can_differ_between_seasons(tmp_path):
    write(tmp_path, "games", 2025, "SELECT 2025 AS season")
    write(tmp_path, "games", 2026, "SELECT 2026 AS season, true AS neutral_site")
    con = build(tmp_path)
    assert con.sql(
        "SELECT season, neutral_site FROM nba.games ORDER BY 1"
    ).fetchall() == [
        (2025, None),
        (2026, True),
    ]


def test_players_have_their_latest_name(tmp_path):
    write(
        tmp_path,
        "player_seasons",
        2024,
        "SELECT 2024 AS season, '1' AS player_id, 'Old' AS name",
    )
    write(
        tmp_path,
        "player_seasons",
        2025,
        "SELECT 2025 AS season, '1' AS player_id, 'New' AS name",
    )
    # no name from the NBA doesn't replace a known one
    write(
        tmp_path,
        "player_seasons",
        2026,
        "SELECT 2026 AS season, * FROM (VALUES ('1', NULL), ('2', NULL)) t(player_id, name)",
    )
    con = build(tmp_path)
    assert con.sql("FROM nba.players ORDER BY player_id").fetchall() == [
        ("1", "New", 2024, 2026),
        ("2", None, 2026, 2026),
    ]


def test_per_mode_stats(tmp_path):
    write(tmp_path, "player_season_stats", 2026, season_stats(2026, 10, 720, 1000, 200))
    con = build(tmp_path)
    row = "SELECT pts, fg2m, min, gp, ts_pct, def_ws FROM nba.player_season_stats_{}"
    assert con.sql(row.format("per_game")).fetchone() == (20, 20, 72, 10, 0.5, 0.025)
    # the NBA's per-36 and per-100 stats keep total minutes
    assert con.sql(row.format("per_36")).fetchone() == (10, 10, 720, 10, 0.5, 0.0125)
    assert con.sql(row.format("per_100")).fetchone() == (20, 20, 720, 10, 0.5, 0.025)


def test_per_mode_def_ws_uses_unrounded_value(tmp_path):
    write(
        tmp_path,
        "player_season_stats",
        2026,
        season_stats(2026, 10, 720, 1000, 200, def_ws_raw=0.254),
    )
    con = build(tmp_path)
    row = con.sql(
        "SELECT def_ws, def_ws_raw FROM nba.player_season_stats_per_game"
    ).fetchone()
    assert row == (pytest.approx(0.0254), pytest.approx(0.0254))


def test_per_mode_with_no_minutes(tmp_path):
    write(tmp_path, "player_season_stats", 2026, season_stats(2026, 1, 0, 0, 0))
    con = build(tmp_path)
    assert con.sql("SELECT pts FROM nba.player_season_stats_per_36").fetchone() == (
        None,
    )


def test_metadata(tmp_path):
    write(tmp_path, "games", 2025, "SELECT 2025 AS season")
    write(tmp_path, "games", 2026, "SELECT 2026 AS season")
    write(tmp_path, "team_box", 2026, "SELECT 2026 AS season", source="espn")
    con = build(tmp_path)
    rows = con.sql(
        "SELECT dataset, source, first_season, last_season, seasons FROM nba.metadata ORDER BY 1"
    )
    assert rows.fetchall() == [
        ("games", "stats", 2025, 2026, 2),
        ("team_box", "espn", 2026, 2026, 1),
    ]
    updated = con.sql(
        "SELECT updated FROM nba.metadata WHERE dataset = 'games'"
    ).fetchone()
    assert updated and abs((datetime.now(UTC) - updated[0]).total_seconds()) < 60


def test_new_seasons_appear_on_rebuild(tmp_path):
    write(tmp_path, "games", 2025, "SELECT 2025 AS season")
    build(tmp_path).close()
    write(tmp_path, "games", 2026, "SELECT 2026 AS season")
    con = build(tmp_path)
    assert con.sql("SELECT count(*) FROM nba.games").fetchone() == (2,)


def test_raw_files_are_ignored(tmp_path):
    write(tmp_path, "games", 2026, "SELECT 2026 AS season")
    raw = tmp_path / "nba" / "raw" / "stats" / "2026"
    raw.mkdir(parents=True)
    (raw / "team_game_logs_base.json.gz").write_bytes(b"")
    con = build(tmp_path)
    assert con.sql("SELECT dataset FROM nba.metadata").fetchall() == [("games",)]


def test_dataset_in_two_sources_fails():
    files = [
        catalog.File("nba/stats/games/2026.parquet", "stats", "games", 2026, T0),
        catalog.File("nba/espn/games/2026.parquet", "espn", "games", 2026, T0),
    ]
    with pytest.raises(ValueError, match="games is in both"):
        catalog.by_dataset(files)


def test_no_files_fails(tmp_path):
    with pytest.raises(ValueError, match="no parquet files"):
        build(tmp_path)


class FakeS3:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


def test_list_bucket():
    s3 = FakeS3(
        [
            {
                "Contents": [
                    {"Key": "nba/raw/espn/2026/2025-10-21.json.gz", "LastModified": T0},
                    {"Key": "nba/espn/team_box/2026.parquet", "LastModified": T0},
                ]
            },
            {"Contents": [{"Key": "nba/stats/games/2011.parquet", "LastModified": T0}]},
            {},
        ]
    )
    files = catalog.list_bucket("basketball-data", s3)
    assert [f.key for f in files] == [
        "nba/espn/team_box/2026.parquet",
        "nba/stats/games/2011.parquet",
    ]
    assert s3.calls == [{"Bucket": "basketball-data", "Prefix": "nba/"}]
