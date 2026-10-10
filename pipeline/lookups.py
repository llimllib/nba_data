"""
Lookup tables built from a season's raw stats.nba.com responses, which carry
the names and abbreviations the other tables leave out:

    team_seasons    team_id -> abbreviation and name that season
    player_seasons  player_id -> name that season
    games           game_id -> date, teams, and what the game id encodes

Called from pipeline.stats.build_season, which has loaded the raw responses
(tgl_base, pgl_base, ps_*_base) and built team_game_logs.
"""

import duckdb

# the first three digits of a game id; see "NBA Game ID Format" in
# docs/DATA_DICTIONARY.md
GAME_TYPES = {
    "001": "preseason",
    "002": "regular_season",
    "003": "all_star",
    "004": "playoffs",
    "005": "play_in",
    "006": "cup_final",
}

KEYS = {
    "team_seasons": ["team_id"],
    "player_seasons": ["player_id"],
    "games": ["game_id"],
}


def build_team_seasons(con: duckdb.DuckDBPyConnection, season: int) -> None:
    # a team's name can't change mid-season, but if a source ever disagrees,
    # use what it was called in its latest game
    con.execute(
        f"""
        CREATE OR REPLACE TABLE team_seasons AS
        SELECT {season}::INTEGER AS season, team_id::VARCHAR AS team_id,
            arg_max(team_abbreviation, game_date) AS nba_abbrev,
            arg_max(team_name, game_date) AS full_name
        FROM (
            SELECT team_id, team_abbreviation, team_name, game_date FROM tgl_base
            UNION ALL
            SELECT team_id, team_abbreviation, team_name, game_date FROM pgl_base
        )
        GROUP BY team_id
        ORDER BY team_id
        """
    )


def build_player_seasons(con: duckdb.DuckDBPyConnection, season: int) -> None:
    # season stats rows have no date, so they sort before every game
    con.execute(
        f"""
        CREATE OR REPLACE TABLE player_seasons AS
        SELECT {season}::INTEGER AS season, player_id::VARCHAR AS player_id,
            arg_max(player_name, game_date) AS name
        FROM (
            SELECT player_id, player_name, game_date FROM pgl_base
            UNION ALL
            SELECT player_id, player_name, '' FROM ps_regular_season_base
            UNION ALL
            SELECT player_id, player_name, '' FROM ps_playoffs_base
        )
        WHERE player_id IS NOT NULL
        GROUP BY player_id
        ORDER BY player_id
        """
    )


def build_games(con: duckdb.DuckDBPyConnection, season: int) -> None:
    types = ", ".join(f"'{k}': '{v}'" for k, v in GAME_TYPES.items())
    unknown = con.execute(
        f"SELECT DISTINCT left(game_id, 3) FROM team_game_logs "
        f"WHERE left(game_id, 3) NOT IN ({', '.join(repr(k) for k in GAME_TYPES)})"
    ).fetchall()
    if unknown:
        raise ValueError(f"unknown game id prefixes: {sorted(u[0] for u in unknown)}")

    # team_game_logs.home is NULL when the NBA lists neither team as home
    con.execute(
        f"""
        CREATE OR REPLACE TABLE games AS
        SELECT {season}::INTEGER AS season, game_id,
            any_value(game_date) AS game_date,
            MAP {{{types}}}[left(game_id, 3)] AS game_type,
            CASE WHEN left(game_id, 3) IN ('004', '005')
                 THEN substr(game_id, 8, 1)::INTEGER END AS playoff_round,
            CASE WHEN left(game_id, 3) IN ('004', '005')
                 THEN substr(game_id, 9, 1)::INTEGER END AS series_number,
            CASE WHEN left(game_id, 3) IN ('004', '005')
                 THEN substr(game_id, 10, 1)::INTEGER END AS series_game,
            any_value(team_id) FILTER (WHERE home) AS home_team_id,
            any_value(team_id) FILTER (WHERE NOT home) AS away_team_id,
            count(*) FILTER (WHERE home IS NULL) > 0 AS neutral_site
        FROM team_game_logs
        GROUP BY game_id
        ORDER BY game_date, game_id
        """
    )


BUILDERS = {
    "team_seasons": build_team_seasons,
    "player_seasons": build_player_seasons,
    "games": build_games,
}
