# NBA Data Dictionary

This document describes all data files in the `data/` directory.

## Season Naming Convention

Season years represent the **end** of the season (e.g., `2025` = 2024-25 season).

Data coverage:
- Main data: 2009-10 season (2010) through current
- ESPN data: 2018-19 season (2019) through current

---

## v2 Schema Conventions

These rules apply to the v2 dataset in the `basketball-data` Space
(milestone "v2: object storage & a coherent dataset"). The v1 files in `data/`
described below do not follow them.

### Keys

Every table uses NBA ids as keys, with the same name and type everywhere:

| Column | Type | Example | Notes |
|--------|------|---------|-------|
| `game_id` | VARCHAR | `0022500130` | 10 characters, zero-padded; see [NBA Game ID Format](#nba-game-id-format) |
| `player_id` | VARCHAR | `1629027` | NBA person id |
| `team_id` | VARCHAR | `1610612737` | NBA team id; see [Teams](#teams) |
| `season` | INTEGER | `2026` | End year of the season (2026 = 2025-26) |

Ids are identifiers, not numbers, so they are all strings: no arithmetic, no
loss of leading zeros, no overflow if the NBA issues a larger id. They are
stored exactly as the NBA publishes them. Sources that return ids as numbers
are converted directly from integer to string, never through a float (which
would produce `1629027.0`). `season` stays an INTEGER because it is used in
arithmetic and ranges.

Columns that refer to another team or player end in `_team_id` or
`_player_id` and hold the same ids (`opp_team_id`, `home_team_id`,
`assist_player_id`). The integrity check finds key columns by these names.

Source-specific names for these keys (`gameId`, `gmId`, `personId`, `plyrID`,
`teamId`, `tmID`, ...) are renamed when the parquet is written.

### Teams

A `team_id` is a franchise as the NBA defines it, not a name. For example,
`1610612766` covers the 1988-2002 Charlotte Hornets, the Charlotte Bobcats, and
the current Charlotte Hornets, while `1610612740` covers the New Orleans
Hornets and the Pelicans.

- **Joining:** a team's city, nickname and abbreviation for a given row come
  from joining `team_seasons` on `(team_id, season)`. A 2013 Charlotte game
  shows "Bobcats" and a 2016 one shows "Hornets" with no special cases.
  `team_seasons` is generated from the NBA game logs, so renames, All-Star
  teams and exhibition opponents appear automatically.
- **Ingesting:** a source that identifies teams only by abbreviation is resolved
  through `src/team_abbrevs.csv`, a hand-maintained
  `(source, abbrev, first_season, last_season) -> team_id` table. An unknown
  `(source, abbrev, season)` fails the run.
- NBA abbreviations are not unique within a season (in 2026 `MEL` is both
  Melbourne United and the All-Star Team Melo), another reason they are never
  keys.
- Abbreviations and team names are never stored in data tables. They live only
  in `team_seasons` and `team_abbrevs.csv`.

### Columns

- Column names are snake_case and never start with a digit (ESPN's
  `2pt_oNetPts` becomes `fg2_o_net_pts`)
- Numeric columns are INTEGER, DOUBLE, or BIGINT only when a value needs it.
  DuckDB-wasm returns BIGINT as a JavaScript BigInt, which Observable Plot
  can't handle
- `game_date` is a DATE, the date the NBA lists for the game (US Eastern)
- Yes/no values are BOOLEAN
- Missing values are NULL, never `0` or `''`

### Types

Types in this document are DuckDB types, since DuckDB writes the parquet
files. They map to parquet like this (checked with `parquet_schema()`):

| DuckDB | Parquet physical type | Parquet annotation | pyarrow / polars |
|--------|-----------------------|--------------------|------------------|
| VARCHAR | BYTE_ARRAY | UTF8 | string / String |
| INTEGER | INT32 | INT_32 | int32 / Int32 |
| BIGINT | INT64 | INT_64 | int64 / Int64 |
| DOUBLE | DOUBLE | (none) | double / Float64 |
| BOOLEAN | BOOLEAN | (none) | bool / Boolean |
| DATE | INT32 | DATE | date32 / Date |

### Where normalization happens

All of the above is applied when parquet files are written, not in the catalog
views. Reading a parquet file directly gives the same clean data as querying
through `nba.duckdb`.

### Layout

```
nba/raw/<source>/<season>/...           raw source responses, gzipped
nba/<source>/<dataset>/<season>.parquet one file per season
nba/nba.duckdb                          catalog of views
```

Finished seasons are never rewritten; each run rewrites only the current
season. Lookup tables (`team_seasons`, `players`, `games`) are single files.

---

## Main Data Files (`data/`)

### Team Game Logs

#### `gamelog_<season>.parquet` / `gamelogs.parquet`
Team-level box scores and advanced stats for each game.

| Column | Type | Description |
|--------|------|-------------|
| season_year | VARCHAR | Season identifier |
| team_id | INTEGER | NBA team ID |
| team_abbreviation | VARCHAR | 3-letter team code (e.g., "BOS") |
| team_name | VARCHAR | Full team name |
| game_id | VARCHAR | Unique game identifier |
| game_date | VARCHAR | Date of game |
| matchup | VARCHAR | Game matchup string (e.g., "BOS vs. NYK") |
| wl | VARCHAR | Win/Loss result ("W" or "L") |
| **Basic Stats** | | |
| min | DOUBLE | Minutes played |
| fgm / fga / fg_pct | INT/INT/DOUBLE | Field goals made/attempted/percentage |
| fg3m / fg3a / fg3_pct | INT/INT/DOUBLE | 3-point field goals |
| ftm / fta / ft_pct | INT/INT/DOUBLE | Free throws |
| oreb / dreb / reb | INTEGER | Offensive/Defensive/Total rebounds |
| ast | INTEGER | Assists |
| tov | DOUBLE | Turnovers |
| stl / blk | INTEGER | Steals / Blocks |
| blka | INTEGER | Blocks against |
| pf / pfd | INTEGER | Personal fouls / Personal fouls drawn |
| pts | INTEGER | Points scored |
| plus_minus | DOUBLE | Plus/minus |
| **Advanced Stats** | | |
| off_rating / def_rating / net_rating | DOUBLE | Offensive/Defensive/Net rating |
| e_off_rating / e_def_rating / e_net_rating | DOUBLE | Estimated ratings |
| ast_pct | DOUBLE | Assist percentage |
| ast_to | DOUBLE | Assist to turnover ratio |
| ast_ratio | DOUBLE | Assist ratio |
| oreb_pct / dreb_pct / reb_pct | DOUBLE | Rebound percentages |
| tm_tov_pct | DOUBLE | Team turnover percentage |
| efg_pct | DOUBLE | Effective field goal percentage |
| ts_pct | DOUBLE | True shooting percentage |
| pace / e_pace | DOUBLE | Pace (possessions per 48 min) |
| pace_per40 | DOUBLE | Pace per 40 minutes |
| poss | INTEGER | Total possessions |
| pie | DOUBLE | Player Impact Estimate |
| **Rank Columns** | | Various `*_rank` columns for league rankings |
| available_flag | INTEGER | Data availability flag |

---

### Player Game Logs

#### `playerlog_<season>.parquet` / `player_game_logs.parquet`
Player-level box scores and advanced stats for each game.

| Column | Type | Description |
|--------|------|-------------|
| gameId | VARCHAR | Unique game identifier |
| teamId | INTEGER | NBA team ID |
| teamCity / teamName | VARCHAR | Team location and name |
| teamTricode | VARCHAR | 3-letter team code |
| teamSlug | VARCHAR | URL-friendly team name |
| personId | INTEGER | NBA player ID |
| firstName / familyName | VARCHAR | Player name |
| nameI | VARCHAR | Name initial format (e.g., "L. James") |
| playerSlug | VARCHAR | URL-friendly player name |
| position | VARCHAR | Playing position |
| comment | VARCHAR | Game status notes (e.g., injury) |
| jerseyNum | VARCHAR | Jersey number |
| minutes | VARCHAR | Minutes played (MM:SS format) |
| **Basic Stats** | | |
| fieldGoalsMade / fieldGoalsAttempted / fieldGoalsPercentage | | Field goals |
| threePointersMade / threePointersAttempted / threePointersPercentage | | 3-pointers |
| freeThrowsMade / freeThrowsAttempted / freeThrowsPercentage | | Free throws |
| reboundsOffensive / reboundsDefensive / reboundsTotal | INTEGER | Rebounds |
| assists / steals / blocks | INTEGER | Assists, steals, blocks |
| turnovers | INTEGER | Turnovers |
| foulsPersonal | INTEGER | Personal fouls |
| points | INTEGER | Points scored |
| plusMinusPoints | DOUBLE | Plus/minus |
| **Advanced Stats** | | |
| offensiveRating / defensiveRating / netRating | DOUBLE | Player ratings |
| estimatedOffensiveRating / estimatedDefensiveRating / estimatedNetRating | DOUBLE | Estimated ratings |
| assistPercentage / assistToTurnover / assistRatio | DOUBLE | Assist metrics |
| offensiveReboundPercentage / defensiveReboundPercentage / reboundPercentage | DOUBLE | Rebound % |
| turnoverRatio | DOUBLE | Turnover ratio |
| effectiveFieldGoalPercentage | DOUBLE | eFG% |
| trueShootingPercentage | DOUBLE | TS% |
| usagePercentage / estimatedUsagePercentage | DOUBLE | Usage rate |
| pace / estimatedPace / pacePer40 | DOUBLE | Pace metrics |
| possessions | DOUBLE | Possessions played |
| PIE | DOUBLE | Player Impact Estimate |

---

### Player Season Stats

#### `players_<season>.parquet` / `playerstats.parquet`
Aggregated player statistics for entire seasons.

| Column | Type | Description |
|--------|------|-------------|
| player_id | INTEGER | NBA player ID |
| player_name | VARCHAR | Full player name |
| nickname | VARCHAR | Player nickname |
| team_id | INTEGER | Team ID |
| team_abbreviation | VARCHAR | 3-letter team code |
| age | DOUBLE | Player age |
| gp | INTEGER | Games played |
| w / l | INTEGER | Wins / Losses |
| w_pct | DOUBLE | Win percentage |
| year | INTEGER | Season year |
| **Totals** | | All basic stats as season totals |
| **Per Game (`*_pergame`)** | | Stats per game |
| **Per 36 (`*_per36`)** | | Stats per 36 minutes |
| **Per 100 Possessions (`*_per100possessions`)** | | Pace-adjusted stats |
| **Advanced** | | |
| off_rating / def_rating / net_rating | DOUBLE | Player ratings |
| usg_pct | DOUBLE | Usage percentage |
| ts_pct / efg_pct | DOUBLE | Shooting efficiency |
| ast_pct / ast_to / ast_ratio | DOUBLE | Assist metrics |
| oreb_pct / dreb_pct / reb_pct | DOUBLE | Rebound percentages |
| pie | DOUBLE | Player Impact Estimate |
| def_ws | DOUBLE | Defensive win shares |
| **Biographical** | | |
| player_height / player_height_inches | VARCHAR/INT | Height |
| player_weight | VARCHAR | Weight |
| college | VARCHAR | College attended |
| country | VARCHAR | Country of origin |
| draft_year / draft_round / draft_number | VARCHAR | Draft info |
| **Shooting Splits** | | |
| fg2m / fg2a / fg2_pct | INT/INT/DOUBLE | 2-point field goals |
| fga_frequency / fg2a_frequency / fg3a_frequency | DOUBLE | Shot type frequencies |

#### `players_<season>_playoffs.parquet` / `playerstats_playoffs.parquet`
Same schema as above, but for playoff games only.

---

### Team Summary Stats

#### `team_summary_<season>.json` / `team_summary.json`
Season-level team statistics.

| Field | Type | Description |
|-------|------|-------------|
| updated | STRING | Last update timestamp |
| teams | OBJECT | Dictionary keyed by team abbreviation |
| TEAM_ID | INTEGER | NBA team ID |
| TEAM_NAME | VARCHAR | Full team name |
| GP | INTEGER | Games played |
| W / L | INTEGER | Wins / Losses |
| MIN | DOUBLE | Total minutes |
| OFF_RATING / DEF_RATING / NET_RATING | DOUBLE | Team ratings |
| AST_PCT / AST_TO / AST_RATIO | DOUBLE | Assist metrics |
| OREB_PCT / DREB_PCT / REB_PCT | DOUBLE | Rebound percentages |
| TM_TOV_PCT | DOUBLE | Team turnover percentage |
| EFG_PCT / TS_PCT | DOUBLE | Shooting efficiency |
| PACE / PACE_PER40 | DOUBLE | Pace metrics |
| POSS | INTEGER | Total possessions |

---

### Team Efficiency

#### `team_efficiency_<season>.json`
Per-game team efficiency data.

| Field | Type | Description |
|-------|------|-------------|
| game_id | VARCHAR | Unique game ID |
| team_id | INTEGER | Team ID |
| team_abbreviation | VARCHAR | 3-letter team code |
| game_date | VARCHAR | Game date |
| matchup | VARCHAR | Matchup string |
| off_rating / def_rating | DOUBLE | Offensive/Defensive rating |
| pts / opp_pts | INTEGER | Points scored / allowed |
| poss / opp_poss | INTEGER | Team / opponent possessions |

---

### Metadata

#### `metadata.json`
Data freshness information.

| Field | Type | Description |
|-------|------|-------------|
| updated | STRING | ISO timestamp of last data update |

---

## ESPN Data (`data/espn/`)

Data from [espnanalytics.com](https://espnanalytics.com/).

### `four_factors.parquet`
Four factors analysis per team per game.

| Column | Type | Description |
|--------|------|-------------|
| gameId | VARCHAR | ESPN game ID |
| team | VARCHAR | Team name |
| season | BIGINT | Season start year |
| **2pt Shooting** | | |
| 2pt_oScPoss / 2pt_oPoss / 2pt_oPtsProd / 2pt_oNetPts | | 2-point scoring possessions, total possessions, points produced, net points |
| **3pt Shooting** | | |
| 3pt_oScPoss / 3pt_oPoss / 3pt_oPtsProd / 3pt_oNetPts | | 3-point metrics |
| **Free Throws** | | |
| freethrow_oScPoss / freethrow_oPoss / freethrow_oPtsProd / freethrow_oNetPts | | Free throw metrics |
| **Rebounding** | | |
| rebound_oScPoss / rebound_oPoss / rebound_oPtsProd / rebound_oNetPts | | Rebounding impact |
| **Turnovers** | | |
| turnover_oScPoss / turnover_oPoss / turnover_oPtsProd / turnover_oNetPts | | Turnover impact |

---

### `player_box.parquet`
Player box score data with advanced metrics.

| Column | Type | Description |
|--------|------|-------------|
| season | BIGINT | Season start year |
| game_id | VARCHAR | Game ID |
| player_id | BIGINT | ESPN player ID |
| team_id | BIGINT | Team ID |
| team | VARCHAR | Team name |
| home | BIGINT | Home team flag (1/0) |
| name | VARCHAR | Player name |
| starter | BIGINT | Starter flag (1/0) |
| **Net Points Metrics** | | |
| oNetPts / dNetPts / tNetPts | DOUBLE | Offensive/Defensive/Total net points |
| oUsg / dUsg | DOUBLE | Offensive/Defensive usage |
| **Box Score Events** | | |
| assistedShooter / assister | BIGINT | Assisted shot events |
| blockplyr | BIGINT | Blocks |
| drebounder / orebounder | BIGINT | Rebounds |
| fgaplyr / fgmplyr | BIGINT | FG attempted/made |
| fg3aplyr / fg3mplyr | BIGINT | 3PT attempted/made |
| ftaplyr / ftmplyr | BIGINT | FT attempted/made |
| stlr | BIGINT | Steals |
| tov1 / livetov1 | BIGINT | Turnovers |
| pts | BIGINT | Points |
| plusMinusPoints | BIGINT | Plus/minus |
| minutes_played | VARCHAR | Minutes (MM:SS) |
| played | BIGINT | Played flag |
| **Win Probability Added** | | |
| oWPA / dWPA / tWPA | DOUBLE | Offensive/Defensive/Total WPA |

---

### `player_details.parquet`
Detailed player performance by play type.

| Column | Type | Description |
|--------|------|-------------|
| playerId | BIGINT | ESPN player ID |
| gameId | VARCHAR | Game ID |
| name | VARCHAR | Player name |
| team | VARCHAR | Team name |
| season | BIGINT | Season start year |
| **Play Type Net Points** | | Each has `_oNetPts`, `_dNetPts`, `_tNetPts` variants |
| 2pt / 3pt | | 2-point / 3-point plays |
| 2ptShooting / 3ptShooting | | Shooting breakdown |
| assist | | Assist value |
| cutting / driving / floating | | Shot types |
| dunk / layup / hook | | Close range shots |
| corner / mid / rim | | Shot locations |
| fade / bank | | Shot techniques |
| fastbreak / putback | | Transition plays |
| freethrow | | Free throw value |
| rebound / turnover | | Possession plays |
| foul / badpass / grenade | | Negative plays |
| total | | Total net points |

---

### `team_box.parquet`
Team box score data per game.

| Column | Type | Description |
|--------|------|-------------|
| season | BIGINT | Season start year |
| game_id | VARCHAR | Game ID |
| team_id | BIGINT | Team ID |
| homeTm | BIGINT | Home team flag |
| tmName | VARCHAR | Team name |
| **Shooting** | | |
| eFG | DOUBLE | Effective FG% |
| fg2p / fg3p | DOUBLE | 2PT% / 3PT% |
| ftr | DOUBLE | Free throw rate |
| **Net Points by Factor** | | |
| netPts2s / netPts3s | DOUBLE | 2PT/3PT net points |
| netPtsShooting | DOUBLE | Total shooting net points |
| netPtsTurnover | DOUBLE | Turnover net points |
| netPtsRebound | DOUBLE | Rebounding net points |
| netPtsFreethrow | DOUBLE | Free throw net points |
| **Possessions** | | |
| totPoss | DOUBLE | Total possessions |
| oPoss / dPoss / tPoss | INTEGER | Offensive/Defensive/Total player possessions |
| oTmPoss / dTmPoss / tTmPoss | INTEGER | Team possessions |
| oppPoss | DOUBLE | Opponent possessions |
| oppPts | BIGINT | Opponent points |
| win | BIGINT | Win flag |

---

### `data/espn/<season>/<date>.json`
Raw daily game data from ESPN. Contains detailed play-by-play and box score data for each game date.

---

## NBA Game ID Format

Game IDs follow this pattern: `00X YY ZZ GGGG`

| Prefix | Game Type |
|--------|-----------|
| 001 | Preseason |
| 002 | Regular Season |
| 003 | All-Star |
| 004 | Playoffs |
| 005 | Play-In Tournament |
| 006 | NBA Cup Final |

- `YY` = Season year (24 = 2024-25)
- `ZZ` + `GGGG` = Game-specific identifier
