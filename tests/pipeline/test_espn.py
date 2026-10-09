import gzip
import io
import json
from datetime import date

import duckdb
import pytest
from botocore.exceptions import ClientError

from pipeline import espn
from pipeline.teams import UnknownTeamAbbrev

BKN = "1610612751"
NOP = "1610612740"
GAME = "0022500171"


def player(plyr_id, abbrev, team_id=None, home=0, **extra):
    row = {
        "gmId": GAME,
        "plyrID": plyr_id,
        "tmName": abbrev,
        "hmTm": home,
        "starter": 1,
        "played": 1,
        "pts": 10,
        "plusMinusPoints": -3,
        "assisterLU": 2,
        "minutes_played": "31:41",
    }
    if team_id:
        # ESPN started sending teamId partway through its history
        row["teamId"] = int(team_id)
    return row | extra


def day(players, four_factor_abbrevs=("BRK", "NOR"), game=GAME):
    return {
        "four_factors": [
            {"gameId": game, "deanAbbrev": a, "actionType": t, "oNetPts": 1.5}
            for a in four_factor_abbrevs
            for t in ("2pt", "3pt")
        ],
        "player_box": players,
        "team_box": [
            {
                "gameId": game,
                "tmID": int(t),
                "homeTm": h,
                "win": h,
                "assisterLu": 4,
                "minutes_played": "240:00",
            }
            for t, h in ((BKN, 0), (NOP, 1))
        ],
        "player_details": [
            {
                "gmID": game,
                "plyrID": p["plyrID"],
                "teamId": p.get("teamId"),
                "deanAbbrev": p["tmName"],
                "actionType": "total",
                "tNetPts": 0.5,
            }
            for p in players
        ],
    }


def write_raw(outdir, season, day_str, data):
    path = outdir / espn.RAW_DIR / str(season) / f"{day_str}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(data, f)


def read(outdir, dataset, season):
    return duckdb.sql(
        f"SELECT * FROM '{outdir / espn.OUT_DIR / dataset / f'{season}.parquet'}'"
    )


def test_build_resolves_abbreviations(tmp_path):
    # a 2023-style file: no teamId on players, so team ids come from the lookup
    write_raw(
        tmp_path, 2023, "2023-01-05", day([player(1, "BRK"), player(2, "NOR", home=1)])
    )
    assert espn.build_season(tmp_path, 2023)

    rows = read(tmp_path, "player_box", 2023).fetchall()
    cols = read(tmp_path, "player_box", 2023).columns
    by_player = {r[cols.index("player_id")]: dict(zip(cols, r)) for r in rows}
    assert by_player["1"]["team_id"] == BKN
    assert by_player["2"]["team_id"] == NOP
    assert by_player["2"]["home"] is True
    assert by_player["1"]["season"] == 2023
    assert by_player["1"]["ast_layup"] == 2

    ff = read(tmp_path, "four_factors", 2023)
    assert {r[0] for r in ff.select("team_id").fetchall()} == {BKN, NOP}
    pd = read(tmp_path, "player_details", 2023)
    assert {r[0] for r in pd.select("team_id").fetchall()} == {BKN, NOP}


def test_output_types(tmp_path):
    write_raw(tmp_path, 2026, "2025-11-05", day([player(1, "BRK", BKN)]))
    espn.build_season(tmp_path, 2026)
    for dataset in espn.QUERIES:
        types = dict(
            duckdb.sql(
                f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM "
                f"'{tmp_path / espn.OUT_DIR / dataset / '2026.parquet'}')"
            ).fetchall()
        )
        assert types["season"] == "INTEGER"
        assert types["game_id"] == "VARCHAR"
        assert types["team_id"] == "VARCHAR"
        assert "BIGINT" not in types.values(), dataset
        assert not any(c[0].isdigit() for c in types), dataset
    pb = dict(
        duckdb.sql(
            f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM "
            f"'{tmp_path / espn.OUT_DIR / 'player_box' / '2026.parquet'}')"
        ).fetchall()
    )
    assert pb["player_id"] == "VARCHAR"
    assert pb["home"] == "BOOLEAN"


def test_seconds_played(tmp_path):
    # prefer ESPN's seconds_played; fall back to parsing minutes_played
    write_raw(
        tmp_path,
        2026,
        "2025-11-05",
        day([player(1, "BRK", BKN, seconds_played=1902), player(2, "NOR", NOP)]),
    )
    espn.build_season(tmp_path, 2026)
    secs = dict(
        read(tmp_path, "player_box", 2026)
        .select("player_id, seconds_played")
        .fetchall()
    )
    assert secs == {"1": 1902, "2": 31 * 60 + 41}
    team_secs = read(tmp_path, "team_box", 2026).select("seconds_played").fetchall()
    assert team_secs == [(240 * 60,), (240 * 60,)]


