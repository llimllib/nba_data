import duckdb

from pipeline.output import write_parquet


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
