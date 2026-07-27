"""
Editable spawn-rate configuration for the Maximo: Ghosts to Glory randomizer.

This module turns the randomizer's hard-coded weight tables into a user-editable
config, organized by TAG (enemies / items / structures) and, for enemies, split
WORLD-by-WORLD. Each entry carries a weight (0-100) and an enabled flag; a
disabled entry (or weight 0) is removed from every spawn pool so it never
appears in the randomized game.

The config is JSON-serializable so the GUI can save/load it, and exposes helper
methods that produce the exact weight dicts the randomizer pipeline consumes:

  - universal_weights(world): weights for randomize_universal()'s item pool
        (items + structures + the enemies available in that world).
  - enemy_weights_for_world(world): per-enemy weights for the cli.py even-out
        balancing bag.
  - disabled_enemies(world): enemy type-ids to drop from a world's pool.

Defaults are derived from items.DEFAULT_ITEM_WEIGHTS, scaled so the largest
entry maps to 100. Because the randomizer only cares about RELATIVE weights,
the default config reproduces the stock spawn behavior exactly.
"""
from __future__ import annotations

import json
from pathlib import Path

from .items import DEFAULT_ITEM_WEIGHTS, ENEMY_TIER_TYPES_PER_WORLD, ENEMY_TIER_WEIGHTS_PER_TYPE
from .catalog import (
    GRAVE_WALKING_MELEE,
    UNDER_WALKING_MELEE,
    SWAMP_WALKING_MELEE,
    ICE_WALKING_MELEE,
    CASTLE_WALKING_MELEE,
    WALKING_MELEE_WEIGHTS,
)

CONFIG_VERSION = 1

# Default config file name (lives next to the exe / cwd).
DEFAULT_CONFIG_FILENAME = "spawn_config.json"

# ---------------------------------------------------------------------------
# Type classification + display names
# ---------------------------------------------------------------------------
# Enemies (walking-melee roster). These are configured per-world; only the
# subset available in a world is shown/used there.
ENEMY_TYPES: dict[int, str] = {
    0x47: "Basic Skeleton",
    0x2F: "Axe Skeleton",
    0x39: "Sword Skeleton",
    0x54: "Guard Skeleton",
    0x49: "Bomb Skeleton",
    0x3A: "Pirate Skeleton",
    0x48: "Basic Zombie",
    0x6A: "Torso Zombie",
    0x10: "Frozen Zombie",
    0x19: "Swamp Zombie",
    0x0D: "Zombie Crocodile",
    0x6B: "Ghost / Poltergeist",
    0x37: "Raven",
    0x1D: "Snowman",
    0x13: "Goat Devil",
    0x27: "Hammer Devil",
    0x5D: "Doomed Soul",
    0x08: "Plant Monster",
    0x53: "Dark Knight",
    0x58: "Crazed Prisoner",
    0x16: "Axe Guard",
}

# Items (pickups + chests + ability/skill pickups).
ITEM_TYPES: dict[int, str] = {
    0x21: "Gold Coin",
    0x46: "Bag of Gold",
    0x52: "Gem",
    0x25: "Wooden Chest",
    0x5B: "Locked Chest",
    0x65: "Skeleton Key",
    0x45: "Armor Power-Up",
    0x51: "Full Health",
    0x4E: "Extra Life",
    0x24: "Collector",
    0x20: "Ability / Skill",
}

# Structures (smashables, decorations, spawners).
STRUCTURE_TYPES: dict[int, str] = {
    0x0A: "Popup Headstone",
    0x01: "Bone Tower",
    0x02: "Bonus Grave",
    0xB0: "Smashable Torch",
    0x30: "Grave Smashable Glass",
    0xA0: "Coffin Lid",
    0xC9: "Breakable Rockwall",
    0xAE: "Underworld Lantern",
    0xD3: "Breakable Tiki Torch",
    0x6C: "Coin Container",
    0xC8: "Spirit Statue",
    0xB5: "Swamp Coffin Lid",
    0xBB: "Thorn Vine",
    0x17: "Smashable Snow Pirate",
    0xB1: "Smashable Ice Wall",
    0x7C: "Ice Plant",
    0x1B: "Popup Ice",
    0x18: "Ice Torch",
    0xAA: "Smashable Door",
    0xAD: "Spirit Torch",
    0x4A: "Globe",
    0xBC: "Smashable Coffin",
    0x50: "Standing Coffin",
    0x5F: "Monster Generator",
}

WORLDS = ("grave", "under", "swamp", "ice", "castle")

# Special chest outcomes (not entity types) — controlled via chest content
# tags. Mimic = a chest that's secretly an enemy; Wizard = a chest that spawns
# a wizard. The editor value is a DIRECT PERCENT CHANCE that a given wooden
# chest becomes that type (independent for mimic and wizard).
SPECIAL_NAMES = {
    "mimic": "Mimic chest",
    "wizard": "Wizard chest",
}
SPECIAL_DEFAULTS = {"mimic": 12.0, "wizard": 10.0}  # default % chance per chest

