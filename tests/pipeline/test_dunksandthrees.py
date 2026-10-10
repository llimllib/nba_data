import math
from datetime import date

import duckdb
import pytest

from pipeline import dunksandthrees as d
from pipeline.output import dataset_path

SEASON = 2026

# the page's columns, in its order (a subset; the rest are optional)
KEYS = [
    "season", "age", "seasontype", "scope_id", "team_id", "team_alias", "player_id",
    "player_name", "gp", "start", "mp", "mpg", "mpg_attr", "off", "def", "tot",
]  # fmt: skip


def js(value) -> str:
    """`value` as devalue writes it: bare keys, no leading zeros, void 0"""
    if value is None:
        return "void 0"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{js(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(js(v) for v in value) + "]"
    if isinstance(value, float):
        return repr(value).replace("0.", ".", 1) if abs(value) < 1 else repr(value)
    return str(value)


def player(player_id, gp=10, seasontype=2, season=SEASON, **kw):
    row = {
        "season": season, "age": 25, "seasontype": seasontype, "scope_id": 0,
        "team_id": 1610612738, "team_alias": "BOS", "player_id": player_id,
        "player_name": f"Player {player_id}", "gp": gp, "start": 0, "mp": 100.5,
        "mpg": 10.05, "mpg_attr": {"z": -0.5, "rk": 3, "pctl": 0.25}, "off": 1.5,
        "def": -0.25, "tot": 1.25,
    }  # fmt: skip
    row.update(kw)
    return row


def page(players, seasontype=2, season=SEASON, keys=KEYS):
    stats = [[p.get(k) for k in keys] for p in players]
    k = {name: i for i, name in enumerate(keys)}
    data = (
        f"{{stats:{js(stats)},k:{js(k)},seasons:[{{season:{season}}}],"
        f'statsType:"adv",seasontype:{seasontype},season:{season}}}'
    )
    return (
        "<script>kit.start(app, element, {node_ids: [0, 3, 23], data: ["
        '{type:"data",data:{user:void 0},uses:{}},{type:"data",data:{},uses:{}},'
        f'{{type:"data",data:{data},uses:{{search_params:["season"]}}}}],'
        "form: null});</script>"
    )


def rows(players, season_type="regular_season", **kw):
    st = d.SEASON_TYPES[season_type]
    return d.epm_rows(d.page_data(page(players, seasontype=st, **kw)), season_type)


@pytest.mark.parametrize(
    "text,value",
    [
        ("{a:1,b:[.5,-.25,1e-5,2.]}", {"a": 1, "b": [0.5, -0.25, 1e-5, 2.0]}),
        ('{"quoted key":"x\\"y\\n\\u00e9\\x41"}', {"quoted key": 'x"y\néA'}),
        ("[void 0,null,!0,!1,true,false]", [None, None, True, False, True, False]),
        ("['single',\"\"]", ["single", ""]),
        ("[]", []),
        ("{}", {}),
    ],
)
def test_js_literal(text, value):
    assert d.JSLiteral(text).value() == value


def test_js_literal_nan():
    assert math.isnan(d.JSLiteral("NaN").value())


@pytest.mark.parametrize(
    "text",
    [
        # devalue hoists repeated objects into a function's arguments
        "(function(a){return {x:a}}(1))",
        'new Date("2026-01-01")',
        "{a:1",
        "[1 2]",
        '"unterminated',
    ],
)
def test_js_literal_rejects_what_it_doesnt_know(text):
    with pytest.raises(d.JSLiteralError):
        d.JSLiteral(text).value()


def test_rows():
    [row] = rows([player(1629027)])
    assert row == {
        **{name: None for name, _ in d.COLUMNS.values()},
        "season": SEASON,
        "season_type": "regular_season",
        "player_id": "1629027",
        "team_id": "1610612738",
        "age": 25,
        "gp": 10,
        "gs": 0,
        "min": 100.5,
        "min_per_game": 10.05,
        "o_epm": 1.5,
        "d_epm": -0.25,
        "epm": 1.25,
    }


def test_players_with_few_minutes_have_no_epm():
    [row] = rows([player(1, off=None, tot=None, **{"def": None})])
    assert row["epm"] is None and row["gp"] == 10


def test_playoffs():
    [row] = rows([player(1, seasontype=4)], season_type="playoffs")
    assert row["season_type"] == "playoffs"


def test_no_page_data_fails():
    with pytest.raises(ValueError, match="no `data:{stats:\\["):
        d.page_data("<html>Not authorized</html>")


def test_missing_required_column_fails():
    with pytest.raises(ValueError, match=r"no \['tot'\] column"):
        rows([player(1)], keys=[k for k in KEYS if k != "tot"])


def test_missing_key_value_fails():
    with pytest.raises(ValueError, match=r"row with no \['player_id'\]"):
        rows([player(None)])


def test_wrong_season_type_fails():
    with pytest.raises(ValueError, match="page is for seasontype 2, expected 4"):
        d.epm_rows(d.page_data(page([player(1)])), "playoffs")


def test_row_from_another_season_fails():
    with pytest.raises(ValueError, match="row for season 2025"):
        rows([player(1, season=2025)])


def test_non_number_fails():
    with pytest.raises(ValueError, match="off 'x' isn't a number"):
        rows([player(1, off="x")])


def test_too_many_rows_fails():
    with pytest.raises(ValueError, match="1001 rows"):
        rows([player(i) for i in range(d.MAX_ROWS + 1)])


def test_players_who_didnt_play_are_dropped():
    assert [r["player_id"] for r in d.played(rows([player(1), player(2, gp=0)]))] == [
        "1"
    ]


def read(path):
    return duckdb.sql(
        f"SELECT * FROM read_parquet('{path}', hive_partitioning = false) ORDER BY ALL"
    )


def test_build_epm(tmp_path):
    pages = {
        "regular_season": d.page_data(page([player(1), player(2, gp=0)])),
        "playoffs": d.page_data(page([player(1, seasontype=4)], seasontype=4)),
    }
    path = d.build_epm(tmp_path, SEASON, pages)
    assert path == dataset_path(tmp_path / d.OUT_DIR, "epm", SEASON)
    assert read(path).select("season_type, player_id").fetchall() == [
        ("playoffs", "1"),
        ("regular_season", "1"),
    ]
    rel = read(path)
    schema = {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
    assert schema["season"] == "INTEGER"
    assert schema["player_id"] == "VARCHAR"
    assert schema["gp"] == "INTEGER"
    assert schema["epm"] == "DOUBLE"


def test_nothing_written_before_anyone_plays(tmp_path):
    pages = {"regular_season": d.page_data(page([player(1, gp=0)]))}
    assert d.build_epm(tmp_path, SEASON, pages) is None
    assert not (tmp_path / "nba").exists()


def predicted(player_id, season=SEASON, **kw):
    row = {
        "season": season, "game_dt": "2026-06-13", "player_id": player_id,
        "player_name": f"Player {player_id}", "team_id": 1610612738,
        "team_alias": "BOS", "age": 25, "inches": "80", "position": "F-C",
        "off": 1.5, "def": -0.25, "tot": 1.25, "tot_change": None, "p_mp_48": 30.5,
        "p_usg": 0.2, "p_mp_48_rk": 4, "p_mp_48_z": 1.5,
    }  # fmt: skip
    row.update(kw)
    return row


def predictive_page(players, season=SEASON, as_of="2026-06-13"):
    data = {
        "date": as_of,
        "stats": players,
        "season": season,
        "seasons": [season, season - 1],
        "has_access": 1,
    }
    return (
        "<script>kit.start(app, element, {node_ids: [0, 2], data: ["
        '{type:"data",data:{user:void 0},uses:{}},'
        f'{{type:"data",data:{js(data)},uses:{{search_params:["date"]}}}}],'
        "form: null});</script>"
    )


def predictive_rows(players, **kw):
    return d.predictive_rows(d.predictive_data(predictive_page(players, **kw)))


def test_predictive_rows():
    [row] = predictive_rows([predicted(1629027)])
    assert row == {
        **{name: None for name, _ in d.PREDICTIVE_COLUMNS.values()},
        "season": SEASON,
        "as_of_date": date(2026, 6, 13),
        "player_id": "1629027",
        "team_id": "1610612738",
        "age": 25,
        # the page has heights as strings
        "height_inches": 80,
        "position": "F-C",
        "o_epm": 1.5,
        "d_epm": -0.25,
        "epm": 1.25,
        "min_per_48": 30.5,
        "usg_pct": 0.2,
    }


def test_no_predictive_data_fails():
    with pytest.raises(ValueError, match='no `{type:"data",data:{date`'):
        d.predictive_data(page([player(1)]))


def test_predictive_bad_date_fails():
    with pytest.raises(ValueError, match="predictive date 'yesterday'"):
        predictive_rows([predicted(1)], as_of="yesterday")


def test_predictive_missing_required_column_fails():
    row = predicted(1)
    del row["p_mp_48"]
    with pytest.raises(ValueError, match=r"no \['p_mp_48'\] column"):
        predictive_rows([row])


def test_predictive_missing_key_value_fails():
    with pytest.raises(ValueError, match=r"row with no \['team_id'\]"):
        predictive_rows([predicted(1, team_id=None)])


def test_predictive_row_from_another_season_fails():
    with pytest.raises(ValueError, match="row for season 2025"):
        predictive_rows([predicted(1, season=2025)])


def test_predictive_malformed_row_fails():
    data = d.predictive_data(predictive_page([predicted(1)]))
    data["stats"].append([1, 2])
    with pytest.raises(ValueError, match="malformed row"):
        d.predictive_rows(data)


def test_build_predictive_keeps_players_who_played(tmp_path):
    data = d.predictive_data(predictive_page([predicted(i) for i in (1, 2, 3)]))
    path = d.build_predictive(tmp_path, SEASON, data, {"1", "2"})
    assert path == dataset_path(tmp_path / d.OUT_DIR, "epm_predictive", SEASON)
    rel = read(path)
    assert rel.select("player_id").fetchall() == [("1",), ("2",)]
    schema = {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
    assert schema["as_of_date"] == "DATE"
    assert schema["height_inches"] == "INTEGER"


def test_build_predictive_with_few_known_players_fails(tmp_path):
    data = d.predictive_data(predictive_page([predicted(i) for i in (1, 2, 3)]))
    with pytest.raises(ValueError, match="only 1 of 3 players are in player_seasons"):
        d.build_predictive(tmp_path, SEASON, data, {"1", "7", "8"})
    assert not (tmp_path / "nba").exists()


def write_player_seasons(outdir, player_ids, season=SEASON):
    path = dataset_path(outdir / d.STATS_DIR, "player_seasons", season)
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = ", ".join(f"('{i}')" for i in player_ids)
    duckdb.sql(
        f"COPY (SELECT {season} AS season, i AS player_id FROM (VALUES {ids}) v(i)) "
        f"TO '{path}' (FORMAT parquet)"
    )


def test_known_players_reads_out_first(tmp_path):
    out, bucket = tmp_path / "out", tmp_path / "bucket"
    write_player_seasons(bucket, ["1", "2"])
    assert d.known_players(out, SEASON, str(bucket)) == {"1", "2"}
    write_player_seasons(out, ["3"])
    assert d.known_players(out, SEASON, str(bucket)) == {"3"}


def test_build_predictive_before_anyone_plays(tmp_path):
    data = d.predictive_data(predictive_page([predicted(1)]))
    assert d.build_predictive(tmp_path, SEASON, data, set()) is None
    assert not (tmp_path / "nba").exists()


def fake_fetch(monkeypatch, pages):
    monkeypatch.setattr(d, "fetch", lambda page: pages[page])


def season_pages(season=SEASON, playoff_season=SEASON):
    return {
        "regular_season": page(
            [player(1, season=season), player(2, gp=0, season=season)], season=season
        ),
        "playoffs": page(
            [player(1, seasontype=4, season=playoff_season)],
            seasontype=4,
            season=playoff_season,
        ),
    }


def test_main(tmp_path, monkeypatch):
    fake_fetch(
        monkeypatch,
        {
            **season_pages(),
            "predictive": predictive_page([predicted(i) for i in (1, 2, 3)]),
        },
    )
    # 2 is rostered but hasn't played; 3 isn't in player_seasons
    write_player_seasons(tmp_path, ["1", "2"])
    d.main(["--out", str(tmp_path)])
    epm = dataset_path(tmp_path / d.OUT_DIR, "epm", SEASON)
    predictive = dataset_path(tmp_path / d.OUT_DIR, "epm_predictive", SEASON)
    assert read(epm).select("season_type").fetchall() == [
        ("playoffs",),
        ("regular_season",),
    ]
    assert read(predictive).select("player_id").fetchall() == [("1",), ("2",)]
    for season_type in d.SEASON_TYPES:
        assert d.raw_path(tmp_path, SEASON, season_type).is_file()
    assert d.predictive_raw_path(tmp_path, SEASON, "2026-06-13").is_file()

    # and the same from the saved pages, using the latest predictive page
    later = predictive_page([predicted(1, tot=5.0)], as_of="2026-06-14")
    d.save_raw(d.predictive_raw_path(tmp_path, SEASON, "2026-06-14"), later)
    epm.unlink()
    predictive.unlink()
    d.main(["--out", str(tmp_path), "--no-fetch"])
    assert len(read(epm).fetchall()) == 2
    assert read(predictive).select("as_of_date::VARCHAR, epm").fetchall() == [
        ("2026-06-14", 5.0)
    ]


def test_main_skips_last_seasons_pages(tmp_path, monkeypatch):
    fake_fetch(
        monkeypatch,
        {
            **season_pages(season=2027, playoff_season=2026),
            "predictive": predictive_page([predicted(1)]),
        },
    )
    d.main(["--out", str(tmp_path)])
    path = dataset_path(tmp_path / d.OUT_DIR, "epm", 2027)
    assert read(path).select("season_type").fetchall() == [("regular_season",)]
    # the 2026 pages are kept, but their files aren't rewritten without
    # their regular season
    assert d.raw_path(tmp_path, 2026, "playoffs").is_file()
    assert d.predictive_raw_path(tmp_path, 2026, "2026-06-13").is_file()
    assert not dataset_path(tmp_path / d.OUT_DIR, "epm", 2026).exists()
    assert not (tmp_path / d.OUT_DIR / "epm_predictive").exists()


def test_main_keeps_a_page_it_cant_parse(tmp_path, monkeypatch):
    fake_fetch(monkeypatch, {"regular_season": "<html>changed</html>"})
    with pytest.raises(ValueError, match="no `data:{stats:"):
        d.main(["--out", str(tmp_path)])
    [raw] = (tmp_path / d.RAW_DIR).glob("unparsed-*/epm_regular_season.html.gz")
    assert raw.is_file()
    assert not (tmp_path / d.OUT_DIR).exists()


def test_a_bad_predictive_page_still_writes_epm(tmp_path, monkeypatch):
    fake_fetch(monkeypatch, {**season_pages(), "predictive": "<html>changed</html>"})
    with pytest.raises(ValueError, match="no `{type"):
        d.main(["--out", str(tmp_path)])
    assert dataset_path(tmp_path / d.OUT_DIR, "epm", SEASON).is_file()
    [raw] = (tmp_path / d.RAW_DIR).glob("unparsed-*/epm_predictive.html.gz")
    assert raw.is_file()
    assert not (tmp_path / d.OUT_DIR / "epm_predictive").exists()
