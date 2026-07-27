"""
ELF (SLUS_200.17) patching for runtime-displayed level titles.

The PSX-stored Level_Column instance_name is just an internal identifier.
The actual level TITLE shown on-screen is hardcoded in the retail ELF at
fixed offsets, looked up by world+level_id.

Renaming Level_Columns alone has no visual effect — the engine reads
from the ELF string table. We have to patch the ELF too.

All 18 known level-title slots in retail SLUS_200.17 (verified by string
search against a Maximo level list):

  GRAVE:
    0x000CD1C8: 16 - "Grave Danger"      (world name)
    0x000CD1D8: 16 - "The Tomb Tower"    (intro/locked area)
    0x000CD1E8: 16 - "Dead Heat"         (level 1)
    0x000CD1F8: 24 - "Coffin Canyon"     (level 2)
    0x000CD210: 16 - "Bad to the Bone"   (level 3)
    0x000CD220: 16 - "Ghastly Gus"       (boss)
  SWAMP:
    0x000CD230: 16 - "Watery Grave"      (locked column)
    0x000CD240: 16 - "The Magic Tree"    (intro)
    0x000CD250: 16 - "Voodoo Village"    (level 1)
    0x000CD260: 16 - "'Dem Bones"        (level 2)
    0x000CD270: 24 - "Quick and the Dead" (level 3)
    0x000CD288: 24 - "Bokor LaBas"       (boss)
  ICE:
    0x000CD2A0: 24 - "Shiver Me Timbers" (locked column)
    0x000CD2B8: 24 - "The Dark House"    (intro)
    0x000CD2D0: 32 - "Go With The Floe"  (level 1)
    0x000CD2F0: 24 - "Dead in the Water" (level 2)
    0x000CD308: 24 - "Cannonball Run"    (level 3)
    0x000CD320: 16 - "Captain Cadaver"   (boss)
  UNDER:
    0x000CD330: 24 - "Infernal Devices"  (locked column)
    0x000CD348: 24 - "The Iron Tower"    (intro)
    0x000CD360: 16 - "Crushed Spirits"   (level 1)
    0x000CD370: 32 - "The Unkindest Cut" (level 2)
    0x000CD390: 16 - "Down The Gullet"   (level 3)
    0x000CD3A0: 24 - "Lord Glutterscum"  (boss)
  CASTLE:
    0x000CD3B8: 16 - "The Seige"         (locked column)
    0x000CD3C8: 24 - "The Keep"          (level 2 in C_HUB)
    0x000CD3E0: 16 - "The Great Escape"  (level 1 in C_HUB)
"""
from __future__ import annotations
import random
import re
import struct
from pathlib import Path


# Known boot-executable filenames per region. The randomizer auto-detects which
# one is present and applies the same patches to it (all patches are located by
# signature or anchor, so they are region-independent).
#   US: SLUS_200.17   JP: SLPM_621.27
KNOWN_EXECUTABLES = ("SLUS_200.17", "SLPM_621.27")


def find_executable(folder: Path | str) -> Path | None:
    """Return the path to the game's boot executable inside `folder`, matching
    any known region filename (case-insensitive). None if not found."""
    folder = Path(folder)
    names = {n.upper() for n in KNOWN_EXECUTABLES}
    # Fast path: exact known names.
    for n in KNOWN_EXECUTABLES:
        p = folder / n
        if p.exists():
            return p
    # Case-insensitive fallback.
    if folder.is_dir():
        for child in folder.iterdir():
            if child.is_file() and child.name.upper() in names:
                return child
    return None


# Title-table anchor for region-independent level-title patching. The English
# title block is byte-identical across US/JP; only its file offset shifts. We
# find this anchor and apply the delta to every LEVEL_TITLE_SLOTS offset.
TITLE_TABLE_ANCHOR = b"Grave Danger\x00"
TITLE_TABLE_ANCHOR_US_OFFSET = 0x000CD1C8


# ============================================================================
# Per-level ability/enchantment drop tables
# ============================================================================
# The retail SLUS holds 24 per-level "ability weight" tables (one per level),
# 31 bytes each at a 32-byte stride. Each table is indexed by ABILITY ID and
# the byte value is a spawn/drop weight (vanilla uses 0/5/10/15/20/25). These
# tables decide which power-ups a level can drop (verified: Grave's table has
# Fire Sword (id 15)=5, Swamp's has Sun (17)=5, Ice's has Ice (16)=5 — matching
# the world-locked sword enchantments enemies drop in vanilla).
#
# Sword enchantment ability IDs: 15=Fire, 16=Ice, 17=Sun, 18=Armageddon.
# Other ability IDs the tables use are skills (4=Projectiles, 5=Mask of Sorrow,
# 10/12/13/20=shield variants, plus a few higher slots). 22=Health.
#
# To let ANY world drop ANY skill/enchantment, we make every ability that
# vanilla drops SOMEWHERE droppable in EVERY level (with seed-randomized
# weights). The set of indices is computed from the vanilla tables themselves
# (so we only ever enable proven-valid drops), always including the four sword
# enchantments. Health (22) is left per-level so the health economy is
# unchanged. The block is located by the vanilla signature of the first table
# (AbilityGIntro) so we don't hard-code a file offset.
ABILITY_TABLE_SIGNATURE = bytes.fromhex(
    "00000f0a00000a00000000000f0f00050000000000000f00050a0f00000000")
ABILITY_TABLE_COUNT = 24
ABILITY_TABLE_STRIDE = 32
ABILITY_TABLE_LEN = 31          # bytes actually used per table (32nd is pad)
SWORD_ENCHANT_INDICES = (15, 16, 17, 18)  # Fire / Ice / Sun / Armageddon
HEALTH_ABILITY_INDEX = 22       # consumable — left per-level (not forced)
ABILITY_DROP_WEIGHTS = (5, 10, 15)


def patch_ability_drops(data: bytearray, seed: int | None) -> dict:
    """Randomize the per-level ability/skill DROP pool so every world can drop
    every skill and sword enchantment, not just its vanilla ones.

    Mutates `data` (the SLUS bytes) in place. Computes the union of every
    ability index that vanilla drops in any level (always including the four
    sword enchantments, excluding Health) and, for each of the 24 level tables,
    sets each of those indices to a seed-random non-zero weight. Returns stats.
    """
    base = bytes(data).find(ABILITY_TABLE_SIGNATURE)
    if base < 0:
        return {"found": False, "patched": 0, "base": None, "abilities": 0}
    snap = bytes(data[base:base + ABILITY_TABLE_COUNT * ABILITY_TABLE_STRIDE])
    # Union of ability indices vanilla drops anywhere (proven-valid drops).
    union: set[int] = set(SWORD_ENCHANT_INDICES)
    for i in range(ABILITY_TABLE_COUNT):
        for idx in range(ABILITY_TABLE_LEN):
            if snap[i * ABILITY_TABLE_STRIDE + idx] != 0:
                union.add(idx)
    union.discard(HEALTH_ABILITY_INDEX)
    union_sorted = sorted(union)
    rng = random.Random((seed if seed is not None else 0) ^ 0x5AB11D1E)
    patched = 0
    for i in range(ABILITY_TABLE_COUNT):
        toff = base + i * ABILITY_TABLE_STRIDE
        for idx in union_sorted:
            o = toff + idx
            if o < len(data):
                data[o] = rng.choice(ABILITY_DROP_WEIGHTS)
                patched += 1
    return {"found": True, "patched": patched, "base": base,
            "tables": ABILITY_TABLE_COUNT, "abilities": len(union)}


