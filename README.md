# nba_data

Up to date NBA data dumps

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
