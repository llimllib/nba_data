import gzip
from datetime import date

import duckdb
import pytest

from pipeline import ctg
from pipeline.output import dataset_path
from pipeline.teams import load_team_abbrevs

SEASON = 2026
TEAM_IDS = {
    a.abbrev: a.team_id
    for a in load_team_abbrevs()
    if a.source == "ctg" and a.covers(SEASON)
}
SEASON_HEADERS = list(ctg.HEADERS)
LAST_2WK_HEADERS = ctg.LAST_2WK_HEADERS


def values(i, headers, prefix=""):
    """a team's cells: a value for each header, with a rank cell before some"""
    sample = {
        "Point Diff": f"+{i}.5",
        "W": str(40 + i),
        "L": str(42 - i),
        "Win%": "62.5%",
        "Exp W82": "50.1",
        "Exp W": "49.9",
        "Win Diff": "-0.3",
        "Offense": f"{110 + i}.0",
        "Defense": "108.0",
        "Spread Diff": "1,000.0",
        # a column CTG might add
        "Pace": "99.0",
    }
    cells = []
    for h in headers:
        if h in ("Point Diff", "Offense", "Defense"):
            cells.append('<td class="stat rank" style="color: red;">7</td>')
        cells.append(f'<td class="stat value">{prefix}{sample[h]}</td>')
    return "".join(cells)


def team_row(team, i, season_headers, last_2wk_headers):
    cells = values(i, season_headers)
    if last_2wk_headers:
        cells += '<td class="spacer"></td>' + values(i, last_2wk_headers)
    return (
        '<tr><td class="team_logo"><img src="x.png" alt="Team"/></td>'
        f'<td class="team_name"><a href="/stats/team/{team}/team">Team {team}</a></td>'
        f"{cells}</tr>"
    )


