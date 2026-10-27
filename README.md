# nba_data

Up to date NBA data dumps

## Query everything from one URL

The data is in object storage, as one dataset with consistent keys. Every
dataset is a table in a small DuckDB catalog:

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

You can [browse the dataset here](https://duckui.com/?load=https://basketball-data.billmill.org/nba/nba.duckdb&sql=SELECT%20*%20FROM%20nba.player_game_logs%20LIMIT%20100)

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
  `https://basketball-data.billmill.org/nba/stats/games/season=2026/data.parquet`
  (DuckDB reads `season` from the path as a BIGINT; see the data dictionary).
  To find which seasons each dataset has without DuckDB, read
  [`nba/metadata.json`](https://basketball-data.billmill.org/nba/metadata.json),
  the `metadata` table as JSON
- **Docs:** every table and column is in
  [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md); the decisions
  behind it are in [docs/v2.md](docs/v2.md)

### Men's college basketball

[Bart Torvik](https://barttorvik.com)'s T-Rank team ratings and four
factors, 2007-08 on, and the ratings as they were every day of the season,
2009-10 on, are in a separate catalog with every game and box score since
2004-05 from ESPN (history via [sportsdataverse](https://github.com/sportsdataverse/sportsdataverse-data)'s
hoopR data). torvik's tables are keyed by season and torvik's team names,
ESPN's by ESPN ids, and `team_seasons.torvik_team` joins them:

```sql
ATTACH 'https://basketball-data.billmill.org/ncaam/ncaam.duckdb' AS ncaam;
SELECT r.team, r.conf, r.wins, r.losses, round(r.adj_oe, 1) AS adj_oe,
    round(r.adj_de, 1) AS adj_de, round(f.efg_pct, 3) AS efg_pct
FROM ncaam.torvik_team_results r
JOIN ncaam.torvik_four_factors f USING (season, team)
WHERE r.season = 2026 ORDER BY r.barthag DESC LIMIT 10;

-- UConn's rating every day of the 2025-26 season
SELECT date, wins, losses, adj_oe, adj_de, barthag
FROM ncaam.torvik_ratings_daily
WHERE season = 2026 AND team = 'Connecticut' ORDER BY date;

-- UConn's 2025-26 games, from ESPN's box scores
SELECT b.game_date, o.display_name AS opponent, b.pts, b.opp_pts, b.fga, b.tov
FROM ncaam.team_box b
JOIN ncaam.team_seasons t USING (season, team_id)
JOIN ncaam.team_seasons o ON o.season = b.season AND o.team_id = b.opp_team_id
WHERE b.season = 2026 AND t.torvik_team = 'Connecticut' ORDER BY b.game_date;
```

## Code

`pipeline/` fetches each source and builds its parquet files, run every 4
hours (stats.nba.com, ESPN) and daily (the other sources) by the workflows in
`.github/workflows/`. The data itself isn't in this repo; until October 2026
it was committed to `data/` (v1), which was removed from the history when v2
replaced it.

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
