import gzip
import http.client
import urllib.error
from datetime import date
from email.message import Message

import duckdb
import pytest

from pipeline import torvik
from pipeline.output import dataset_path

SEASON = 2026
TODAY = date(2026, 10, 11)

# a team's values in torvik's columns, in order
TEAM_RESULTS_ROW = {
    "rank": "1",
    "team": "Michigan",
    "conf": "B10",
    "record": "37-3",
    "adjoe": "129.5",
    "oe Rank": "4",
    "adjde": "91.5",
    "de Rank": "1",
    "barthag": "0.98",
    "rank2": "1",
    "proj. W": "37.0",
    "Proj. L": "3.0",
    "Pro Con W": "19.0",
    "Pro Con L": "1.0",
    "Con Rec.": "19-1",
    "sos": "0.81",
    "ncsos": "0.73",
    "consos": "0.82",
    "Proj. SOS": "0.81",
    "Proj. Noncon SOS": "0.73",
    "Proj. Con SOS": "0.82",
    "elite SOS": "0.56",
    "elite noncon SOS": "0.67",
    "Opp OE": "119.8",
    "Opp DE": "102.1",
    "Opp Proj. OE": "119.8",
    "Opp Proj DE": "102.1",
    "Con Adj OE": "129.5",
    "Con Adj DE": "93.8",
    "Qual O": "129.2",
    "Qual D": "88.2",
    "Qual Barthag": "0.99",
    "Qual Games": "26.0",
    "FUN": "0.08",
    "ConPF": "1719.0",
    "ConPA": "1418.0",
    "ConPoss": "1400.4",
    "ConOE": "1.25",
    "ConDE": "1.0",
    "ConSOSRemain": "0.0",
    "Conf Win%": "0.95",
    "WAB": "14.2",
    "WAB Rk": "1",
    "Fun Rk": "21",
    "adjt": "71.1",
}
FOUR_FACTORS_ROW = ["Michigan"] + [
    v for i in range(20) for v in (f"{40 + i}.300000000000004", str(i + 1))
]


def csv_text(file, teams=341, header=None, row=None):
    """a CSV of `teams` rows, team i named Team i"""
    if header is None:
        header = torvik.KNOWN_HEADERS[file][0]
    if row is None:
        row = list(
            TEAM_RESULTS_ROW.values() if file == "team_results" else FOUR_FACTORS_ROW
        )
    name = 1 if file == "team_results" else 0
    rows = [line(header)]
    for i in range(teams):
        r = list(row)
        r[name] = f"Team {i}"
        rows.append(line(r))
    return "\n".join(rows) + "\n"


def line(values):
    return ",".join(f'"{v}"' if "," in v else v for v in values)


def one_row(file, **values):
    """the parsed row of a one-team CSV, with some of torvik's values replaced"""
    row = dict(TEAM_RESULTS_ROW) | values
    text = csv_text(file, row=list(row.values()))
    return torvik.parse(text, file, SEASON)[0]


def test_team_results():
    rows = torvik.parse(csv_text("team_results"), "team_results", SEASON)
    assert len(rows) == 341
    row = rows[0]
    assert set(row) == {name for name, _ in torvik.columns(torvik.TEAM_RESULTS)} - {
        "as_of_date"
    }
    assert row["team"] == "Team 0"
    assert row["conf"] == "B10"
    assert (row["wins"], row["losses"]) == (37, 3)
    assert (row["conf_wins"], row["conf_losses"]) == (19, 1)
    assert row["adj_oe"] == 129.5
    assert row["qual_games"] == 26
    assert row["conf_pts"] == 1719
    # points per possession, stored per 100
    assert row["conf_oe"] == pytest.approx(125)
    # no conference games left
    assert row["conf_sos_remain"] is None
    assert row["wab"] == 14.2


def test_old_team_results_header():
    # before 2022, the last header was one cell over two columns
    old = torvik.KNOWN_HEADERS["team_results"][1]
    assert old[-1] == "Fun Rk, adjt"
    rows = torvik.parse(csv_text("team_results", header=old), "team_results", 2008)
    assert rows[0]["adj_tempo"] == 71.1


