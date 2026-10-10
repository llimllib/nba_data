import re
from datetime import UTC, datetime

import duckdb
import pytest

from pipeline import catalog, integrity
from pipeline.output import dataset_path

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def write(outdir, dataset, season, query, source="stats"):
    path = dataset_path(outdir / "nba" / source, dataset, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.sql(f"COPY ({query}) TO '{path}' (FORMAT parquet)")


def write_season_stats(outdir, gp, min, poss, value, def_ws_raw="NULL"):
    """
    a 2026 player_season_stats row with every per-mode total set to `value`,
    and its player
    """
    totals = ", ".join(f"{value} AS {c}" for c in catalog.PER_MODE if c != "def_ws_raw")
    query = f"""
        SELECT 2026 AS season, 'regular_season' AS season_type, '1' AS player_id,
            {gp} AS gp, {min}::DOUBLE AS min, {poss} AS poss, 0.5 AS ts_pct,
            {totals}, 0.25 AS def_ws, {def_ws_raw}::DOUBLE AS def_ws_raw
    """
    write(outdir, "player_season_stats", 2026, query)
    write(
        outdir,
        "player_seasons",
        2026,
        "SELECT 2026 AS season, '1' AS player_id, 'A' AS name",
    )


def build(outdir):
    path = outdir / catalog.LOCAL_CATALOG
    catalog.build(path, catalog.list_dir(outdir), str(outdir))
    con = duckdb.connect()
    con.execute(f"ATTACH '{path}' AS nba (READ_ONLY)")
    return con


def test_parse():
    f = catalog.parse("nba/espn/team_box/season=2026/data.parquet", T0)
    assert f == catalog.File(
        "nba/espn/team_box/season=2026/data.parquet", "espn", "team_box", 2026, T0
    )
    assert catalog.parse("nba/raw/stats/2026/team_game_logs_base.json.gz", T0) is None
    assert catalog.parse("nba/nba.duckdb", T0) is None
    assert catalog.parse("nba/stats/games/season=2026/.data.parquet.tmp", T0) is None


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
    write_season_stats(tmp_path, 10, 720, 1000, 200)
    con = build(tmp_path)
    row = "SELECT pts, fg2m, min, gp, ts_pct, def_ws FROM nba.player_season_stats_{}"
    assert con.sql(row.format("per_game")).fetchone() == (20, 20, 72, 10, 0.5, 0.025)
    # the NBA's per-36 and per-100 stats keep total minutes
    assert con.sql(row.format("per_36")).fetchone() == (10, 10, 720, 10, 0.5, 0.0125)
    assert con.sql(row.format("per_100")).fetchone() == (20, 20, 720, 10, 0.5, 0.025)


def test_per_mode_def_ws_uses_unrounded_value(tmp_path):
    write_season_stats(tmp_path, 10, 720, 1000, 200, def_ws_raw=0.254)
    con = build(tmp_path)
    row = con.sql(
        "SELECT def_ws, def_ws_raw FROM nba.player_season_stats_per_game"
    ).fetchone()
    assert row == (pytest.approx(0.0254), pytest.approx(0.0254))


def test_per_mode_with_no_minutes(tmp_path):
    write_season_stats(tmp_path, 1, 0, 0, 0)
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
        catalog.File(
            "nba/stats/games/season=2026/data.parquet", "stats", "games", 2026, T0
        ),
        catalog.File(
            "nba/espn/games/season=2026/data.parquet", "espn", "games", 2026, T0
        ),
    ]
    with pytest.raises(ValueError, match="games is in both"):
        catalog.by_dataset(files)


def test_no_files_fails(tmp_path):
    with pytest.raises(ValueError, match="no parquet files"):
        build(tmp_path)


def source_run(tmp_path, player_id):
    """
    a run of the other sources: `out` has only a source's file, and the
    bucket (`bucket`, read as a URL prefix) has the lookups
    """
    out, bucket = tmp_path / "out", tmp_path / "bucket"
    write(
        out,
        "epm",
        2026,
        f"SELECT 2026 AS season, '{player_id}' AS player_id, '10' AS team_id",
        source="dunksandthrees",
    )
    write(bucket, "player_seasons", 2026, "SELECT 2026 AS season, '1' AS player_id")
    write(bucket, "team_seasons", 2026, "SELECT 2026 AS season, '10' AS team_id")
    # a season the run doesn't touch, whose lookups aren't read
    write(bucket, "player_seasons", 2025, "SELECT 'bad' AS season")
    return out, bucket


