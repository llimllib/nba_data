import duckdb
import pytest

from pipeline import catalog, integrity

BOS = "1610612738"
NYK = "1610612752"
GAME = "0022500001"
CUP_FINAL = "0062500001"


def write(outdir, dataset, query, season=2026, source="stats"):
    path = outdir / "nba" / source / dataset / f"{season}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    duckdb.sql(f"COPY ({query}) TO '{path}' (FORMAT parquet)")


@pytest.fixture
def out(tmp_path):
    """a consistent 2026: two teams, one player, two games"""
    write(
        tmp_path,
        "team_seasons",
        f"SELECT 2026 AS season, * FROM (VALUES ('{BOS}'), ('{NYK}')) t(team_id)",
    )
    write(
        tmp_path,
        "player_seasons",
        "SELECT 2026 AS season, '1' AS player_id, 'A' AS name",
    )
    # the Cup final is at a neutral site, so it has no home or away team
    write(
        tmp_path,
        "games",
        f"""
        SELECT 2026 AS season, * FROM (VALUES
            ('{GAME}', '{BOS}', '{NYK}'),
            ('{CUP_FINAL}', NULL, NULL)
        ) t(game_id, home_team_id, away_team_id)
        """,
    )
    write(
        tmp_path,
        "team_game_logs",
        f"""
        SELECT 2026 AS season, * FROM (VALUES
            ('{GAME}', '{BOS}', '{NYK}'), ('{GAME}', '{NYK}', '{BOS}')
        ) t(game_id, team_id, opp_team_id)
        """,
    )
    write(
        tmp_path,
        "player_box",
        f"SELECT 2026 AS season, '{GAME}' AS game_id, '1' AS player_id, '{BOS}' AS team_id",
        source="espn",
    )
    return tmp_path


def build(outdir):
    catalog.build(outdir / catalog.LOCAL_CATALOG, catalog.list_dir(outdir), str(outdir))


def player_box(outdir, game_id=GAME, player_id="1", team_id=BOS, season=2026):
    write(
        outdir,
        "player_box",
        f"SELECT {season} AS season, '{game_id}' AS game_id, "
        f"'{player_id}' AS player_id, '{team_id}' AS team_id",
        season=season,
        source="espn",
    )


def test_consistent_data_passes(out):
    build(out)
    assert (out / catalog.LOCAL_CATALOG).is_file()


def test_lookup_for():
    assert integrity.lookup_for("team_id") == "team_seasons"
    assert integrity.lookup_for("opp_team_id") == "team_seasons"
    assert integrity.lookup_for("assist_player_id") == "player_seasons"
    assert integrity.lookup_for("game_id") == "games"
    assert integrity.lookup_for("steam_id") is None
    assert integrity.lookup_for("team_count") is None


def test_unknown_team_fails(out):
    player_box(out, team_id="1610612799")
    with pytest.raises(integrity.IntegrityError, match=r"player_box\.team_id: 1 "):
        build(out)


def test_unknown_player_fails(out):
    player_box(out, player_id="2")
    with pytest.raises(integrity.IntegrityError, match=r"player_box\.player_id"):
        build(out)


def test_unknown_game_fails(out):
    player_box(out, game_id="0022500002")
    with pytest.raises(integrity.IntegrityError, match=r"player_box\.game_id"):
        build(out)


def test_columns_ending_in_a_key_are_checked(out):
    write(
        out,
        "team_game_logs",
        f"SELECT 2026 AS season, '{GAME}' AS game_id, '{BOS}' AS team_id, "
        "'1610612799' AS opp_team_id",
    )
    with pytest.raises(integrity.IntegrityError, match=r"team_game_logs\.opp_team_id"):
        build(out)


def test_keys_must_exist_in_the_same_season(out):
    # 2025 has no lookups, so BOS, player 1 and the game are all unknown there
    write(out, "team_seasons", f"SELECT 2025 AS season, '{BOS}' AS team_id", 2025)
    player_box(out, season=2025)
    with pytest.raises(integrity.IntegrityError) as e:
        build(out)
    message = str(e.value)
    assert "player_box.player_id" in message
    assert "player_box.game_id" in message
    assert "player_box.team_id" not in message


def test_game_id_must_match_season(out):
    write(
        out,
        "games",
        "SELECT 2025 AS season, '0022500009' AS game_id",
        season=2025,
    )
    with pytest.raises(integrity.IntegrityError, match="game_id doesn't match season"):
        build(out)


def test_every_problem_is_reported(out):
    player_box(out, game_id="0022500002", player_id="2", team_id="1610612799")
    with pytest.raises(integrity.IntegrityError) as e:
        build(out)
    assert str(e.value).count("player_box.") == 3


def test_missing_lookup_fails(out):
    (out / "nba" / "stats" / "player_seasons" / "2026.parquet").unlink()
    with pytest.raises(integrity.IntegrityError, match="no player_seasons"):
        build(out)


def test_failure_keeps_the_previous_catalog(out):
    build(out)
    path = out / catalog.LOCAL_CATALOG
    before = path.read_bytes()
    player_box(out, player_id="2")
    with pytest.raises(integrity.IntegrityError):
        build(out)
    assert path.read_bytes() == before
    assert not (out / f".{path.name}.tmp").exists()
