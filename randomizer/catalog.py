"""
Catalog of known entity types in Maximo: Ghosts to Glory worlds.

Entity-type IDs and their per-world availability. Built from analyzing G_INTRO.PSX
and U_INTRO.PSX. Each pool contains entities that have been verified as drop-in
compatible with each other (same record format, no cross-spawn dependencies).
"""
from __future__ import annotations


# Walking-melee enemy pool: safe to swap freely within a world.
# All have similar AI/spawn requirements (just appear and walk toward player).
# Ghost included so it gets type-swapped too (its AI is different but the
# template-copy approach has worked fine in our tests).
WALKING_MELEE_POOL = {
    0x47: "Basic_Skeleton",
    0x2F: "Axe_Skeleton",
    0x39: "SwordSkeleton",
    0x48: "Basic_Zombie",
    0x6B: "Ghost",
    0x54: "Guard_Skeleton",  # native to GRAVE in HUB/SUB1/SUB2
    0x6A: "Torso_Zombie",    # zombie variant
    0x37: "Raven",           # small flying enemy
    0x49: "Bomb_Skeleton",   # Underworld + Ice + Castle
    0x27: "Hammer_Devil",    # Underworld only
    0x5D: "Doomed_Soul",     # Underworld only
    # World-specific enemies (added per user request)
    0x10: "Frozen_Zombie",      # ICE (41 vanilla)
    0x3A: "PirateSkeleton",     # ICE (35 vanilla)
    0x1D: "Snowman",            # ICE (18 vanilla)
    0x13: "Goat_Devil",         # UNDER (14 vanilla)
    0x19: "Swamp_Zombie",       # SWAMP (20 vanilla)
    0x0D: "Zombie_Crocodile",   # SWAMP (16 vanilla)
    0x08: "Plant_Monster",      # SWAMP+CASTLE (23 vanilla)
    0x53: "Dark_Knight",        # CASTLE (10 vanilla)
    0x58: "Crazed_Prisoner",    # CASTLE (7 vanilla)
    0x16: "Axe_Guard",          # CASTLE (17 vanilla)
}

# Per-world available walking-melee types. These reflect what each
# world's BEF TOC actually declares (verified via BEF parsing) — NOT
# what cli.py originally guessed. Worlds whose catalog entry was
# narrower than reality (Castle missing 0x48/0x6A, Ice missing 0x6B)
# have been corrected so the BEF inject step doesn't waste budget
# trying to add types the host already has.
GRAVE_WALKING_MELEE  = {0x2F, 0x37, 0x39, 0x47, 0x48, 0x54, 0x6A, 0x6B}
UNDER_WALKING_MELEE  = {0x13, 0x27, 0x2F, 0x39, 0x47, 0x48, 0x49, 0x54,
                        0x5D, 0x6A, 0x6B}
SWAMP_WALKING_MELEE  = {0x08, 0x0D, 0x19, 0x2F, 0x37, 0x39, 0x47, 0x48,
                        0x54, 0x6A, 0x6B}
ICE_WALKING_MELEE    = {0x10, 0x1D, 0x39, 0x3A, 0x47, 0x49, 0x54, 0x6B}
CASTLE_WALKING_MELEE = {0x08, 0x16, 0x2F, 0x37, 0x39, 0x47, 0x48, 0x49,
                        0x53, 0x54, 0x58, 0x6A, 0x6B}

# CROSS-WORLD universal pool: ALL walking-melee enemy types. Only usable when
# cross-world BEF injection is active (every world's BEF has all enemy blobs).
UNIVERSAL_WALKING_MELEE = set(WALKING_MELEE_POOL.keys())

# Enemies whose mesh/texture resources are in WORLD-SPECIFIC PRS files.
# These CANNOT be injected into other worlds via cross-world mode — their
# visuals (and often animation/texture-page indices) only exist in their
# home world's resource pack, so injecting them elsewhere and forcing
# GLOBAL resource loading makes the engine look up a mesh/texture that was
# never loaded, crashing (ejecting the disc on PCSX2) as soon as that
# world's map tries to render the foreign enemy.
#
# IMPORTANT CORRECTION: every world actually ships its own .PRS/.PRT pair
# on the retail disc (GRAVE.PRS, UNDER.PRS, ICE.PRS/ICE_B.PRS, SWAMP.PRS/
# SWAMP_B.PRS, CASTLE.PRS/CASTLE_K.PRS/CASTLE_Q.PRS), in addition to
# GLOBAL.PRS. An earlier version of this comment claimed ICE/SWAMP/CASTLE
# had "no separate PRS" and treated their world-exclusive monsters as
# cross-world-safe — that was never verified and is contradicted by the
# files on disc. Any enemy that's only native to 1-2 worlds is almost
# certainly stored in that world's own PRS, not GLOBAL.PRS, and must be
# locked here. Only enemies natively used in 3+ worlds (strong evidence of
# a shared/global mesh, since it would be wasteful to duplicate the same
# monster's assets into 3+ separate per-world PRS files) are left unlocked.
PRS_LOCKED_ENEMIES: set[int] = set()
# PRS_LOCKED_ENEMIES was previously populated with world-exclusive enemies
# based on the assumption that "only in 1-2 world BEFs = mesh in world PRS only".
# This heuristic was proven WRONG by GRAVE_CROSSWORLD.BEF:
#   - 0x0D (Zombie_Crocodile) is only in SWAMP.BEF but works perfectly in
#     GRAVE_CROSSWORLD.BEF (zero byte modifications to the blob).
#   - 0x16 (Axe_Guard) is only in CASTLE.BEF and also works in GRAVE_CROSSWORLD.
# Both enemies render correctly because ALL enemy meshes are in GLOBAL.PRS,
# not in world-specific PRS files. World-specific PRS files contain only
# level geometry, particles, and prop textures — not enemy meshes.
# inject_cross_world_enemies copies blobs as-is (remap_blob_textures is a no-op),
# so every enemy in the pool is safe to inject into every world's BEF.

