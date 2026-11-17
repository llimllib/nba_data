import urllib.error
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path

import duckdb
import pytest

from pipeline import injury_report as ir
from pipeline import stats
from pipeline.output import DROPPED_ENV, dataset_path
from pipeline.seasons import EASTERN

FIXTURES = Path(__file__).parent.parent / "fixtures" / "injury_report"
R2019 = FIXTURES / "Injury-Report_2019-01-10_05PM.pdf"
R2018 = FIXTURES / "Injury-Report_2018-12-22_05PM.pdf"
R2025 = FIXTURES / "Injury-Report_2025-01-10_06PM.pdf"
R2027 = FIXTURES / "Injury-Report_2026-11-16_06_00PM.pdf"

CHI, CLE, POR, MIN = "1610612741", "1610612739", "1610612757", "1610612750"


def et(*args) -> datetime:
    return datetime(*args, tzinfo=EASTERN)


def names(slots) -> list[str]:
    return [s.name for s in slots]


# --- slots


def test_slots_in_2019_are_three_a_day():
    slots = ir.slots_before(et(2019, 1, 10, 19, 0))
    assert names(list(slots)[:4]) == [
        "2019-01-10_05PM",
        "2019-01-10_01PM",
        "2019-01-09_08PM",
        "2019-01-09_05PM",
    ]


def test_hourly_slots_are_published_at_half_past():
    first = next(ir.slots_before(et(2025, 1, 10, 19, 0)))
    assert (first.name, first.published) == ("2025-01-10_06PM", et(2025, 1, 10, 18, 30))
    # a 7:30 tip-off's report is still 6:30's
    assert next(ir.slots_before(et(2025, 1, 10, 19, 30))).name == "2025-01-10_06PM"
    assert next(ir.slots_before(et(2025, 1, 10, 19, 31))).name == "2025-01-10_07PM"
    assert next(ir.slots_before(et(2025, 1, 10, 1, 0))).name == "2025-01-10_12AM"


def test_slots_every_15_minutes_since_december_2025():
    slots = list(ir.slots_before(et(2026, 11, 16, 19, 0)))[:2]
    assert names(slots) == ["2026-11-16_06_45PM", "2026-11-16_06_30PM"]
    assert next(ir.slots_before(et(2026, 11, 16, 12, 10))).name == "2026-11-16_12_00PM"
    # the changeover
    assert names(list(ir.slots_before(et(2025, 12, 22, 0, 10)))[:2]) == [
        "2025-12-22_12_00AM",
        "2025-12-21_11PM",
    ]


def test_no_slots_before_the_first_report():
    assert names(ir.slots_before(et(2018, 12, 17, 19, 0))) == ["2018-12-17_05PM"]
    assert not list(ir.slots_before(et(2018, 12, 1, 19, 0)))


def test_slot_season():
    assert next(ir.slots_before(et(2026, 10, 20, 19, 0))).season == 2027
    assert next(ir.slots_before(et(2026, 6, 10, 20, 0))).season == 2026


# --- fetching


class FakeCDN:
    """serves `pdfs`, slot name -> body; anything else is missing"""

    def __init__(self, pdfs):
        self.pdfs = pdfs
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        name = url.removeprefix(ir.BASE_URL + "Injury-Report_").removesuffix(".pdf")
        return self.pdfs.get(name)

    def json(self, url):
        raise AssertionError(url)


def test_before_steps_back_past_missing_slots(tmp_path):
    cdn = FakeCDN({"2026-11-16_06_15PM": b"%PDF-1.4 a"})
    reports = ir.Reports(tmp_path, cdn, datetime(2026, 11, 18, tzinfo=UTC))
    path = reports.before(et(2026, 11, 16, 19, 0))
    assert path == tmp_path / (
        "nba/raw/stats/2027/injury_report/Injury-Report_2026-11-16_06_15PM.pdf"
    )
    assert path and path.read_bytes() == b"%PDF-1.4 a"
    assert len(cdn.urls) == 3
    # the missing slots are recorded, so they aren't asked for again
    missing = (tmp_path / "nba/raw/stats/2027/injury_report/missing.txt").read_text()
    assert missing == "2026-11-16_06_30PM\n2026-11-16_06_45PM\n"
    again = ir.Reports(tmp_path, cdn, datetime(2026, 11, 18, tzinfo=UTC))
    assert again.before(et(2026, 11, 16, 19, 0)) == path
    assert len(cdn.urls) == 3