_WORLD_ENEMY_AVAIL: dict[str, set[int]] = {
    "grave": set(GRAVE_WALKING_MELEE),
    "under": set(UNDER_WALKING_MELEE),
    "swamp": set(SWAMP_WALKING_MELEE),
    "ice": set(ICE_WALKING_MELEE),
    "castle": set(CASTLE_WALKING_MELEE),
}


def world_enemy_types(world: str) -> dict[int, str]:
    """Enemy type-id -> name map available in a world (vanilla BEF roster)."""
    avail = _WORLD_ENEMY_AVAIL.get(world, set())
    return {tid: ENEMY_TYPES[tid] for tid in ENEMY_TYPES if tid in avail}


# Default weight for a single enemy class level (tier) when vanilla used an
# even split. Basic_Zombie and any other type in ENEMY_TIER_WEIGHTS_PER_TYPE
# keep their stock bias.
TIER_DEFAULT_EQUAL = 50.0


def world_enemy_tier_types(world: str) -> dict[int, tuple[str, list[int]]]:
    """Tier-eligible enemies in a world -> (name, [valid tier values])."""
    return dict(ENEMY_TIER_TYPES_PER_WORLD.get(world, {}))


def tier_label(tier_value: int) -> str:
    """User-facing label for a tier value (0-indexed internally)."""
    return f"Lv {tier_value + 1}"


# ---------------------------------------------------------------------------
# Default weight derivation
# ---------------------------------------------------------------------------
def _scaled_default(tid: int, scale: float, fallback: float = 5.0) -> float:
    base = DEFAULT_ITEM_WEIGHTS.get(tid)
    if base is None:
        return fallback
    return round(base * scale, 1)


# Weight assigned to a "normal" enemy (one with WALKING_MELEE_WEIGHTS 1.0).
# Enemy defaults are derived from WALKING_MELEE_WEIGHTS — the table the
# randomizer normally uses to balance enemy frequency — so the editor's
# "Default" restores the exact stock enemy distribution (Raven/Ghost/Torso/
# Guard reduced, everything else full).
ENEMY_DEFAULT_BASE = 100.0


def _enemy_default(tid: int) -> float:
    return round(WALKING_MELEE_WEIGHTS.get(tid, 1.0) * ENEMY_DEFAULT_BASE, 1)


def _default_scale() -> float:
    mx = max(DEFAULT_ITEM_WEIGHTS.values()) if DEFAULT_ITEM_WEIGHTS else 100.0
    return 100.0 / mx if mx else 1.0