# Types safe to inject cross-world (meshes in GLOBAL.PRS, always available)
CROSS_WORLD_SAFE_ENEMIES = UNIVERSAL_WALKING_MELEE - PRS_LOCKED_ENEMIES


# Per-type spawn weights for the even-out bag in cli.py.
# Default is 1.0 for every type. Lowered values mean a type appears LESS
# often in the balanced bag (slot count = round(weight * N / total_weight)).
#
# Raven, Ghost, and Torso_Zombie get reduced weight because:
#   - Raven has a small AI sight radius (vanilla) and often feels passive,
#     so an even share of Ravens makes maps feel under-populated.
#   - Ghost and Torso_Zombie are visually prominent floating/wandering enemies
#     and feel over-represented at full share even when statistically equal.
WALKING_MELEE_WEIGHTS = {
    0x37: 0.2,  # Raven         (reduced further — user reported too many)
    0x6B: 0.3,  # Ghost         (reduced further — user reported too many)
    0x6A: 0.2,  # Torso_Zombie  (reduced further — user reported too many)
    0x54: 0.4,  # Guard_Skeleton
    # Everything else defaults to 1.0
}


# Enemy instance-name patterns that are EVENT-TIED to level progression.
# These enemies trigger gates / level finisher / boss doors when killed —
# replacing them with foreign types that don't fire the death event will
# soft-lock the level. Match is substring + lowercase.
# Enemy instance-name patterns that are EVENT-TIED to level progression.
#
# This list is now EMPTY. Empirical testing + reverse-engineering confirmed
# that the engine wires kill events by instance_name + instance_id (which we
# always preserve when type-swapping), NOT by entity type. So any walking-melee
# enemy that dies in a 'gate1' / 'endgate1' / 'caveexit' slot fires the same
# event. Type-swapping is fully safe.
#
# Original protected patterns (now disabled): caveexit, final, goal, boss.
# All of them turned out to be triggered by the SAME instance-name event, so
# any walking-melee type works as a stand-in.
EVENT_ENEMY_NAME_PATTERNS: tuple = ()

# Specific instance names that ARE event-tied (override list). Empty.
EVENT_TIED_INSTANCE_NAMES = frozenset()


def is_event_tied_enemy(instance_name: str) -> bool:
    """True if this enemy's instance name suggests it's tied to a level event
    (level finisher, gate trigger, etc). Such enemies must NOT be replaced
    with foreign types — only swapped with other native walking-melee types
    whose death events behave the same way.

    Patterns are deliberately narrow now. The earlier 'end' / 'gate' substrings
    were over-protective: they matched regular kill-room enemies (end1..endN,
    cursedend, gatepath1, etc.) that don't actually fire any script trigger.
    Only enemies whose names map to KNOWN gate / level-finisher triggers are
    protected.
    """
    n = instance_name.lower()
    if n in EVENT_TIED_INSTANCE_NAMES:
        return True
    return any(pat in n for pat in EVENT_ENEMY_NAME_PATTERNS)


# Item/treasure pool: chests, coins, etc. Probably swappable within a category.
TREASURE_POOL = {
    0x21: "GoldCoin",
    0x46: "Bag_Of_Gold",
    0x6C: "CoinContainer",  # underworld
    0x4E: "Extra_Life",
    0x65: "Skeleton_Key",
    0x25: "Wooden_Chest",
    0x5B: "Locked_Chest",
}

# Trigger/control pool — typically NOT randomized (would break level flow)
TRIGGER_POOL = {
    0x1F: "Trigger",
    0xA3: "CamTrigger",
    0xD7: "Rain_Trigger",
    0xAF: "TriggerOnHit",
}


def get_world_from_bef_path(bef_path: str) -> str | None:
    """Identify a world from its BEF reference path."""
    bef_lower = bef_path.lower()
    if "grave" in bef_lower:
        return "grave"
    if "under" in bef_lower:
        return "under"
    if "swamp" in bef_lower:
        return "swamp"
    if "ice" in bef_lower:
        return "ice"
    if "castle" in bef_lower:
        return "castle"
    return None


def get_walking_melee_for_world(world: str, cross_world: bool = False) -> set[int]:
    """Get available walking-melee type IDs for a world.

    When cross_world=True, returns the CROSS_WORLD_SAFE set (enemies whose
    meshes are in GLOBAL.PRS and work anywhere). The cli.py override further
    limits this to types actually in the world's BEF after injection.
    """
    if cross_world:
        return CROSS_WORLD_SAFE_ENEMIES.copy()
    if world == "grave":
        return GRAVE_WALKING_MELEE
    if world == "under":
        return UNDER_WALKING_MELEE
    if world == "swamp":
        return SWAMP_WALKING_MELEE
    if world == "ice":
        return ICE_WALKING_MELEE
    if world == "castle":
        return CASTLE_WALKING_MELEE
    return {0x47, 0x48}


def name_for_type(type_id: int) -> str:
    """Get readable name for a type id (looks across all pools)."""
    for pool in (WALKING_MELEE_POOL, TREASURE_POOL, TRIGGER_POOL):
        if type_id in pool:
            return pool[type_id]
    return f"Unknown_0x{type_id:02X}"
