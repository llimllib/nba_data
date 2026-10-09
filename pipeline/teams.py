"""
Resolve team abbreviations from sources that don't give a team_id.

NBA data always carries team_id, so this is only for sources like ESPN that
identify teams by abbreviation. The mapping lives in team_abbrevs.csv, one row
per (source, abbrev) over a range of seasons. An empty last_season means the
abbreviation is still in use.

see "v2 Schema Conventions" in docs/DATA_DICTIONARY.md
"""
import csv
from dataclasses import dataclass
from pathlib import Path

TEAM_ABBREVS_FILE = Path(__file__).parent / "team_abbrevs.csv"


@dataclass(frozen=True)
class TeamAbbrev:
    source: str
    abbrev: str
    team_id: str
    first_season: int
    last_season: int | None

    def covers(self, season: int) -> bool:
        return self.first_season <= season and (
            self.last_season is None or season <= self.last_season
        )


class UnknownTeamAbbrev(Exception):
    pass


def load_team_abbrevs(path: Path = TEAM_ABBREVS_FILE) -> list[TeamAbbrev]:
    """
    Load and validate the abbreviation table. Raises ValueError if a row is
    malformed or if two rows could map the same (source, abbrev, season) to a
    team
    """
    rows = []
    with open(path, newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            row = TeamAbbrev(
                source=r["source"],
                abbrev=r["abbrev"],
                team_id=r["team_id"],
                first_season=int(r["first_season"]),
                last_season=int(r["last_season"]) if r["last_season"] else None,
            )
            if not (row.source and row.abbrev):
                raise ValueError(f"{path}:{i}: source and abbrev are required")
            if not row.team_id.isdigit():
                raise ValueError(f"{path}:{i}: team_id {row.team_id!r} is not an NBA id")
            if row.last_season is not None and row.last_season < row.first_season:
                raise ValueError(f"{path}:{i}: last_season is before first_season")
            rows.append(row)

    # two ranges for the same (source, abbrev) overlap if each starts before
    # the other ends
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if (a.source, a.abbrev) != (b.source, b.abbrev):
                continue
            if (a.last_season is None or b.first_season <= a.last_season) and (
                b.last_season is None or a.first_season <= b.last_season
            ):
                raise ValueError(
                    f"{a.source} {a.abbrev} has overlapping season ranges: {a}, {b}"
                )

    return rows


def team_id_for(
    rows: list[TeamAbbrev], source: str, abbrev: str, season: int
) -> str:
    """
    Return the team_id that `source` meant by `abbrev` in `season`. Raises
    UnknownTeamAbbrev if there isn't one
    """
    for row in rows:
        if row.source == source and row.abbrev == abbrev and row.covers(season):
            return row.team_id
    raise UnknownTeamAbbrev(f"no team_id for {source} {abbrev!r} in season {season}")
