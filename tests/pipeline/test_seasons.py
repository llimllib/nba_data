from datetime import date

from pipeline.seasons import current_season, season_days, season_window


def test_current_season():
    assert current_season(date(2025, 10, 25)) == 2026
    assert current_season(date(2026, 6, 13)) == 2026
    assert current_season(date(2026, 8, 31)) == 2026
    assert current_season(date(2026, 9, 1)) == 2027


def test_covid_seasons():
    # the 2020 bubble ran into October
    assert season_window(2020)[1] >= date(2020, 10, 11)
    # 2021 started December 22
    assert season_window(2021)[0] <= date(2020, 12, 22)
    assert season_window(2021)[1] >= date(2021, 7, 20)


def test_season_days_stop_at_until():
    days = season_days(2026, date(2025, 10, 17))
    assert days == [date(2025, 10, 15), date(2025, 10, 16), date(2025, 10, 17)]
    assert season_days(2026, date(2025, 1, 1)) == []
