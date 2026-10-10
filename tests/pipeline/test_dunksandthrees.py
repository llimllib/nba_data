import math

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
        f"SELECT * FROM read_parquet('{path}', hive_partitioning = false) "
        "ORDER BY season_type, player_id"
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


def fake_fetch(monkeypatch, pages):
    monkeypatch.setattr(d, "fetch", lambda season_type: pages[season_type])


def test_main(tmp_path, monkeypatch):
    fake_fetch(
        monkeypatch,
        {
            "regular_season": page([player(1)]),
            "playoffs": page([player(1, seasontype=4)], seasontype=4),
        },
    )
    d.main(["--out", str(tmp_path)])
    path = dataset_path(tmp_path / d.OUT_DIR, "epm", SEASON)
    assert read(path).select("season_type").fetchall() == [
        ("playoffs",),
        ("regular_season",),
    ]
    for season_type in d.SEASON_TYPES:
        assert d.raw_path(tmp_path, SEASON, season_type).is_file()

    # and the same from the saved pages
    path.unlink()
    d.main(["--out", str(tmp_path), "--no-fetch"])
    assert len(read(path).fetchall()) == 2


def test_main_skips_last_seasons_playoffs(tmp_path, monkeypatch):
    fake_fetch(
        monkeypatch,
        {
            "regular_season": page([player(1, season=2027)], season=2027),
            "playoffs": page([player(1, seasontype=4)], seasontype=4),
        },
    )
    d.main(["--out", str(tmp_path)])
    path = dataset_path(tmp_path / d.OUT_DIR, "epm", 2027)
    assert read(path).select("season_type").fetchall() == [("regular_season",)]
    # the 2026 playoffs page is kept, but its file isn't rewritten without
    # its regular season
    assert d.raw_path(tmp_path, 2026, "playoffs").is_file()
    assert not dataset_path(tmp_path / d.OUT_DIR, "epm", 2026).exists()


def test_main_keeps_a_page_it_cant_parse(tmp_path, monkeypatch):
    fake_fetch(monkeypatch, {"regular_season": "<html>changed</html>"})
    with pytest.raises(ValueError, match="no `data:{stats:"):
        d.main(["--out", str(tmp_path)])
    [raw] = (tmp_path / d.RAW_DIR).glob("unparsed-*/epm_regular_season.html.gz")
    assert raw.is_file()
    assert not (tmp_path / d.OUT_DIR).exists()
