import duckdb
import pytest

from pipeline.output import DROPPED_ENV, check_keys, write_parquet


def test_write_parquet(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS x FROM range(10)")
    path = tmp_path / "t.parquet"
    write_parquet(con, "t", path)
    codecs = con.execute(
        f"SELECT DISTINCT compression FROM parquet_metadata('{path}')"
    ).fetchall()
    assert codecs == [("ZSTD",)]
    keys = con.execute(
        f"SELECT decode(key) FROM parquet_kv_metadata('{path}')"
    ).fetchall()
    assert ("updated",) in keys
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_parquet_row_groups(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS x FROM range(10000)")
    path = tmp_path / "t.parquet"
    write_parquet(con, "t", path, row_group_size=4096)
    groups = con.execute(
        f"SELECT num_row_groups FROM parquet_file_metadata('{path}')"
    ).fetchone()
    assert groups == (3,)


def bad_keys():
    con = duckdb.connect()
    con.execute(
        """
        CREATE TABLE t AS FROM (VALUES
            ('g1', 'p1', 10), ('g1', 'p1', 0), ('g1', 'p2', 5),
            ('g2', NULL, 3), ('g2', 'p1', 7)) v(game_id, player_id, pts)
        """
    )
    return con


def test_check_keys_raises(monkeypatch):
    monkeypatch.delenv(DROPPED_ENV, raising=False)
    with pytest.raises(ValueError, match=r"duplicate .*NULL"):
        check_keys(bad_keys(), "t", ["game_id", "player_id"], "test t")
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 'g1' AS game_id")
    check_keys(con, "t", ["game_id"], "test t")


def test_check_keys_drops(tmp_path, monkeypatch):
    dropped = tmp_path / "dropped.txt"
    monkeypatch.setenv(DROPPED_ENV, str(dropped))
    con = bad_keys()
    check_keys(con, "t", ["game_id", "player_id"], "test t")
    # both rows of the duplicated key go, and the NULL one
    rows = con.execute("SELECT game_id, player_id FROM t ORDER BY ALL").fetchall()
    assert rows == [("g1", "p2"), ("g2", "p1")]
    lines = dropped.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("test t: duplicate (game_id, player_id)")
    assert "1 rows with a NULL" in lines[1]