def test_preseason_zeros_are_null():
    zeros = {
        h: "0"
        for h in [
            "sos", "ncsos", "consos", "Opp OE", "Opp DE", "Con Adj OE",
            "Con Adj DE", "Qual O", "Qual D", "Qual Barthag", "Qual Games",
            "FUN", "ConPF", "ConPA", "ConPoss", "ConOE", "ConDE", "Conf Win%",
            "WAB", "Fun Rk",
        ]
    }  # fmt: skip
    row = one_row(
        "team_results",
        record="0-0",
        **{"Con Rec.": "0-0", "ConSOSRemain": "0.8"} | zeros,
    )
    for name in [
        "sos", "nc_sos", "conf_sos", "opp_oe", "opp_de", "conf_adj_oe",
        "conf_adj_de", "qual_oe", "qual_de", "qual_barthag", "fun", "conf_pts",
        "conf_opp_pts", "conf_poss", "conf_oe", "conf_de", "conf_win_pct",
    ]:  # fmt: skip
        assert row[name] is None, name
    # counts and WAB are really 0; projections are there before any games
    assert (row["wins"], row["losses"], row["qual_games"], row["wab"]) == (0, 0, 0, 0)
    assert row["proj_sos"] == 0.81
    assert row["conf_sos_remain"] == 0.8


def test_winless_in_conference_is_zero():
    row = one_row("team_results", **{"Con Rec.": "0-18", "Conf Win%": "0"})
    assert row["conf_win_pct"] == 0
    assert row["conf_pts"] == 1719


def test_four_factors():
    rows = torvik.parse(csv_text("fffinal"), "fffinal", SEASON)
    row = rows[0]
    assert set(row) == {name for name, _ in torvik.columns(torvik.FOUR_FACTORS)} - {
        "as_of_date"
    }
    # percentages are fractions, without the float noise
    assert row["efg_pct"] == 0.403
    assert row["opp_efg_pct"] == 0.413
    assert row["opp_ast_rate"] == 0.573
    # the unlabeled columns
    assert row["blk_pct"] == 0.583
    assert row["opp_blk_pct"] == 0.593


@pytest.mark.parametrize(
    "file, text",
    [
        # torvik's bot check, which is also what a 404 returns
        ("team_results", "<!DOCTYPE html>\n<title>Verifying Browser...</title>\n"),
        ("team_results", ""),
        # a changed header
        ("fffinal", csv_text("fffinal").replace("eFG% Def", "eFG% D", 1)),
        ("team_results", csv_text("team_results").replace(",adjt", ",tempo", 1)),
        # too few teams
        ("team_results", csv_text("team_results", teams=250)),
        # a column more than the header
        ("fffinal", csv_text("fffinal", row=FOUR_FACTORS_ROW + ["1"])),
        # not a number, not a record, a fractional count, no team name
        ("fffinal", csv_text("fffinal", row=["A", "x", *FOUR_FACTORS_ROW[2:]])),
        ("team_results", csv_text("team_results").replace("37-3", "37", 1)),
        ("team_results", csv_text("team_results").replace("26.0", "26.5", 1)),
        ("team_results", csv_text("team_results").replace(",B10,", ",,", 1)),
    ],
)
def test_bad_csv_fails(file, text):
    with pytest.raises(ValueError):
        torvik.parse(text, file, SEASON)


class FakeFetcher:
    def __init__(self, files):
        self.files = files
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self.files.get(path)


def test_season_files_fetch_and_reuse(tmp_path):
    fetcher = FakeFetcher(
        {
            "/2026_team_results.csv": csv_text("team_results"),
            "/2026_fffinal.csv": csv_text("fffinal"),
        }
    )
    files = torvik.season_files(fetcher, tmp_path, SEASON, TODAY, refresh=False)
    assert fetcher.paths == ["/2026_team_results.csv", "/2026_fffinal.csv"]
    assert files["fffinal"][0] == TODAY
    raw = tmp_path / "ncaam/raw/torvik/2026/four_factors/2026-10-11.csv.gz"
    with gzip.open(raw, "rt") as f:
        assert f.read() == csv_text("fffinal")

    # a past season is fetched once
    torvik.season_files(fetcher, tmp_path, SEASON, date(2026, 10, 12), refresh=False)
    assert len(fetcher.paths) == 2
    # the current one every run, kept under each day
    torvik.season_files(fetcher, tmp_path, SEASON, date(2026, 10, 12), refresh=True)
    assert len(fetcher.paths) == 4
    assert len(list(raw.parent.glob("*.csv.gz"))) == 2


