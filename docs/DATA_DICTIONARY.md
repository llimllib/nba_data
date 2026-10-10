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

- **Joining:** a team's name and abbreviation for a given row come
  from joining `team_seasons` on `(team_id, season)`. A 2013 Charlotte game
  shows "Bobcats" and a 2016 one shows "Hornets" with no special cases.
  `team_seasons` is generated from the NBA game logs, so renames, All-Star
  teams and exhibition opponents appear automatically.
- **Ingesting:** a source that identifies teams only by abbreviation is resolved
  through `pipeline/team_abbrevs.csv`, a hand-maintained
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

Files are compressed with zstd. DuckDB (including DuckDB-wasm), pyarrow,
polars and pandas read it; some lightweight JavaScript readers need an add-on
(hyparquet needs `hyparquet-compressors`).

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
season. That includes the lookup tables (`team_seasons`, `player_seasons`,
`games`), which are per season like everything else; the catalog combines
them.

---

## v2 Catalog (`nba/nba.duckdb`)

A small DuckDB database of views over every season's parquet files, rebuilt
every run by `pipeline/catalog.py`, so new seasons appear automatically:

```sql
ATTACH 'https://basketball-data.sfo3.cdn.digitaloceanspaces.com/nba/nba.duckdb' AS nba;
SELECT p.name, round(s.pts, 1) AS ppg
FROM nba.player_season_stats_per_game s JOIN nba.players p USING (player_id)
WHERE s.season = 2026 AND s.season_type = 'regular_season' AND s.gp >= 50
ORDER BY ppg DESC LIMIT 10;
```

| Name | Type | Description |
|------|------|-------------|
| `team_game_logs`, `player_game_logs`, `player_season_stats`, `team_seasons`, `player_seasons`, `games` | view | Every season of each `nba/stats/` dataset |
| `four_factors`, `player_box`, `team_box`, `player_details` | view | Every season of each `nba/espn/` dataset |
| `players` | view | One row per `player_id`: `name` (the latest one known), `first_season`, `last_season` |
| `player_season_stats_per_game`, `player_season_stats_per_36`, `player_season_stats_per_100` | view | `player_season_stats` with counting stats scaled; see below |
| `metadata` | table | Per dataset: `source`, `first_season`, `last_season`, `seasons`, and `updated`, when its newest file was uploaded |

The views read files with `union_by_name`, so a column added in a later
season is NULL in earlier ones.

**Integrity:** the catalog is only published if every key matches the lookup
tables. Every non-NULL `team_id`/`*_team_id` is in `team_seasons`, every
`player_id`/`*_player_id` in `player_seasons`, and every `game_id` in `games`,
all for the row's season. Each `games.game_id`'s season digits also match its
`season`. So joining any table to a lookup on `(season, <key>)` never drops
rows.

---

## v2 NBA Stats Data (`nba/stats/`)

Built by `pipeline/stats.py` from stats.nba.com. Each run refetches the
whole current season (14 requests), so stat corrections the NBA makes after
games are picked up. Raw responses are kept at
`nba/raw/stats/<season>/<request>.json.gz`.

Columns are the NBA's names, lowercased. Names and abbreviations are dropped
(use the lookup tables), as are the `*_rank` columns (use `rank() OVER
(...)`). Counting stats are INTEGER and other numbers DOUBLE, set by column
name: the NBA sometimes sends counts as floats.

**Known gaps:** the game log endpoints omit some All-Star weekend exhibitions
(4 games in 2025-26, such as the Rising Stars games), and player game logs
only include players who played; DNPs aren't listed. Players with no NBA id
(seen on international teams in preseason exhibitions) are skipped.

### `team_game_logs/<season>.parquet`

One row per team per game, for every game type (preseason through the
finals). The NBA's traditional and advanced team box scores, joined.

| Column | Type | Description |
|--------|------|-------------|
| opp_team_id | VARCHAR | The other team in the game |
| game_date | DATE | |
| home | BOOLEAN | NULL for neutral-site games, where the NBA lists both teams as away |
| win | BOOLEAN | |
| min, fgm, fga, ..., pts, plus_minus | | Traditional box score |
| off_rating, def_rating, net_rating, pace, poss, pie, ... | | Advanced box score; `e_` columns are the NBA's estimates |

### `player_game_logs/<season>.parquet`

One row per player per game they played in, with the same traditional and
advanced columns as the team logs plus usage (`usg_pct`), fantasy points and
`dd2`/`td3`. `opp_team_id` and `home` come from the team's game log.

### `player_season_stats/<season>.parquet`

