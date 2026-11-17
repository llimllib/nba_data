# NBA official injury report: design

[#109](https://github.com/llimllib/nba_data/issues/109). The league's
pre-game injury report, as the main input for predicting who will play;
`player_game_status` ([#107](https://github.com/llimllib/nba_data/issues/107))
is the outcome.

## The source

Reports are PDFs at
`https://ak-static.cms.nba.com/referee/injury/Injury-Report_<date>_<slot>.pdf`.
official.nba.com lists only the current day's, so past file names have
to be guessed. Probed on 2026-11-17:

| Dates | Slot names | Reports |
|---|---|---|
| 2018-12-17 5 PM to 2019-06 (season 2019) | `_01PM`, `_05PM`, `_08PM` | 3 a day. Nothing earlier |
| 2019-20 to 2025-12-21 | `_08AM` ... `_11PM` | Hourly; the header reads :30 (`_05PM` is 05:30 PM) |
| 2025-12-22 on | `_HH_MMAM`, `_HH_MMPM` | Every 15 minutes (`_05_00PM`, `_05_15PM`) |

A slot that doesn't exist returns 403.

Layouts:

- Season 2019: `Game Date`, `Game Time`, `Matchup`, `Team`, `Player Name`,
  `Category`, `Reason`, `Current Status`, `Previous Status`.
- 2020 on: `Game Date`, `Game Time`, `Matchup`, `Team`, `Player Name`,
  `Current Status`, `Reason`, where the reason is `Category - detail`.
  Until December 2025 the detail is `Body Part; Injury`
  (`Right Ankle; Sprain`); since then it's free text
  (`Left Finger Sprain`).
- Game date, time, matchup and team are printed only on their first row.
  A team that hasn't reported has one `NOT YET SUBMITTED` row, and one
  with nobody to report (seen in 2019) `ALL PLAYERS AVAILABLE`.
- All-Star weekend's reports list `Non-NBA Team`; rows whose team or game
  isn't in the schedule are skipped and listed.
- Wrapped cells are split above and below their row (vertically
  centered), so line-based `pdftotext` parsing doesn't work. Since 2024
  only page 1 has the header.

In the 5:30 PM report, 5 to 8 of the day's teams had not submitted
(2020-2023 samples), some with 7:00 games. So one report a day isn't
enough.

## Which reports

For each game day, take each distinct tip-off time from `schedule`,
find the last slot published before it, and fetch each slot once. In the
hourly era, 7:00 and 7:30 tip-offs both use `_06PM`. A 403 steps back one
slot, up to a few times, before the day is reported missing. That's about
3 requests a day and ~400 a season. Each game takes its rows from the
last report before its own tip-off.

Past tip-off times come from `schedule`, which is fetched for 2019-2026
(one `ScheduleLeagueV2` request a season through
`pipeline.stats.fetch_schedule`; 2018-19's returned all 1,395 games with
times). This reverses the earlier decision not to backfill `schedule`.

## Module

`pipeline/injury_report.py`:
`python -m pipeline.injury_report --out out [--season ...] [--no-fetch]`.
It fetches only slots it doesn't have yet, 1-2s apart. Each PDF is saved
as received at `nba/raw/stats/<season>/injury_report/<file>.pdf`, and the
season is rebuilt from every saved PDF.

- **Current season:** `injuries.yml` already runs before each cluster of
  tip-offs, and gets a step that fetches the newest report.
- **Backfill:** seasons 2019-2026, ~3,000 requests in all, run locally
  and resumable, then uploaded.

## Parsing

pdfplumber, by word positions:

- Column positions come from the header row on page 1 and carry over to
  later pages. The `Page N of M` footer is dropped.
- Each row is anchored on its `Current Status` cell (always one line), and
  every other word joins the nearest anchor. Date, time, matchup and team
  carry down.
- `NOT YET SUBMITTED` rows are kept for the checks, then dropped.
- `reason` and `detail`: in 2019, the two columns. From 2020, the text
  split on the first ` - ` (`G League - Two-Way` becomes `G League` and
  `Two-Way`). Both are stored as printed.

A prototype parsed 2019-2023's reports correctly, wrapped reasons
included.

## Matching

- **Game:** the date plus the matchup's tricodes (`CHI@CLE`) find the
  `game_id` in `schedule`.
- **Team:** whichever of the game's two teams has the printed name
  (`team_seasons.full_name`).
- **Player:** `Last, First` is flipped to `First Last` and matched with
  `bbref.normalize_name`, first against that game's players in
  `player_game_status` (every player in the box score, inactive ones
  included), or for a game not played yet, `rosters_daily`. If that fails,
  a name only one player in `player_seasons` has.
- **Exceptions:** `player_ids.csv`, source `nba_injury`, with the printed
  name as `source_id`. Names with no NBA id at all (two-way players who
  never played) are listed in `pipeline/injury_report_no_id.txt`, and
  their rows left out.
- **A match not in that season's `player_seasons`** is left out and
  listed, since the integrity check would reject it.
- **Unmatched rows** are reported through `PIPELINE_DROPPED`.

## Dataset

`nba/stats/injury_report/season=<season>/data.parquet`, one row per
`game_id` and `player_id`:

| Column | |
|---|---|
| season, game_id, player_id | Key |
| game_date, team_id | |
| report_time | TIMESTAMPTZ, from the report's header |
| status | `Out`, `Doubtful`, `Questionable`, `Probable`, `Available` |
| reason, detail | As printed |

## Checks

- Usual key and `team_id`/`player_id` integrity checks.
- These fail the build: an unknown status, a report that parses to no
  rows, or a scheduled game with neither team in its report.
- A team still `NOT YET SUBMITTED` before its game is printed with a
  count but doesn't fail; that's the league's data.

## Tests

- The parser, on a small saved PDF from each layout (2019, 2020-2025,
  2026 on): wrapped reasons, carried-down teams, pages without a header,
  `NOT YET SUBMITTED`.
- Slot choice in each era.
- Name matching, including `Udeh, Jr., Ernest` and `Porter Jr., Michael`.

## Open questions

- Whether `ak-static.cms.nba.com` serves GitHub Actions IPs. `injuries.yml`
  runs without the Tailscale exit node; if the CDN blocks Actions, it needs
  one.
- ~~Suspensions~~: `Team Suspension` and `League Suspension`, with no
  detail (found in the test reports).

## Done when

`injury_report` covers 2019-2027, and a join to `player_game_status` shows
how often `Out`, `Doubtful`, `Questionable` and `Probable` players
played.