# Backwards-compatible alias (older callers).
def patch_sword_enchant_drops(data: bytearray, seed: int | None) -> dict:
    return patch_ability_drops(data, seed)


# ============================================================================
# Damage Maximo TAKES from enemies
# ============================================================================
# Maximo's on-screen health bar is GameVar #2 (the player game-variable array
# lives at gPlayer+0x48, so index 2 == gPlayer+0x50 — the float the HUD reads
# and CheckPlayerFullHealth tests). Health damage is applied in Player::OnHit
# via a call to GameVarSub(this, /*var*/2, /*amount*/damage):
#     addiu a1, zero, 2      # 24050002   var index 2 = HEALTH
#     sw    v0, 0(v1)        # ac620000
#     por   a0, s4, zero     # 72802628   a0 = this (gPlayer)
#     por   a2, s2, zero     # 72403628   a2 = s2 = incoming damage   <-- target
#     jal   GameVarSub
#     por   a3, zero, zero   # 70003e28
# (The earlier player+0x70 write that we used to patch is a DIFFERENT HUD bar,
# not health — scaling it had no visible effect. Verified against the demo
# debug symbols: gPlayer @0x287500, health == GameVar #2, OnHit feeds the
# damage to GameVarSub with var index 2.)
#
# We replace the `por a2, s2, zero` move with `sra/sll a2, s2, N`, which both
# moves the damage into a2 AND scales it in a single instruction — no free slot
# needed and it lands exactly on the health-damage argument. The 16-byte
# sequence (addiu a1,2 / sw / por a0,s4 / por a2,s2) is unique in the SLUS, so
# we locate it by signature and patch its last word.
DAMAGE_HIT_SIGNATURE = bytes.fromhex("02000524000062ac2826807228364072")
DAMAGE_POR_A2_S2 = 0x72403628  # por a2, s2, zero  (the instruction we replace)
DAMAGE_SCALE_WORDS = {
    "0.25": 0x00123083,  # sra a2,s2,2  -> x0.25 damage
    "0.50": 0x00123043,  # sra a2,s2,1  -> x0.5  damage
    "2x":   0x00123040,  # sll a2,s2,1  -> x2    damage
    "4x":   0x00123080,  # sll a2,s2,2  -> x4    damage
}


def patch_damage_taken(data: bytearray, mode: str | None) -> dict:
    """Scale the damage Maximo takes from enemies by a power-of-two factor.

    mode: 'quarter' | 'half' | 'normal' | 'double' | 'quad' (None/'normal' =
    no change). Mutates `data` in place. Returns a stats dict.

    Patches the `por a2, s2, zero` that feeds the incoming damage into
    GameVarSub(this, 2 /*HEALTH*/, amount) inside Player::OnHit, turning the
    plain move into a power-of-two scale of the damage.
    """
    if not mode or mode == "normal":
        return {"applied": False, "mode": "normal"}
    word = DAMAGE_SCALE_WORDS.get(mode)
    if word is None:
        return {"applied": False, "mode": mode, "reason": "unknown mode"}
    sig = bytes(data).find(DAMAGE_HIT_SIGNATURE)
    if sig < 0:
        return {"found": False, "applied": False, "mode": mode}
    slot = sig + 12  # the `por a2, s2, zero` (last word of the signature)
    if bytes(data[slot:slot + 4]) != struct.pack("<I", DAMAGE_POR_A2_S2):
        return {"found": True, "applied": False, "mode": mode,
                "reason": "expected 'por a2,s2' not found at target"}
    struct.pack_into("<I", data, slot, word)
    return {"found": True, "applied": True, "mode": mode, "slot": slot}


# ============================================================================
# Damage Maximo DEALS to enemies
# ============================================================================
# Maximo's attack damage is configured in Player::SetAction, which writes the
# attacking object's damage range via DEDynamicObject::SetDamage(this, min,
# max). The `min` value (register a1) is the one scaled by Maximo's sword-power
# level (the per-action base in the low nibble of the action byte, multiplied
# by the sword upgrade level), so it is Maximo's effective melee damage. When
# the sword hitbox collides with an enemy, ObjectCollision reads it back with
# GetDamage and forwards it to CVirtualMachine::OnHit (the enemy-hit path —
# distinct from OnPlayerHit, so this does NOT touch the damage Maximo takes).
#
# Just before the SetDamage call the compiler emits a redundant
# `andi a1, a1, 0xff` (a1 was already masked one instruction earlier). We
# replace that redundant mask with `sra/sll a1, a1, N` to scale Maximo's
# outgoing melee damage in a single instruction. The 16-byte sequence
# (addu v0,a1,v0 / andi a1,v0,0xff / lw a0,0(s4) / andi a1,a1,0xff) is unique
# in the SLUS, so we locate it by signature and patch its last word.
DAMAGE_DEALT_SIGNATURE = bytes.fromhex("2110a200ff0045300000848eff00a530")
DAMAGE_DEALT_ANDI_A1 = 0x30a500ff  # andi a1, a1, 0xff (redundant — we replace it)
DAMAGE_DEALT_SCALE_WORDS = {
    "0.25": 0x00052883,  # sra a1,a1,2  -> x0.25 damage dealt
    "0.50": 0x00052843,  # sra a1,a1,1  -> x0.5  damage dealt
    "2x":   0x00052840,  # sll a1,a1,1  -> x2    damage dealt
    "4x":   0x00052880,  # sll a1,a1,2  -> x4    damage dealt
}


def patch_damage_dealt(data: bytearray, mode: str | None) -> dict:
    """Scale the melee damage Maximo deals to enemies by a power-of-two factor.

    mode: 'quarter' | 'half' | 'normal' | 'double' | 'quad' (None/'normal' =
    no change). Mutates `data` in place. Returns a stats dict.

    Replaces the redundant `andi a1, a1, 0xff` just before
    DEDynamicObject::SetDamage in Player::SetAction with a power-of-two scale of
    the damage (register a1).
    """
    if not mode or mode == "normal":
        return {"applied": False, "mode": "normal"}
    word = DAMAGE_DEALT_SCALE_WORDS.get(mode)
    if word is None:
        return {"applied": False, "mode": mode, "reason": "unknown mode"}
    sig = bytes(data).find(DAMAGE_DEALT_SIGNATURE)
    if sig < 0:
        return {"found": False, "applied": False, "mode": mode}
    slot = sig + 12  # the redundant `andi a1, a1, 0xff` (last word of signature)
    if bytes(data[slot:slot + 4]) != struct.pack("<I", DAMAGE_DEALT_ANDI_A1):
        return {"found": True, "applied": False, "mode": mode,
                "reason": "expected 'andi a1,a1,0xff' not found at target"}
    struct.pack_into("<I", data, slot, word)
    return {"found": True, "applied": True, "mode": mode, "slot": slot}


