# nba_data

Up to date NBA data dumps

## v2: query everything from one URL

The data is moving out of this repo and into object storage, as one dataset
with consistent keys. Every dataset is a table in a small DuckDB catalog:

```sql
ATTACH 'https://basketball-data.billmill.org/nba/nba.duckdb' AS nba;
USE nba;
FROM metadata;   -- every dataset, its seasons and when it was updated

-- scoring leaders, 2025-26 regular season
SELECT p.name, t.nba_abbrev AS team, s.gp, round(s.pts, 1) AS ppg
FROM player_season_stats_per_game s
JOIN players p USING (player_id)
JOIN team_seasons t USING (season, team_id)
WHERE s.season = 2026 AND s.season_type = 'regular_season' AND s.gp >= 50
ORDER BY s.pts DESC LIMIT 10;

-- the Celtics' latest games, with opponent names
SELECT g.game_date, o.nba_abbrev AS opp, g.home, g.win, g.pts, g.plus_minus
FROM team_game_logs g
JOIN team_seasons t USING (season, team_id)
JOIN team_seasons o ON o.season = g.season AND o.team_id = g.opp_team_id
WHERE g.season = 2026 AND t.nba_abbrev = 'BOS'
ORDER BY g.game_date DESC LIMIT 10;

-- ESPN net points leaders
SELECT p.name, count(*) AS games, round(sum(b.t_net_pts), 1) AS net_pts
FROM player_box b JOIN players p USING (player_id)
WHERE b.season = 2026
GROUP BY ALL ORDER BY net_pts DESC LIMIT 10;
```

That works in the DuckDB CLI or any DuckDB client (in Python:
`duckdb.connect().sql("ATTACH ...")`). Only the parts of the files a query
needs are downloaded.

- **What's there:** NBA game logs, player season stats and lookup tables
  (teams, players, games) from stats.nba.com for 2009-10 on, and ESPN's net
  points data for 2018-19 on, updated every 4 hours
- **Keys:** NBA ids (`player_id`, `team_id`, `game_id`, as strings) in every
  table, and `season` is the end year (2026 = 2025-26). Names come from
  `players` and `team_seasons`
- **Files:** each dataset is one parquet file per season, readable without
  the catalog, e.g.
  `https://basketball-data.billmill.org/nba/stats/games/2026.parquet`
- **Docs:** every table and column is in
  [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) (the "v2" sections);
  the plan is in [docs/v2.md](docs/v2.md)

The `data/` directory below is v1. It's still updated, but frozen in
format, and will be removed once everything has moved to v2.

## data/

In the `data` directory, all seasons represent the end of the season, so 2025 means the 2024-25 nba season. `data` has data for the 2009-10 season up to the current season

- **`gamelog_<season>.parquet`**: game logs per team for the given season
- **`gamelogs.parquet`**: game logs per team for all seasons
- **`metadata.json`**: the date of the last update
- **`playerlog_<season>.parquet`**: game logs per player
- **`player_game_logs.parquet`**: game logs per player, all seasons
- **`players_<season>.parquet`**: player data for the whole season
- **`players_<season>_playoffs.parquet`**: player data for the whole playoffs
- **`playerstats.parquet`**: player data per season for all collected seasons
- **`playerstats_playoffs.parquet`**: player data per playoff season for all collected seasons
- **`team_efficiency_<season>.json`**: team efficiency stats
- **`team_summary_<season>.json`**: team summary stats for a given season
- **`team_summary.json`**: team summary stats for all seasons

## data/espn

Data collected from [espnanalytics.com](https://espnanalytics.com/). Covers the 2018-19 season through the current season.

**note**: in this directory, the season name represents the _first_ year of the season, not the last. Apologies for the inconsistency

**note update**: as of feb 24 2026, this is no longer true. If you're using this data, you will need to update your usages. I apologize for the churn and regret the initial error I made

- **`four_factors.parquet`**: four factors data per game
- **`player_box.parquet`**: player box score data per game
- **`player_details.parquet`**: more detailed player box scores
- **`team_box.parquet`**: team box score data per game

## src

The source code for updating the data. See the makefile for how to run it

## NBA Game ID Prefixes

| Prefix | Game Type                            | Example                                                 |
| ------ | ------------------------------------ | ------------------------------------------------------- |
| 001    | Preseason                            | `0012500068` - Oct 2-17 game                            |
| 002    | Regular Season                       | `0022400123` - main season game                         |
| 003    | All-Star                             | `0032400001` - All-Star weekend game                    |
| 004    | Playoffs                             | `0042400101` - playoff game (round/series/game encoded) |
| 005    | Play-In Tournament                   | `0052400101` - play-in game                             |
| 006    | NBA Cup (In-Season Tournament) Final | `0062500001` - the NBA Cup championship game            |

The full game ID format is: `00X YYZZ GGGG` where:

- `00X` = game type prefix
- `YY` = season year (24 = 2024-25 season)
- `ZZ` + `GGGG` = game-specific identifier (varies by type)

## A brief note on plus-minus

A team's plus-minus has exactly one job: points scored minus points allowed.
It's subtraction. There's no model, no possession estimate, no secret sauce.

And yet: on October 5, 2009, the Detroit Pistons beat the Miami Heat 87-83 in
a preseason game, and the NBA's stats API will tell you, to this day, that
Detroit finished that game +4.4. The 0.4 is unaccounted for. It may still be
out there somewhere. It's not the only one, either: eight more preseason games
have whole-number team plus-minuses that are simply wrong (Boston beat the
76ers by 19 in October 2014 and was apparently +20 for it).

So, in v2, a team's `plus_minus` is computed as its margin instead of taken
from the NBA. Player plus-minus is left alone; those all came out as whole
numbers, and we have no way to second-guess them anyway.