def test_unknown_abbreviation_fails(tmp_path):
    write_raw(
        tmp_path, 2026, "2025-11-05", day([player(1, "BRK", BKN)], ("BRK", "XXX"))
    )
    with pytest.raises(UnknownTeamAbbrev, match="XXX"):
        espn.build_season(tmp_path, 2026)


def test_abbreviation_outside_its_seasons_fails(tmp_path):
    # ESPN abbreviations start in 2019
    write_raw(tmp_path, 2018, "2018-01-05", day([player(1, "BRK", BKN)]))
    with pytest.raises(UnknownTeamAbbrev):
        espn.build_season(tmp_path, 2018)


def test_unknown_abbreviation_ok_when_espn_gives_team_id(tmp_path):
    # players carry ESPN's team id, so their abbreviation isn't needed
    write_raw(tmp_path, 2026, "2025-11-05", day([player(1, "ZZZ", BKN)]))
    espn.build_season(tmp_path, 2026)
    assert read(tmp_path, "player_box", 2026).select("team_id").fetchall() == [(BKN,)]


def test_abbreviation_disagreeing_with_team_id_fails(tmp_path):
    write_raw(tmp_path, 2026, "2025-11-05", day([player(1, "BRK", NOP)]))
    with pytest.raises(ValueError, match="disagrees"):
        espn.build_season(tmp_path, 2026)


def test_duplicate_game_fails(tmp_path):
    data = day([player(1, "BRK", BKN)])
    write_raw(tmp_path, 2026, "2025-11-05", data)
    write_raw(tmp_path, 2026, "2025-11-06", data)
    with pytest.raises(ValueError, match="duplicate"):
        espn.build_season(tmp_path, 2026)


def test_no_raw_files(tmp_path):
    assert espn.build_season(tmp_path, 2026) is False


def test_rebuild_overwrites(tmp_path):
    write_raw(tmp_path, 2026, "2025-11-05", day([player(1, "BRK", BKN)]))
    espn.build_season(tmp_path, 2026)
    write_raw(
        tmp_path, 2026, "2025-11-06", day([player(2, "BRK", BKN)], game="0022500200")
    )
    espn.build_season(tmp_path, 2026)
    assert read(tmp_path, "player_box", 2026).count("*").fetchone() == (2,)


def client_error(code):
    return ClientError({"Error": {"Code": code}}, "GetObject")


class FakeS3:
    def __init__(self, days, error=None):
        self.days = days
        self.error = error
        self.requested = []

    def get_object(self, Bucket, Key):
        self.requested.append(Key)
        if self.error:
            raise self.error
        name = Key.rsplit("/", 1)[1]
        day_str = name[:10]
        if day_str not in self.days:
            # ESPN's bucket doesn't allow listing, so missing files are denied
            raise client_error("AccessDenied")
        body = (
            []
            if name.endswith("_player.json")
            else {"four_factors": [], "day": day_str}
        )
        return {"Body": io.BytesIO(json.dumps(body).encode())}


def test_fetch_season(tmp_path):
    today = date(2025, 10, 25)
    s3 = FakeS3({"2025-10-21", "2025-10-24", "2025-10-25"})
    fetched = espn.fetch_season(s3, tmp_path, 2026, today)
    assert fetched == [date(2025, 10, 21), date(2025, 10, 24), date(2025, 10, 25)]
    # ESPN's keys use the season's start year
    assert all(k.startswith("NBA/netpts/2025/") for k in s3.requested)

    path = espn.raw_path(tmp_path, 2026, date(2025, 10, 21))
    with gzip.open(path, "rt") as f:
        assert json.load(f) == {
            "four_factors": [],
            "day": "2025-10-21",
            "player_details": [],
        }

    # a later run only refetches days it doesn't have, plus today and yesterday
    s3 = FakeS3({"2025-10-21", "2025-10-24", "2025-10-25"})
    espn.fetch_season(s3, tmp_path, 2026, today)
    days = {k.rsplit("/", 1)[1][:10] for k in s3.requested}
    assert "2025-10-21" not in days
    assert {"2025-10-24", "2025-10-25", "2025-10-22"} <= days


def test_fetch_errors_are_raised(tmp_path):
    s3 = FakeS3(set(), error=client_error("SlowDown"))
    with pytest.raises(ClientError):
        espn.fetch_season(s3, tmp_path, 2026, date(2025, 10, 25))


def test_season_with_no_data_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(espn, "get_s3_client", lambda: FakeS3(set()))
    monkeypatch.setattr(espn, "today_eastern", lambda: date(2025, 11, 15))
    with pytest.raises(SystemExit, match="no data for season 2026"):
        espn.main(["--out", str(tmp_path)])

    # early in the season, an empty result is normal
    monkeypatch.setattr(espn, "today_eastern", lambda: date(2025, 10, 20))
    espn.main(["--out", str(tmp_path)])