def test_check_reads_missing_lookups_from_the_bucket(tmp_path):
    out, bucket = source_run(tmp_path, "1")
    with pytest.raises(integrity.IntegrityError, match="no player_seasons"):
        catalog.check(catalog.list_dir(out), str(out))
    catalog.check(catalog.list_dir(out), str(out), str(bucket))


def test_check_with_remote_lookups_finds_bad_keys(tmp_path):
    out, bucket = source_run(tmp_path, "2")
    with pytest.raises(integrity.IntegrityError, match=r"epm.player_id"):
        catalog.check(catalog.list_dir(out), str(out), str(bucket))


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
                    {
                        "Key": "nba/espn/team_box/season=2026/data.parquet",
                        "LastModified": T0,
                    },
                ]
            },
            {
                "Contents": [
                    {
                        "Key": "nba/stats/games/season=2011/data.parquet",
                        "LastModified": T0,
                    }
                ]
            },
            {},
        ]
    )
    files = catalog.list_bucket("basketball-data", s3)
    assert [f.key for f in files] == [
        "nba/espn/team_box/season=2026/data.parquet",
        "nba/stats/games/season=2011/data.parquet",
    ]
    assert s3.calls == [{"Bucket": "basketball-data", "Prefix": "nba/"}]


def test_season_filters_skip_other_seasons_files(tmp_path):
    for season in (2024, 2025, 2026):
        write(
            tmp_path,
            "games",
            season,
            f"SELECT {season} AS season, '002{season - 2001}00001' AS game_id",
        )
    con = build(tmp_path)
    # the first file is opened for the schema; with 2025's file broken, a 2026
    # query only works if 2025's is never opened
    dataset_path(tmp_path / "nba" / "stats", "games", 2025).write_bytes(b"junk")
    assert con.sql("SELECT game_id FROM nba.games WHERE season = 2026").fetchall() == [
        ("0022500001",)
    ]
    with pytest.raises(duckdb.Error):
        con.sql("SELECT count(*) FROM nba.games").fetchall()


def test_season_is_integer(tmp_path):
    write(tmp_path, "games", 2026, "SELECT 2026::INTEGER AS season")
    con = build(tmp_path)
    assert con.sql("SELECT typeof(season) FROM nba.games").fetchone() == ("INTEGER",)


def test_players_is_a_table(tmp_path):
    write(
        tmp_path,
        "player_seasons",
        2026,
        "SELECT 2026 AS season, '1' AS player_id, 'A' AS name",
    )
    con = build(tmp_path)
    # it's stored in the catalog, so it reads no files
    dataset_path(tmp_path / "nba" / "stats", "player_seasons", 2026).unlink()
    assert con.sql("FROM nba.players").fetchall() == [("1", "A", 2026, 2026)]


def test_views_dont_reference_other_views(tmp_path):
    write_season_stats(tmp_path, 10, 720, 1000, 200)
    con = build(tmp_path)
    names = [
        r[0]
        for r in con.sql(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = 'nba'"
        ).fetchall()
    ]
    for view, sql in con.sql(
        "SELECT view_name, sql FROM duckdb_views() WHERE database_name = 'nba'"
    ).fetchall():
        for name in names:
            assert not re.search(rf"\b(FROM|JOIN)\s+\"?{name}\b", sql, re.IGNORECASE), (
                view,
                name,
            )
    # so a client that doesn't USE the catalog can read every view
    for view in names:
        con.sql(f"SELECT count(*) FROM nba.{view}").fetchall()


def test_storage_version_is_pinned(tmp_path):
    write(tmp_path, "games", 2026, "SELECT 2026 AS season")
    con = build(tmp_path)
    tags = con.sql(
        "SELECT tags FROM duckdb_databases() WHERE database_name = 'nba'"
    ).fetchone()
    assert tags and tags[0]["storage_version"].startswith(catalog.STORAGE_VERSION)
