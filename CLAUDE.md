# CLAUDE.md - Project Guidelines for AI Assistants

## Project Overview
This repository contains up-to-date NBA data dumps in parquet and JSON formats, covering seasons from 2009-10 to present.

## Data Documentation
See **[docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)** for complete documentation of all data files, schemas, and column definitions.

**If you notice any changes to the data structure, new files, or schema modifications, please update the data dictionary.**

## Data Analysis Guidelines

### Use DuckDB, Not Pandas
When analyzing data in this repository, **use DuckDB instead of pandas**. DuckDB:
- Reads parquet files directly and efficiently
- Uses familiar SQL syntax
- Handles large datasets without loading everything into memory
- Can query JSON files directly

### DuckDB Quick Reference

**Basic Setup (Python)**
```python
import duckdb

# Create connection (in-memory)
con = duckdb.connect()

# Or connect to a persistent database
con = duckdb.connect('analysis.db')
```

**Reading Parquet Files**
```python
# Query parquet directly
con.execute("SELECT * FROM 'data/players_2025.parquet' LIMIT 10").fetchdf()

# Use glob patterns for multiple files
con.execute("SELECT * FROM 'data/gamelog_*.parquet'").fetchdf()

# All player game logs
con.execute("SELECT * FROM 'data/player_game_logs.parquet'").fetchdf()
```

**Reading JSON Files**
```python
# Query JSON directly
con.execute("SELECT * FROM 'data/team_efficiency_2025.json'").fetchdf()
```

### Sample Queries

**Top scorers this season**
```sql
SELECT player_name, team_abbreviation, pts_pergame, gp
FROM 'data/players_2025.parquet'
WHERE gp >= 20
ORDER BY pts_pergame DESC
LIMIT 10;
```

**Team offensive ratings**
```sql
SELECT team_abbreviation, 
       AVG(off_rating) as avg_off_rating,
       COUNT(*) as games
FROM 'data/gamelog_2025.parquet'
GROUP BY team_abbreviation
ORDER BY avg_off_rating DESC;
```

**Player game log analysis**
```sql
SELECT firstName || ' ' || familyName as player,
       AVG(points) as ppg,
       AVG(reboundsTotal) as rpg,
       AVG(assists) as apg
FROM 'data/player_game_logs.parquet'
WHERE teamTricode = 'BOS'
GROUP BY player
ORDER BY ppg DESC;
```

**Join team and player data**
```sql
SELECT p.player_name, p.pts_pergame, g.team_name
FROM 'data/players_2025.parquet' p
JOIN 'data/gamelog_2025.parquet' g 
  ON p.team_id = g.team_id
WHERE p.gp >= 40
GROUP BY p.player_name, p.pts_pergame, g.team_name
ORDER BY p.pts_pergame DESC
LIMIT 10;
```

**ESPN four factors analysis**
```sql
SELECT team, 
       AVG("2pt_oNetPts") as avg_2pt_net,
       AVG("3pt_oNetPts") as avg_3pt_net,
       AVG(turnover_oNetPts) as avg_tov_net
FROM 'data/espn/four_factors.parquet'
WHERE season = 2024
GROUP BY team
ORDER BY avg_3pt_net DESC;
```

**Cross-season player comparison**
```sql
SELECT year, player_name, pts_pergame, ts_pct
FROM 'data/playerstats.parquet'
WHERE player_name = 'LeBron James'
ORDER BY year;
```

### Command Line Usage
```bash
# Quick query from terminal
duckdb -c "SELECT player_name, pts_pergame FROM 'data/players_2025.parquet' ORDER BY pts_pergame DESC LIMIT 5"
```

## Season Naming Convention
- **Main data (`data/`)**: Year = end of season (2025 = 2024-25 season)
- **ESPN data (`data/espn/`)**: Year = start of season (2024 = 2024-25 season)

## Source Code
The `src/` directory contains the data update scripts. See the Makefile for usage.
