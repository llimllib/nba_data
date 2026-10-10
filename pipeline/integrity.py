"""
Check that every key in every table refers to something in the lookup
tables. Key columns are found by name (see "v2 Schema Conventions" in
docs/DATA_DICTIONARY.md):

    team_id, *_team_id      in team_seasons (season, team_id)
    player_id, *_player_id  in player_seasons (season, player_id)
    game_id, *_game_id      in games (season, game_id)

so a key must exist in the same season as the row that refers to it. NULLs
aren't checked: the pipelines reject NULL keys, and other id columns can be
NULL (games.home_team_id at a neutral site). The season digits in each
games.game_id must also match its season.

Called by pipeline.catalog on the catalog's views before the catalog is
written, so a bad key fails the run and leaves the previous catalog in place.
"""

import duckdb

# key column suffix -> the lookup table that defines it
LOOKUPS = {
    "team_id": "team_seasons",
    "player_id": "player_seasons",
    "game_id": "games",
}
KEY_OF = {lookup: key for key, lookup in LOOKUPS.items()}

# how many bad values to show per column
EXAMPLES = 5


class IntegrityError(ValueError):
    pass


def lookup_for(column: str) -> str | None:
    for suffix, lookup in LOOKUPS.items():
        if column == suffix or column.endswith(f"_{suffix}"):
            return lookup
    return None


def columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [
        c[0]
        for c in con.execute(f"SELECT column_name FROM (DESCRIBE {table})").fetchall()
    ]


def key_columns(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    """`table`'s key columns -> the lookup each must be found in"""
    cols = {}
    for name in columns(con, table):
        lookup = lookup_for(name)
        # a lookup's own key defines it rather than referring to it
        if lookup and not (lookup == table and name == KEY_OF[lookup]):
            cols[name] = lookup
    return cols


def problems(con: duckdb.DuckDBPyConnection, tables: list[str]) -> list[str]:
    """every integrity problem in `tables`, as messages"""
    found = []
    for table in tables:
        cols = key_columns(con, table)
        if not cols:
            continue
        missing = sorted(set(cols.values()) - set(tables))
        if missing:
            found.append(f"{table}: no {', '.join(missing)} to check its keys against")
            continue

        # scan the table once, for the distinct combinations of its keys
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE keys AS "
            f"SELECT DISTINCT season, {', '.join(cols)} FROM {table}"
        )
        for col, lookup in cols.items():
            key = KEY_OF[lookup]
            bad = con.execute(
                f"""
                SELECT DISTINCT k.season, k.{col} FROM temp.keys k
                WHERE k.{col} IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM {lookup} l
                    WHERE l.season = k.season AND l.{key} = k.{col})
                ORDER BY ALL
                """
            ).fetchall()
            if bad:
                found.append(
                    f"{table}.{col}: {len(bad)} (season, {col}) not in {lookup}, "
                    f"e.g. {bad[:EXAMPLES]}"
                )

    # game ids encode the year the season starts: 0022500001 is 2025-26
    if "games" in tables and "game_id" in columns(con, "games"):
        bad = con.execute(
            f"""
            SELECT season, game_id FROM games
            WHERE substr(game_id, 4, 2) <> lpad(((season - 1) % 100)::VARCHAR, 2, '0')
            ORDER BY ALL LIMIT {EXAMPLES}
            """
        ).fetchall()
        if bad:
            found.append(f"games: game_id doesn't match season, e.g. {bad}")
    con.execute("DROP TABLE IF EXISTS temp.keys")
    return found


def check(con: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
    """raise IntegrityError listing every problem in `tables`"""
    found = problems(con, tables)
    if found:
        raise IntegrityError(
            "integrity check failed:\n" + "\n".join(f"  {p}" for p in found)
        )