def test_missing_file_is_skipped(tmp_path):
    # the four factors before the season starts are a 404
    fetcher = FakeFetcher({"/2027_team_results.csv": csv_text("team_results")})
    files = torvik.season_files(fetcher, tmp_path, 2027, TODAY, refresh=True)
    assert list(files) == ["team_results"]
    paths = torvik.build_season(tmp_path, 2027, files)
    assert [p.parent.parent.name for p in paths] == ["torvik_team_results"]


def test_a_bad_file_is_not_saved(tmp_path):
    good = csv_text("team_results")
    raw = torvik.raw_dir(tmp_path, SEASON, "team_results")
    fetcher = FakeFetcher({"/2026_team_results.csv": good})
    torvik.season_files(fetcher, tmp_path, SEASON, TODAY, refresh=True)

    fetcher.files["/2026_team_results.csv"] = "<html>Verifying Browser...</html>"
    with pytest.raises(ValueError):
        torvik.season_files(fetcher, tmp_path, SEASON, date(2026, 10, 12), True)
    # the good one is still the latest
    assert [p.name for p in raw.iterdir()] == ["2026-10-11.csv.gz"]
    assert torvik.latest_file(tmp_path, SEASON, "team_results") == (TODAY, good)


def test_build_season(tmp_path):
    files = {
        "team_results": (date(2026, 4, 1), csv_text("team_results")),
        "fffinal": (date(2026, 4, 7), csv_text("fffinal")),
    }
    paths = torvik.build_season(tmp_path, SEASON, files)
    assert paths == [
        dataset_path(tmp_path / "ncaam/torvik", "torvik_team_results", SEASON),
        dataset_path(tmp_path / "ncaam/torvik", "torvik_four_factors", SEASON),
    ]
    rel = duckdb.sql(
        f"SELECT * FROM read_parquet('{paths[1]}', hive_partitioning = false)"
    )
    assert rel.count("*").fetchone() == (341,)
    assert rel.select("as_of_date::VARCHAR").distinct().fetchall() == [("2026-04-07",)]
    types = {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
    assert types["season"] == "INTEGER"
    assert types["team"] == "VARCHAR"
    assert types["efg_pct"] == "DOUBLE"
    assert types["as_of_date"] == "DATE"
    # no NBA key columns, which the NBA integrity check would look up
    assert not any(c.endswith("_id") for c in rel.columns)


def test_duplicate_team_writes_nothing(tmp_path):
    text = csv_text("fffinal").replace("Team 1,", "Team 0,", 1)
    with pytest.raises(ValueError, match="duplicate"):
        torvik.build_season(tmp_path, SEASON, {"fffinal": (TODAY, text)})
    assert not (tmp_path / "ncaam" / "torvik").exists()


class FlakyResponse:
    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if self.error:
            raise self.error
        return b"ok"


def fetcher_with(monkeypatch, *responses):
    """a Fetcher whose requests get `responses` in turn (an exception is raised)"""
    queue = list(responses)

    def urlopen(req, timeout):
        r = queue.pop(0)
        if isinstance(r, urllib.error.HTTPError):
            raise r
        return FlakyResponse(r)

    monkeypatch.setattr(torvik.urllib.request, "urlopen", urlopen)
    return torvik.Fetcher(delay=0, sleep=lambda s: None, clock=lambda: 0.0)


def http_error(code):
    return urllib.error.HTTPError("u", code, "x", Message(), None)


def test_fetcher_retries_a_truncated_response_once(monkeypatch):
    truncated = http.client.IncompleteRead(b"x", 10)
    assert fetcher_with(monkeypatch, truncated, None).get("/a") == "ok"
    with pytest.raises(http.client.IncompleteRead):
        fetcher_with(monkeypatch, truncated, truncated).get("/a")


def test_fetcher_404_is_none(monkeypatch):
    assert fetcher_with(monkeypatch, http_error(404)).get("/a") is None
    # other HTTP errors aren't retried
    with pytest.raises(urllib.error.HTTPError):
        fetcher_with(monkeypatch, http_error(500), None).get("/a")
