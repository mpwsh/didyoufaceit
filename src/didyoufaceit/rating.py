"""Player rating: an approximation of HLTV Rating 2.0 from FACEIT stats.

FACEIT's own FACEIT Rating (Season 8) is not exposed by the Data API, and HLTV Rating 3.0
needs round-level win probabilities, so we use the community regression of HLTV 2.0:

    Rating = 0.0073*KAST + 0.3591*KPR - 0.5329*DPR + 0.2372*Impact + 0.0032*ADR + 0.1587
    Impact = 2.13*KPR + 0.42*APR - 0.41

FACEIT does not expose KAST, so it is held at a constant. Estimating it from K/D would
count K/D twice, since KPR and DPR already carry it.

Two sources feed it:
- lifetime per-map segments (/players/{id}/stats/cs2): large samples, but years old;
- recent matches (/players/{id}/games/cs2/stats): one row per match, used for current form.
"""

from __future__ import annotations

from collections.abc import Iterable

from .maps import normalize_map_id

KAST_CONSTANT = 72.0  # typical KAST %, FACEIT does not expose it
RECENT_MATCHES = 100  # one /games/cs2/stats page
SHRINK_MATCHES = 8  # per-map rating is pulled toward the player's overall rating by this many matches


def hltv2_approx(kills: float, deaths: float, assists: float, rounds: float, adr: float) -> float:
    """Rating from totals. Returns 0.0 when there are no rounds."""
    if rounds <= 0:
        return 0.0
    kpr = kills / rounds
    dpr = deaths / rounds
    apr = assists / rounds
    impact = 2.13 * kpr + 0.42 * apr - 0.41
    return 0.0073 * KAST_CONSTANT + 0.3591 * kpr - 0.5329 * dpr + 0.2372 * impact + 0.0032 * adr + 0.1587


def shrink(value: float, n: float, prior: float, k: float = SHRINK_MATCHES) -> float:
    """Pull a small-sample value toward a prior: (n*value + k*prior) / (n + k)."""
    if n <= 0:
        return prior
    return (n * value + k * prior) / (n + k)


def _num(stats: dict, key: str) -> float:
    try:
        return float(stats.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


class _Totals:
    __slots__ = ("matches", "wins", "kills", "deaths", "assists", "rounds", "damage")

    def __init__(self) -> None:
        self.matches = self.wins = 0
        self.kills = self.deaths = self.assists = self.rounds = self.damage = 0.0

    def add(self, stats: dict) -> None:
        rounds = _num(stats, "Rounds")
        self.matches += 1
        self.wins += 1 if str(stats.get("Result")) == "1" else 0
        self.kills += _num(stats, "Kills")
        self.deaths += _num(stats, "Deaths")
        self.assists += _num(stats, "Assists")
        self.rounds += rounds
        # Damage is the raw total when present; otherwise rebuild it from ADR x rounds
        self.damage += _num(stats, "Damage") or _num(stats, "ADR") * rounds

    def summary(self) -> dict:
        adr = self.damage / self.rounds if self.rounds else 0.0
        return {
            "matches": self.matches,
            "wins": self.wins,
            "kd": round(self.kills / self.deaths, 2) if self.deaths else round(self.kills, 2),
            "adr": round(adr, 1),
            "rating": round(hltv2_approx(self.kills, self.deaths, self.assists, self.rounds, adr), 2),
        }


def recent_form(rows: Iterable[dict]) -> dict:
    """Aggregate /games/cs2/stats items into {"overall": {...}, "maps": {map_id: {...}}}.

    Only 5v5 matches count. Each map's rating is shrunk toward the overall rating, so a
    map played twice doesn't swing the player's number.
    """
    overall = _Totals()
    by_map: dict[str, _Totals] = {}
    for row in rows:
        stats = row.get("stats", row)
        if stats.get("Game Mode", "5v5") != "5v5":
            continue
        map_id = normalize_map_id(stats.get("Map"))
        if not map_id or _num(stats, "Rounds") <= 0:
            continue
        overall.add(stats)
        by_map.setdefault(map_id, _Totals()).add(stats)

    total = overall.summary()
    maps = {}
    for map_id, t in by_map.items():
        s = t.summary()
        s["rating_raw"] = s["rating"]
        s["rating"] = round(shrink(s["rating"], t.matches, total["rating"]), 2)
        maps[map_id] = s
    return {"overall": total, "maps": maps}


def lifetime_map_rating(stats: dict) -> tuple[float, float, float]:
    """(rating, pooled K/D, ADR) from one lifetime 5v5 map segment.

    Segment "K/D Ratio" is a sum of per-match ratios and "Average K/D Ratio" a mean of them;
    both run high, so K/D is recomputed from the kill and death totals.
    """
    kills, deaths = _num(stats, "Kills"), _num(stats, "Deaths")
    adr = _num(stats, "ADR")
    rating = hltv2_approx(kills, deaths, _num(stats, "Assists"), _num(stats, "Rounds"), adr)
    kd = kills / deaths if deaths else kills
    return round(rating, 2), round(kd, 2), round(adr, 1)
