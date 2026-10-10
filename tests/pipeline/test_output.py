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
