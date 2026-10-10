import duckdb
import pytest

from pipeline import bbref
from pipeline.output import dataset_path
from pipeline.teams import load_team_abbrevs

SEASON = 2026
TEAM_IDS = {
    a.abbrev: a.team_id
    for a in load_team_abbrevs()
    if a.source == "bbref" and a.covers(SEASON)
}
ABBREVS = sorted(TEAM_IDS)


def value(stat, i):
    if stat == "arena_name":
        return f"Arena {i}"
    if stat in ("attendance", "attendance_per_g"):
        return "764,842"
    if stat == "g":
        return "82"
    return str(100 + i)


def team_row(abbrev, i, stats, playoff=False):
    name = f"Team {abbrev}{'*' if playoff else ''}"
    cells = "".join(f'<td data-stat="{s}" >{value(s, i)}</td>' for s in stats)
    return (
        f'<tr ><th data-stat="ranker" >{i}</th>'
        f'<td data-stat="team" ><a href="/teams/{abbrev}/{SEASON}.html">{name}</a></td>{cells}</tr>'
    )


def league_page(drop=(), teams=ABBREVS):
    tables = []
    for table_id, stats in bbref.TEAM_TABLES.items():
        stats = [s for s in stats if s not in drop]
        rows = [team_row(a, i, stats, playoff=i < 16) for i, a in enumerate(teams)]
        rows.append(
            '<tr><th data-stat="ranker"></th><td data-stat="team">League Average</td></tr>'
        )
        tables.append(f'<table id="{table_id}"><tbody>{"".join(rows)}</tbody></table>')
    # bbref hides some tables in comments
    return f"<html>{''.join(tables[:2])}<!-- {''.join(tables[2:])} --></html>"


def test_team_stats():
    rows = bbref.team_stats(league_page(), SEASON, TEAM_IDS)
    assert len(rows) == 30
    first = next(r for r in rows if r["team_id"] == TEAM_IDS[ABBREVS[0]])
    # bbref's names become the NBA's
    assert first["fgm"] == 100 and first["reb"] == 100 and first["opp_fgm"] == 100
    assert first["min"] == 100.0
    assert first["attendance"] == 764842
    assert first["arena_name"] == "Arena 0"
    assert first["made_playoffs"] is True
    assert sum(r["made_playoffs"] for r in rows) == 16


def test_missing_optional_column_is_null(capsys):
    rows = bbref.team_stats(league_page(drop={"avg_dist"}), SEASON, TEAM_IDS)
    assert all(r["avg_dist"] is None for r in rows)
    assert "avg_dist" in capsys.readouterr().out


def test_missing_required_column_fails():
    with pytest.raises(ValueError, match="no pts column"):
        bbref.team_stats(league_page(drop={"pts"}), SEASON, TEAM_IDS)


def test_missing_team_fails():
    with pytest.raises(ValueError, match="29 team rows"):
        bbref.team_stats(league_page(teams=ABBREVS[:29]), SEASON, TEAM_IDS)


def test_unknown_team_fails():
    with pytest.raises(ValueError, match="no team_id for XXX"):
        bbref.team_stats(league_page(teams=[*ABBREVS[:29], "XXX"]), SEASON, TEAM_IDS)


def test_non_number_fails():
    html = league_page().replace('data-stat="srs" >100<', 'data-stat="srs" >N/A<', 1)
    with pytest.raises(ValueError, match="srs: 'N/A'"):
        bbref.team_stats(html, SEASON, TEAM_IDS)


def test_build_team_stats(tmp_path):
    path = bbref.build_team_stats(tmp_path, league_page(), SEASON)
    assert path == dataset_path(tmp_path / bbref.OUT_DIR, "bbref_team_stats", SEASON)
    types = dict(
        duckdb.sql(
            f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM "
            f"read_parquet('{path}', hive_partitioning = false))"
        ).fetchall()
    )
    assert list(types)[:3] == ["season", "team_id", "made_playoffs"]
    assert types["season"] == "INTEGER" and types["team_id"] == "VARCHAR"
    assert types["pts"] == "INTEGER" and types["srs"] == "DOUBLE"
    assert "BIGINT" not in types.values()
    assert not any(c[0].isdigit() for c in types)


def test_bad_page_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        bbref.build_team_stats(tmp_path, league_page(drop={"pts"}), SEASON)
    assert not (tmp_path / bbref.OUT_DIR).exists()
