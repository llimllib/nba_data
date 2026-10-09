"""
Helpers for writing pipeline output: atomic writes, parquet files and key
checks
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import duckdb


def write_atomic(path: Path, write: Callable[[Path], object]) -> None:
    """call write(tmp_path), then move the result into place"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    write(tmp)
    os.replace(tmp, path)


def write_parquet(con: duckdb.DuckDBPyConnection, table: str, path: Path) -> None:
    """write `table` to `path`, recording the time in the file's metadata"""
    updated = datetime.now(UTC).isoformat()
    write_atomic(
        path,
        lambda tmp: con.execute(
            f"COPY {table} TO '{tmp}' (FORMAT parquet, KV_METADATA {{updated: '{updated}'}})"
        ),
    )


def check_keys(
    con: duckdb.DuckDBPyConnection, table: str, keys: list[str], label: str
) -> None:
    """raise if any row of `table` has a NULL key or shares its key with another"""
    cols = ", ".join(keys)
    dupes = con.execute(
        f"SELECT {cols}, count(*) FROM {table} GROUP BY ALL HAVING count(*) > 1 LIMIT 5"
    ).fetchall()
    if dupes:
        raise ValueError(f"{label}: duplicate ({cols}): {dupes}")
    nulls = con.execute(
        f"SELECT count(*) FROM {table} WHERE {' OR '.join(f'{k} IS NULL' for k in keys)}"
    ).fetchone()
    if nulls and nulls[0]:
        raise ValueError(f"{label}: {nulls[0]} rows with a NULL in ({cols})")
