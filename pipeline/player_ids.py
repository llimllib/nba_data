"""
Map other sources' player ids to NBA player ids.

This module matches basketball-reference players. ESPN's season net points
files use ESPN ids too, but pipeline.espn_net_pts matches those by name as
it builds, using only the hand-made espn rows here. The mapping lives in
player_ids.csv, one row per (source, source_id):

    source,source_id,player_id,method
    bbref,jamesle01,2544,name_team

where method says how it was found:

    name_team    same normalized name on the same team in the same season
    name_season  same normalized name in the same season (team didn't match)
    player_page  the source's player page links to the NBA id
    manual       set by hand; never changed by the matcher

Running the matcher adds rows for new players and leaves existing ones
alone. Players it can't match unambiguously are looked up on their player
page if --fetch is given (bbref pages link to stats.nba.com), otherwise
printed for a hand fix.

usage: python -m pipeline.player_ids [--out out] [--fetch]
"""

import argparse
import csv
import gzip
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import bbref
from .teams import load_team_abbrevs

PLAYER_IDS_FILE = Path(__file__).parent / "player_ids.csv"
FIELDS = ["source", "source_id", "player_id", "method"]
METHODS = {"name_team", "name_season", "player_page", "manual"}


@dataclass(frozen=True)
class PlayerId:
    source: str
    source_id: str
    player_id: str
    method: str


class UnknownPlayer(Exception):
    pass