def test_a_recent_slot_isnt_recorded_missing(tmp_path):
    cdn = FakeCDN({"2026-11-16_06_30PM": b"%PDF-1.4 a"})
    now = et(2026, 11, 16, 18, 50)
    reports = ir.Reports(tmp_path, cdn, now)
    path = reports.before(now)
    assert path and path.name.endswith("06_30PM.pdf")
    assert not reports.missing_path(2027).exists()


def test_slots_not_published_yet_arent_fetched(tmp_path):
    cdn = FakeCDN({})
    reports = ir.Reports(tmp_path, cdn, et(2026, 11, 16, 17, 0))
    assert reports.before(et(2026, 11, 16, 19, 0)) is None
    assert cdn.urls == []


def test_a_response_thats_not_a_pdf_fails(tmp_path):
    cdn = FakeCDN({"2026-11-16_06_45PM": b"<html>"})
    reports = ir.Reports(tmp_path, cdn, datetime(2026, 11, 17, tzinfo=UTC))
    with pytest.raises(ValueError, match="isn't a PDF"):
        reports.before(et(2026, 11, 16, 19, 0))


def test_fetcher_treats_403_as_missing(monkeypatch):
    def forbidden(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    fetcher = ir.Fetcher(delay=0, not_found=(403, 404))
    assert fetcher.get(ir.BASE_URL + "x.pdf") is None


# --- parsing


def by_player(report):
    return {r.player: r for r in report.rows if r.player}


def test_parse_2019_layout():
    rep = ir.parse_report(R2019)
    assert rep.time == et(2019, 1, 10, 17, 30)
    assert len(rep.rows) == 100
    rows = by_player(rep)
    # a reason wrapped above and below its row
    baynes = rows["Baynes, Aron"]
    assert (baynes.game_date, baynes.matchup, baynes.team) == (
        date(2019, 1, 10),
        "BOS@MIA",
        "Boston Celtics",
    )
    assert (baynes.status, baynes.reason, baynes.detail) == (
        "Out",
        "Injury/Illness",
        "Left Fourth Metacarpal Fracture",
    )
    # the team carried down, and "-" is no detail
    bird = rows["Bird, Jabari"]
    assert (bird.team, bird.reason, bird.detail) == (
        "Boston Celtics",
        "Personal Reasons",
        None,
    )
    assert rows["Harris, Gary"].status == "Questionable"
    # the next day's games are listed too, and teams that haven't reported
    missing = [(r.game_date, r.team) for r in rep.rows if r.player is None]
    assert missing == [
        (date(2019, 1, 11), "Los Angeles Lakers"),
        (date(2019, 1, 11), "Utah Jazz"),
    ]


def test_parse_2025_layout():
    rep = ir.parse_report(R2025)
    assert rep.time == et(2025, 1, 10, 18, 30)
    rows = by_player(rep)
    curry = rows["Curry, Stephen"]
    assert (curry.matchup, curry.status, curry.reason, curry.detail) == (
        "GSW@IND",
        "Out",
        "Injury/Illness",
        "Bilateral Knee; Injury Management",
    )
    # page 2 has no header, and its first row's reason starts above it
    wiseman = rows["Wiseman, James"]
    assert (wiseman.team, wiseman.detail) == (
        "Indiana Pacers",
        "Left Achilles Tendon; Tear",
    )
    assert (rows["Jackson, Quenton"].reason, rows["Jackson, Quenton"].detail) == (
        "G League",
        "Two-Way",
    )
    assert rows["Williamson, Zion"].reason == "Team Suspension"
    # the footer isn't part of the page's last row
    assert rows["Turner, Myles"].detail == "N/A; Illness"
    assert all("Page" not in (r.team or "") for r in rep.rows)


def test_parse_2027_layout():
    rep = ir.parse_report(R2027)
    assert rep.time == et(2026, 11, 16, 18, 0)
    rows = by_player(rep)
    assert rows["Smith, Jalen"].detail == "Left Finger Sprain"
    # a reason continued at the top of the next page
    assert rows["Jackson, GG"].detail == "Left 5th Metatarsal Fracture"
    assert rows["Jerome, Ty"].detail == "Right Hip Soreness"
    assert rows["Smart, Marcus"].reason == "League Suspension"
    # a reason of "-"
    assert (rows["Diop, Khalifa"].reason, rows["Diop, Khalifa"].detail) == (
        None,
        None,
    )
    assert rows["Udeh, Jr., Ernest"].team == "Cleveland Cavaliers"


def test_parse_teams_with_no_players():
    rep = ir.parse_report(R2018)
    notes = {(r.matchup, r.team): r.note for r in rep.rows}
    assert notes[("DAL@POR", "Dallas Mavericks")] == ir.NOT_SUBMITTED
    assert notes[("DAL@POR", "Portland Trail Blazers")] == ir.ALL_AVAILABLE
    assert notes[("DAL@GSW", "Dallas Mavericks")] is None


def test_join_words_rejoins_hyphenated_words():
    assert ir.join_words(["Left", "Tibia;", "Non-", "Displaced"]) == (
        "Left Tibia; Non-Displaced"
    )
    assert ir.join_words(["Injury/Illness", "-", "Left"]) == "Injury/Illness - Left"


def test_flip_name():
    assert ir.flip_name("Porter Jr., Michael") == "Michael Porter Jr."
    assert ir.flip_name("Udeh, Jr., Ernest") == "Ernest Udeh, Jr."
    key = ir.normalize_name
    assert key(ir.flip_name("Udeh, Jr., Ernest")) == key("Ernest Udeh Jr.")
    assert key(ir.flip_name("Mbah a Moute, Luc")) == "luc mbah a moute"


# --- building

CHI_CLE = "0022600200"
POR_MIN = "0022600210"
SEASON = 2027

CHI_PLAYERS = [
    ("1", "Tobe Awaka"),
    ("2", "Orlando Robinson"),
    ("3", "Jaylin Sellers"),
    ("4", "Jalen Smith"),
    ("5", "Caleb Wilson"),
]
CLE_PLAYERS = [
    ("11", "Khalifa Diop"),
    ("12", "Sam Merrill"),
    ("13", "Evan Mobley"),
    ("14", "Nae'Qwan Tomlin"),
    ("15", "Jaylon Tyson"),
    ("16", "Ernest Udeh Jr."),
    ("17", "Peyton Watson"),
]


def write(outdir, dataset, sql, season=SEASON):
    path = dataset_path(outdir / stats.OUT_DIR, dataset, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.default_connection().sql(sql).write_parquet(str(path))


def sql(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def values(rows) -> str:
    return ", ".join("(" + ", ".join(map(sql, r)) + ")" for r in rows)


@pytest.fixture
def season(tmp_path, monkeypatch):
    """a season with the 2026-11-16 report saved and the datasets it needs"""
    ids = tmp_path / "player_ids.csv"
    ids.write_text("source,source_id,player_id,method\n")
    monkeypatch.setattr(ir, "PLAYER_IDS_FILE", ids)
    monkeypatch.setattr(ir, "NO_ID_FILE", tmp_path / "no_id.txt")

    teams = {r.team for r in ir.parse_report(R2027).rows}
    known = {
        "Chicago Bulls": CHI,
        "Cleveland Cavaliers": CLE,
        "Portland Trail Blazers": POR,
        "Minnesota Timberwolves": MIN,
    }
    team_rows = [(known.get(t, f"9{i}"), t, True) for i, t in enumerate(sorted(teams))]
    write(
        tmp_path,
        "team_seasons",
        f"SELECT * FROM (VALUES {values(team_rows)}) t(team_id, full_name, is_nba)",
    )
    write(
        tmp_path,
        "schedule",
        f"""
        SELECT game_id, game_date::DATE AS game_date,
            start_time::TIMESTAMPTZ AS start_time, home_team_id, away_team_id, status
        FROM (VALUES
            ('{CHI_CLE}', '2026-11-16', '2026-11-17 00:00:00+00', '{CLE}', '{CHI}', 'final'),
            ('{POR_MIN}', '2026-11-17', '2026-11-18 01:00:00+00', '{MIN}', '{POR}', 'scheduled'),
            -- preseason isn't reported
            ('0012600001', '2026-11-16', '2026-11-16 12:00:00+00', '{MIN}', '{POR}', 'final')
        ) t(game_id, game_date, start_time, home_team_id, away_team_id, status)
        """,
    )
    write_players(tmp_path, CHI_PLAYERS + CLE_PLAYERS)
    # Cleveland's box score, and one of Chicago's from another game
    status = [(CHI_CLE, CLE, pid) for pid, _ in CLE_PLAYERS] + [
        ("0022600100", CHI, pid) for pid, _ in CHI_PLAYERS
    ]
    write(
        tmp_path,
        "player_game_status",
        f"SELECT * FROM (VALUES {values(status)}) t(game_id, team_id, player_id)",
    )
    path = tmp_path / "nba/raw/stats/2027/injury_report" / R2027.name
    path.parent.mkdir(parents=True)
    path.write_bytes(R2027.read_bytes())
    return tmp_path


def write_players(outdir, players, season=SEASON):
    write(
        outdir,
        "player_seasons",
        f"SELECT * FROM (VALUES {values(players)}) t(player_id, name)",
        season,
    )


def build(outdir):
    # after the games; and a bucket with nothing in it
    now = et(2026, 11, 16, 23, 0)
    ir.build_season(outdir, SEASON, now, url=str(outdir / "bucket"))
    path = dataset_path(outdir / stats.OUT_DIR, ir.DATASET, SEASON)
    return duckdb.read_parquet(str(path), hive_partitioning=False)


def test_build(season):
    table = build(season)
    types = dict(zip(table.columns, map(str, table.types)))
    assert types == {
        "season": "INTEGER",
        "game_id": "VARCHAR",
        "game_date": "DATE",
        "team_id": "VARCHAR",
        "player_id": "VARCHAR",
        "report_time": "TIMESTAMP WITH TIME ZONE",
        "status": "VARCHAR",
        "reason": "VARCHAR",
        "detail": "VARCHAR",
    }
    rows = table.fetchall()
    assert len(rows) == 12
    assert {r[1] for r in rows} == {CHI_CLE}
    by_id = {r[4]: r for r in rows}
    assert by_id["16"][3] == CLE
    assert by_id["16"][6:] == ("Out", "G League", "Two-Way")
    assert by_id["4"][3] == CHI
    assert by_id["4"][6:] == ("Questionable", "Injury/Illness", "Left Finger Sprain")
    assert by_id["4"][5] == et(2026, 11, 16, 18, 0)


def test_a_report_after_tipoff_isnt_used(season):
    write(
        season,
        "schedule",
        f"""
        SELECT '{CHI_CLE}' AS game_id, DATE '2026-11-16' AS game_date,
            TIMESTAMPTZ '2026-11-16 22:00:00+00' AS start_time,
            '{CLE}' AS home_team_id, '{CHI}' AS away_team_id, 'final' AS status
        """,
    )
    with pytest.raises(ValueError, match="1 played games have no report"):
        build(season)


def test_unmatched_players_are_reported(season, tmp_path, monkeypatch):
    write_players(season, CHI_PLAYERS[1:] + CLE_PLAYERS)
    dropped = tmp_path / "dropped.txt"
    monkeypatch.setenv(DROPPED_ENV, str(dropped))
    rows = build(season).fetchall()
    assert len(rows) == 11
    assert "Awaka, Tobe (Chicago Bulls)" in dropped.read_text()


def test_players_from_another_season_are_left_out(season):
    write_players(season, CHI_PLAYERS[1:] + CLE_PLAYERS)
    write_players(season, CHI_PLAYERS[:1], season=2026)
    assert len(build(season).fetchall()) == 11


def test_players_with_no_nba_id_are_left_out(season):
    write_players(season, CHI_PLAYERS[1:] + CLE_PLAYERS)
    ir.NO_ID_FILE.write_text("# never played\nAwaka, Tobe\n")
    assert len(build(season).fetchall()) == 11


def test_a_match_outside_the_season_is_left_out(season):
    write_players(season, CHI_PLAYERS[1:] + CLE_PLAYERS)
    ir.PLAYER_IDS_FILE.write_text(
        'source,source_id,player_id,method\nnba_injury,"Awaka, Tobe",99,manual\n'
    )
    assert len(build(season).fetchall()) == 11


def test_player_ids_csv_matches_a_name(season):
    write_players(season, [("99", "Tobias Awaka")] + CHI_PLAYERS[1:] + CLE_PLAYERS)
    ir.PLAYER_IDS_FILE.write_text(
        'source,source_id,player_id,method\nnba_injury,"Awaka, Tobe",99,manual\n'
    )
    by_id = {r[4]: r for r in build(season).fetchall()}
    assert by_id["99"][3] == CHI