# ============================================================================
# Starting inventory (GameVarTbl)
# ============================================================================
# Player::InitVars() seeds the player's 17-entry GameVar array (at player+0x48,
# 4 bytes/var) from a constant table `GameVarTbl`. Each table entry is 6 bytes:
#   [0:2] int16  initial value (what a NEW GAME starts with)
#   [2:4] uint16 max/cap value
#   [4]   u8     "locked/read-only" flag (checked by GameVarSet/Sub)
#   [5]   u8     persist flag
#
# Index meanings were recovered from the demo debug symbols (GameVarSub/Add
# special-cases): #4 = Lives (decremented on death, game-over at 0),
# #5 = Gold/Koins (cap 9999), #7 = Keys (drives UpdateKey). #2 = Health,
# #0/#3 = max/current armor.
#
# The table is byte-identical across DEMO/US/JP; only its file offset shifts,
# so we locate it by a unique structural anchor: entry #5's cap 0x270f (9999)
# followed 6 bytes later by entry #6's cap 0x03e7 (999). We then verify a few
# known caps before writing, and only overwrite the initial-value field of the
# requested vars (clamped to each var's existing cap — caps are left intact so
# the HUD never overflows).
import re as _re

GAMEVAR_TABLE_LEN = 17
GAMEVAR_ENTRY_SIZE = 6
# entry5 = ".. .. 0f 27 .. 01" , entry6 = ".. .. e7 03 .." -> anchor on the two caps
_GAMEVAR_ANCHOR = _re.compile(rb"\x0f\x27.\x01..\xe7\x03", _re.DOTALL)
GAMEVAR_GOLD_INDEX = 5
GAMEVAR_LIVES_INDEX = 4
GAMEVAR_KEYS_INDEX = 7
GAMEVAR_DEATHCOINS_INDEX = 8   # HUD "deathcoins#2" -> code 0x108 (cap 99)
GAMEVAR_SWORD_INDEX = 1        # sword enchant tier (1=Fire 2=Ice 3=Sun 4=Armageddon)
GAMEVAR_SHIELD_INDEX = 9       # elemental shield (1=Wind 2=Magnetic 3=Lightning)
GAMEVAR_SWORDUNITS_INDEX = 11  # sword charge/points (HUD "sword_units", cap 20)


def find_gamevar_table(data: bytearray | bytes) -> int | None:
    """Locate GameVarTbl (starting-inventory table) by structural anchor.
    Returns the file offset of entry #0, or None if not found / not verified."""
    m = _GAMEVAR_ANCHOR.search(bytes(data))
    if not m:
        return None
    tbl = m.start() - GAMEVAR_GOLD_INDEX * GAMEVAR_ENTRY_SIZE - 2
    if tbl < 0 or tbl + GAMEVAR_TABLE_LEN * GAMEVAR_ENTRY_SIZE > len(data):
        return None

    def cap(i: int) -> int:
        o = tbl + i * GAMEVAR_ENTRY_SIZE + 2
        return int.from_bytes(data[o:o + 2], "little")

    # Verify known caps: gold=9999, score=999, lives=99, keys=9.
    if cap(5) == 9999 and cap(6) == 999 and cap(4) == 99 and cap(7) == 9:
        return tbl
    return None


def patch_starting_inventory(data: bytearray, gold: int | None = None,
                             lives: int | None = None,
                             keys: int | None = None,
                             deathcoins: int | None = None,
                             sword_enchant: int | None = None,
                             elemental_shield: int | None = None) -> dict:
    """Set Maximo's NEW-GAME starting inventory by editing GameVarTbl.

    Any arg left as None is unchanged. `sword_enchant` sets the sword tier
    GameVar #1 (0=normal,1=Fire,2=Ice,3=Sun,4=Armageddon). `elemental_shield`
    sets GameVar #9 (0=none,1=Wind,2=Magnetic,3=Lightning) — the combat element
    read by ApplyGroundEffect/OnHit/ShieldThrowMove (no SetAbility, so no
    freeze). Values are clamped to each var's valid range.
    """
    targets = {GAMEVAR_GOLD_INDEX: gold,
               GAMEVAR_LIVES_INDEX: lives,
               GAMEVAR_KEYS_INDEX: keys,
               GAMEVAR_DEATHCOINS_INDEX: deathcoins,
               GAMEVAR_SWORD_INDEX: sword_enchant,
               GAMEVAR_SHIELD_INDEX: elemental_shield}
    if all(v is None for v in targets.values()):
        return {"applied": False, "reason": "no values requested"}
    tbl = find_gamevar_table(data)
    if tbl is None:
        return {"found": False, "applied": False}
    changed: dict[str, int] = {}
    names = {GAMEVAR_GOLD_INDEX: "gold", GAMEVAR_LIVES_INDEX: "lives",
             GAMEVAR_KEYS_INDEX: "keys", GAMEVAR_DEATHCOINS_INDEX: "deathcoins",
             GAMEVAR_SWORD_INDEX: "sword_enchant",
             GAMEVAR_SHIELD_INDEX: "elemental_shield"}
    for idx, val in targets.items():
        if val is None:
            continue
        off = tbl + idx * GAMEVAR_ENTRY_SIZE
        cap = int.from_bytes(data[off + 2:off + 4], "little")
        if idx == GAMEVAR_SWORD_INDEX:
            cap = min(cap, 4)   # only tiers 0-4 are valid sword enchants
        if idx == GAMEVAR_SHIELD_INDEX:
            cap = min(cap, 3)   # only 0-3 are valid elemental shields
        v = max(0, min(int(val), cap))
        struct.pack_into("<h", data, off, v)
        changed[names[idx]] = v
    # A sword enchant needs charge/points to be usable; SetAbility's enchant
    # path sets sword-units (GameVar #11) to its cap. Mirror that so the
    # enchanted move isn't spent after a single use.
    if sword_enchant is not None and changed.get("sword_enchant", 0) > 0:
        u_off = tbl + GAMEVAR_SWORDUNITS_INDEX * GAMEVAR_ENTRY_SIZE
        u_cap = int.from_bytes(data[u_off + 2:u_off + 4], "little")
        struct.pack_into("<h", data, u_off, u_cap)
        changed["sword_units"] = u_cap
    return {"found": True, "applied": bool(changed), "table": tbl,
            "changed": changed}


