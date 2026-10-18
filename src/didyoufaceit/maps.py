"""CS2 map pool — the one place to change when FACEIT rotates a map.

The matchroom analyzer prefers the pool FACEIT sends with each match
(``voting.map.entities``), so a rotation usually needs no change here at all.
``ACTIVE_POOL`` is only the fallback for matches without voting data.

To rotate without a deploy, set ``MAP_POOL`` (comma-separated), e.g.
``MAP_POOL=de_ancient,de_anubis,de_cache,de_dust2,de_inferno,de_mirage,de_nuke,de_vertigo``.
"""

from __future__ import annotations

import os

# Every map a FACEIT 5v5 room can have: the seven-map pool since 2026-07-06 (Cache replaced
# Overpass) plus Vertigo, an optional map since Season 9 (2026-08-05). Players can turn maps off,
# so a room's real pool is often smaller; this is only the fallback when FACEIT sends none.
DEFAULT_POOL: tuple[str, ...] = (
    "de_ancient",
    "de_anubis",
    "de_cache",
    "de_dust2",
    "de_inferno",
    "de_mirage",
    "de_nuke",
    "de_vertigo",
)

# Display names for current and retired maps. Unknown ids fall back to a title-cased name.
MAP_NAMES: dict[str, str] = {
    "de_ancient": "Ancient",
    "de_anubis": "Anubis",
    "de_cache": "Cache",
    "de_cobblestone": "Cobblestone",
    "de_dust2": "Dust2",
    "de_inferno": "Inferno",
    "de_mirage": "Mirage",
    "de_nuke": "Nuke",
    "de_overpass": "Overpass",
    "de_train": "Train",
    "de_vertigo": "Vertigo",
}


def normalize_map_id(label: str | None) -> str | None:
    """Turn a FACEIT label ('Dust2', 'de_dust2', 'Dust 2', 'dust2') into a map id ('de_dust2')."""
    if not label:
        return None
    key = label.strip().lower().replace(" ", "")
    if not key:
        return None
    if key.startswith(("de_", "cs_", "ar_")):
        return key
    return "de_" + key.replace("_", "")


def _pool_from_env() -> tuple[str, ...]:
    raw = os.environ.get("MAP_POOL", "")
    ids = [normalize_map_id(part) for part in raw.split(",")]
    pool = tuple(dict.fromkeys(i for i in ids if i))  # dedupe, keep order
    return pool or DEFAULT_POOL


ACTIVE_POOL: tuple[str, ...] = _pool_from_env()


def map_display_name(map_id: str) -> str:
    return MAP_NAMES.get(map_id) or map_id.removeprefix("de_").capitalize()
