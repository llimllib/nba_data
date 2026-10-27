"""
Helpers for writing pipeline output: atomic writes, parquet files and key
checks
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import duckdb


def dataset_path(source_dir: Path, dataset: str, season: int) -> Path:
    """
    Where a season of a dataset goes: <source_dir>/<dataset>/season=<season>/data.parquet.
    The season is in the path so that readers using hive partitioning only
    open the files a season filter needs
    """
    return source_dir / dataset / f"season={season}" / "data.parquet"


def write_atomic(path: Path, write: Callable[[Path], object]) -> None:
    """
    call write(tmp_path), then move the result into place. If write fails,
    the temporary file is removed so it can't be uploaded
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        write(tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def write_parquet(
    con: duckdb.DuckDBPyConnection,
    table: str,
    path: Path,
    row_group_size: int | None = None,
) -> None:
    """
    write `table` to `path`, recording the time in the file's metadata.
    zstd makes files about a third smaller than DuckDB's default, snappy.
    DuckDB's row groups are 122,880 rows unless `row_group_size` says
    otherwise
    """
    updated = datetime.now(UTC).isoformat()
    row_groups = f", ROW_GROUP_SIZE {row_group_size}" if row_group_size else ""
    write_atomic(
        path,
        lambda tmp: con.execute(
            f"COPY {table} TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, "
            f"KV_METADATA {{updated: '{updated}'}}{row_groups})"
        ),
    )


# when set, a file that check_keys appends the rows it drops to, rather than
# raising. The workflows set it so that one bad row doesn't stop the update,
# and fail the run after publishing if the file isn't empty
DROPPED_ENV = "PIPELINE_DROPPED"


def check_keys(
    con: duckdb.DuckDBPyConnection, table: str, keys: list[str], label: str
) -> None:
    """
    raise if any row of `table` has a NULL key or shares its key with
    another. If DROPPED_ENV is set, delete those rows instead (every row
    with a duplicated key, since there's no telling which is right), and
    record them in its file
    """
    cols = ", ".join(keys)
    dupes = con.execute(
        f"SELECT {cols}, count(*) FROM {table} GROUP BY ALL HAVING count(*) > 1"
    ).fetchall()
    any_null = " OR ".join(f"{k} IS NULL" for k in keys)
    nulls = con.execute(f"SELECT count(*) FROM {table} WHERE {any_null}").fetchone()
    problems = []
    if dupes:
        problems.append(f"{label}: duplicate ({cols}): {dupes[:5]}")
    if nulls and nulls[0]:
        problems.append(f"{label}: {nulls[0]} rows with a NULL in ({cols})")
    if not problems:
        return
    dropped = os.environ.get(DROPPED_ENV)
    if not dropped:
        raise ValueError("; ".join(problems))
    con.execute(
        f"""
        DELETE FROM {table} WHERE {any_null} OR ({cols}) IN (
            SELECT ({cols}) FROM {table} GROUP BY ALL HAVING count(*) > 1)
        """
    )
    with open(dropped, "a") as f:
        for problem in problems:
            print(f"{problem}; dropping them")
            f.write(f"{problem}; dropped them\n")
