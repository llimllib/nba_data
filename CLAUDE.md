# CLAUDE.md - Project Guidelines for AI Assistants

## Project Overview
This repository contains up-to-date NBA data dumps in parquet and JSON formats, covering seasons from 2009-10 to present.

## v2 Transition
This repo is being migrated to v2: data in object storage, consistent keys, a DuckDB catalog. **Read [docs/v2.md](docs/v2.md) before working on v2** (code in `pipeline/`); it records the decisions and how v1 and v2 coexist. Don't change v1 code in `src/` except to fix breakage.

v2 commands (tools come from `mise.toml`):
- `mise run test` / `mise run lint` (ruff format, ruff check, ty; v2 code only). CI runs both; keep them passing
- `uv run python -m pipeline.stats --out out --season 2026` fetches from stats.nba.com (works directly from the home network; CI needs the Tailscale exit node)
- `uv run python -m pipeline.espn --out out --season 2026`; add `--no-fetch` to either to rebuild parquet from raw files only
- `uv run python -m pipeline.catalog --out out` builds `out/nba.local.duckdb` over the local files; `--bucket basketball-data` lists the bucket instead and writes `out/nba/nba.duckdb` (the parquet must already be uploaded); `--check` only runs the integrity check on `out/`
- `out/` (gitignored) is laid out like the `basketball-data` bucket
- `uv run python -m pipeline.bbref --out out --season 2026` builds `bbref_team_stats` (`--no-fetch` to rebuild from saved pages)
- `uv run python -m pipeline.player_ids --out out [--fetch]` maps new basketball-reference players to NBA ids in `pipeline/player_ids.csv`, from the bbref season pages saved in `out/nba/raw/bbref/`. basketball-reference bans clients that go over ~20 requests a minute: always fetch through `pipeline.bbref.Fetcher`, never in a loop of your own
- The live v2 data: `ATTACH 'https://basketball-data.billmill.org/nba/nba.duckdb' AS nba` (examples in the README). Prefer it to `data/` for analysis; it has every season, consistent keys and no stale stats
- The bucket is written by `.github/workflows/update.yml` every 4 hours (NBA, ESPN) and `sources.yml` daily (bbref; add new scraped sources there, as their own `continue-on-error` step listed in `SOURCES`); uploading by hand needs Spaces credentials (`AWS_ENDPOINT_URL=https://sfo3.digitaloceanspaces.com`)

Track v2 work in the GitHub milestone "v2: object storage & a coherent dataset"; record decisions in docs/v2.md and comment on the issue when the plan changes.

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
WHERE season = 2025
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
- Year = end of season (2025 = 2024-25 season)

## Source Code
The `src/` directory contains the data update scripts. See the Makefile for usage.