# ============================================================================
# Starting skills (full grant via the icon array + LoadIcons)
# ============================================================================
# Maximo's learned abilities live in two places: the active bit-flags in
# player+0x24, AND the "icon slot" array at player+0x424 (31 slots x 2 bytes:
# [ability id, state]; id 0xff/-1 = empty) that drives the skills MENU. On every
# level start Player::LoadIcons() walks player+0x424 and calls SetAbility(id)
# for each non-empty slot — which does the FULL grant: sets the 0x24 bit (so the
# move works) AND builds the coloured menu icon. Player::InitVars() fills the
# whole 0x424 array with -1 on a new game.
#
# So to grant starting skills properly (move + menu icon) we just write the
# chosen ability IDs into the first 0x424 slots at new-game time and let the
# game's own LoadIcons do the rest. We do this with a tiny code cave:
#   * InitVars tail (located by signature) is: addiu v1,zero,0x12 ; sw v1,0x24(a0)
#     ; jr ra ; sb zero,0xb6(a0).  $a0 is the player throughout InitVars.
#   * We replace the `jr ra` with `j CAVE` (keeping the `sb 0xb6` delay slot), so
#     after the default flags are written we branch to the cave.
#   * The cave does, per selected skill: addiu v1,zero,<id> ; sb v1,0x424+slot*2(a0)
#     then `jr ra ; nop`. $a0 (player) is preserved across the jump.
# The cave is placed in zero padding that follows a NUL-terminated ASCII string
# (string-table padding — loaded, executable on the EE's RWX segment, and never
# used at runtime). Region-independent: hook by signature, cave by scan.
STARTING_SKILLS_SIGNATURE = bytes.fromhex("12000324240083ac")
# name -> ability id (index into the ability descriptor table)
# IMPORTANT: the sword ENCHANTMENTS (ids 15-18, SetAbility handler type -2) call
# a destructive icon-array routine that rewrites 0x424 while LoadIcons is walking
# it -> re-entrant freeze before the map loads. They are EXCLUDED here and are
# instead granted by setting the sword-tier GameVar #1 directly (see
# `sword_enchant` in patch_starting_inventory). Everything below is safe to grant
# from inside LoadIcons (verified in-game).
STARTING_SKILL_IDS = {
    # sword techniques
    "sword720":         1,   # 720-degree spin attack
    "double_slash":     2,   # double-sword slash
    "mighty_blow":      3,   # mighty sword blow
    "masquerade":       5,   # masquerade
    "sword_power":      0,   # increase sword power
    "projectile":       4,   # throwing projectile
    # shields (flag-based, safe to grant via LoadIcons; the ELEMENTAL shields
    # are NOT here — their type -3 SetAbility re-enters the icon array and
    # freezes LoadIcons, so they're applied via GameVar #9 instead, see
    # `elemental_shield` in patch_starting_inventory)
    "return_shield":    6,   # throw / return shield
    "hover_shield":     11,
    # armor
    "increase_armor":   14,
    # throw / misc
    "wide_shockwave":   7,
    "damage_shockwave": 8,
    "find_treasure":    9,
    "smart_bomb":       10,
    "increase_throw":   23,
}
# Backwards-compat alias (older callers referenced *_MASKS for the key set).
STARTING_SKILL_MASKS = STARTING_SKILL_IDS

# The player+0x24 ability bit for each FLAG ability (0 = not a flag ability, i.e.
# granted purely via SetAbility GameVars: armor/elemental shields). Move
# animations are loaded early in InitPlayer from these 0x24 bits, so we set them
# in the cave BEFORE LoadIcons runs — otherwise the move works but has no anim.
STARTING_SKILL_FLAGS = {
    "sword720":         0x00000100,
    "double_slash":     0x00001000,
    "mighty_blow":      0x00002000,
    "masquerade":       0x00004000,
    "sword_power":      0x01000000,
    "projectile":       0x00800000,
    "return_shield":    0x00010000,
    "hover_shield":     0x00020000,
    "wide_shockwave":   0x00080000,
    "damage_shockwave": 0x00100000,
    "find_treasure":    0x00200000,
    "smart_bomb":       0x40000000,
    "increase_throw":   0x00040000,
    # armor / elemental shields have no 0x24 flag (state lives in GameVars)
    "increase_armor":   0,
    "wind_shield":      0,
    "magnetic_shield":  0,
    "lightning_shield": 0,
}

_JR_RA = 0x03e00008
_SB_ZERO_B6_A0 = 0xa08000b6


def _first_load_segment(data: bytes):
    """Return (vaddr, file_off, filesz) of the first PT_LOAD segment, or None."""
    if data[:4] != b"\x7fELF":
        return None
    e_phoff = struct.unpack_from("<I", data, 0x1c)[0]
    e_phnum = struct.unpack_from("<H", data, 0x2c)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x2a)[0]
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        p_type, p_off, p_vaddr, p_paddr, p_filesz = struct.unpack_from("<IIIII", data, o)
        if p_type == 1:
            return p_vaddr, p_off, p_filesz
    return None


def _find_string_padding_cave(data: bytes, load_vaddr: int, load_off: int,
                              filesz: int, need: int) -> int | None:
    """Find a zero-run >= `need` bytes that follows a NUL-terminated ASCII
    string within the loaded image (string-table padding). Returns the cave
    FILE offset (4-aligned, a few bytes past the string terminator)."""
    end = min(load_off + filesz, len(data))
    for m in re.finditer(rb"\x00{%d,}" % (need + 8), data[load_off:end]):
        run_start = load_off + m.start()
        # the bytes immediately before the run should be printable ASCII (the
        # string body); the run's leading zeros are its NUL terminator + padding.
        k = run_start - 1
        ascii_len = 0
        while k > load_off and 0x20 <= data[k] < 0x7f:
            k -= 1
            ascii_len += 1
        if ascii_len >= 4:
            # leave the string terminator intact; start a few bytes in, 4-aligned
            cave = (run_start + 4) & ~3
            if cave + need <= run_start + len(m.group()):
                return cave
    return None


