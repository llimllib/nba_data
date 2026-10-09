"""
Season helpers. A season is named by its end year: 2026 = 2025-26
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# game dates are US Eastern, so "today" is too
EASTERN = ZoneInfo("America/New_York")


def today_eastern() -> date:
    return datetime.now(EASTERN).date()


def current_season(today: date) -> int:
    """
    The season in progress (or most recently finished) on `today`. Seasons
    start in October, so from September on we're in the next season
    """
    return today.year + 1 if today.month >= 9 else today.year


def season_window(season: int) -> tuple[date, date]:
    """
    First and last dates that might have games in `season`, preseason
    excluded
    """
    match season:
        # covid: suspended March 2020, finished in the bubble in October
        case 2020:
            return date(2019, 10, 15), date(2020, 10, 15)
        # covid: started late
        case 2021:
            return date(2020, 12, 20), date(2021, 7, 31)
        case _:
            return date(season - 1, 10, 15), date(season, 6, 30)


def season_days(season: int, until: date) -> list[date]:
    """Every day in the season's window up to and including `until`"""
    start, end = season_window(season)
    end = min(end, until)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
