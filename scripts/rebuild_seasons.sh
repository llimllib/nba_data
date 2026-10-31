#!/usr/bin/env bash
# Rebuild past seasons' team_game_logs from the bucket's saved responses and
# publish them, so every season has the columns the current code writes
# (team_oreb, from game_events; NULL for seasons without play-by-play). If
# the seasons' columns differ, the catalog reads team_game_logs with
# union_by_name, which opens every season's file for every query.
#
# With --rotations, it first fetches the season's missing play-by-play
# (pipeline.rotations: two requests a game, only for games the bucket
# doesn't have) and publishes it with game_events and game_rotations. That's
# the whole play-by-play backfill of a season: a few hours, ~2,700 requests.
# If stats.nba.com keeps failing, or any games are still missing at the end,
# it stops before uploading; run it again to fetch the rest (or pass
# --allow-missing to publish without them).
#
# Each season works in its own directory, out/rebuild/<season>, not out/, so
# stale local files can't get published. The games fetched are kept there
# until the season is published, so a run that stops picks up where it left
# off. Each season's rebuilt team_game_logs is compared with the bucket's
# first: if anything but team_oreb differs, it stops.
#
# The catalog isn't rebuilt here: that purges the CDN first, which needs
# DO_CDN_API_KEY. The next update.yml run does it; to run it now:
#
#     gh workflow run update.yml
#
# usage: scripts/rebuild_seasons.sh [--rotations] [--allow-missing] [--dry-run] SEASON...
# needs the Spaces credentials and AWS_ENDPOINT_URL (mise.local.toml)

set -euo pipefail

BUCKET=basketball-data
URL=https://basketball-data.billmill.org
UPLOAD=(--acl public-read --cache-control "public, max-age=0, must-revalidate, s-maxage=3600" --no-progress)

rotations=false
allow_missing=false
dry_run=false
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --rotations) rotations=true ;;
    --allow-missing) allow_missing=true ;;
    --dry-run) dry_run=true ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
if [ $# -eq 0 ]; then
  echo "usage: $0 [--rotations] [--allow-missing] [--dry-run] SEASON..." >&2
  exit 2
fi

current=$(uv run python -c 'from pipeline.seasons import current_season, today_eastern; print(current_season(today_eastern()))')
for season in "$@"; do
  if ! [[ "$season" =~ ^[0-9]{4}$ ]] || [ "$season" -ge "$current" ]; then
    echo "$season isn't a past season; update.yml rebuilds the current one" >&2
    exit 2
  fi
done

upload() {
  if $dry_run; then
    echo "dry run: aws s3 $*"
  else
    aws s3 "$@" "${UPLOAD[@]}"
  fi
}

# rows of the rebuilt file and the bucket's that differ, in any column the
# bucket's has but team_oreb
compare() {
  uv run python - "$1" "$2" <<'EOF'
import sys

import duckdb

local, remote = sys.argv[1:]
con = duckdb.connect()


def read(path):
    return f"read_parquet('{path}', hive_partitioning = false)"


cols = [
    c
    for (c,) in con.execute(f"SELECT column_name FROM (DESCRIBE FROM {read(remote)})").fetchall()
    if c != "team_oreb"
]
sel = ", ".join(f'"{c}"' for c in cols)
diff = con.execute(
    f"""
    SELECT 'rebuilt' AS side, * FROM (SELECT {sel} FROM {read(local)} EXCEPT ALL SELECT {sel} FROM {read(remote)})
    UNION ALL
    SELECT 'bucket', * FROM (SELECT {sel} FROM {read(remote)} EXCEPT ALL SELECT {sel} FROM {read(local)})
    """
).fetchall()
if diff:
    print(f"{len(diff)} rows differ from the bucket's:")
    for row in diff[:10]:
        print(" ", row[:4])
    sys.exit(1)
filled, rows = con.execute(f"SELECT count(team_oreb), count(*) FROM {read(local)}").fetchone()
print(f"matches the bucket's; team_oreb for {filled} of {rows} rows")
EOF
}

# games in the season's games table with no saved play-by-play
missing_games() {
  uv run python - "$1" "$2" <<'EOF'
import sys
from pathlib import Path

from pipeline import rotations

out, season = Path(sys.argv[1]), int(sys.argv[2])
saved = {p.name.removesuffix(".json.gz") for p in rotations.saved_games(out, season)}
missing = [g for g, _ in rotations.season_games(out, season) if g not in saved]
print(" ".join(missing))
EOF
}

for season in "$@"; do
  echo "== $season"
  out="out/rebuild/$season"
  mkdir -p "$out"
  echo "working in $out"
  raw="nba/raw/stats/$season"
  aws s3 sync "s3://$BUCKET/$raw/" "$out/$raw/" --exclude 'playbyplay/*' --no-progress
  # builds the lookups and game logs pipeline.rotations reads
  uv run python -m pipeline.stats --out "$out" --season "$season" --no-fetch
  if $rotations; then
    aws s3 sync "s3://$BUCKET/$raw/playbyplay/" "$out/$raw/playbyplay/" --no-progress
    if ! uv run python -m pipeline.rotations --out "$out" --season "$season"; then
      echo "stopped; the games fetched are saved in $out. Run this again later to fetch the rest" >&2
      exit 1
    fi
    missing=$(missing_games "$out" "$season")
    if [ -n "$missing" ] && ! $allow_missing; then
      echo "$(wc -w <<<"$missing" | tr -d ' ') games have no play-by-play: $missing" >&2
      echo "run this again to fetch them, or pass --allow-missing to publish without them" >&2
      exit 1
    fi
    # again, for team_oreb from the game_events just built
    uv run python -m pipeline.stats --out "$out" --season "$season" --no-fetch
  fi
  uv run python -m pipeline.catalog --out "$out" --check

  key="nba/stats/team_game_logs/season=$season/data.parquet"
  compare "$out/$key" "$URL/$key"

  if $rotations; then
    # sync only uploads the games that are new
    upload sync "$out/$raw/playbyplay/" "s3://$BUCKET/$raw/playbyplay/"
    for dataset in game_events game_rotations; do
      k="nba/stats/$dataset/season=$season/data.parquet"
      upload cp "$out/$k" "s3://$BUCKET/$k"
    done
  fi
  upload cp "$out/$key" "s3://$BUCKET/$key"
  # published: everything in it is in the bucket now. A dry run keeps it,
  # so the real run doesn't fetch the games again
  if ! $dry_run; then
    rm -rf "$out"
  fi
done

echo "done; the next update.yml run rebuilds the catalog (gh workflow run update.yml to run it now)"
