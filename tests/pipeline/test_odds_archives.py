import gzip
import json
from datetime import UTC, date, datetime

import duckdb
import pytest

from pipeline import espn_odds as eo
from pipeline import odds_archives as oa
from pipeline.output import dataset_path

from .test_espn_odds import DEN, IND, FakeESPN, event, read, scoreboard

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def sbr_row(**kw):
    """IND hosting DEN, favored by 4.5 at the close"""
    return {
        "season": 2014,
        "date": 20150110.0,
        "home_team": "Pacers",
        "away_team": "Nuggets",
        "home_close_ml": -190,
        "away_close_ml": 165,
        "home_open_spread": -5.0,
        "home_close_spread": -4.5,
        "open_over_under": 195.5,
        "close_over_under": 193.0,
    } | kw


def write_sbr(outdir, rows):
    path = outdir / oa.SBR.path
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(rows, f)


def write_team_game_logs(outdir, season, games):
    """games: (game_id, game_date, home team, away team)"""
    path = dataset_path(outdir / eo.STATS_DIR, "team_game_logs", season)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = ", ".join(
        f"('{g}', '{d}'::DATE, '{t}')" for g, d, h, a in games for t in (h, a)
    )
    duckdb.sql(
        f"COPY (FROM (VALUES {values}) v(game_id, game_date, team_id)) TO '{path}'"
    )


@pytest.mark.parametrize(
    "row,expected",
    [
        (sbr_row(), (-4.5, 193.0)),
        # swapped, with the total's sign lost or kept
        (sbr_row(home_close_spread=193.0, close_over_under=4.5), (-4.5, 193.0)),
        (sbr_row(home_close_spread=-193.0, close_over_under=4.5), (-4.5, 193.0)),
        (
            sbr_row(
                home_close_spread=193.0,
                close_over_under=4.5,
                home_close_ml=165,
                away_close_ml=-190,
            ),
            (4.5, 193.0),
        ),
        (sbr_row(home_close_spread=193.0, close_over_under=0.0), (0.0, 193.0)),
        # no moneylines to say who was favored
        (
            sbr_row(home_close_spread=193.0, close_over_under=4.5, home_close_ml=0),
            (None, 193.0),
        ),
        # typos
        (sbr_row(close_over_under=1955.5), (-4.5, None)),
    ],
)
def test_sbr_close(row, expected):
    assert oa.sbr_close(row) == expected


def test_sbr_dates_the_bubble_a_year_early():
    assert oa.sbr_date(sbr_row(season=2019, date=20190830.0)) == date(2020, 8, 30)
    assert oa.sbr_date(sbr_row(season=2019, date=20191030.0)) == date(2019, 10, 30)


def test_sbr_unknown_team_fails(tmp_path):
    write_sbr(tmp_path, [sbr_row(home_team="Bobcats")])
    with pytest.raises(ValueError, match="Bobcats"):
        oa.sbr_lines(tmp_path, 2015)


def test_build_season_from_sbr(tmp_path):
    write_sbr(
        tmp_path,
        [
            sbr_row(),
            # a day off
            sbr_row(date=20150112.0, home_team="Nuggets", away_team="Pacers"),
            # the last Finals game, with no home team and its lines garbled
            sbr_row(date=20150615.0, home_team=0, away_team="Pacers"),
            # another season
            sbr_row(season=2015),
        ],
    )
    write_team_game_logs(
        tmp_path,
        2015,
        [
            ("0021400001", "2015-01-10", IND, DEN),
            ("0021400002", "2015-01-11", DEN, IND),
            ("0041400406", "2015-06-15", DEN, IND),
        ],
    )
    lines = read(eo.build_season(tmp_path, 2015, now=NOW))
    assert lines.select(
        "game_id, provider_id, home_team_id, open_spread, close_spread, "
        "close_home_moneyline, espn_event_id"
    ).order("game_id").fetchall() == [
        ("0021400001", "sbr", IND, -5.0, -4.5, -190, None),
        ("0021400002", "sbr", DEN, -5.0, -4.5, -190, None),
    ]


def test_unmatched_sbr_games_are_reported(tmp_path):
    write_sbr(tmp_path, [sbr_row()])
    write_team_game_logs(tmp_path, 2015, [("0021400001", "2015-01-20", IND, DEN)])
    with pytest.raises(ValueError, match="sbr-2015-01-10"):
        eo.build_season(tmp_path, 2015, now=NOW)


def test_a_season_needs_its_archives(tmp_path):
    with pytest.raises(FileNotFoundError, match="nba_archive_10Y"):
        eo.build_season(tmp_path, 2015, now=NOW)


def write_odds_api(outdir, rows):
    path = outdir / oa.ODDS_API.path
    path.parent.mkdir(parents=True, exist_ok=True)
    values = ", ".join(
        f"({e}, {s}, {t}, 'odds_api_consensus_close')" for e, s, t in rows
    )
    duckdb.sql(
        f"COPY (FROM (VALUES {values}) "
        f"v(game_id, home_point, over_under, spread_source)) TO '{path}'"
    )


def test_build_season_from_the_odds_api(tmp_path):
    # matched through ESPN's scoreboards, which need no odds of ESPN's own
    espn = FakeESPN({date(2025, 10, 23): scoreboard(event("401", "2025-10-23T23:00Z"))})
    eo.fetch_scoreboards(espn, tmp_path, 2026, date(2025, 10, 25))
    write_odds_api(tmp_path, [(401, -8.5, 229.5), (402, 3.0, 220.0)])
    write_team_game_logs(tmp_path, 2026, [("0022500010", "2025-10-23", IND, DEN)])
    lines = read(eo.build_season(tmp_path, 2026, now=NOW))
    assert lines.select(
        "game_id, provider_id, close_spread, close_total, open_spread, espn_event_id"
    ).fetchall() == [
        ("0022500010", "odds_api_consensus", -8.5, 229.5, None, "401"),
    ]


def test_fetch_only_the_seasons_archives(tmp_path):
    class Fetcher:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            return b"[]"

    fetcher = Fetcher()
    oa.fetch(fetcher, tmp_path, 2027)
    assert fetcher.urls == []
    oa.fetch(fetcher, tmp_path, 2015)
    assert fetcher.urls == [oa.SBR.url]
    with gzip.open(tmp_path / oa.SBR.path) as f:
        assert f.read() == b"[]"
    oa.fetch(fetcher, tmp_path, 2015)
    assert fetcher.urls == [oa.SBR.url]
