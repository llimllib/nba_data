import gzip

import duckdb
import pytest

from pipeline import bbref
from pipeline import bbref_salaries as s
from pipeline.output import dataset_path
from pipeline.teams import load_team_abbrevs

SEASON = 2026
TEAMS = {
    a.abbrev: a.team_id
    for a in load_team_abbrevs()
    if a.source == "bbref" and a.covers(SEASON)
}


def salary_row(i, bbref_id, name, pay):
    return (
        f'<tr ><th scope="row" data-stat="ranker" csk="{i}" >{i}</th>'
        f'<td class="left " data-append-csv="{bbref_id}" data-stat="player" >'
        f'<a href="/players/{bbref_id[0]}/{bbref_id}.html">{name}</a></td>'
        f'<td class="right " data-stat="salary" csk="1" >{pay}</td></tr>'
    )


def page(players):
    """a team page; bbref hides the salaries table in a comment"""
    rows = "".join(salary_row(i, *p) for i, p in enumerate(players, start=1))
    return (
        '<html><table id="roster"><tbody></tbody></table><!--\n'
        f'<table id="salaries2"><tbody>{rows}</tbody></table>\n--></html>'
    )


PLAYERS = [
    ("tatumja01", "Jayson Tatum", "$54,126,450"),
    ("brownja02", "Jaylen Brown", "$53,142,264"),
    ("whitede01", "Derrick White", "$28,100,000"),
    ("hausesa01", "Sam Hauser", "$10,044,644"),
    ("luisrj01", "RJ Luis", ""),
]


def test_team_salaries():
    rows = s.team_salaries(page(PLAYERS), "BOS")
    assert rows[0] == ("tatumja01", "Jayson Tatum", 54126450)
    # two-way contracts have no salary
    assert rows[-1] == ("luisrj01", "RJ Luis", None)


def test_players_without_bbref_ids_are_skipped():
    html = (
        page(PLAYERS)
        .replace(' data-append-csv="luisrj01"', "")
        .replace("/players/l/luisrj01.html", "")
    )
    rows = s.team_salaries(html, "BOS")
    assert [r[0] for r in rows] == ["tatumja01", "brownja02", "whitede01", "hausesa01"]

    unlinked = page(PLAYERS)
    for bbref_id, _, _ in PLAYERS:
        unlinked = unlinked.replace(f' data-append-csv="{bbref_id}"', "").replace(
            f"/players/{bbref_id[0]}/{bbref_id}.html", ""
        )
    with pytest.raises(ValueError, match="5 salary rows with no bbref id"):
        s.team_salaries(unlinked, "BOS")


def test_id_from_the_link_before_the_season():
    # preseason pages leave out data-append-csv
    html = page(PLAYERS).replace(' data-append-csv="tatumja01"', "")
    assert s.team_salaries(html, "BOS")[0][0] == "tatumja01"


def test_too_few_rows_fails():
    with pytest.raises(ValueError, match="2 salary rows"):
        s.team_salaries(page(PLAYERS[:2]), "BOS")


def test_bad_salary_fails():
    bad = [*PLAYERS[:4], ("x01", "X", "1.2M")]
    with pytest.raises(ValueError, match="'1.2M' isn't a salary"):
        s.team_salaries(page(bad), "BOS")


def test_no_salaries_fails():
    with pytest.raises(ValueError, match="no salaries"):
        s.team_salaries(page([(b, n, "") for b, n, _ in PLAYERS]), "BOS")


KNOWN = [("1", "Jayson Tatum"), ("2", "Jaylen Brown"), ("3", "Derrick White")]


def test_resolve():
    salaries = [
        ("10", "tatumja01", "Jayson Tatum", 100),
        # by name, accents and suffixes aside
        ("10", "brownja02", "Jaylen Brown Jr.", 90),
        # player_ids.csv wins
        ("10", "whitede01", "D. White", 80),
        # waived and never played: kept, with no player_id
        ("10", "rubiori01", "Ricky Rubio", 70),
    ]
    rows = s.resolve(salaries, KNOWN, {"whitede01": "3"}, "test")
    assert [(r["bbref_id"], r["player_id"], r["salary"]) for r in rows] == [
        ("tatumja01", "1", 100),
        ("brownja02", "2", 90),
        ("whitede01", "3", 80),
        ("rubiori01", None, 70),
    ]


def test_mapped_player_not_in_the_season_has_no_id():
    rows = s.resolve([("10", "oldgu01", "Old Guy", 5)], KNOWN, {"oldgu01": "9"}, "t")
    assert rows[0]["player_id"] is None


def test_listed_twice_keeps_the_larger_salary():
    rows = s.resolve(
        [
            ("10", "tatumja01", "Jayson Tatum", 5),
            ("10", "tatumja01", "Jayson Tatum", 9),
        ],
        KNOWN,
        {},
        "t",
    )
    assert [r["salary"] for r in rows] == [9]


def test_too_many_unplaced_fails():
    salaries = [("10", f"p{i}", f"Nobody {i}", 1) for i in range(s.MAX_UNPLACED + 1)]
    with pytest.raises(ValueError, match="61 players not in player_seasons"):
        s.resolve(salaries, KNOWN, {}, "t")


def write_pages(outdir):
    for abbrev in TEAMS:
        path = s.team_page_path(outdir, SEASON, abbrev)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as f:
            f.write(page(PLAYERS))
    ps = dataset_path(outdir / s.STATS_DIR, "player_seasons", SEASON)
    ps.parent.mkdir(parents=True)
    duckdb.sql(
        # Tatum's NBA id, as player_ids.csv maps him
        f"COPY (SELECT {SEASON} AS season, '1628369' AS player_id, 'Jayson Tatum' AS name) "
        f"TO '{ps}' (FORMAT parquet)"
    )


def test_build_season(tmp_path):
    write_pages(tmp_path)
    path = s.build_season(None, tmp_path, SEASON, refresh=False)
    assert path == dataset_path(tmp_path / bbref.OUT_DIR, s.DATASET, SEASON)
    rel = duckdb.sql(f"SELECT * FROM read_parquet('{path}', hive_partitioning = false)")
    types = {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
    assert types == {
        "season": "INTEGER",
        "team_id": "VARCHAR",
        "bbref_id": "VARCHAR",
        "player_id": "VARCHAR",
        "salary": "INTEGER",
    }
    assert rel.count("*").fetchone() == (30 * len(PLAYERS),)
    assert rel.filter("player_id IS NOT NULL").count("*").fetchone() == (30,)


def test_missing_page_writes_nothing(tmp_path):
    write_pages(tmp_path)
    s.team_page_path(tmp_path, SEASON, "BOS").unlink()
    assert s.build_season(None, tmp_path, SEASON, refresh=False) is None
    assert not (tmp_path / bbref.OUT_DIR).exists()


class FakeFetcher:
    def __init__(self):
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return page(PLAYERS)


def test_pages_are_fetched_once_unless_refreshed(tmp_path):
    fetcher = FakeFetcher()
    s.team_page(fetcher, tmp_path, SEASON, "BOS", refresh=False)
    s.team_page(fetcher, tmp_path, SEASON, "BOS", refresh=False)
    assert fetcher.paths == ["/teams/BOS/2026.html"]
    s.team_page(fetcher, tmp_path, SEASON, "BOS", refresh=True)
    assert len(fetcher.paths) == 2