One row per player per `season_type` (`regular_season` or `playoffs`):
season **totals** from the NBA's Base, Defense and Advanced player stats,
2-point shooting, and bio data. A player traded mid-season has one row;
`team_id` is their last team.

Per-mode stats are computed from the totals, which reproduces the NBA's
PerGame, Per36 and Per100Possessions values within their rounding. The
catalog's `player_season_stats_per_game`, `_per_36` and `_per_100` views do
this: they have the same columns as `player_season_stats`, with the counting
stats (`pts`, `fgm`, `reb`, `def_ws`, ...) scaled. Rates, ratings, `gp`, `w`,
`l`, `dd2`, `td3` and `poss` aren't scaled, and `min` is only scaled per game
(the NBA's per-36 and per-100 modes keep total minutes). A zero denominator
gives NULL. Formulas:

| Mode | Formula |
|------|---------|
| Per game | `total / gp` |
| Per 36 minutes | `total / min * 36` |
| Per 100 possessions | `total / poss * 100` |

For defensive win shares use `def_ws_raw`; `def_ws` is rounded to 2
decimals, which distorts per-mode values for low-minute players. The per-mode
views compute `def_ws` from `def_ws_raw` when it's present.

| Column | Type | Source |
|--------|------|--------|
| gp, w, l, min, pts, ... | | Base |
| def_rating, def_ws, def_ws_raw, opp_pts_paint, ... | | Defense |
| off_rating, usg_pct, ts_pct, poss, pie, ... | | Advanced |
| fg2m, fg2a, fg2_pct, fga_frequency, ... | | 2-point shots; NULL for players with no 2-point attempts |
| player_last_team_id | VARCHAR | 2-point shots |
| player_height, player_height_inches, player_weight | VARCHAR, INTEGER, INTEGER | Bio |
| college, country | VARCHAR | Bio |
| draft_year, draft_round, draft_number | INTEGER | Bio; NULL if undrafted |

---

### Lookup tables

Built from the same responses, one file per season under `nba/stats/`.

**`team_seasons/<season>.parquet`**: one row per team that played that
season, including All-Star teams and international preseason opponents.

| Column | Type | Description |
|--------|------|-------------|
| season, team_id | | Key |
| nba_abbrev | VARCHAR | The NBA's abbreviation that season (NJN, BKN, ...). Not unique within a season |
| full_name | VARCHAR | e.g. "Charlotte Bobcats", "LA Clippers" |

**`player_seasons/<season>.parquet`**: one row per player who played or has
season stats, with `name` as of their latest game that season. Players on
international preseason opponents have a NULL `name`; the NBA doesn't send
one.

**`games/<season>.parquet`**: one row per game in `team_game_logs`.

| Column | Type | Description |
|--------|------|-------------|
| season, game_id | | Key |
| game_date | DATE | |
| game_type | VARCHAR | `preseason`, `regular_season`, `all_star`, `playoffs`, `play_in`, `cup_final` |
| playoff_round, series_number, series_game | INTEGER | Decoded from the game id for playoffs and play-in; NULL otherwise. `series_number` starts at 0 |
| home_team_id, away_team_id | VARCHAR | NULL for neutral-site games |
| neutral_site | BOOLEAN | The NBA lists neither team as home: the Cup final and some games played abroad |

---

## v2 ESPN Data (`nba/espn/`)

Built by `pipeline/espn.py` from ESPN's net points data
([espnanalytics.com](https://espnanalytics.com/)), seasons 2019 on. Every
table has `season`, `game_id` and `team_id`; player tables also have
`player_id`. Player and team names come from the lookup tables.

ESPN added fields over time, and its files for seasons 2022-2025 were saved
before that, so `o_poss`, `o_team_poss`, `o_wpa` and their `d_`/`t_`
variants, and `pts_allowed_off_live_tov`, are NULL for those seasons.

### Raw: `nba/raw/espn/<season>/<yyyy-mm-dd>.json.gz`

ESPN's file for each game day, with its separate player details file added
under `player_details`. A saved day is never refetched except for today and
yesterday, because ESPN's copies of past days can change and lose data.

### `four_factors/<season>.parquet`

One row per team, game and action type. Action types: `2pt`, `3pt`,
`freethrow`, `rebound`, `turnover`, `period`, `stoppage`, `timeout`,
`jumpball`, `violation`.

| Column | Type | ESPN field | Description |
|--------|------|------------|-------------|
| action_type | VARCHAR | actionType | |
| o_scoring_poss | DOUBLE | oScPoss | Scoring possessions |
| o_poss | DOUBLE | oPoss | Possessions |
| o_pts_produced | DOUBLE | oPtsProd | Points produced |
| o_net_pts | DOUBLE | oNetPts | Net points |

To get one column per action type, as in v1:
`PIVOT four_factors ON action_type USING first(o_net_pts) GROUP BY game_id, team_id`

### `player_details/<season>.parquet`

Net points per player, game and action type (`2pt`, `3ptShooting`, `assist`,
`layup`, `rim`, `total`, ... 31 in all).

| Column | Type | ESPN field |
|--------|------|------------|
| action_type | VARCHAR | actionType |
| o_net_pts / d_net_pts / t_net_pts | DOUBLE | oNetPts / dNetPts / tNetPts |

### `player_box/<season>.parquet` and `team_box/<season>.parquet`

One row per player per game, and one per team per game.

| Column | Type | ESPN field | Description |
|--------|------|------------|-------------|
| home | BOOLEAN | hmTm / homeTm | |
| seconds_played | INTEGER | seconds_played, else minutes_played | ESPN's `mm:ss` truncates; `seconds_played` is exact when present |
| fgm / fga | INTEGER | fgmplyr / fgaplyr | |
| fg3m / fg3a | INTEGER | fg3mplyr / fg3aplyr | |
| ftm / fta | INTEGER | ftmplyr / ftaplyr | |
| layup_fgm / layup_fga | INTEGER | lumplyr / luaplyr | Layups made / attempted |
| oreb / dreb / reb | INTEGER | orebounder / drebounder / rebounder | |
| ast | INTEGER | assister | |
| ast_fg3 / ast_layup | INTEGER | assister3pt / assisterLU | Assists on 3s / layups |
| assisted_fgm / assisted_fg3m / assisted_layup_fgm | INTEGER | assistedShooter / assisted3ptShooter / assistedLUShooter | Made shots that were assisted |
| stl / blk | INTEGER | stlr / blockplyr | |
| tov / live_tov | INTEGER | tov1 / livetov1 | Turnovers / live-ball turnovers |
| off_fouls / def_fouls | INTEGER | ofoulplyr / dfoulplyr | |
| pts | INTEGER | pts | |

`player_box` only:

| Column | Type | ESPN field | Description |
|--------|------|------------|-------------|
| starter / played | BOOLEAN | starter / played | |
| plus_minus | INTEGER | plusMinusPoints | |
| o_net_pts / d_net_pts / t_net_pts | DOUBLE | oNetPts / dNetPts / tNetPts | Net points |
| o_usg / d_usg | DOUBLE | oUsg / dUsg | Usage |
| o_poss / d_poss / t_poss | DOUBLE | oPoss / dPoss / tPoss | Player possessions |
| o_team_poss / d_team_poss / t_team_poss | DOUBLE | oTmPoss / dTmPoss / tTmPoss | Team possessions while on court |
| o_wpa / d_wpa / t_wpa | DOUBLE | oWPA / dWPA / tWPA | Win probability added |
| d_avg_pos | DOUBLE | dAvgPos | |

`team_box` only:

| Column | Type | ESPN field | Description |
|--------|------|------------|-------------|
| win | BOOLEAN | win | |
| poss / opp_poss | DOUBLE | totPoss / oppPoss | |
| opp_pts | INTEGER | oppPts | |
| efg_pct / fg2_pct / fg3_pct | DOUBLE | eFG / fg2p / fg3p | |
| ft_rate | DOUBLE | ftr | |
| fg2_net_pts / fg3_net_pts | DOUBLE | netPts2s / netPts3s | |
| shooting_net_pts / turnover_net_pts / rebound_net_pts / freethrow_net_pts | DOUBLE | netPtsShooting / ... | Net points by factor |
| pts_allowed_off_live_tov | INTEGER | ptsAllwdOffLive | |
| n_times_pts_allowed_off_live_tov | INTEGER | nTimesPtsAllwd | |

Not kept from ESPN's player rows: names and team abbreviations (use the
lookup tables), draft fields, and `seasonType` (decoded from `game_id` in
`games`). They remain in the raw files.

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

For playoff and play-in games, the last three digits encode the bracket:

```
004 YY 00 R S G    playoffs:  R = round (1-4), S = series within the round,
                              numbered from 0, G = game within the series (1-7)
005 YY 00 R S 1    play-in:   R = round (1-2), S = game within the round,
                              numbered from 0
```

So `0042500407` is game 7 of the 2025-26 Finals, and `0052500211` is the
second game of the 2025-26 play-in's second round. Verified against the
2025-26 postseason: round 1 has series 0-7, round 2 has 0-3, round 3 has
0-1, and the Finals 0.

In v2, these fields are decoded once into the `games` table (`game_type`,
`playoff_round`, `series_number`, `series_game`); other tables join `games` on
`game_id` rather than parsing the id.
