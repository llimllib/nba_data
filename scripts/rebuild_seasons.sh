#!/usr/bin/env bash
# Rebuild past seasons' team_game_logs from the bucket's saved responses and
# publish them, so every season has the columns the current code writes
# (team_oreb, from game_events; NULL for seasons without play-by-play). If
# the seasons' columns differ, the catalog reads team_game_logs with
# union_by_name, which opens every season's file for every query.
#
# With --rotations, it first fetches the season's missing play-by-play
# (pipeline.rotations: two requests a game, only for games the bucket
# doesn't have) and publishes it with game_events and game_rotations.
#
# It works in a new scratch directory, not out/, so stale local files can't
# get published. Each season's rebuilt team_game_logs is compared with the
# bucket's first: if anything but team_oreb differs, it stops.
#
# The catalog isn't rebuilt here: that purges the CDN first, which needs
# DO_CDN_API_KEY. The next update.yml run does it; to run it now:
#
#     gh workflow run update.yml
#
# usage: scripts/rebuild_seasons.sh [--rotations] [--dry-run] SEASON...
# needs the Spaces credentials and AWS_ENDPOINT_URL (mise.local.toml)

set -euo pipefail

BUCKET=basketball-data
URL=https://basketball-data.billmill.org
UPLOAD=(--acl public-read --cache-control "public, max-age=0, must-revalidate, s-maxage=3600" --no-progress)

rotations=false
dry_run=false
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --rotations) rotations=true ;;
    --dry-run) dry_run=true ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
if [ $# -eq 0 ]; then
  echo "usage: $0 [--rotations] [--dry-run] SEASON..." >&2
  exit 2
fi

current=$(uv run python -c 'from pipeline.seasons import current_season, today_eastern; print(current_season(today_eastern()))')
for season in "$@"; do
  if ! [[ "$season" =~ ^[0-9]{4}$ ]] || [ "$season" -ge "$current" ]; then
    echo "$season isn't a past season; update.yml rebuilds the current one" >&2
    exit 2
  fi
done

out=$(mktemp -d -t rebuild_seasons)
echo "working in $out"

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

for season in "$@"; do
  echo "== $season"
  raw="nba/raw/stats/$season"
  aws s3 sync "s3://$BUCKET/$raw/" "$out/$raw/" --exclude 'playbyplay/*' --no-progress
  # builds the lookups and game logs pipeline.rotations reads
  uv run python -m pipeline.stats --out "$out" --season "$season" --no-fetch
  if $rotations; then
    aws s3 sync "s3://$BUCKET/$raw/playbyplay/" "$out/$raw/playbyplay/" --no-progress
    uv run python -m pipeline.rotations --out "$out" --season "$season"
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
done

rm -rf "$out"
echo "done; the next update.yml run rebuilds the catalog (gh workflow run update.yml to run it now)"
