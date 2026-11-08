"""
Parse ESPN's betting lines into rows of one shape, for `betting_lines` in
both leagues (#99). ESPN has them in two places:

- the core API's odds for a game (pipeline.espn_odds, for the NBA):
  sports.core.api.espn.com/v2/sports/basketball/leagues/<league>/events/<id>/competitions/<id>/odds
- the `pickcenter` list in a game's summary (pipeline.ncaam_espn, which
  fetches each college game's summary anyway)

Both have a provider's line as of the request at the top level and, since
about 2025, its opening and closing lines in `open` and `close`. A row has
both: close_* is the closing line, or the top-level one where a provider
has no close (fetched once the game is final, so its last line). Spreads
are the home team's (negative when it's favored, so compare -close_spread
with the home team's margin), and prices are American odds.
"""

# each (open|close)_ column, after the row's provider
LINE_COLUMNS = [
    ("spread", "DOUBLE"),
    ("total", "DOUBLE"),
    ("home_moneyline", "INTEGER"),
    ("away_moneyline", "INTEGER"),
    ("home_spread_odds", "INTEGER"),
    ("away_spread_odds", "INTEGER"),
    ("over_odds", "INTEGER"),
    ("under_odds", "INTEGER"),
]
PROVIDER = [("provider_id", "VARCHAR"), ("provider", "VARCHAR")]
LINES = [
    (f"{when}_{name}", t) for when in ("open", "close") for name, t in LINE_COLUMNS
]
# a row's columns
COLUMNS = PROVIDER + LINES

# what a missing line looks like
OFF = {"", "-", "--", "OFF"}


def line(value) -> float | None:
    """a spread or total: -6.5, "+6.5", "o229.5", "PK" """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = value.strip().lstrip("ou")
    if value.upper() in OFF:
        return None
    if value.upper() in ("PK", "EVEN"):
        return 0.0
    return float(value)


def price(value) -> int | None:
    """American odds: -110, "-110", "+261", "EVEN" """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(value)
    value = value.strip()
    if value.upper() in OFF:
        return None
    if value.upper() == "EVEN":
        return 100
    return int(value)


def is_live(provider: dict) -> bool:
    """in-game lines ("... - Live Odds"), which aren't a pregame line"""
    return "live" in (provider.get("name") or "").lower()


def current(item: dict) -> dict:
    """a provider's line at the top level, as it was when fetched"""
    home, away = item.get("homeTeamOdds") or {}, item.get("awayTeamOdds") or {}
    return {
        "spread": line(item.get("spread")),
        "total": line(item.get("overUnder")),
        "home_moneyline": price(home.get("moneyLine")),
        "away_moneyline": price(away.get("moneyLine")),
        "home_spread_odds": price(home.get("spreadOdds")),
        "away_spread_odds": price(away.get("spreadOdds")),
        "over_odds": price(item.get("overOdds")),
        "under_odds": price(item.get("underOdds")),
    }


def row(item: dict, open_: dict, close: dict) -> dict:
    provider = item["provider"]
    last = current(item)
    return {
        "provider_id": str(provider["id"]),
        "provider": provider.get("name"),
        **{f"open_{k}": open_[k] for k, _ in LINE_COLUMNS},
        **{
            f"close_{k}": close[k] if close[k] is not None else last[k]
            for k, _ in LINE_COLUMNS
        },
    }


def core_lines(data: dict) -> list[dict]:
    """
    The rows in a core API odds response, one per provider. Raises
    ValueError if it isn't as expected
    """
    items = data.get("items")
    if items is None:
        raise ValueError("espn odds: no items")
    if data.get("pageCount", 1) > 1:
        raise ValueError(f"espn odds: {data['pageCount']} pages")

    def american(block: dict | None, key: str):
        return ((block or {}).get(key) or {}).get("american")

    def side(item: dict, when: str) -> dict:
        home = (item.get("homeTeamOdds") or {}).get(when)
        away = (item.get("awayTeamOdds") or {}).get(when)
        both = item.get(when)
        return {
            "spread": line(american(home, "pointSpread")),
            "total": line(american(both, "total")),
            "home_moneyline": price(american(home, "moneyLine")),
            "away_moneyline": price(american(away, "moneyLine")),
            "home_spread_odds": price(american(home, "spread")),
            "away_spread_odds": price(american(away, "spread")),
            "over_odds": price(american(both, "over")),
            "under_odds": price(american(both, "under")),
        }

    return [
        row(item, side(item, "open"), side(item, "close"))
        for item in items
        if not is_live(item["provider"])
    ]


def pickcenter_lines(pickcenter: list[dict]) -> list[dict]:
    """
    The rows in a summary's pickcenter, one per provider. Raises ValueError
    if it isn't as expected
    """

    def get(item: dict, market: str, side: str, when: str, key: str):
        return (((item.get(market) or {}).get(side) or {}).get(when) or {}).get(key)

    def side(item: dict, when: str) -> dict:
        return {
            "spread": line(get(item, "pointSpread", "home", when, "line")),
            "total": line(get(item, "total", "over", when, "line")),
            "home_moneyline": price(get(item, "moneyline", "home", when, "odds")),
            "away_moneyline": price(get(item, "moneyline", "away", when, "odds")),
            "home_spread_odds": price(get(item, "pointSpread", "home", when, "odds")),
            "away_spread_odds": price(get(item, "pointSpread", "away", when, "odds")),
            "over_odds": price(get(item, "total", "over", when, "odds")),
            "under_odds": price(get(item, "total", "under", when, "odds")),
        }

    return [
        row(item, side(item, "open"), side(item, "close"))
        for item in pickcenter
        if not is_live(item["provider"])
    ]