def patch_starting_skills(data: bytearray, skills) -> dict:
    """Grant Maximo new-game starting skills (move + skills-menu icon) by
    writing their ability IDs into player+0x424 via a code cave hooked at
    Player::InitVars. `skills` is an iterable of names from STARTING_SKILL_IDS.
    Mutates `data` in place. No-op (graceful) if the site/cave isn't found."""
    sel = [s for s in (skills or ()) if s in STARTING_SKILL_IDS]
    if not sel:
        return {"applied": False, "reason": "no skills requested"}
    if data[:4] != b"\x7fELF":
        return {"found": False, "applied": False, "reason": "not ELF"}
    seg = _first_load_segment(bytes(data))
    if seg is None:
        return {"found": False, "applied": False, "reason": "no LOAD seg"}
    load_vaddr, load_off, filesz = seg

    sig = bytes(data).find(STARTING_SKILLS_SIGNATURE)
    if sig < 0:
        return {"found": False, "applied": False, "reason": "InitVars not found"}
    jr_off = sig + 8       # the `jr ra`
    if struct.unpack_from("<I", data, jr_off)[0] != _JR_RA:
        return {"found": True, "applied": False, "reason": "jr ra not at +8"}
    if struct.unpack_from("<I", data, jr_off + 4)[0] != _SB_ZERO_B6_A0:
        return {"found": True, "applied": False, "reason": "sb 0xb6 not at +12"}

    # Build the cave. First (if any flag abilities are selected) OR their
    # 0x24 ability bits in EARLY so InitPlayer loads the move animations:
    #   lw v1,0x24(a0) ; lui v0,HI ; ori v0,v0,LO ; or v1,v1,v0 ; sw v1,0x24(a0)
    # Then per skill write the icon id:  addiu v1,zero,id ; sb v1,off(a0)
    # End: jr ra ; nop.   ($a0 = player throughout InitVars.)
    ids = [STARTING_SKILL_IDS[s] for s in sel]
    combined = 0
    for s in sel:
        combined |= STARTING_SKILL_FLAGS.get(s, 0)
    cave_words = []
    if combined:
        cave_words.append(0x8c830024)                        # lw  v1,0x24(a0)
        cave_words.append(0x3c020000 | ((combined >> 16) & 0xffff))  # lui v0,HI
        cave_words.append(0x34420000 | (combined & 0xffff))  # ori v0,v0,LO
        cave_words.append(0x00621825)                        # or  v1,v1,v0
        cave_words.append(0xac830024)                        # sw  v1,0x24(a0)
    for slot, aid in enumerate(ids):
        off = 0x424 + slot * 2
        cave_words.append(0x24030000 | (aid & 0xffff))      # addiu v1,zero,id
        cave_words.append(0xa0830000 | (off & 0xffff))      # sb v1,off(a0)
    # Set player+0x462 = 1 so the first LoadIcons PROCESSES the icon array (on a
    # new game it's 0, so the grant is otherwise skipped until a death runs
    # ResetAbility). SAFE because the icon list now contains only abilities whose
    # SetAbility uses the in-place AddIcon tail — the re-entrant type -2/-3
    # abilities (sword enchants, elemental shields) are applied via GameVars.
    cave_words.append(0x24030001)                            # addiu v1,zero,1
    cave_words.append(0xa0830462)                            # sb v1,0x462(a0)
    cave_words.append(_JR_RA)                                # jr ra
    cave_words.append(0x00000000)                            # nop (delay)
    need = len(cave_words) * 4

    cave_off = _find_string_padding_cave(bytes(data), load_vaddr, load_off,
                                         filesz, need)
    if cave_off is None or cave_off + need > len(data):
        return {"found": True, "applied": False, "reason": "no cave"}
    # Ensure the cave region is actually all-zero where we write.
    if any(data[cave_off:cave_off + need]):
        return {"found": True, "applied": False, "reason": "cave not clear"}

    cave_vaddr = load_vaddr + (cave_off - load_off)
    # write the cave
    for i, w in enumerate(cave_words):
        struct.pack_into("<I", data, cave_off + i * 4, w)
    # hook: replace `jr ra` with `j cave`
    j_instr = 0x08000000 | ((cave_vaddr >> 2) & 0x03ffffff)
    struct.pack_into("<I", data, jr_off, j_instr)
    return {"found": True, "applied": True, "skills": sel,
            "cave_vaddr": cave_vaddr, "cave_bytes": need,
            "slots": len(ids)}


# ============================================================================
# Level randomization (cross-world level loading via LevelTable)
# ============================================================================
# The engine has a master `LevelTable`: an array of `LevelFiles` entries, one
# per level slot, stride 0x34 (52 bytes). Recovered from the demo debug
# symbols (LevelTable @ demo vaddr 0x1e98a0). Each entry:
#   +0x00  u32  PSX path ptr      (e.g. "PSXDATA\GRAVE\G_SUB1.PSX")
#   +0x04  u32  sky PSX ptr
#   +0x08  u32  directory ptr
#   +0x0c  u32  extra ptr (usually 0)
#   +0x10  u32  world-code<<24 | global-index<<16 | flags
#   +0x14..+0x1c  camera / params
#   +0x20  u32  0
#   +0x24  u32  ability-table ptr
#   +0x28  u32  level-id (world<<16 | type<<8 | local-index)  <- IDENTITY
#   +0x2c  u32  flag
#   +0x30  u32  progression ptr
#
# To randomize levels without breaking hub/save progression we shuffle ONLY the
# file-pointer block [0x00:0x10] (PSX, sky, dir, extra — the 16 bytes that decide
# which geometry/textures load) among the playable sub-levels (paths matching
# "_SUB\d"). Everything else stays with the slot, so the hub still treats the
# column as its original level and progression advances normally.
#
# IMPORTANT — the shuffle is constrained to WITHIN EACH WORLD. Cross-world
# loading (e.g. a Swamp level in a Grave slot) hangs the engine on an infinite
# "loading" screen: the level's per-world resource context (BEF enemy/instance
# definitions, sound-effect bank at entry +0x13, world streaming set) is bound
# to the hub's world, not to the file pointers, so a foreign level waits forever
# on resources that never arrive. Keeping the shuffle inside one world means the
# loaded geometry always matches the already-resident world assets, so it loads
# cleanly. World membership is taken from the PSX path's folder
# (GRAVE/SWAMP/ICESHIP/UNDER/CASTLE).
#
# The table is located by STRUCTURAL signature (region-portable): a dword array
# at stride 0x34 whose +0x00 dword points at a ".PSX"-ending string for >=10
# consecutive entries. Verified: US file 0xcefc0, JP file 0xd19a0,
# demo file 0xe9920.
LEVEL_TABLE_STRIDE = 0x34
LEVEL_TABLE_MIN_ENTRIES = 10
LEVEL_FILE_BLOCK = 0x10            # PSX(+0x00) sky(+0x04) dir(+0x08) extra(+0x0c)
LEVEL_GEO_BLOCK = 0x08            # PSX(+0x00) + sky(+0x04) only (no dir change)
LEVEL_DEF_BLOCK = 0x28
# Byte ranges (offset, length) that a level swap copies from the source entry to
# the destination entry. We move:
#   [0x00:0x10]  the four file pointers (PSX / sky / directory / extra)
#   [0x14:0x1e]  the level's camera + player-start/world-bounds params (read by
#                InitWorld on level load) — this is what makes the player spawn
#                on the loaded geometry / inside the right kill-plane.
# We deliberately do NOT touch:
#   [0x10:0x14]  world/completion/music/sfx bytes,
#   [0x1e:0x20]  the LOAD-CLASS LIMIT (mglRunLevel feeds +0x1e to
#                SetLoadClassLimit) — a foreign value here makes the loader wait
#                on a class count that never matches and hangs the title /
#                attract "loading" screen,
#   [0x20:0x24]  an aux param,
#   [0x24:0x28]  the per-level drop/ability-table POINTER, and
#   [0x28:0x34]  the identity/flag/progression fields.
LEVEL_SWAP_RANGES = ((0x00, 0x10), (0x14, 0x0a))
_LEVEL_PSX_STR_RE = re.compile(rb"\x00([ -~]{3,}\.PSX)\x00")
# Boss-arena entries (scripted encounters): *_BOSS / *_KING / *_QUEEN. A world
# whose block has a boss-arena anywhere except the final slot has a non-standard
# layout (Castle: KING then QUEEN) and is EXCLUDED from the whole-world swap, so
# we never drop a boss arena into a normal level slot (which hangs the loader).
_LEVEL_BOSS_RE = re.compile(rb"_(?:BOSS|KING|QUEEN)\d*\.PSX$", re.IGNORECASE)
_LEVEL_SUBLEVEL_RE = re.compile(rb"_SUB\d", re.IGNORECASE)
# Extract the world folder from a path like "PSXDATA\GRAVE\G_SUB1.PSX".
_LEVEL_WORLD_RE = re.compile(rb"[\\/]([A-Za-z0-9]+)[\\/][^\\/]+\.PSX", re.IGNORECASE)


