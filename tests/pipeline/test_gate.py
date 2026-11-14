from datetime import UTC, datetime

import duckdb

from pipeline import gate, stats
from pipeline.output import dataset_path

NOWHERE = "http://nowhere.invalid"
# 11pm Eastern on November 13, 2026: season 2027
NOW = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)


def write(outdir, dataset, sql, season=2027):
    path = dataset_path(outdir / stats.OUT_DIR, dataset, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")


def schedule(outdir, games):
    """games: [(game_id, start time, status_text)]"""
    rows = ", ".join(f"('{g}', TIMESTAMPTZ '{t}', '{s}')" for g, t, s in games)
    write(
        outdir,
        "schedule",
        f"SELECT * FROM (VALUES {rows}) t(game_id, start_time, status_text)",
    )


def games(outdir, ids, game_date="2026-11-13"):
    rows = ", ".join(f"('{g}', DATE '{game_date}')" for g in ids) or "('x', NULL)"
    write(
        outdir,
        "games",
        f"SELECT * FROM (VALUES {rows}) t(game_id, game_date) WHERE game_date IS NOT NULL",
    )


def events(outdir, ids):
    rows = ", ".join(f"('{g}', {i})" for i, g in enumerate(ids)) or "('x', 0)"
    write(
        outdir,
        "game_events",
        f"SELECT * FROM (VALUES {rows}) t(game_id, action_id) WHERE game_id <> 'x'",
    )


def test_games_over_but_not_in_games(tmp_path):
    schedule(
        tmp_path,
        [
            # over and in games
            ("0022600001", "2026-11-14 00:00:00+00", "Final"),
            # over and not in games
            ("0022600002", "2026-11-14 01:00:00+00", "Final"),
            # still on: tipped 1.5 hours ago
            ("0022600003", "2026-11-14 02:30:00+00", "Q3"),
            # postponed
            ("0022600004", "2026-11-14 00:00:00+00", "PPD"),
            # a day ago: too old to wait for
            ("0022600005", "2026-11-13 00:00:00+00", "Final"),
        ],
    )
    games(tmp_path, ["0022600001"])
    events(tmp_path, ["0022600001"])
    assert gate.work(tmp_path, NOW, NOWHERE) == [
        "games over but not in games: 0022600002"
    ]


def test_games_without_play_by_play(tmp_path):
    schedule(tmp_path, [("0022600001", "2026-11-14 00:00:00+00", "Final")])
    games(tmp_path, ["0022600001"])
    events(tmp_path, [])
    assert gate.work(tmp_path, NOW, NOWHERE) == [
        "games without play-by-play: 0022600001"
    ]


def test_nothing_to_do(tmp_path):
    schedule(tmp_path, [("0022600001", "2026-11-14 00:00:00+00", "Final")])
    games(tmp_path, ["0022600001"])
    events(tmp_path, ["0022600001"])
    assert gate.work(tmp_path, NOW, NOWHERE) == []


def test_no_schedule_yet_is_nothing_to_do(tmp_path, monkeypatch):
    # the bucket answers 403 for a file that isn't there
    def forbidden(*a, **kw):
        raise duckdb.HTTPException("HTTP Error: HTTP GET error (HTTP 403 Forbidden)")

    monkeypatch.setattr(gate, "read_stats", forbidden)
    assert gate.work(tmp_path, NOW, NOWHERE) == []


def test_an_unreadable_bucket_means_update(tmp_path, monkeypatch):
    def broken(*a, **kw):
        raise duckdb.IOException("connection reset")

    monkeypatch.setattr(gate, "read_stats", broken)
    (reason,) = gate.work(tmp_path, NOW, NOWHERE)
    assert reason.startswith("couldn't read the bucket")


def test_main_writes_the_output(tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(gate, "work", lambda *a: [])
    gate.main(["--out", str(tmp_path)])
    monkeypatch.setattr(gate, "work", lambda *a: ["games over"])
    gate.main(["--out", str(tmp_path)])
    assert out.read_text() == "run=false\nrun=true\n"
