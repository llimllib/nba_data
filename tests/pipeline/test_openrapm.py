import gzip
import json
from datetime import date

import duckdb
import pytest

from pipeline import openrapm as o
from pipeline.output import dataset_path

BUILT = "2026-10-25"


@pytest.fixture(autouse=True)
def few_rows(monkeypatch):
    monkeypatch.setattr(o, "MIN_ROWS", 1)


def rating(season, player_id, off=1.5, def_=0.25, poss=1000):
    return {"s": season, "n": f"Player {player_id}", "o": off, "d": def_,
            "t": off + def_, "p": poss, "id": player_id}  # fmt: skip


def ratings(*rows, built=BUILT):
    return {"meta": {"built": built, "seasons": sorted({r["s"] for r in rows})},
            "rows": list(rows)}  # fmt: skip


def split(total):
    """five pieces that add up to `total`, then `total`"""
    return [total - 0.5, 0.25, 0.25, 0.0, 0.0, total]


def decompose(*rows, built=BUILT):
    by_season = {}
    for r in rows:
        by_season.setdefault(str(r["s"]), []).append(
            [
                r["n"],
                r["id"],
                r["p"],
                split(r["o"]),
                split(r["d"]),
                split(r["t"]),
                "BOS",
            ]
        )
    return {
        "meta": {"built": built, "fields": o.DECOMPOSE_FIELDS,
                 "pieces": [*o.PIECES, "rating"]},
        "rows": by_season,
    }  # fmt: skip


def known(players):
    """a `known` for build: season -> player ids"""
    return lambda outdir, season: players.get(season)


def read(path):
    return duckdb.sql(
        f"SELECT * FROM read_parquet('{path}', hive_partitioning = false) "
        "ORDER BY player_id"
    ).fetchall()


def test_rating_rows():
    rows = o.rating_rows(ratings(rating(2026, 1641705, 3.02, 5.84, 5375)))
    assert rows == [
        {"season": 2026, "player_id": "1641705", "built": date(2026, 10, 25),
         "possessions": 5375, "o_rating": 3.02, "d_rating": 5.84, "rating": 8.86},
    ]  # fmt: skip


@pytest.mark.parametrize(
    "doc",
    [
        {"meta": {}, "rows": [rating(2026, 1)]},
        {"meta": {"built": "yesterday"}, "rows": [rating(2026, 1)]},
        {"meta": {"built": BUILT}, "rows": {}},
        ratings({"s": 2026, "id": 1}),
        ratings(rating(2026, 1) | {"o": "1.5"}),
        ratings(rating(2026, 1.5)),
    ],
)
def test_bad_ratings_fail(doc):
    with pytest.raises(ValueError):
        o.rating_rows(doc)


def test_too_few_rows_fail(monkeypatch):
    monkeypatch.setattr(o, "MIN_ROWS", 2)
    with pytest.raises(ValueError, match="at least 2 rows"):
        o.rating_rows(ratings(rating(2026, 1)))


def test_pieces():
    rows = o.with_pieces(o.rating_rows(ratings(r := rating(2026, 1))), decompose(r))
    assert rows[0]["o_box_score"] == 1.0
    assert rows[0]["o_on_court"] == 0.25
    assert rows[0]["d_box_score"] == -0.25
    assert rows[0]["d_swap"] == 0.0


def test_no_decomposition_leaves_pieces_null():
    rows = o.with_pieces(o.rating_rows(ratings(rating(2026, 1))), None)
    assert rows[0]["o_box_score"] is None
    assert rows[0]["o_rating"] == 1.5


def test_decomposition_of_another_version_leaves_pieces_null():
    r = rating(2026, 1)
    rows = o.with_pieces(o.rating_rows(ratings(r)), decompose(r, built="2026-10-24"))
    assert rows[0]["o_box_score"] is None


def test_decomposition_that_disagrees_leaves_that_row_null():
    a, b = rating(2026, 1), rating(2026, 2)
    rows = o.with_pieces(
        o.rating_rows(ratings(a, b)), decompose(a, rating(2026, 2, off=0.5))
    )
    assert rows[0]["o_box_score"] == 1.0
    assert rows[1]["o_box_score"] is None


def test_pieces_that_dont_add_up_fail():
    doc = decompose(r := rating(2026, 1))
    doc["rows"]["2026"][0][3] = [1.0, 1.0, 1.0, 0.0, 0.0, 1.5]
    with pytest.raises(ValueError, match="don't add up"):
        o.with_pieces(o.rating_rows(ratings(r)), doc)


def test_changed_decomposition_fields_fail():
    doc = decompose(r := rating(2026, 1))
    doc["meta"]["pieces"] = ["box_score", "rating"]
    with pytest.raises(ValueError, match="pieces"):
        o.with_pieces(o.rating_rows(ratings(r)), doc)


