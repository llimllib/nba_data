import pytest

from pipeline import player_ids as pi
from pipeline.player_ids import PlayerId, SourcePlayer

BOS = "1610612738"
LAL = "1610612747"


def test_committed_table_is_valid():
    ids = pi.load_player_ids()
    bbref = [p for p in ids.values() if p.source == "bbref"]
    assert len(bbref) > 1900
    assert len({p.player_id for p in bbref}) == len(bbref)


def write_csv(tmp_path, body):
    path = tmp_path / "player_ids.csv"
    path.write_text("source,source_id,player_id,method\n" + body)
    return path


@pytest.mark.parametrize(
    "body,message",
    [
        ("bbref,jamesle01,,manual\n", "malformed"),
        ("bbref,jamesle01,2544,guess\n", "unknown method"),
        ("bbref,jamesle01,2544.0,manual\n", "isn't an NBA id"),
        ("bbref,jamesle01,2544,manual\nbbref,jamesle01,2545,manual\n", "duplicate"),
        ("bbref,jamesle01,2544,manual\nbbref,jamesle02,2544,manual\n", "both map"),
    ],
)
def test_bad_rows_rejected(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        pi.load_player_ids(write_csv(tmp_path, body))


def test_round_trip(tmp_path):
    path = tmp_path / "player_ids.csv"
    ids = {("bbref", "jamesle01"): PlayerId("bbref", "jamesle01", "2544", "manual")}
    pi.save_player_ids(ids, path)
    assert pi.load_player_ids(path) == ids


def test_resolve():
    ids = {("bbref", "jamesle01"): PlayerId("bbref", "jamesle01", "2544", "name_team")}
    assert pi.resolve(ids, "bbref", ["jamesle01"]) == {"jamesle01": "2544"}
    with pytest.raises(pi.UnknownPlayer, match="newguy01"):
        pi.resolve(ids, "bbref", ["jamesle01", "newguy01"])


def nba(*rows):
    """(season, team_id, player_id, name) with the name normalized"""
    return [(s, t, p, pi.bbref.normalize_name(n)) for s, t, p, n in rows]


def test_match_by_name_and_team():
    matched, unresolved = pi.match(
        [SourcePlayer("doncilu01", "Luka Dončić", 2026, LAL)],
        nba((2026, LAL, "1629029", "Luka Doncic")),
    )
    assert matched == {"doncilu01": ("1629029", "name_team")}
    assert unresolved == {}


def test_same_name_on_another_team_is_resolved_by_team():
    matched, _ = pi.match(
        [SourcePlayer("davisan02", "Anthony Davis", 2026, LAL)],
        nba(
            (2026, LAL, "203076", "Anthony Davis"), (2026, BOS, "999", "Anthony Davis")
        ),
    )
    assert matched == {"davisan02": ("203076", "name_team")}


def test_name_without_team_match_falls_back_to_season():
    matched, _ = pi.match(
        [SourcePlayer("x01", "Some Player", 2026, BOS)],
        nba((2026, LAL, "1", "Some Player")),
    )
    assert matched == {"x01": ("1", "name_season")}


def test_ambiguous_names_are_unresolved():
    # two NBA players with the player's name on his team
    _, unresolved = pi.match(
        [SourcePlayer("davisjo02", "Josh Davis", 2012, BOS)],
        nba((2012, BOS, "2668", "Josh Davis"), (2012, BOS, "201820", "Josh Davis")),
    )
    assert unresolved == {"davisjo02": {"2668", "201820"}}


def test_conflicting_seasons_are_unresolved():
    # one bbref id whose name points to different NBA players in two seasons
    _, unresolved = pi.match(
        [
            SourcePlayer("x01", "Same Name", 2010, BOS),
            SourcePlayer("x01", "Same Name", 2020, BOS),
        ],
        nba((2010, BOS, "1", "Same Name"), (2020, BOS, "2", "Same Name")),
    )
    assert unresolved == {"x01": {"1", "2"}}


def test_two_source_ids_for_one_nba_player_are_unresolved():
    _, unresolved = pi.match(
        [
            SourcePlayer("a01", "Same Name", 2020, BOS),
            SourcePlayer("b01", "Same Name", 2020, BOS),
        ],
        nba((2020, BOS, "1", "Same Name")),
    )
    assert set(unresolved) == {"a01", "b01"}


def test_no_match_is_unresolved():
    _, unresolved = pi.match([SourcePlayer("x01", "Nobody", 2020, BOS)], nba())
    assert unresolved == {"x01": set()}