class SpawnConfig:
    """Editable spawn-rate table.

    Internal storage:
      items:       {tid: {"weight": float, "enabled": bool}}
      structures:  {tid: {"weight": float, "enabled": bool}}
      enemies:     {world: {tid: {"weight": float, "enabled": bool}}}
      enemy_tiers: {world: {tid: {tier_int: float weight}}}  (class levels)
      specials:    {key: {"weight": float, "enabled": bool}}  (mimic/wizard)
    """

    def __init__(self, items: dict, structures: dict, enemies: dict,
                 enemy_tiers: dict | None = None, specials: dict | None = None):
        self.items = items
        self.structures = structures
        self.enemies = enemies
        self.enemy_tiers = enemy_tiers if enemy_tiers is not None else {}
        self.specials = specials if specials is not None else {}

    # ---- construction ----------------------------------------------------
    @classmethod
    def default(cls) -> "SpawnConfig":
        scale = _default_scale()
        items = {
            tid: {"weight": _scaled_default(tid, scale), "enabled": True}
            for tid in ITEM_TYPES
        }
        structures = {
            tid: {"weight": _scaled_default(tid, scale), "enabled": True}
            for tid in STRUCTURE_TYPES
        }
        enemies = {}
        for world in WORLDS:
            avail = _WORLD_ENEMY_AVAIL[world]
            enemies[world] = {
                tid: {"weight": _enemy_default(tid), "enabled": True}
                for tid in ENEMY_TYPES if tid in avail
            }
        enemy_tiers = {}
        for world in WORLDS:
            tbl = {}
            for tid, (name, tiers) in ENEMY_TIER_TYPES_PER_WORLD.get(world, {}).items():
                bias = ENEMY_TIER_WEIGHTS_PER_TYPE.get(tid, {})
                tbl[tid] = {
                    t: float(bias.get(t, TIER_DEFAULT_EQUAL)) for t in tiers
                }
            enemy_tiers[world] = tbl
        specials = {
            key: {"weight": SPECIAL_DEFAULTS[key], "enabled": True}
            for key in SPECIAL_NAMES
        }
        return cls(items, structures, enemies, enemy_tiers, specials)

    # ---- JSON ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": CONFIG_VERSION,
            "items": {f"0x{t:02X}": v for t, v in self.items.items()},
            "structures": {f"0x{t:02X}": v for t, v in self.structures.items()},
            "enemies": {
                world: {f"0x{t:02X}": v for t, v in tbl.items()}
                for world, tbl in self.enemies.items()
            },
            "enemy_tiers": {
                world: {
                    f"0x{t:02X}": {str(tier): w for tier, w in tiers.items()}
                    for t, tiers in tbl.items()
                }
                for world, tbl in self.enemy_tiers.items()
            },
            "specials": dict(self.specials),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpawnConfig":
        # Start from defaults so missing/added types are handled gracefully,
        # then overlay whatever the saved file specifies.
        cfg = cls.default()

        def _overlay(target: dict, saved: dict):
            for k, v in (saved or {}).items():
                try:
                    tid = int(k, 16) if isinstance(k, str) else int(k)
                except (ValueError, TypeError):
                    continue
                if tid not in target:
                    continue
                if isinstance(v, dict):
                    if "weight" in v:
                        target[tid]["weight"] = _clamp(v["weight"])
                    if "enabled" in v:
                        target[tid]["enabled"] = bool(v["enabled"])

        _overlay(cfg.items, d.get("items"))
        _overlay(cfg.structures, d.get("structures"))
        for world, tbl in (d.get("enemies") or {}).items():
            if world in cfg.enemies:
                _overlay(cfg.enemies[world], tbl)
        # Enemy tiers (class levels).
        for world, tbl in (d.get("enemy_tiers") or {}).items():
            if world not in cfg.enemy_tiers:
                continue
            for k, tiers in (tbl or {}).items():
                try:
                    tid = int(k, 16) if isinstance(k, str) else int(k)
                except (ValueError, TypeError):
                    continue
                if tid not in cfg.enemy_tiers[world]:
                    continue
                for tk, w in (tiers or {}).items():
                    try:
                        tier = int(tk)
                    except (ValueError, TypeError):
                        continue
                    if tier in cfg.enemy_tiers[world][tid]:
                        cfg.enemy_tiers[world][tid][tier] = _clamp(w)
        # Specials (mimic / wizard) — string-keyed.
        for k, v in (d.get("specials") or {}).items():
            if k in cfg.specials and isinstance(v, dict):
                if "weight" in v:
                    cfg.specials[k]["weight"] = _clamp(v["weight"])
                if "enabled" in v:
                    cfg.specials[k]["enabled"] = bool(v["enabled"])
        return cfg

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "SpawnConfig":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)

    # ---- effective-weight helpers (consumed by the randomizer) -----------
    def chest_special_chances(self) -> dict[str, float]:
        """Per-chest % chance (0-100) that a wooden chest becomes a mimic /
        wizard. A disabled special contributes 0. The chest randomizer treats
        these as independent probabilities (clamped so mimic+wizard <= 100)."""
        out: dict[str, float] = {}
        for key in SPECIAL_NAMES:
            ent = self.specials.get(key, {})
            out[key] = float(ent.get("weight", 0)) if ent.get("enabled", True) else 0.0
        return out

    def disabled_enemies(self, world: str) -> set[int]:
        """Enemy type-ids that must NOT spawn in this world (disabled or 0)."""
        tbl = self.enemies.get(world, {})
        out = set()
        for tid, ent in tbl.items():
            if not ent.get("enabled", True) or ent.get("weight", 0) <= 0:
                out.add(tid)
        return out

    def enemy_weights_for_world(self, world: str) -> dict[int, float]:
        """Per-enemy relative weights for a world (enabled types only)."""
        tbl = self.enemies.get(world, {})
        out: dict[int, float] = {}
        for tid, ent in tbl.items():
            if ent.get("enabled", True) and ent.get("weight", 0) > 0:
                out[tid] = float(ent["weight"])
        return out

    def enemy_tier_weights_for_world(self, world: str) -> dict[int, dict[int, float]]:
        """Per-enemy class-level (tier) weights for a world, as consumed by
        reroll_enemy_tier: {type_id: {tier: weight}}. Every tier-eligible type
        is included (even all-zero) so the randomizer respects the user's
        choice to suppress a level rather than falling back to a uniform roll."""
        out: dict[int, dict[int, float]] = {}
        for tid, tiers in self.enemy_tiers.get(world, {}).items():
            out[tid] = {int(t): float(w) for t, w in tiers.items()}
        return out

    def universal_weights(self, world: str) -> dict[int, float]:
        """Weight dict for randomize_universal()'s pool in a given world:
        every enabled item + structure, plus the enemies available AND
        enabled in that world. Disabled entries are omitted (weight 0 →
        randomize_universal excludes them)."""
        out: dict[int, float] = {}
        for tid, ent in self.items.items():
            if ent.get("enabled", True) and ent.get("weight", 0) > 0:
                out[tid] = float(ent["weight"])
        for tid, ent in self.structures.items():
            if ent.get("enabled", True) and ent.get("weight", 0) > 0:
                out[tid] = float(ent["weight"])
        for tid, w in self.enemy_weights_for_world(world).items():
            out[tid] = w
        return out


def _clamp(value, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        v = float(value)
    except (ValueError, TypeError):
        return lo
    return max(lo, min(hi, round(v, 1)))


def load_spawn_config(path: str | Path | None):
    """Load a SpawnConfig from `path`. Returns None when no path is given or
    the file doesn't exist — the caller then uses stock behavior."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return SpawnConfig.load(p)
    except Exception:
        return None