def test_build(tmp_path):
    rows = [rating(2009, 1), rating(2026, 1), rating(2026, 2), rating(2027, 1)]
    paths = o.build(
        tmp_path,
        ratings(*rows),
        decompose(*rows),
        known({2026: {"1", "2"}, 2027: {"1"}}),
    )
    # 2009 is before FIRST_SEASON
    assert paths == [
        dataset_path(tmp_path / o.OUT_DIR, "openrapm", 2026),
        dataset_path(tmp_path / o.OUT_DIR, "openrapm", 2027),
    ]
    got = read(paths[0])
    assert [r[:4] for r in got] == [
        (2026, "1", date(2026, 10, 25), 1000),
        (2026, "2", date(2026, 10, 25), 1000),
    ]
    cols = [c for c, _ in o.COLUMNS]
    assert dict(zip(cols, got[0]))["o_box_score"] == 1.0


def test_build_drops_a_few_unknown_players(tmp_path, monkeypatch):
    monkeypatch.setattr(o, "MAX_DROPPED", 0.5)
    rows = [rating(2026, 1), rating(2026, 2)]
    [path] = o.build(tmp_path, ratings(*rows), None, known({2026: {"1"}}))
    assert [r[1] for r in read(path)] == ["1"]


def test_build_with_many_unknown_players_writes_nothing(tmp_path):
    rows = [rating(2026, 1), rating(2027, 1), rating(2027, 2)]
    with pytest.raises(ValueError, match="aren't in player_seasons"):
        o.build(tmp_path, ratings(*rows), None, known({2026: {"1"}, 2027: {"1"}}))
    assert not (tmp_path / "nba").exists()


def test_build_skips_seasons_without_player_seasons(tmp_path):
    rows = [rating(2026, 1), rating(2027, 1)]
    paths = o.build(tmp_path, ratings(*rows), None, known({2026: {"1"}}))
    assert paths == [dataset_path(tmp_path / o.OUT_DIR, "openrapm", 2026)]


def fake_fetch(monkeypatch, docs):
    fetched = []

    def fetch(name):
        fetched.append(name)
        return json.dumps(docs[name])

    monkeypatch.setattr(o, "fetch", fetch)
    return fetched


@pytest.fixture
def no_bucket(monkeypatch):
    """published_built reads only out/, never the bucket"""
    published_built = o.published_built

    def local(outdir, season):
        path = dataset_path(outdir / o.OUT_DIR, "openrapm", season)
        return published_built(outdir, season) if path.is_file() else None

    monkeypatch.setattr(o, "published_built", local)


def test_update(tmp_path, monkeypatch, no_bucket):
    rows = [rating(2026, 1)]
    fetched = fake_fetch(
        monkeypatch, {"ratings": ratings(*rows), "decompose_openrapm": decompose(*rows)}
    )
    monkeypatch.setattr(o, "known_players", lambda outdir, season: {"1"})
    o.update(tmp_path)
    assert fetched == ["ratings", "decompose_openrapm"]
    for name in o.FILES:
        assert o.raw_path(tmp_path, BUILT, name).is_file()
    assert o.published_built(tmp_path, 2026) == date(2026, 10, 25)

    # the same version again: nothing more is fetched or written
    fetched.clear()
    o.update(tmp_path)
    assert fetched == ["ratings"]


def test_update_without_decomposition(tmp_path, monkeypatch, no_bucket):
    def fetch(name):
        if name == "decompose_openrapm":
            raise OSError("404")
        return json.dumps(ratings(rating(2026, 1)))

    monkeypatch.setattr(o, "fetch", fetch)
    monkeypatch.setattr(o, "known_players", lambda outdir, season: {"1"})
    o.update(tmp_path)
    [row] = read(dataset_path(tmp_path / o.OUT_DIR, "openrapm", 2026))
    assert row[4] == 1.5
    assert row[7] is None


def test_published_built_reads_out_first(tmp_path):
    out, bucket = tmp_path / "out", tmp_path / "bucket"
    for outdir, built in ((out, BUILT), (bucket, "2026-10-24")):
        o.build(
            outdir, ratings(rating(2026, 1), built=built), None, known({2026: {"1"}})
        )
    assert o.published_built(out, 2026, url=str(bucket)) == date(2026, 10, 25)
    assert o.published_built(tmp_path / "empty", 2026, url=str(bucket)) == date(
        2026, 10, 24
    )


def test_no_fetch_rebuilds_from_the_newest_version(tmp_path, monkeypatch):
    old, new = [rating(2026, 1, off=0.5)], [rating(2026, 1)]
    for built, rows in (("2026-10-24", old), (BUILT, new)):
        doc = ratings(*rows, built=built)
        path = o.raw_path(tmp_path, built, "ratings")
        path.parent.mkdir(parents=True)
        with gzip.open(path, "wt") as f:
            json.dump(doc, f)
    monkeypatch.setattr(o, "known_players", lambda outdir, season: {"1"})
    o.main(["--out", str(tmp_path), "--no-fetch"])
    [row] = read(dataset_path(tmp_path / o.OUT_DIR, "openrapm", 2026))
    assert row[2] == date(2026, 10, 25)
    assert row[4] == 1.5


def test_fetch_retries(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b'{"meta": {}}'

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise o.http.client.IncompleteRead(b"", 10)
        return Response()

    monkeypatch.setattr(o.urllib.request, "urlopen", urlopen)
    slept = []
    assert o.fetch("ratings", sleep=slept.append) == '{"meta": {}}'
    assert slept == o.RETRY_DELAYS
    assert calls == [f"{o.BASE_URL}/ratings.json"] * 3