def find_level_table(data: bytearray | bytes):
    """Locate the engine's LevelTable by structural signature.

    Returns (table_file_offset, entry_count, (load_vaddr, load_off, filesz))
    or None if not found. Region-portable (no hard-coded offsets): scans the
    first PT_LOAD segment for a stride-0x34 dword array whose +0x00 entry points
    to a ".PSX"-terminated string for at least LEVEL_TABLE_MIN_ENTRIES rows."""
    b = bytes(data)
    if b[:4] != b"\x7fELF":
        return None
    seg = _first_load_segment(b)
    if seg is None:
        return None
    load_vaddr, load_off, filesz = seg
    seg_end = min(load_off + filesz, len(b))
    # Collect vaddrs of ".PSX"-ending strings within the loaded image.
    psx_vaddrs: set[int] = set()
    for m in _LEVEL_PSX_STR_RE.finditer(b):
        s_off = m.start() + 1               # string starts after the leading NUL
        if load_off <= s_off < seg_end:
            psx_vaddrs.add(load_vaddr + (s_off - load_off))
    if not psx_vaddrs:
        return None
    # Scan for a stride-0x34 array whose +0x00 points at a PSX string.
    o = load_off
    limit = seg_end - 4
    while o <= limit:
        count = 0
        while True:
            eo = o + count * LEVEL_TABLE_STRIDE
            if eo + 4 > seg_end:
                break
            ptr = struct.unpack_from("<I", b, eo)[0]
            if ptr in psx_vaddrs:
                count += 1
            else:
                break
        if count >= LEVEL_TABLE_MIN_ENTRIES:
            return o, count, seg
        o += 4
    return None


