import urllib.error
from email.message import Message
from io import BytesIO

import pytest

from pipeline import bbref


def row(bbref_id, name, team, games=10):
    return (
        f'<tr ><th scope="row" data-stat="ranker" >1</th>'
        f'<td class="left " data-append-csv="{bbref_id}" data-stat="name_display" >'
        f'<a href="/players/x/{bbref_id}.html">{name}</a></td>'
        f'<td data-stat="team_name_abbr" ><a href="/teams/{team}/2026.html">{team}</a></td>'
        f'<td data-stat="games" >{games}</td></tr>'
    )


def table(table_id, rows):
    head = '<tr class="thead"><th data-stat="ranker">Rk</th></tr>'
    return f'<table id="{table_id}"><thead></thead><tbody>{head}{"".join(rows)}</tbody></table>'


# a traded player has a total row ("2TM") and a row per team
REGULAR = [
    row("hardeja01", "James Harden", "LAC"),
    row("hardeja01", "James Harden", "CLE"),
]
REGULAR.insert(0, row("hardeja01", "James Harden", "2TM"))
REGULAR += [row(f"filler{i:02d}", f"Player {i}", "BOS") for i in range(300)]
REGULAR.append(
    '<tr><th data-stat="ranker"></th><td data-stat="name_display">League Average</td>'
    '<td data-stat="team_name_abbr"></td><td data-stat="games"></td></tr>'
)


def page(regular=REGULAR, playoffs=()):
    # bbref hides some tables in HTML comments
    return (
        f"<html><body>{table('per_game_stats', regular)}"
        f"<!-- {table('per_game_stats_post', list(playoffs))} --></body></html>"
    )


def test_season_players():
    html = page(
        playoffs=[
            row("hardeja01", "James Harden", "CLE", games=5),
            row("doncilu01", "Luka Dončić", "LAL"),
        ]
    )
    players = bbref.season_players(html)
    harden = [p for p in players if p.bbref_id == "hardeja01"]
    # no 2TM row, and the playoff row doesn't duplicate the regular season's
    assert harden == [
        bbref.SeasonPlayer("hardeja01", "James Harden", "CLE"),
        bbref.SeasonPlayer("hardeja01", "James Harden", "LAC"),
    ]
    # playoff-only rows are found, inside the comment
    assert bbref.SeasonPlayer("doncilu01", "Luka Dončić", "LAL") in players
    assert len(players) == 303


def test_too_few_rows_fails():
    with pytest.raises(ValueError, match="only 3 rows"):
        bbref.season_players(page(regular=REGULAR[:3]))


def test_missing_column_fails():
    broken = REGULAR[:-1] + [
        '<tr><td data-append-csv="x01" data-stat="name_display">X</td></tr>'
    ]
    with pytest.raises(ValueError, match="team_name_abbr"):
        bbref.season_players(page(regular=broken))


def test_nba_id_from_player_page():
    assert (
        bbref.nba_id_from_player_page(
            '<a href="https://www.nba.com/stats/player/2544/">'
        )
        == "2544"
    )
    assert (
        bbref.nba_id_from_player_page('<a href="https://stats.nba.com/player/2544">')
        == "2544"
    )
    assert bbref.nba_id_from_player_page("<html>no link</html>") is None
    with pytest.raises(ValueError, match="several"):
        bbref.nba_id_from_player_page("stats.nba.com/player/1 stats.nba.com/player/2")


@pytest.mark.parametrize(
    "name,normalized",
    [
        ("Nikola Jokić", "nikola jokic"),
        ("Kevin Porter Jr.", "kevin porter"),
        ("Gary Trent Jr", "gary trent"),
        ("Shai Gilgeous-Alexander", "shai gilgeous alexander"),
        ("D'Angelo Russell", "dangelo russell"),
        ("Marvin Bagley III", "marvin bagley"),
    ],
)
def test_normalize_name(name, normalized):
    assert bbref.normalize_name(name) == normalized


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def time(self):
        return self.now


def test_fetcher_waits_between_requests(monkeypatch):
    monkeypatch.setattr(
        bbref.urllib.request, "urlopen", lambda req, timeout: BytesIO(b"<html>")
    )
    clock = FakeClock()
    f = bbref.Fetcher(delay=6, sleep=clock.sleep, clock=clock.time)
    f.get("/a")
    clock.now += 2
    f.get("/b")
    assert clock.slept == [pytest.approx(4)]
    assert f.count == 2


def test_rate_limit_is_not_retried(monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        headers = Message()
        headers["Retry-After"] = "3600"
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", headers, None)

    monkeypatch.setattr(bbref.urllib.request, "urlopen", urlopen)
    clock = FakeClock()
    with pytest.raises(bbref.RateLimited, match="3600"):
        bbref.Fetcher(sleep=clock.sleep, clock=clock.time).get("/a")
    assert len(calls) == 1


def test_cached_pages_are_not_refetched(tmp_path):
    class Counting:
        count = 0

        def get(self, path):
            self.count += 1
            return f"<html>{path}</html>"

    f = Counting()
    raw = tmp_path / "x.html.gz"
    assert bbref.cached(f, "/x", raw) == "<html>/x</html>"
    assert bbref.cached(f, "/x", raw) == "<html>/x</html>"
    assert f.count == 1
    bbref.cached(f, "/x", raw, refresh=True)
    assert f.count == 2
