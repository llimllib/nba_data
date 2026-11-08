import pytest

from pipeline import odds


def american(value):
    return {"alternateDisplayValue": value, "american": value}


def core_item(provider="DraftKings", provider_id="100", close=True):
    """a core API odds item: DEN -6.5 at open, -8.5 at close, at IND"""
    item = {
        "provider": {"id": provider_id, "name": provider, "priority": 1},
        "details": "DEN -8.5",
        "overUnder": 229.5,
        "spread": 8.5,
        "overOdds": -107.0,
        "underOdds": -113.0,
        "awayTeamOdds": {
            "moneyLine": -324,
            "spreadOdds": -103.0,
            "open": {
                "pointSpread": american("-6.5"),
                "spread": american("-114"),
                "moneyLine": american("-241"),
            },
        },
        "homeTeamOdds": {
            "moneyLine": 261,
            "spreadOdds": -118.0,
            "open": {
                "pointSpread": american("+6.5"),
                "spread": american("-106"),
                "moneyLine": american("+199"),
            },
        },
        "open": {
            "over": american("-106"),
            "under": american("-114"),
            "total": american("228.5"),
        },
    }
    if close:
        item["awayTeamOdds"]["close"] = {
            "pointSpread": american("-8.5"),
            "spread": american("-103"),
            "moneyLine": american("-324"),
        }
        item["homeTeamOdds"]["close"] = {
            "pointSpread": american("+8.5"),
            "spread": american("-118"),
            "moneyLine": american("+261"),
        }
        item["close"] = {
            "over": american("-107"),
            "under": american("-113"),
            "total": american("229.5"),
        }
    return item


def core(*items):
    return {"count": len(items), "pageCount": 1, "items": list(items)}


EXPECTED = {
    "provider_id": "100",
    "provider": "DraftKings",
    "open_spread": 6.5,
    "open_total": 228.5,
    "open_home_moneyline": 199,
    "open_away_moneyline": -241,
    "open_home_spread_odds": -106,
    "open_away_spread_odds": -114,
    "open_over_odds": -106,
    "open_under_odds": -114,
    "close_spread": 8.5,
    "close_total": 229.5,
    "close_home_moneyline": 261,
    "close_away_moneyline": -324,
    "close_home_spread_odds": -118,
    "close_away_spread_odds": -103,
    "close_over_odds": -107,
    "close_under_odds": -113,
}


def test_core_lines():
    assert odds.core_lines(core(core_item())) == [EXPECTED]


def test_core_lines_without_a_close_use_the_last_line():
    # older seasons have only the line as of the request
    (row,) = odds.core_lines(core(core_item(close=False)))
    assert row == EXPECTED


def test_live_odds_are_left_out():
    items = [core_item(), core_item("Caesars - Live Odds", "2001")]
    assert [r["provider"] for r in odds.core_lines(core(*items))] == ["DraftKings"]


def test_no_odds():
    assert odds.core_lines({"count": 0, "pageCount": 0, "items": []}) == []


@pytest.mark.parametrize("data", [{}, {"pageCount": 2, "items": []}])
def test_bad_core_response_fails(data):
    with pytest.raises(ValueError):
        odds.core_lines(data)


def pickcenter_item():
    """the same lines as core_item, as a summary's pickcenter has them"""
    item = core_item()
    for key in ("open", "close"):
        del item[key], item["homeTeamOdds"][key], item["awayTeamOdds"][key]

    def when(open_, close):
        return {"open": open_, "close": close}

    item |= {
        "moneyline": {
            "home": when({"odds": "+199"}, {"odds": "+261"}),
            "away": when({"odds": "-241"}, {"odds": "-324"}),
        },
        "pointSpread": {
            "home": when(
                {"line": "+6.5", "odds": "-106"}, {"line": "+8.5", "odds": "-118"}
            ),
            "away": when(
                {"line": "-6.5", "odds": "-114"}, {"line": "-8.5", "odds": "-103"}
            ),
        },
        "total": {
            "over": when(
                {"line": "o228.5", "odds": "-106"}, {"line": "o229.5", "odds": "-107"}
            ),
            "under": when(
                {"line": "u228.5", "odds": "-114"}, {"line": "u229.5", "odds": "-113"}
            ),
        },
    }
    return item


def test_pickcenter_lines():
    assert odds.pickcenter_lines([pickcenter_item()]) == [EXPECTED]


def test_pickcenter_lines_off_the_board():
    # college games often have no moneyline
    item = pickcenter_item()
    item["moneyline"]["home"] = {"open": {"odds": "OFF"}, "close": {"odds": "OFF"}}
    del item["homeTeamOdds"]["moneyLine"]
    (row,) = odds.pickcenter_lines([item])
    assert (row["open_home_moneyline"], row["close_home_moneyline"]) == (None, None)


@pytest.mark.parametrize(
    "value,expected",
    [("+6.5", 6.5), ("-6.5", -6.5), ("o229.5", 229.5), ("PK", 0.0), ("OFF", None)],
)
def test_line(value, expected):
    assert odds.line(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("-110", -110), ("+261", 261), ("EVEN", 100), (-107.0, -107), (None, None)],
)
def test_price(value, expected):
    assert odds.price(value) == expected