def patch_randomize_levels(data: bytearray, seed: int | None,
                           cross_world: bool = False) -> dict:
    """Randomize which level(s) load. Mutates `data` (SLUS/SLPM bytes) in place.

    cross_world=False (default): shuffle the playable sub-levels WITHIN each
    world (paths matching "_SUB\\d"), swapping only their file-pointer block
    [0x00:0x10]. Same-world geometry always matches the resident world assets
    (BEF/PRS/PRT/textures/sfx), so levels load cleanly. Slot identity (+0x28)
    and progression (+0x30) are untouched, so the hub/save still treat each
    column as its original level.

    cross_world=True (WHOLE-WORLD SWAP): permute ENTIRE worlds. Each world in
    the LevelTable is a contiguous run of entries (intro, hub, sub-levels,
    boss). We seed-permute these world blocks and, position-by-position, copy
    each entry's file pointers (and, for sub-levels/boss, its camera/spawn
    params too) from the source world to the destination world, AND repoint
    the destination's static-data index (+0x28 upper 16 bits) at the source
    world's resource set (BEF/PRS/PRT) so the loaded geometry's entities
    resolve against the right templates — without this, every swapped entry
    silently ran on the wrong world's resource set (see set_static_data_index
    above). Intro/intro-B and hub get file-pointer-only swaps (their own
    camera/spawn params stay put); sub-levels/boss get the full swap. Worlds
    with a non-standard boss layout (boss-arena filename outside the final
    slot, e.g. Castle's C_KING/C_QUEEN) are excluded from the permutable set
    entirely. Slot identity/progression fields are left in place so the
    world-map/save still track each slot."""
    res = find_level_table(data)
    if res is None:
        return {"found": False, "applied": False, "reason": "LevelTable not found"}
    tbl_off, count, seg = res
    load_vaddr, load_off, filesz = seg
    b = bytes(data)

    def read_path(eo: int) -> bytes:
        ptr = struct.unpack_from("<I", b, eo)[0]
        s_off = load_off + (ptr - load_vaddr)
        if not (load_off <= s_off < load_off + filesz):
            return b""
        nul = b.find(b"\x00", s_off)
        return b[s_off:nul if nul >= 0 else s_off]

    def world_of(eo: int) -> str:
        wm = _LEVEL_WORLD_RE.search(read_path(eo))
        return wm.group(1).decode("ascii", "replace").upper() if wm else "?"

    rng = random.Random((seed if seed is not None else 0) ^ 0x1E7E1AB1)

    def _is_standard_boss_layout(entries) -> bool:
        """True iff a boss-arena-style filename (_BOSS/_KING/_QUEEN) only
        appears in this block's FINAL entry. Worlds whose boss content is
        split across multiple non-final slots (Castle: C_KING then C_QUEEN,
        both matching the boss pattern, with C_KING in slot 4 of 6) have a
        non-standard layout: position-by-position copying would drop a
        normal sub-level's file/camera data into a scripted boss-arena slot
        (or vice versa), which hangs the loader. Such blocks are excluded
        from the permutable set entirely (documented intent above —
        previously unenforced: _LEVEL_BOSS_RE existed but was never called)."""
        for pos, eo in enumerate(entries):
            path = read_path(eo).upper()
            if _LEVEL_BOSS_RE.search(path) and pos != len(entries) - 1:
                return False
        return True

    def copy_entry(dst_eo: int, src_full: bytes) -> None:
        """Copy the swappable ranges (files + camera/spawn params) from a source
        entry snapshot into the destination entry, leaving identity / pointer /
        boot-read fields untouched."""
        for roff, rlen in LEVEL_SWAP_RANGES:
            data[dst_eo + roff:dst_eo + roff + rlen] = src_full[roff:roff + rlen]

    def set_static_data_index(dst_eo: int, src_full: bytes) -> None:
        """Intentionally a no-op.

        A previous version wrote the upper 16 bits of +0x28 from the source
        entry into the destination entry, believing +0x28 to be a static-data
        resource index. This was a misread of the disassembly.

        +0x28 is the level IDENTITY word (world<<16 | type<<8 | local-index),
        documented in LEVEL_TABLE layout above and confirmed in the LevelTable
        dump. Writing a foreign world's value here corrupts the slot identity,
        breaking the title-screen attract-mode loader: it reads this field to
        select the resource context, gets a mismatched value, and hangs or
        causes the PS2 to reboot with repeated CDVD Read Aborts.

        The field MUST stay with the destination slot. Cross-world resource
        loading is handled by BEF injection (inject_cross_world_enemies in
        bef.py), not by patching this identity field.
        """
        pass  # deliberately do nothing — +0x28 must never be modified

    # ---------------------------------------------------------------- WORLD SWAP
    if cross_world:
        # Build contiguous runs of entries that share a world folder; each run
        # is one world block (intro/hub/subs/boss).
        runs = []  # list of [entry_offset, ...]
        run_world = []
        prev = None
        for i in range(count):
            eo = tbl_off + i * LEVEL_TABLE_STRIDE
            w = world_of(eo)
            if w == prev and runs:
                runs[-1].append(eo)
            else:
                runs.append([eo])
                run_world.append(w)
            prev = w
        # The five main worlds are equal-length blocks; pick the dominant block
        # length and permute only those (skips the 1-entry bonus run, etc.).
        from collections import Counter
        lengths = Counter(len(r) for r in runs)
        block_len = max(lengths, key=lambda L: (lengths[L], L))

        candidate_idxs = [i for i, r in enumerate(runs) if len(r) == block_len]
        excluded_worlds = [run_world[i] for i in candidate_idxs
                           if not _is_standard_boss_layout(runs[i])]
        blocks = [(run_world[i], runs[i]) for i in candidate_idxs
                  if _is_standard_boss_layout(runs[i])]
        if len(blocks) < 2:
            return {"found": True, "applied": False, "table": tbl_off,
                    "entries": count, "cross_world": True,
                    "excluded_worlds": excluded_worlds,
                    "reason": "could not identify swappable world blocks"}
        # Snapshot each world block's full entries, then permute the blocks and
        # copy position-by-position (intro->intro, hub->hub, sub->sub,
        # boss->boss for same-shaped worlds). Position-by-position keeps a
        # one-to-one mapping (no duplicate level-table entries), which the boot
        # / save-init needs — duplicating an entry hangs the title menu.
        src_blocks = [[bytes(data[eo:eo + LEVEL_TABLE_STRIDE]) for eo in r]
                      for _, r in blocks]
        order = list(range(len(blocks)))
        for _ in range(12):
            rng.shuffle(order)
            if all(order[i] != i for i in range(len(order))):
                break
        mapping = []
        applied = False
        for dst_i, src_i in enumerate(order):
            dst_world, dst_entries = blocks[dst_i]
            src_world = blocks[src_i][0]
            for pos, dst_eo in enumerate(dst_entries):
                src_full = src_blocks[src_i][pos]
                _p = read_path(dst_eo).upper()
                if b"_INTRO" in _p:
                    # INTRO / INTRO-B: file-pointer swap only (camera/spawn
                    # params stay the destination's own — same caution as
                    # HUB below), PLUS the static-data-index fix. Previously
                    # these were left fully untouched because the title-
                    # screen attract-mode hang was (incorrectly) attributed to
                    # them; that demo path actually uses hardcoded literal
                    # filenames unrelated to this table (confirmed in the
                    # ELF disassembly), and the real cause — a missing
                    # static-data-index swap — is fixed below, so intro can
                    # now safely take part in the whole-world swap.
                    data[dst_eo:dst_eo + LEVEL_FILE_BLOCK] = src_full[0:LEVEL_FILE_BLOCK]
                    set_static_data_index(dst_eo, src_full)
                elif b"_HUB" in _p:
                    # The HUB is the in-world level-select "menu" — file-
                    # pointer-only swap (keep the host's camera/spawn params)
                    # plus the static-data-index fix.
                    data[dst_eo:dst_eo + LEVEL_FILE_BLOCK] = src_full[0:LEVEL_FILE_BLOCK]
                    set_static_data_index(dst_eo, src_full)
                else:
                    copy_entry(dst_eo, src_full)
                    set_static_data_index(dst_eo, src_full)
            if src_i != dst_i:
                applied = True
            mapping.append((dst_world, src_world))
        return {"found": True, "applied": applied, "table": tbl_off,
                "entries": count, "cross_world": True,
                "block_len": block_len,
                "worlds": [w for w, _ in blocks],
                "excluded_worlds": excluded_worlds,
                "mapping": mapping}

    # ------------------------------------------------------- WITHIN-WORLD SHUFFLE
    groups: dict[str, list] = {}   # world -> [(entry_offset, file_block, path)]
    for i in range(count):
        eo = tbl_off + i * LEVEL_TABLE_STRIDE
        path = read_path(eo)
        if not _LEVEL_SUBLEVEL_RE.search(path):
            continue
        wm = _LEVEL_WORLD_RE.search(path)
        world = wm.group(1).decode("ascii", "replace").upper() if wm else "?"
        groups.setdefault(world, []).append(
            (eo, bytes(data[eo:eo + LEVEL_TABLE_STRIDE]), path))

    total_subs = sum(len(v) for v in groups.values())
    if total_subs < 2:
        return {"found": True, "applied": False, "table": tbl_off,
                "entries": count, "sublevels": total_subs,
                "reason": "not enough sub-levels to shuffle"}

    mapping = []
    shuffled_any = False
    for world in sorted(groups):
        subs = groups[world]
        if len(subs) < 2:
            continue
        order = list(range(len(subs)))
        for _ in range(8):
            rng.shuffle(order)
            if any(order[i] != i for i in range(len(order))):
                break
        for dst_i, src_i in enumerate(order):
            dst_eo = subs[dst_i][0]
            src_full = subs[src_i][1]
            # BUG FIX (investigated after reports of infinite loading on Dead
            # Heat / Quick and the Dead / Crushed Spirits with the GUI's
            # "always on" within-world level shuffle): this loop used to call
            # copy_entry(), which copies LEVEL_SWAP_RANGES = [0x00:0x10] AND
            # [0x14:0x1e] -- the camera/player-start/world-bounds params --
            # even though the docstring above explicitly says the within-
            # world shuffle "swaps only their file-pointer block [0x00:0x10]".
            # Ground-truth disassembly of InitWorld (MAXDEMOR.ELF debug build,
            # research/CROSSWORLD_TEXTURE_RESEARCH.md) confirms the engine
            # does real per-level math on the 0x14:0x1e halfwords (grid-size
            # division, conditional branches) -- two DIFFERENT sub-levels in
            # the same world can have geometry-specific bounds here, so
            # swapping them (even within one world) can hand a level bounds
            # that don't match its own geometry and hang on load. Restricting
            # this path to the file-pointer block only (as documented) avoids
            # that; cross_world=True still needs the fuller copy_entry() copy
            # since there the geometry itself is also being swapped.
            data[dst_eo:dst_eo + LEVEL_FILE_BLOCK] = src_full[0:LEVEL_FILE_BLOCK]
            if src_i != dst_i:
                shuffled_any = True
            mapping.append((subs[dst_i][2].decode("ascii", "replace"),
                            subs[src_i][2].decode("ascii", "replace")))
    return {"found": True, "applied": shuffled_any, "table": tbl_off,
            "entries": count, "sublevels": total_subs,
            "cross_world": False,
            "worlds": {w: len(v) for w, v in groups.items()},
            "mapping": mapping}