def page(
    season=SEASON,
    heading="regular season",
    teams=range(1, 31),
    season_headers=SEASON_HEADERS,
    last_2wk_headers=LAST_2WK_HEADERS,
    rows=None,
):
    head = "".join(f'<th colspan="2" class="x">{h}</th>' for h in season_headers)
    if last_2wk_headers:
        head += '<th class="sorter-false"></th>'
        head += "".join(f"<th>{h}</th>" for h in last_2wk_headers)
    if rows is None:
        rows = "".join(
            team_row(t, i, season_headers, last_2wk_headers)
            for i, t in enumerate(teams)
        )
    return (
        f"<h2>{season - 1}-{season % 100:02d} {heading}</h2>"
        '<table id="league_summary" class="stat_table">'
        '<thead><tr class="section_header"><th colspan="15"></th>'
        "<th>Last 2 Weeks</th></tr>"
        f'<tr><th colspan="2">Team</th>{head}</tr>'
        '<tr class="league_averages"><td class="team_name">Average</td>'
        '<td class="stat value">111.0</td></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def summary(html, season_type="regular_season", season=SEASON):
    return ctg.team_summary(html, season, season_type, TEAM_IDS)


def test_team_summary():
    rows = summary(page())
    assert len(rows) == 30
    first = rows[0]
    assert first == {
        "season": SEASON,
        "season_type": "regular_season",
        "team_id": TEAM_IDS["1"],
        "as_of_date": None,
        "point_diff": 0.5,
        "wins": 40,
        "losses": 42,
        "win_pct": 0.625,
        "exp_wins_82": 50.1,
        "exp_wins": 49.9,
        "win_diff": -0.3,
        "off_rtg": 110.0,
        "def_rtg": 108.0,
        "spread_diff": 1000.0,
        "wins_last_2wk": 40,
        "losses_last_2wk": 42,
        "point_diff_last_2wk": 0.5,
        "off_rtg_last_2wk": 110.0,
        "def_rtg_last_2wk": 108.0,
        "spread_diff_last_2wk": 1000.0,
    }


def test_missing_optional_column_is_null():
    rows = summary(
        page(
            season_headers=[h for h in SEASON_HEADERS if h != "Spread Diff"],
            last_2wk_headers=[h for h in LAST_2WK_HEADERS if h != "Spread Diff"],
        )
    )
    assert rows[0]["spread_diff"] is None
    assert rows[0]["off_rtg"] == 110.0


def test_unknown_column_is_ignored():
    rows = summary(
        page(season_headers=[*SEASON_HEADERS[:3], "Pace", *SEASON_HEADERS[3:]])
    )
    assert rows[0]["win_pct"] == 0.625


def test_missing_required_column_fails():
    with pytest.raises(ValueError, match=r"no \['off_rtg'\] column"):
        summary(
            page(
                season_headers=[h for h in SEASON_HEADERS if h != "Offense"],
                last_2wk_headers=[],
            )
        )


def test_no_games_in_last_two_weeks():
    # CTG leaves the last two weeks' cells out for a team with no games then
    rows = "".join(
        team_row(t, i, SEASON_HEADERS, LAST_2WK_HEADERS if t == 1 else [])
        for i, t in enumerate(range(1, 17))
    )
    result = summary(page(heading="playoffs", rows=rows), season_type="playoffs")
    assert len(result) == 16
    assert result[0]["wins_last_2wk"] == 40
    assert result[1]["wins_last_2wk"] is None
    assert result[1]["off_rtg"] == 111.0


def test_wrong_number_of_cells_fails():
    row = team_row(1, 0, SEASON_HEADERS, LAST_2WK_HEADERS[:2])
    with pytest.raises(ValueError, match="12 values for 16 columns"):
        summary(page(rows=row))


def test_wrong_heading_fails():
    # CTG ignored our parameters and showed its default page
    with pytest.raises(ValueError, match="expected the heading"):
        summary(page(heading="preseason"))


def test_before_the_season():
    html = (
        "<h2>2025-26 playoffs</h2><p>Looks like there’s no data matching the "
        "filters you chose!</p>"
    )
    assert summary(html, season_type="playoffs") == []


def test_no_table_fails():
    with pytest.raises(ValueError, match="no league_summary table"):
        summary("<h2>2025-26 regular season</h2><p>We changed everything</p>")


def test_unknown_team_fails():
    with pytest.raises(ValueError, match="no team_id for ctg team 31"):
        summary(page(teams=range(2, 32)))


def test_regular_season_needs_every_team():
    with pytest.raises(ValueError, match="29 teams have played, expected 30"):
        summary(page(teams=range(1, 30)))


def test_teams_without_games_are_dropped():
    zero = team_row(2, 0, SEASON_HEADERS, LAST_2WK_HEADERS).replace(
        '<td class="stat value">40</td><td class="stat value">42</td>',
        '<td class="stat value">0</td><td class="stat value">0</td>',
        1,
    )
    rows = team_row(1, 0, SEASON_HEADERS, LAST_2WK_HEADERS) + zero
    result = summary(page(heading="playoffs", rows=rows), season_type="playoffs")
    assert [r["team_id"] for r in result] == [TEAM_IDS["1"]]


def test_not_a_number_fails():
    row = team_row(1, 0, SEASON_HEADERS, LAST_2WK_HEADERS).replace("108.0", "N/A", 1)
    with pytest.raises(ValueError, match="def_rtg 'N/A' isn't a number"):
        summary(page(rows=row))


def test_page_path():
    # CTG names seasons by their start year
    assert ctg.page_path(2026, "playoffs") == (
        "/stats/league/summary?season=2025&seasontype=playoffs"
    )


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self.pages[path]


def read(path):
    rel = duckdb.sql(
        f"SELECT * FROM read_parquet('{path}', hive_partitioning = false) ORDER BY ALL"
    )
    return rel


def save(outdir, season, season_type, day, html):
    path = ctg.raw_dir(outdir, season, season_type) / f"{day}.html.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        f.write(html)


def test_season_pages_fetch_and_reuse(tmp_path):
    fetcher = FakeFetcher(
        {
            ctg.page_path(SEASON, "regular_season"): page(),
            ctg.page_path(SEASON, "playoffs"): page(
                heading="playoffs", teams=range(1, 17)
            ),
        }
    )
    today = date(2026, 10, 10)
    pages = ctg.season_pages(fetcher, tmp_path, SEASON, today, refresh=False)
    assert len(fetcher.paths) == 2
    assert pages["playoffs"][0] == today
    assert (ctg.raw_dir(tmp_path, SEASON, "playoffs") / "2026-10-10.html.gz").is_file()

    # a past season is fetched once
    ctg.season_pages(fetcher, tmp_path, SEASON, date(2026, 10, 11), refresh=False)
    assert len(fetcher.paths) == 2
    # the current one every run, kept under each day
    ctg.season_pages(fetcher, tmp_path, SEASON, date(2026, 10, 11), refresh=True)
    assert len(fetcher.paths) == 4
    assert len(list(ctg.raw_dir(tmp_path, SEASON, "playoffs").glob("*.html.gz"))) == 2


def test_build_season_uses_the_latest_page(tmp_path):
    save(tmp_path, SEASON, "regular_season", "2026-04-01", page(teams=range(1, 31)))
    later = page().replace("108.0", "99.0")
    save(tmp_path, SEASON, "regular_season", "2026-04-12", later)
    save(
        tmp_path,
        SEASON,
        "playoffs",
        "2026-04-12",
        page(heading="playoffs", teams=range(1, 17)),
    )
    pages = ctg.season_pages(None, tmp_path, SEASON, date(2026, 10, 10), refresh=True)
    path = ctg.build_season(tmp_path, SEASON, pages)
    assert path == dataset_path(tmp_path / ctg.OUT_DIR, ctg.DATASET, SEASON)
    rel = read(path)
    assert rel.count("*").fetchone() == (46,)
    regular = rel.filter("season_type = 'regular_season'")
    assert regular.select("as_of_date::VARCHAR, def_rtg").distinct().fetchall() == [
        ("2026-04-12", 99.0)
    ]
    types = {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
    assert types["season"] == "INTEGER"
    assert types["team_id"] == "VARCHAR"
    assert types["wins"] == "INTEGER"
    assert types["as_of_date"] == "DATE"


def test_nothing_written_before_the_season(tmp_path):
    empty = "<h2>{} {}</h2>no data matching the filters"
    pages: dict[str, tuple[date, str]] = {
        "regular_season": (
            date(2026, 10, 10),
            empty.format("2026-27", "regular season"),
        ),
        "playoffs": (date(2026, 10, 10), empty.format("2026-27", "playoffs")),
    }
    assert ctg.build_season(tmp_path, 2027, pages) is None
    assert not (tmp_path / "nba" / "ctg").exists()


def test_a_bad_page_writes_nothing(tmp_path):
    pages: dict[str, tuple[date, str]] = {
        "regular_season": (date(2026, 10, 10), page(teams=range(1, 29)))
    }
    with pytest.raises(ValueError):
        ctg.build_season(tmp_path, SEASON, pages)
    assert not (tmp_path / "nba" / "ctg").exists()