def load_player_ids(path: Path = PLAYER_IDS_FILE) -> dict[tuple[str, str], PlayerId]:
    """
    (source, source_id) -> PlayerId. Raises ValueError if a row is
    malformed or two source ids map to one NBA id
    """
    if not path.is_file():
        return {}
    ids: dict[tuple[str, str], PlayerId] = {}
    by_nba: dict[tuple[str, str], str] = {}
    with open(path, newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            if list(row) != FIELDS or not all(row.values()):
                raise ValueError(f"{path}:{i}: malformed row {row}")
            p = PlayerId(**row)
            if p.method not in METHODS:
                raise ValueError(f"{path}:{i}: unknown method {p.method!r}")
            if not p.player_id.isdigit():
                raise ValueError(
                    f"{path}:{i}: player_id {p.player_id!r} isn't an NBA id"
                )
            key = (p.source, p.source_id)
            if key in ids:
                raise ValueError(f"{path}:{i}: duplicate {key}")
            other = by_nba.setdefault((p.source, p.player_id), p.source_id)
            if other != p.source_id:
                raise ValueError(
                    f"{path}:{i}: {p.source} ids {other} and {p.source_id} "
                    f"both map to NBA player {p.player_id}"
                )
            ids[key] = p
    return ids


def save_player_ids(ids: dict[tuple[str, str], PlayerId], path: Path = PLAYER_IDS_FILE):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(FIELDS)
        for key in sorted(ids):
            p = ids[key]
            w.writerow([p.source, p.source_id, p.player_id, p.method])


def resolve(
    ids: dict[tuple[str, str], PlayerId], source: str, source_ids
) -> dict[str, str]:
    """
    source_id -> NBA player_id for `source_ids`. Raises UnknownPlayer naming
    every id with no mapping, so a new player fails the run until it's added
    """
    missing = sorted({s for s in source_ids if (source, s) not in ids})
    if missing:
        raise UnknownPlayer(
            f"no NBA player_id for {source} players {missing}; run "
            "`python -m pipeline.player_ids --fetch` or add them to player_ids.csv"
        )
    return {s: ids[(source, s)].player_id for s in source_ids}


@dataclass(frozen=True)
class SourcePlayer:
    """a source's player on a team in a season"""

    source_id: str
    name: str
    season: int
    team_id: str


def nba_players(outdir: Path) -> list[tuple[int, str, str, str]]:
    """
    (season, team_id, player_id, normalized name) for every NBA player on
    every team they appeared for, from the local parquet files
    """
    files = outdir / "nba" / "stats"
    read = "read_parquet('{}', hive_partitioning = false)"
    rows = duckdb.sql(
        f"""
        WITH teams AS (
            SELECT DISTINCT season, team_id, player_id
            FROM {read.format(files / "player_game_logs" / "*" / "data.parquet")}
            UNION
            SELECT DISTINCT season, team_id, player_id
            FROM {read.format(files / "player_season_stats" / "*" / "data.parquet")}
        )
        SELECT t.season, t.team_id, t.player_id, p.name
        FROM teams t
        JOIN {read.format(files / "player_seasons" / "*" / "data.parquet")} p
            USING (season, player_id)
        WHERE p.name IS NOT NULL
        """
    ).fetchall()
    return [(s, t, p, bbref.normalize_name(n)) for s, t, p, n in rows]


def bbref_players(outdir: Path, seasons) -> list[SourcePlayer]:
    """every bbref (player, team, season) on the saved per-game pages"""
    abbrevs = [a for a in load_team_abbrevs() if a.source == "bbref"]
    players = []
    for season in seasons:
        path = bbref.season_page_path(outdir, season, "per_game")
        if not path.is_file():
            continue
        with gzip.open(path, "rt") as f:
            html = f.read()
        for p in bbref.season_players(html):
            team = [
                a.team_id for a in abbrevs if a.abbrev == p.team and a.covers(season)
            ]
            if len(team) != 1:
                raise ValueError(
                    f"bbref team {p.team} in {season}: no team_id; add it to team_abbrevs.csv"
                )
            players.append(SourcePlayer(p.bbref_id, p.name, season, team[0]))
    return players


def match(
    players: list[SourcePlayer], nba: list[tuple[int, str, str, str]]
) -> tuple[dict[str, tuple[str, str]], dict[str, set[str]]]:
    """
    Match source players to NBA players by name. Returns
    (source_id -> (player_id, method), source_id -> candidate player_ids)
    for the matched and the unresolved. A source id is matched if every
    season where its name matches anyone points to the same single NBA
    player; same-team matches win over same-season ones
    """
    by_team = defaultdict(set)
    by_season = defaultdict(set)
    for season, team_id, player_id, name in nba:
        by_team[(season, team_id, name)].add(player_id)
        by_season[(season, name)].add(player_id)

    team_hits: dict[str, set[str]] = defaultdict(set)
    season_hits: dict[str, set[str]] = defaultdict(set)
    seen = set()
    for p in players:
        seen.add(p.source_id)
        name = bbref.normalize_name(p.name)
        team_hits[p.source_id] |= by_team.get((p.season, p.team_id, name), set())
        season_hits[p.source_id] |= by_season.get((p.season, name), set())

    matched, unresolved = {}, {}
    for sid in sorted(seen):
        if len(team_hits[sid]) == 1:
            matched[sid] = (next(iter(team_hits[sid])), "name_team")
        elif not team_hits[sid] and len(season_hits[sid]) == 1:
            matched[sid] = (next(iter(season_hits[sid])), "name_season")
        else:
            unresolved[sid] = team_hits[sid] | season_hits[sid]

    # two source ids matching one NBA player means one of them is wrong
    by_nba = defaultdict(list)
    for sid, (pid, _) in matched.items():
        by_nba[pid].append(sid)
    for pid, sids in by_nba.items():
        if len(sids) > 1:
            for sid in sids:
                unresolved[sid] = {pid}
                del matched[sid]
    return matched, unresolved


def update(
    outdir: Path,
    seasons,
    fetcher: bbref.Fetcher | None,
    path: Path = PLAYER_IDS_FILE,
    max_fetches: int = 200,
) -> list[str]:
    """
    Add newly matched bbref players to the mapping at `path`. Returns the
    bbref ids still unmapped
    """
    ids = load_player_ids(path)
    players = bbref_players(outdir, seasons)
    new = [p for p in players if ("bbref", p.source_id) not in ids]
    matched, unresolved = match(new, nba_players(outdir))
    taken = {p.player_id for p in ids.values() if p.source == "bbref"}
    for sid, (pid, method) in matched.items():
        if pid in taken:
            unresolved[sid] = {pid}
            continue
        ids[("bbref", sid)] = PlayerId("bbref", sid, pid, method)
        taken.add(pid)
    print(
        f"player_ids: matched {len(matched)} bbref players by name, {len(unresolved)} unresolved"
    )

    left = []
    try:
        for i, sid in enumerate(sorted(unresolved)):
            if fetcher is None or i >= max_fetches:
                left.append(sid)
                continue
            pid = bbref.nba_id_from_player_page(bbref.player_page(fetcher, outdir, sid))
            if pid is None or pid in taken:
                print(f"player_ids: {sid}: player page gives {pid}; needs a hand fix")
                left.append(sid)
                continue
            ids[("bbref", sid)] = PlayerId("bbref", sid, pid, "player_page")
            taken.add(pid)
    finally:
        # keep what we found even if bbref stops us partway
        save_player_ids(ids, path)
    names = {p.source_id: p for p in players}
    for sid in left:
        p = names[sid]
        print(
            f"player_ids: unmapped bbref {sid} ({p.name}, {p.season}), candidates {sorted(unresolved[sid])}"
        )
    return left


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument(
        "--season", type=int, action="append", help="default: every saved season"
    )
    parser.add_argument(
        "--fetch", action="store_true", help="look unresolved players up on bbref"
    )
    parser.add_argument("--max-fetches", type=int, default=200)
    args = parser.parse_args(argv)
    seasons = args.season or range(2010, 2100)
    fetcher = bbref.Fetcher() if args.fetch else None
    left = update(args.out, seasons, fetcher, max_fetches=args.max_fetches)
    if left:
        raise SystemExit(f"player_ids: {len(left)} bbref players unmapped")


if __name__ == "__main__":
    main()
