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

# the season's log, which gets every command's output; the terminal gets a
# line per step
log=/dev/null

# run a step, its output to the log; if it fails, show the log's last lines
step() {
  local label=$1
  shift
  echo "  $label"
  if ! "$@" >>"$log" 2>&1; then
    fail "$label failed"
  fi
}

fail() {
  echo "$1. The end of $log:" >&2
  tail -n 15 "$log" | sed 's/^/    /' >&2
  exit 1
}

upload() {
  if $dry_run; then
    echo "    dry run: aws s3 $1 ${2#"$out/"} -> $3"
  elif ! aws s3 "$@" "${UPLOAD[@]}" >>"$log" 2>&1; then
    fail "uploading ${2#"$out/"} failed"
  fi
}

saved_games() {
  if [ -d "$1" ]; then
    find "$1" -name '*.json.gz' | wc -l | tr -d ' '
  else
    echo 0
  fi
}

# HH:MM, $1 seconds from now
clock_in() {
  local t=$(($(date +%s) + $1))
  date -d "@$t" +%H:%M 2>/dev/null || date -r "$t" +%H:%M
}

# fetch the season's play-by-play, showing progress on one line: games
# saved, failures this run, time left at this run's rate (pauses included),
# and when a pause (stats.nba.com throttling) or the wait before retrying
# the run's failed games ends
fetch_playbyplay() {
  local season=$1 dir=$2 total=$3
  local from before started status="" pid line last_print=-60
  local wait_line="" wait_until=0 wait_note="" printed=""
  from=$(($(wc -l <"$log") + 1))
  before=$(saved_games "$dir")
  started=$SECONDS
  uv run python -m pipeline.rotations --out "$out" --season "$season" >>"$log" 2>&1 &
  pid=$!
  # games are saved with atomic writes, so stopping loses nothing
  trap 'kill "$pid" 2>/dev/null' EXIT
  while kill -0 "$pid" 2>/dev/null; do
    local saved failed elapsed left=""
    saved=$(saved_games "$dir")
    failed=$(tail -n +"$from" "$log" | grep -c '^rotations: [0-9]* failed:' || true)
    elapsed=$((SECONDS - started))
    if [ "$saved" -gt "$before" ] && [ "$elapsed" -gt 60 ]; then
      left=", ~$(((total - saved) * elapsed / (saved - before) / 60)) min left"
    fi
    status="    $saved of $total games saved, $failed failed this run$left"
    # the last pause or retry pipeline.rotations announced, timed from when
    # it's first seen, since its next message can be 100 games later
    line=$(tail -n +"$from" "$log" | grep -n -E '^rotations: .*(pausing [0-9]+ minutes|in [0-9]+ seconds)' | tail -n 1 || true)
    if [ -n "$line" ] && [ "$line" != "$wait_line" ]; then
      wait_line=$line
      local secs
      if [[ "$line" =~ pausing\ ([0-9]+)\ minutes ]]; then
        secs=$((BASH_REMATCH[1] * 60))
        wait_note="stats.nba.com is throttling; paused until $(clock_in "$secs")"
      elif [[ "$line" =~ retrying\ ([0-9]+)\ failed.*in\ ([0-9]+)\ seconds ]]; then
        secs=${BASH_REMATCH[2]}
        wait_note="retrying ${BASH_REMATCH[1]} failed games at $(clock_in "$secs")"
      fi
      wait_until=$((SECONDS + secs))
    fi
    if [ "$SECONDS" -lt "$wait_until" ]; then
      status="$status; $wait_note"
    fi
    if [ -t 1 ]; then
      printf '\r\033[K%s' "$status"
    elif [ $((SECONDS - last_print)) -ge 60 ]; then
      echo "$status"
      printed=$status
      last_print=$SECONDS
    fi
    sleep 5
  done
  # where it finished
  if [ -t 1 ]; then
    printf '\r\033[K'
  fi
  status="    $(saved_games "$dir") of $total games saved, $(tail -n +"$from" "$log" | grep -c '^rotations: [0-9]* failed:' || true) failed this run"
  if [ "$status" != "$printed" ]; then
    echo "$status"
  fi
  trap - EXIT
  if ! wait "$pid"; then
    fail "stats.nba.com kept failing, so it stopped; the games fetched are saved, run this again later to fetch the rest"
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
  out="out/rebuild/$season"
  log="out/rebuild/logs/$season.log"
  mkdir -p "$out" out/rebuild/logs
  echo "== $season (log: $log)"
  echo "== $(date) $0 $*" >>"$log"
  raw="nba/raw/stats/$season"
  step "downloading the saved responses" \
    aws s3 sync "s3://$BUCKET/$raw/" "$out/$raw/" --exclude 'playbyplay/*' --no-progress
  # builds the lookups and game logs pipeline.rotations reads
  step "building the stats" uv run python -m pipeline.stats --out "$out" --season "$season" --no-fetch
  if $rotations; then
    step "downloading the bucket's play-by-play" \
      aws s3 sync "s3://$BUCKET/$raw/playbyplay/" "$out/$raw/playbyplay/" --no-progress
    missing=$(missing_games "$out" "$season")
    games=$(($(saved_games "$out/$raw/playbyplay") + $(wc -w <<<"$missing")))
    echo "  fetching play-by-play: $(wc -w <<<"$missing" | tr -d ' ') of $games games to fetch"
    fetch_playbyplay "$season" "$out/$raw/playbyplay" "$games"
    grep "games' stints don't match" "$log" | tail -n 1 | sed -e 's/^rotations: /    /' -e 's/:$//'
    missing=$(missing_games "$out" "$season")
    if [ -n "$missing" ] && ! $allow_missing; then
      echo "  $(wc -w <<<"$missing" | tr -d ' ') games have no play-by-play: $missing" >&2
      echo "  run this again to fetch them, or pass --allow-missing to publish without them" >&2
      exit 1
    fi
    # again, for team_oreb from the game_events just built
    step "rebuilding the stats with the play-by-play" \
      uv run python -m pipeline.stats --out "$out" --season "$season" --no-fetch
  fi
  step "checking integrity" uv run python -m pipeline.catalog --out "$out" --check

  key="nba/stats/team_game_logs/season=$season/data.parquet"
  echo "  comparing team_game_logs with the bucket's"
  if ! result=$(compare "$out/$key" "$URL/$key" 2>&1); then
    echo "$result" | tee -a "$log" | sed 's/^/    /' >&2
    exit 1
  fi
  echo "$result" | tee -a "$log" | sed 's/^/    /'

  echo "  uploading"
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
  # so the real run doesn't fetch the games again. The log stays
  if ! $dry_run; then
    rm -rf "$out"
  fi
  echo "  done"
done

echo "done; the next update.yml run rebuilds the catalog (gh workflow run update.yml to run it now)"