# Slots that the randomizer overwrites: only the columns the player can pick.
# We do NOT overwrite world names or boss titles by default — those are
# expected to remain readable.
#
# Each tuple: (offset, slot_size, original_text, world)
LEVEL_TITLE_SLOTS = [
    # GRAVE — 3 unlocked columns
    (0x000CD1E8, 16, b"Dead Heat",         "grave"),
    (0x000CD1F8, 24, b"Coffin Canyon",     "grave"),
    (0x000CD210, 16, b"Bad to the Bone",   "grave"),
    # SWAMP — 3 unlocked columns
    (0x000CD250, 16, b"Voodoo Village",    "swamp"),
    (0x000CD260, 16, b"'Dem Bones",        "swamp"),
    (0x000CD270, 24, b"Quick and the Dead","swamp"),
    # ICE — 3 unlocked columns
    (0x000CD2D0, 32, b"Go With The Floe",  "ice"),
    (0x000CD2F0, 24, b"Dead in the Water", "ice"),
    (0x000CD308, 24, b"Cannonball Run",    "ice"),
    # UNDER — 3 unlocked columns
    (0x000CD360, 16, b"Crushed Spirits",   "under"),
    (0x000CD370, 32, b"The Unkindest Cut", "under"),
    (0x000CD390, 16, b"Down The Gullet",   "under"),
    # CASTLE — 5 unlocked columns + boss titles
    (0x000CD3C8, 24, b"The Keep",          "castle"),  # level
    (0x000CD3E0, 32, b"The Great Escape",  "castle"),  # level
    (0x000CD400, 32, b"Dungeon of Despair","castle"),  # level
    (0x000CD420, 24, b"Atop the Great Drill","castle"),  # level
    (0x000CD438, 24, b"Demon Queen",       "castle"),  # boss
]


# ============================================================================
# BEF VALIDATION BYPASS
# ============================================================================
# The engine's BEF loader at vaddr 0x00129390 has a validation check that
# compares the loaded file against an expected state (likely a size check
# returned by a vtable call). If the comparison fails, the loader aborts with
# "***BEF file load failed!! ***".
#
# To allow modified BEF files (with injected enemy type blobs), we patch the
# conditional branch at vaddr 0x001293F4 to always succeed.
#
# Original: beq $s0, $v0, +6  (0x12020006) — branches to success only if equal
# Patched:  beq $zero, $zero, +6  (0x10000006) — always branches to success
#
# ELF layout: base vaddr 0x100000, file offset 0x80 for first LOAD segment.
# vaddr 0x001293F4 => file offset = 0x001293F4 - 0x100000 + 0x80 = 0x029474

BEF_VALIDATION_BYPASS_OFFSET = 0x029474
BEF_VALIDATION_ORIGINAL = b'\x06\x00\x02\x12'  # beq $s0, $v0, +6
BEF_VALIDATION_PATCHED = b'\x06\x00\x00\x10'   # beq $zero, $zero, +6


def patch_bef_validation_bypass(data: bytearray) -> bool:
    """Patch the BEF loader's validation check to always succeed.

    This allows the engine to load modified BEF files (with injected enemy
    type blobs from other worlds) without crashing on map load.

    Args:
        data: Mutable ELF bytes (modified in-place).

    Returns:
        True if patch was applied, False if the expected bytes weren't found.
    """
    off = BEF_VALIDATION_BYPASS_OFFSET
    if off + 4 > len(data):
        return False
    current = bytes(data[off:off + 4])
    if current == BEF_VALIDATION_PATCHED:
        return True  # already patched
    if current != BEF_VALIDATION_ORIGINAL:
        return False  # unexpected bytes, don't patch
    data[off:off + 4] = BEF_VALIDATION_PATCHED
    return True


def apply_level_titles(
    data: bytearray,
    new_title: bytes = b"???",
    worlds: set[str] | None = None,
) -> dict:
    """Core level-title patch operating on a mutable SLUS bytearray (so it can
    be combined with other SLUS patches before writing once). See
    patch_level_titles for the file-based wrapper.

    Version-independent: the English level-title strings are identical across
    regions (US SLUS / JP SLPM) but sit at different file offsets — JP is
    shifted by a constant. We anchor on the first world-name string and apply
    the resulting delta to every slot, so the same LEVEL_TITLE_SLOTS table
    works for any region that shares the English title block."""
    patched = []
    skipped = []
    # Locate the title table by anchor so US and JP (and other English-title
    # regions) both work. "Grave Danger" sits at the very start of the table
    # at US offset 0xCD1C8; the delta is applied to every slot offset below.
    delta = 0
    anchor_at = bytes(data).find(TITLE_TABLE_ANCHOR)
    if anchor_at >= 0:
        delta = anchor_at - TITLE_TABLE_ANCHOR_US_OFFSET
    for off, slot_size, expected, world in LEVEL_TITLE_SLOTS:
        off += delta
        if worlds is not None and world not in worlds:
            skipped.append({"offset": off, "world": world,
                            "expected": expected.decode("ascii", errors="replace"),
                            "reason": f"world '{world}' not in selected set"})
            continue
        if len(new_title) >= slot_size:
            skipped.append({"offset": off, "world": world,
                            "expected": expected.decode("ascii", errors="replace"),
                            "reason": "new_title too long for slot"})
            continue
        actual = bytes(data[off:off + len(expected)])
        if actual != expected:
            skipped.append({"offset": off, "world": world,
                            "expected": expected.decode("ascii", errors="replace"),
                            "actual": actual.decode("ascii", errors="replace")})
            continue
        data[off:off + slot_size] = new_title + b"\x00" * (slot_size - len(new_title))
        patched.append({"offset": off, "world": world,
                        "old": expected.decode("ascii", errors="replace"),
                        "new": new_title.decode("ascii", errors="replace")})
    return {"patched": patched, "skipped": skipped, "delta": delta}


def patch_level_titles(
    elf_in: Path | str,
    elf_out: Path | str,
    new_title: bytes = b"???",
    worlds: set[str] | None = None,
) -> dict:
    """Overwrite per-world level titles in SLUS_200.17 with `new_title`.

    `worlds`: optional set of world names to limit which slots get patched.
              None (default) means ALL worlds.

    Returns a stats dict with patched and skipped slots.
    """
    elf_in = Path(elf_in)
    elf_out = Path(elf_out)
    data = bytearray(elf_in.read_bytes())
    res = apply_level_titles(data, new_title=new_title, worlds=worlds)
    elf_out.parent.mkdir(parents=True, exist_ok=True)
    elf_out.write_bytes(bytes(data))
    res["out_path"] = str(elf_out)
    res["size"] = len(data)
    return res


if __name__ == "__main__":
    src = Path("game_files") / "SLUS_200.17"
    out = Path("output") / "SLUS_200.17"
    res = patch_level_titles(src, out)
    print(f"Wrote: {res['out_path']} ({res['size']:,} bytes)")
    print(f"Patched {len(res['patched'])} slots:")
    for p in res["patched"]:
        print(f"  [{p['world'].upper():<6}] @0x{p['offset']:08X}: '{p['old']}' -> '{p['new']}'")
    if res["skipped"]:
        print(f"Skipped {len(res['skipped'])} slots:")
        for s in res["skipped"]:
            print(f"  [{s['world'].upper():<6}] @0x{s['offset']:08X}: expected '{s['expected']}'"
                  f"{', got ' + repr(s['actual']) if 'actual' in s else ''}"
                  f"{'  (' + s['reason'] + ')' if 'reason' in s else ''}")
