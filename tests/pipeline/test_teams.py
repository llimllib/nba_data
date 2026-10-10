import pytest

from pipeline.teams import UnknownTeamAbbrev, load_team_abbrevs, team_id_for

HEADER = "source,abbrev,team_id,first_season,last_season\n"


def write_csv(tmp_path, body):
    path = tmp_path / "team_abbrevs.csv"
    path.write_text(HEADER + body)
    return path


def test_committed_table_is_valid():
    rows = load_team_abbrevs()
    espn = [r for r in rows if r.source == "espn"]
    # one abbreviation per franchise
    assert len(espn) == 30
    assert len({r.team_id for r in espn}) == 30


@pytest.mark.parametrize(
    "abbrev,team_id",
    [
        ("BRK", "1610612751"),
        ("NOR", "1610612740"),
        ("PHO", "1610612756"),
        ("SAN", "1610612759"),
        ("CHA", "1610612766"),
    ],
)
def test_espn_abbrevs_that_differ_from_nba(abbrev, team_id):
    assert team_id_for(load_team_abbrevs(), "espn", abbrev, 2026) == team_id


@pytest.mark.parametrize("source", ["bbref", "ctg"])
def test_has_30_teams_every_season(source):
    rows = [r for r in load_team_abbrevs() if r.source == source]
    for season in range(2010, 2027):
        assert len({r.team_id for r in rows if r.covers(season)}) == 30, season


def test_ctg_ids_are_franchises():
    # CTG numbers teams alphabetically by city, and keeps a franchise's id
    # through renames: Brooklyn is 3 in New Jersey seasons too
    assert team_id_for(load_team_abbrevs(), "ctg", "3", 2012) == "1610612751"
    assert team_id_for(load_team_abbrevs(), "ctg", "19", 2013) == "1610612740"


@pytest.mark.parametrize(
    "abbrev,season,team_id",
    [
        ("NJN", 2012, "1610612751"),
        ("BRK", 2013, "1610612751"),
        ("CHA", 2014, "1610612766"),
        ("CHO", 2015, "1610612766"),
        ("NOH", 2013, "1610612740"),
        ("NOP", 2014, "1610612740"),
    ],
)
def test_bbref_renamed_franchises(abbrev, season, team_id):
    assert team_id_for(load_team_abbrevs(), "bbref", abbrev, season) == team_id


def test_season_ranges(tmp_path):
    # basketball-reference splits Charlotte by name; both map to one team_id
    rows = load_team_abbrevs(
        write_csv(
            tmp_path,
            "bbref,CHA,1610612766,2005,2014\nbbref,CHO,1610612766,2015,\n",
        )
    )
    assert team_id_for(rows, "bbref", "CHA", 2014) == "1610612766"
    assert team_id_for(rows, "bbref", "CHO", 2015) == "1610612766"
    assert team_id_for(rows, "bbref", "CHO", 2040) == "1610612766"
    with pytest.raises(UnknownTeamAbbrev):
        team_id_for(rows, "bbref", "CHA", 2015)
    with pytest.raises(UnknownTeamAbbrev):
        team_id_for(rows, "bbref", "CHO", 2014)


def test_abbrev_can_move_between_teams(tmp_path):
    # a source may reuse an abbreviation for another team, as long as the
    # ranges don't overlap
    rows = load_team_abbrevs(write_csv(tmp_path, "x,AAA,1,2010,2012\nx,AAA,2,2013,\n"))
    assert team_id_for(rows, "x", "AAA", 2012) == "1"
    assert team_id_for(rows, "x", "AAA", 2013) == "2"


def test_unknown_source_and_abbrev():
    rows = load_team_abbrevs()
    with pytest.raises(UnknownTeamAbbrev):
        team_id_for(rows, "espn", "XXX", 2026)
    with pytest.raises(UnknownTeamAbbrev):
        team_id_for(rows, "nosuch", "BRK", 2026)
    with pytest.raises(UnknownTeamAbbrev):
        team_id_for(rows, "espn", "BRK", 2018)


@pytest.mark.parametrize(
    "body",
    [
        "x,AAA,1,2010,\nx,AAA,2,2020,\n",  # open range overlaps a later one
        "x,AAA,1,2010,2015\nx,AAA,2,2015,2020\n",  # share one season
        "x,AAA,1,2010,2020\nx,AAA,2,2012,2013\n",  # one inside the other
        "x,AAA,1,2010,2015\nx,AAA,1,2014,\n",  # duplicate rows, even for one team
    ],
)
def test_overlapping_ranges_rejected(tmp_path, body):
    with pytest.raises(ValueError, match="overlapping"):
        load_team_abbrevs(write_csv(tmp_path, body))


@pytest.mark.parametrize(
    "body,message",
    [
        ("x,AAA,1,2015,2010\n", "before first_season"),
        ("x,AAA,1610612737.0,2010,\n", "not an NBA id"),
        ("x,,1,2010,\n", "required"),
    ],
)
def test_malformed_rows_rejected(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        load_team_abbrevs(write_csv(tmp_path, body))
