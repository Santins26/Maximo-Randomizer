"""
Maximo: Ghosts to Glory randomizer CLI.

Usage:
  python -m randomizer.cli randomize <psx_folder> --output <out_folder> [--seed N] [--items]
  python -m randomizer.cli stats <psx_file>
  python -m randomizer.cli list-types <psx_folder>
"""
from __future__ import annotations
import argparse
import json
import random
import struct
import sys
import io
from pathlib import Path

# Force stdout to UTF-8 to handle non-ASCII paths in PSX file BEF references.
# In a PyInstaller --windowed build, sys.stdout/sys.stderr are None — skip the
# wrapping in that case (the GUI redirects them to its log widget anyway).
if sys.stdout is not None and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from .psx import PsxFile, make_record_with_template
from .catalog import (
    WALKING_MELEE_POOL,
    GRAVE_WALKING_MELEE,
    UNDER_WALKING_MELEE,
    WALKING_MELEE_WEIGHTS,
    CROSS_WORLD_SAFE_ENEMIES,
    get_world_from_bef_path,
    get_walking_melee_for_world,
    name_for_type,
    is_event_tied_enemy,
)
from .items import (
    aggressive_item_randomize,
    GAME_ABILITY_POOL,
    UNIVERSAL_ITEM_POOL,
    ENEMY_DESTINATIONS,
    reroll_enemy_tier,
    reroll_enemy_drop,
    reroll_enemy_variant,
    force_enemy_props,
    _boost_raven_aggro,
    get_gold_key_protected_names,
    place_start_gold_key,
    place_extra_keys,
    place_fixed_gold_keys,
    START_GOLD_KEY_FILES,
    EXTRA_KEY_FILES,
    FIXED_GOLD_KEY_TARGETS,
    LIFT_PROTECTION_FILES,
    GOLD_KEY_TYPE,
    collect_world_gen_templates,
    MONSTER_GENERATOR_TYPE,
    _is_hub_generator,
    GATE_TYPES,
    duplicate_boss,
    BOSS_DUPLICATION,
    BOSS_FILE_WORLD,
    randomize_player_spawn,
    apply_harder_mode,
)
from .elf_patch import (patch_level_titles, apply_level_titles, patch_ability_drops,
                        patch_damage_taken, patch_damage_dealt, find_executable,
                        patch_starting_inventory, patch_starting_skills,
                        patch_randomize_levels,
                        STARTING_SKILL_MASKS, KNOWN_EXECUTABLES)
from .spawn_config import load_spawn_config


# All worlds the randomizer knows about.
ALL_WORLDS = ("grave", "under", "swamp", "ice", "castle")

# Boss-arena PSX files. Skipped by the randomizer — boss rooms are
# scripted encounters and the boss instance carries event-tied state we
# don't want to touch (e.g. GraveDigger in G_BOSS.PSX).
BOSS_FILES = frozenset({
    "G_BOSS.PSX",  # GraveDigger
    "S_BOSS.PSX",  # Bokor LaBas
    "I_BOSS.PSX",  # Captain Cadaver
    "U_BOSS.PSX",  # Lord Glutterscum
    "C_KING.PSX",  # King (castle boss — duplicatable)
    "C_QUEEN.PSX", # Queen (castle final boss — protected from randomization)
})


def psx_record_size_at(psx: "PsxFile", offset: int) -> int:
    """Get the size of the record at this offset in the source PSX."""
    for r in psx.records:
        if r.offset == offset:
            return r.size
    return 0



def cmd_stats(args) -> None:
    """Show statistics for a PSX file."""
    psx = PsxFile.parse(Path(args.psx_file))
    print(f"File: {psx.path.name}")
    print(f"Size: {len(psx.raw):,} bytes")
    print(f"BEF reference: {psx.bef_path}")
    world = get_world_from_bef_path(psx.bef_path)
    print(f"Detected world: {world or '(unknown)'}")
    print(f"Records: {len(psx.records)}")
    print(f"Records start: 0x{psx.records_start:X}")
    print(f"Record count field (0x{psx.record_count_offset:X}): {psx.record_count_field}")
    print(f"Post-records ptr (0x{psx.post_records_ptr_offset:X}): 0x{psx.post_records_ptr:X}")

    from collections import Counter
    types = Counter(r.type_id for r in psx.records)
    print(f"\nEntity types ({len(types)} unique):")
    for tid, count in sorted(types.items()):
        sample = psx.find_records_by_type(tid)[0]
        print(f"  0x{tid:02X}  {count:3} x {sample.class_name:30s}  size 0x{sample.size:X}")


def cmd_list_types(args) -> None:
    """List all entity types found across all PSX files in a folder."""
    folder = Path(args.psx_folder)
    # On Windows, glob is case-insensitive; deduplicate by resolved path
    seen = set()
    psx_files = []
    for p in sorted(folder.glob("*.PSX")) + sorted(folder.glob("*.psx")):
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            psx_files.append(p)
    if not psx_files:
        print(f"No PSX files in {folder}")
        return

    type_world_map: dict[int, dict[str, list[str]]] = {}
    for path in psx_files:
        try:
            psx = PsxFile.parse(path)
        except Exception as e:
            print(f"Skipping {path.name}: {e}")
            continue
        world = get_world_from_bef_path(psx.bef_path) or "unknown"
        for r in psx.records:
            type_world_map.setdefault(r.type_id, {}).setdefault(world, []).append(
                f"{path.name}({r.class_name})"
            )

    print(f"Found {len(type_world_map)} entity types across {len(psx_files)} files:\n")
    for tid in sorted(type_world_map):
        worlds = type_world_map[tid]
        # Pick a representative class name
        any_files = next(iter(worlds.values()))
        sample_name = any_files[0].split("(")[1].rstrip(")")
        worlds_str = ", ".join(sorted(worlds.keys()))
        total = sum(len(f) for f in worlds.values())
        print(f"  0x{tid:02X}  {sample_name:30s}  worlds=[{worlds_str}]  total={total}")


def cmd_randomize(args) -> None:
    """Randomize enemies in all PSX files in a folder."""
    in_folder = Path(args.psx_folder)
    out_folder = Path(args.output)
    out_folder.mkdir(parents=True, exist_ok=True)
    seed = args.seed if args.seed is not None else random.randint(1, 999999)
    rng_master = random.Random(seed)

    psx_files = sorted(in_folder.glob("*.PSX")) + sorted(in_folder.glob("*.psx"))
    # Dedup (Windows filesystem is case-insensitive)
    _seen = set()
    _deduped = []
    for p in psx_files:
        rp = p.resolve()
        if rp not in _seen:
            _seen.add(rp)
            _deduped.append(p)
    psx_files = _deduped
    if not psx_files:
        print(f"ERROR: No PSX files in {in_folder}")
        sys.exit(1)

    # Resolve which worlds to randomize.
    selected_worlds = _resolve_worlds(getattr(args, "worlds", None))

    print(f"Randomizer running with seed: {seed}")
    print(f"Source: {in_folder}")
    print(f"Output: {out_folder}")
    print(f"PSX files found: {len(psx_files)}")
    print(f"Worlds enabled: {', '.join(sorted(selected_worlds))}")
    all_enemies_mode = getattr(args, "all_enemies", False)
    dup_bosses = getattr(args, "duplicate_bosses", False)
    # HARDER MODE: a single switch that enables a curated "harder" profile —
    # disabled gold/gem/health/life drops (folded into koins), disabled
    # structures (their share spawned as enemies), rarer keys/collectors/
    # skills, more item->enemy rolls, far more empty enemy drops, all enemies
    # at max class, mostly-1-koin chests, mimic/wizard at 33% each, boss
    # duplication (2 clones in Grave and Swamp), Maximo takes 2x damage and
    # deals 0.5x. (Dark_Knight is intentionally NOT excluded.)
    harder_mode = bool(getattr(args, "harder_mode", False))
    # Preserve all vanilla chests (keep their type/position) but still randomize
    # what's inside them. Independent of harder mode; composes with the other
    # category toggles.
    preserve_chests = bool(getattr(args, "preserve_chests", False))
    preserve_iron_keys = bool(getattr(args, "preserve_iron_keys", False))
    forced_chest_special: dict | None = None
    if harder_mode:
        hm = apply_harder_mode()
        print("HARDER MODE ON — "
              f"koins+{hm['koins_share_from_disabled']} from disabled drops, "
              f"enemy-share {hm['enemy_share_target']:.0%}, "
              f"enemy-drop {hm['enemy_drop_probability']:.0%}, max class, "
              "structures disabled, mimic/wizard 33% each, "
              "boss +1 clone (Grave+Swamp+Ice+Under+Castle), damage taken 2x / dealt 0.5x")
        dup_bosses = True
        forced_chest_special = {"mimic": 33, "wizard": 33}
    # Per-boss clone counts. Grave (GraveDigger) and Swamp (BokorLaBas) are
    # configured separately; each falls back to the legacy single
    # `boss_clones` value, then to 1.
    _legacy_clones = getattr(args, "boss_clones", 1) or 1
    dup_count_grave = getattr(args, "boss_clones_grave", None)
    dup_count_grave = _legacy_clones if dup_count_grave is None else dup_count_grave
    dup_count_swamp = getattr(args, "boss_clones_swamp", None)
    dup_count_swamp = _legacy_clones if dup_count_swamp is None else dup_count_swamp
    dup_count_ice = getattr(args, "boss_clones_ice", None)
    dup_count_ice = _legacy_clones if dup_count_ice is None else dup_count_ice
    dup_count_under = getattr(args, "boss_clones_under", None)
    dup_count_under = _legacy_clones if dup_count_under is None else dup_count_under
    dup_count_castle = getattr(args, "boss_clones_castle", None)
    dup_count_castle = _legacy_clones if dup_count_castle is None else dup_count_castle
    if harder_mode:
        dup_count_grave = 1
        dup_count_swamp = 1
        dup_count_ice = 1
        dup_count_under = 1
        dup_count_castle = 1
    _boss_clone_by_world = {"grave": dup_count_grave, "swamp": dup_count_swamp,
                            "ice": dup_count_ice, "under": dup_count_under,
                            "castle": dup_count_castle}
    # Cross-world mode: use the universal enemy pool for all worlds. Reads
    # the --cross-world CLI flag / GUI checkbox (args.cross_world).
    #
    # This was previously hardcoded to False (ignoring the flag/checkbox
    # entirely) as an emergency kill-switch after a map-load crash. The two
    # root causes of that crash are now fixed:
    #   1. bef.py's _NEVER_EVICT was out of sync with the real placeable
    #      item/enemy pool (items.py) -- fixed, now derived directly from it.
    #   2. This function's own target_enemy_types computation (below) wasn't
    #      filtered by CROSS_WORLD_SAFE_ENEMIES, so world-locked enemies
    #      (meshes only in a world-specific .PRS, e.g. Goat_Devil/
    #      Hammer_Devil/Doomed_Soul in UNDER.PRS) got injected into every
    #      world -- fixed, now intersected with CROSS_WORLD_SAFE_ENEMIES.
    cross_world_mode = bool(getattr(args, "cross_world", False))
    # Optional: drop Dark_Knight (0x53) from every randomization pool. The
    # Castle Dark_Knight has a kill-event quirk that can soft-lock a level
    # when it lands on a progression slot, so the user can opt to keep it
    # vanilla-only (it still appears where the game originally placed it).
    exclude_dark_knight = getattr(args, "exclude_dark_knight", False)
    DARK_KNIGHT_TYPE = 0x53
    excluded_types: set[int] = {DARK_KNIGHT_TYPE} if exclude_dark_knight else set()

    # Granular item-pipeline categories. The old single `--items` flag is
    # split into independent toggles: items (universal type swap of every
    # item/structure entity), chests (contents + gold), skills (ability IDs),
    # and columns (HUB blind level-select + level-title ??? patch).
    #
    # Back-compat: a caller that only sets `args.items` (legacy) gets every
    # category. New callers (GUI / split CLI flags) set each one explicitly;
    # an unset category (None) mirrors the master `items` flag.
    do_items = bool(getattr(args, "items", False))

    def _category(name: str) -> bool:
        v = getattr(args, name, None)
        return do_items if v is None else bool(v)

    do_chests = _category("chests")
    do_skills = _category("skills")
    do_columns = _category("columns")
    if harder_mode:
        # The harder-mode drop/structure/enemy-share changes are applied by the
        # universal item pass + chest pass, so make sure both run.
        do_items = True
        do_chests = True
    if preserve_chests:
        # Chest-content randomization must run for the preserved chests.
        do_chests = True
    any_item_pipeline = do_items or do_chests or do_skills or do_columns

    # Maximo spawn-location randomizer: moves the player's level-start position
    # to a random item/structure/enemy location. Independent of the other
    # categories.
    do_spawn_location = bool(getattr(args, "spawn_location", False))
    if do_spawn_location:
        print("Maximo spawn-location randomization ON (start moved to a random entity position)")
    if bool(getattr(args, "gen_tier", True)):
        print("Monster-generator class-level randomization ON (base/upgraded/elite spawns)")
    # Editable spawn-rate config (per-tag / per-world weights + enable flags).
    # Only active when the user supplied a config file; otherwise `spawn` is
    # None and the randomizer uses its stock weight tables unchanged.
    spawn = load_spawn_config(getattr(args, "spawn_config_path", None))
    if spawn is not None:
        print("Custom spawn-rate config loaded (per-tag / per-world weights active)")

    def enemy_weight_table(world: str) -> dict[int, float]:
        """Per-enemy relative weights for the even-out balancing bag. Uses the
        spawn config when present, else the stock WALKING_MELEE_WEIGHTS."""
        if spawn is not None:
            return spawn.enemy_weights_for_world(world)
        return WALKING_MELEE_WEIGHTS

    def enemy_tier_table(world: str):
        """Per-enemy class-level (tier) weights, or None to use stock tier
        weighting. Active only when a spawn config is supplied."""
        if spawn is not None:
            return spawn.enemy_tier_weights_for_world(world)
        return None

    def spawn_patch_for(psx_obj, fname: str, file_seed: int):
        """Build the player-spawn header patch for a level, or None. Skipped
        for HUB maps (level-select) so progression stays intact."""
        if not do_spawn_location:
            return None
        if "_HUB" in fname.upper():
            return None
        patch = randomize_player_spawn(psx_obj, random.Random(file_seed ^ 0x5A5A5A5A))
        return patch or None

    if all_enemies_mode:
        print("Mode: ALL-ENEMIES (chaos) — every item/structure becomes an enemy")
    else:
        print(f"Modes: enemies={not args.no_enemies}, items={do_items}, "
              f"chests={do_chests}, skills={do_skills}, columns={do_columns}")
    if cross_world_mode:
        print("Cross-world mode: ALL enemy types available in ALL worlds")
    else:
        print("Cross-world mode: OFF (world-locked enemies stay in their home worlds)")
    if dup_bosses:
        print(f"Experimental: boss duplication ON (Grave={dup_count_grave}, "
              f"Swamp={dup_count_swamp} clone(s))")
    if exclude_dark_knight:
        print("Dark_Knight EXCLUDED from randomizer pools (kept vanilla-only)")
    print()

    def pool_for_world(world: str) -> set[int]:
        """Return the walking-melee enemy type pool for a world, minus any
        types the user opted to exclude (e.g. Dark_Knight) or disabled in the
        spawn-rate config.

        NOTE: this initial definition is ONLY used for the pre-pass template
        harvest below (world_enemy_candidates / world_templates) -- it is
        replaced by a second, more accurate definition further down once
        bef_native_types is known (see the `cross_world_mode` block).

        This must NOT be narrowed to CROSS_WORLD_SAFE_ENEMIES even when
        cross_world_mode is on: CROSS_WORLD_SAFE_ENEMIES only says which
        enemies are safe to INJECT into a FOREIGN world -- it says nothing
        about a world's own native roster. Using it here starved the
        pre-pass of a Dark_Knight template even in CASTLE (his own home
        world), because 0x53 is PRS-locked (excluded from
        CROSS_WORLD_SAFE_ENEMIES) despite being perfectly native to Castle.
        With no template harvested anywhere, global_enemy_templates never
        got a 0x53 entry either, so Dark_Knight could never be chosen as a
        swap destination in ANY world (including Castle) once cross-world
        mode was on -- silently, with no warning, regardless of whether his
        mesh was later ported to other worlds via inject_locked_enemy. Using
        get_walking_melee_for_world(world, cross_world=False) here always
        harvests each world's FULL native roster (locked enemies included),
        which is what the pre-pass template harvest actually needs.
        """
        pool = get_walking_melee_for_world(world, cross_world=False) - excluded_types
        if spawn is not None:
            pool = pool - spawn.disabled_enemies(world)
        return pool

    # ------------------------------------------------------------------
    # Pre-pass: build a GLOBAL per-world enemy template registry.
    #
    # Why: a single map (e.g. C_HUB.PSX) may only contain Ghost/Raven/
    # Torso_Zombie in vanilla. If we only build templates from records in
    # that file, the "available swap targets" set is just those 3 types,
    # so every enemy ends up rolling Ghost/Raven/Torso again. By harvesting
    # templates across ALL maps in a world we can swap an enemy in C_HUB
    # into a Dark_Knight pulled from C_SUB1, etc.
    # ------------------------------------------------------------------
    parsed_psx: dict[Path, PsxFile] = {}
    world_templates: dict[str, dict[int, "PsxRecord"]] = {}
    world_item_templates: dict[str, dict[int, "PsxRecord"]] = {}
    # Per-world per-type CANDIDATE list: collect every record across every
    # file in the world, then pick the best template afterwards.
    world_enemy_candidates: dict[str, dict[int, list]] = {}
    for path in psx_files:
        if path.name.upper() in BOSS_FILES:
            continue
        try:
            psx = PsxFile.parse(path)
        except Exception:
            continue
        parsed_psx[path] = psx
        world = get_world_from_bef_path(psx.bef_path)
        if world is None:
            continue
        cand_slot = world_enemy_candidates.setdefault(world, {})
        pool_ids = pool_for_world(world)
        for tid in pool_ids:
            recs = psx.find_records_by_type(tid)
            if recs:
                cand_slot.setdefault(tid, []).extend(recs)
        # Cross-file ITEM template registry — needed so maps without a native
        # Ability/etc. record can still roll INTO that type from the universal
        # pool (HUB files in particular don't contain abilities natively).
        item_slot = world_item_templates.setdefault(world, {})
        for tid in UNIVERSAL_ITEM_POOL:
            if tid in item_slot:
                continue
            recs = psx.find_records_by_type(tid)
            if recs:
                item_slot[tid] = recs[0]

    # Cross-WORLD fallback for types that vanilla only places in some worlds.
    # Gold_Key (0x2B) only exists in CASTLE, GRAVE, ICE — but the user wants
    # it to spawn in every world via the 0.01% lottery, so we let SWAMP and
    # UNDER borrow a Gold_Key template from any world that has one. Same
    # principle applies if any other UNIVERSAL_ITEM_POOL type ever gets
    # added with partial-world coverage.
    GLOBAL_TEMPLATE_TYPES = {0x2B}  # Gold_Key
    cross_world_templates: dict[int, "PsxRecord"] = {}
    for tid in GLOBAL_TEMPLATE_TYPES:
        for slot in world_item_templates.values():
            if tid in slot:
                cross_world_templates[tid] = slot[tid]
                break
    for world_slot in world_item_templates.values():
        for tid, tpl in cross_world_templates.items():
            world_slot.setdefault(tid, tpl)

    # Monster_Generator (0x5F) DESTINATION template fix. Items can roll INTO a
    # generator, but the template must be a WORKING (non-HUB) generator — a
    # HUB phase generator cloned into a normal item slot becomes a silent,
    # never-triggering spawner. Castle's only native generators are HUB phase
    # markers (in C_HUB), so Castle LEVELS would otherwise only ever produce
    # dead generators. Harvest a non-HUB generator from any world and use it
    # as the generator template for every world whose own template is HUB-only
    # or missing — so Castle levels (and any other generator-less map) can
    # spawn functioning generators.
    global_nonhub_gen = None
    for slot in world_item_templates.values():
        tpl = slot.get(MONSTER_GENERATOR_TYPE)
        if tpl is not None and not _is_hub_generator(tpl):
            global_nonhub_gen = tpl
            break
    if global_nonhub_gen is not None:
        for world_slot in world_item_templates.values():
            cur = world_slot.get(MONSTER_GENERATOR_TYPE)
            if cur is None or _is_hub_generator(cur):
                world_slot[MONSTER_GENERATOR_TYPE] = global_nonhub_gen

    # Global Gold_Key template — used to guarantee a start-of-level Gold_Key
    # in the four named levels (START_GOLD_KEY_FILES). Swamp files have no
    # native Gold_Key, so we reuse one harvested from Castle/Grave/Ice.
    global_gold_key_template = cross_world_templates.get(GOLD_KEY_TYPE)

    # Global Skeleton_Key (0x65) template for the extra-key injections.
    global_skeleton_key_template = None
    for slot in world_item_templates.values():
        if 0x65 in slot:
            global_skeleton_key_template = slot[0x65]
            break

    # Global weather emitter templates — harvested from any level that has
    # one. Rain (0xD0) lives in Grave/Swamp intros, Snow (0xCF) in Ice. Used
    # to INJECT weather into weather-world levels that lack an emitter so e.g.
    # rain appears across all of Grave, not just the intro.
    global_rain_template = None
    global_snow_template = None
    for psx_obj in parsed_psx.values():
        if global_rain_template is None:
            recs = psx_obj.find_records_by_type(0xD0)
            if recs:
                global_rain_template = recs[0]
        if global_snow_template is None:
            recs = psx_obj.find_records_by_type(0xCF)
            if recs:
                global_snow_template = recs[0]
        if global_rain_template and global_snow_template:
            break

    def weather_template_for(world: str):
        """Weather emitter template appropriate for a world (rain for
        grave/swamp, snow for ice), or None."""
        if world in ("grave", "swamp"):
            return global_rain_template
        if world == "ice":
            return global_snow_template
        return None

    # Per-world Monster_Generator template pool. Gathering one generator per
    # distinct enemy signature across a world lets the randomizer re-spawn
    # item-created generators with the world's VARIED enemy roster (e.g. Grave
    # = basic + sword skeleton) instead of every created portal cloning a
    # single template and spawning the same enemy.
    world_gen_templates: dict[str, list] = {}
    _world_psx: dict[str, list] = {}
    for p, psx_obj in parsed_psx.items():
        wd = get_world_from_bef_path(psx_obj.bef_path)
        if wd:
            _world_psx.setdefault(wd, []).append(psx_obj)
    for wd, lst in _world_psx.items():
        world_gen_templates[wd] = collect_world_gen_templates(lst)

    # Per-world GATE template pool — one template per gate type a world loads.
    # Used by the gate randomizer so gates only swap into / appear as gate
    # types that world's BEF actually renders.
    world_gate_templates: dict[str, dict[int, "PsxRecord"]] = {}
    for wd, lst in _world_psx.items():
        slot = world_gate_templates.setdefault(wd, {})
        for psx_obj in lst:
            for tid in GATE_TYPES:
                if tid in slot:
                    continue
                recs = psx_obj.find_records_by_type(tid)
                if recs:
                    slot[tid] = recs[0]
    gate_mode = getattr(args, "gate_mode", None)
    if gate_mode in ("isolated", "pool"):
        print(f"Gate randomizer ON (mode={gate_mode})")

    # Pick the best enemy template per (world, type). Prefer in this order:
    #   1. A "neutral" record: not a HUB phase marker (instance_name doesn't
    #      start with P1_/P2_/P3_/P4_/FP1_..FP4_). HUB phase markers carry
    #      wave-index values in their props that the engine reads to bind
    #      the spawn to a specific HUB phase — cloning that template into
    #      regular in-level slots makes the cloned enemy never spawn during
    #      normal gameplay (it waits for a HUB phase trigger that never
    #      fires outside the HUB).
    #   2. Within the non-HUB pool, prefer records with prop[5] == 0
    #      (avoids the historic "every Axe_Skeleton drops Iron Key" bug
    #      from cloning a record whose prop[5] reads as a drop indicator).
    #   3. Otherwise the first non-HUB record found.
    #   4. As a last resort, any record (including HUB).
    def _is_hub_phase_record(r):
        n = r.instance_name.lower() if r.instance_name else ""
        return n.startswith(("p1_", "p2_", "p3_", "p4_",
                             "fp1_", "fp2_", "fp3_", "fp4_"))

    for world, cand_map in world_enemy_candidates.items():
        slot = world_templates.setdefault(world, {})
        for tid, recs in cand_map.items():
            non_hub = [r for r in recs if not _is_hub_phase_record(r)]
            preferred_pool = non_hub if non_hub else recs
            neutral = [
                r for r in preferred_pool
                if r.prop_count >= 6
                and r.raw[0x228 + 5*0x20] == 0x07
                and struct.unpack_from('<I', r.raw, 0x228 + 5*0x20 + 8)[0] == 0
            ]
            slot[tid] = (
                neutral[0] if neutral
                else preferred_pool[0] if preferred_pool
                else recs[0]
            )

    # Same preference for the universal pool's per-world item template
    # registry. Without this, world_item_templates picked S_HUB's first
    # Zombie_Crocodile record (prop[4]=3, a phase marker) so every coin
    # rolling INTO a Crocodile got an invisible HUB-bound enemy.
    for world, slot in world_item_templates.items():
        for tid in list(slot.keys()):
            tpl = slot[tid]
            if not _is_hub_phase_record(tpl):
                continue
            # Try to find a non-HUB candidate of this type in the world's files.
            replacement = None
            for path, psx in parsed_psx.items():
                pworld = get_world_from_bef_path(psx.bef_path)
                if pworld != world:
                    continue
                for r in psx.find_records_by_type(tid):
                    if not _is_hub_phase_record(r):
                        replacement = r
                        break
                if replacement:
                    break
            if replacement:
                slot[tid] = replacement

    if not args.no_enemies:
        for world in sorted(selected_worlds):
            avail = world_templates.get(world, {})
            names = [name_for_type(t) for t in sorted(avail.keys())]
            print(f"  enemy templates [{world}]: {len(avail)} types -> "
                  f"{', '.join(names) if names else '(none found)'}")
        print()

    # ------------------------------------------------------------------
    # CROSS-WORLD template sharing: borrow enemy record templates from
    # OTHER worlds' PSX files for types that already exist in the target
    # world's BEF but don't have a native PSX template. This means a
    # world that has the type in its BEF but never uses it in a vanilla
    # level can still spawn it using a template from a world that does.
    #
    # NOTE: We do NOT add types that aren't in the world's BEF — those
    # would crash because the engine can't load assets it doesn't have.
    # We only borrow TEMPLATES (PSX record data) for BEF-native types.
    # ------------------------------------------------------------------
    # Load each world's BEF to know its native enemy-type set.
    # Used in TWO places:
    #   1. cross_world_mode (below): borrow templates for BEF-native types.
    #   2. Item randomizer (always): exclude enemy types NOT in a world's BEF
    #      from the universal-pool destination set, so a GoldCoin in Grave
    #      can never roll into Goat_Devil (UNDER) or Dark_Knight (CASTLE).
    #      Without this, seeds that land on a world-specific enemy type cause
    #      the engine to look up a null mesh pointer in that world's BEF and
    #      crash — the intermittent "crash when loading a level" bug.
    from .bef import BefFile, BefEntry
    bef_folder = Path(args.psx_folder)
    bef_name_map = {
        "grave": "GRAVE.BEF", "under": "UNDER.BEF",
        "swamp": "SWAMP.BEF", "ice": "ICE.BEF", "castle": "CASTLE.BEF",
    }
    bef_native_types: dict[str, set[int]] = {}
    for world, bef_name in bef_name_map.items():
        bef_path = bef_folder / bef_name
        if bef_path.exists():
            try:
                bef = BefFile.parse(bef_path)
                bef_native_types[world] = bef.type_ids()
            except Exception:
                bef_native_types[world] = set()
        else:
            bef_native_types[world] = set()

    # All BEF-managed types in the universal pool — both enemies and world-
    # specific props (smashables, lanterns, statues, etc.). Any of these that
    # aren't defined in a world's BEF will crash the engine on mesh lookup if
    # placed in that world's file.
    ALL_POOL_BEF_TYPES: set[int] = set(ENEMY_DESTINATIONS) | set(UNIVERSAL_ITEM_POOL)

    if cross_world_mode and not args.no_enemies:
        print("Cross-world mode ENABLED: porting enemy meshes/textures/BEF blobs across worlds...")
        print()

        # ------------------------------------------------------------------
        # CROSSWORLD ENEMY INJECTION - FULL MESH + TEXTURE PORTING (CORRECT)
        # Mesh indices are WORLD-RELATIVE! We must port meshes and update indices.
        # ------------------------------------------------------------------
        print("  cross-world: using FULL MESH + TEXTURE PORTING (mesh indices are world-relative)")
        
        # Map of which enemies to inject into which worlds  
        CROSSWORLD_INJECTIONS = {
            "grave": [0x16],  # Just Axe_Guard for minimal test
        }
        
        # Source locations for enemy data (BEF, PRS, PRT, mesh index)
        ENEMY_SOURCE_DATA = {
            0x0D: ("swamp", "SWAMP.BEF", "SWAMP.PRS", "SWAMP.PRT", 25),  # Zombie_Crocodile
            0x16: ("castle", "CASTLE.BEF", "CASTLE.PRS", "CASTLE.PRT", 21),  # Axe_Guard  
            0x53: ("castle", "CASTLE.BEF", "CASTLE.PRS", None, 25),  # Dark_Knight (GLOBAL textures)
        }
        
        from randomizer.bef import inject_locked_enemy
        
        for target_world in sorted(selected_worlds):
            if target_world not in CROSSWORLD_INJECTIONS:
                continue
                
            enemies_to_inject = CROSSWORLD_INJECTIONS[target_world]
            
            target_bef_path = bef_folder / f"{target_world.upper()}.BEF" 
            target_prs_path = bef_folder / f"{target_world.upper()}.PRS"
            target_prt_path = bef_folder / f"{target_world.upper()}.PRT"
            
            if not all(p.exists() for p in [target_bef_path, target_prs_path, target_prt_path]):
                print(f"  [SKIP] {target_world}: missing BEF/PRS/PRT files")
                continue
                
            # Start with current target files
            current_bef_bytes = target_bef_path.read_bytes()
            current_prs_bytes = target_prs_path.read_bytes()  
            current_prt_bytes = target_prt_path.read_bytes()
            
            modified = False
            
            for enemy_type in enemies_to_inject:
                if enemy_type not in ENEMY_SOURCE_DATA:
                    continue
                    
                source_world, source_bef_name, source_prs_name, source_prt_name, mesh_idx = ENEMY_SOURCE_DATA[enemy_type]
                
                source_bef_path = bef_folder / source_bef_name
                source_prs_path = bef_folder / source_prs_name  
                source_prt_path = bef_folder / source_prt_name if source_prt_name else None
                
                missing_files = [f for f in [source_bef_path, source_prs_path] if not f.exists()]
                if source_prt_path:
                    missing_files.extend([f for f in [source_prt_path] if not f.exists()])
                
                if missing_files:
                    print(f"  [WARN] {target_world}: missing source files for {name_for_type(enemy_type)}: {missing_files}")
                    continue
                
                try:
                    print(f"    {target_world}: porting {name_for_type(enemy_type)} (0x{enemy_type:02X}) from {source_world}")
                    
                    # Write current state to temp files for inject_locked_enemy
                    temp_bef = Path(f"temp_{target_world}_{enemy_type:02X}.BEF")
                    temp_prs = Path(f"temp_{target_world}_{enemy_type:02X}.PRS")
                    temp_prt = Path(f"temp_{target_world}_{enemy_type:02X}.PRT")
                    
                    temp_bef.write_bytes(current_bef_bytes)
                    temp_prs.write_bytes(current_prs_bytes)
                    temp_prt.write_bytes(current_prt_bytes)
                    
                    # Port enemy with full mesh + texture porting
                    new_bef_bytes, new_prs_bytes, new_prt_bytes, new_mesh_index = inject_locked_enemy(
                        enemy_type_id=enemy_type,
                        source_bef_path=source_bef_path,
                        source_prs_path=source_prs_path, 
                        target_bef_path=temp_bef,
                        target_prs_path=temp_prs,
                        source_prt_path=source_prt_path,  # None for Dark_Knight (GLOBAL textures)
                        target_prt_path=temp_prt if source_prt_path else None,
                        source_mesh_index=mesh_idx
                    )
                    
                    # Update current state with results
                    current_bef_bytes = new_bef_bytes
                    current_prs_bytes = new_prs_bytes
                    if new_prt_bytes:
                        current_prt_bytes = new_prt_bytes
                    
                    # Cleanup temp files
                    temp_bef.unlink(missing_ok=True)
                    temp_prs.unlink(missing_ok=True) 
                    temp_prt.unlink(missing_ok=True)
                    
                    modified = True
                    print(f"      ✅ ported successfully (mesh index: {new_mesh_index})")
                    
                except Exception as e:
                    print(f"      ❌ failed: {e}")
                    continue
            
            if modified:
                # Write the final modified files to output directory
                (out_folder / f"{target_world.upper()}.BEF").write_bytes(current_bef_bytes)
                (out_folder / f"{target_world.upper()}.PRS").write_bytes(current_prs_bytes)
                (out_folder / f"{target_world.upper()}.PRT").write_bytes(current_prt_bytes)
                print(f"  cross-world: {target_world} files updated with full mesh+texture porting")

        print()
        
        # ------------------------------------------------------------------
        # UPDATE NATIVE TYPE POOLS WITH INJECTED ENEMIES
        # ------------------------------------------------------------------
        print("  cross-world: updating native type pools with injected enemies")
        
        for target_world in sorted(selected_worlds):
            if target_world not in CROSSWORLD_INJECTIONS:
                continue
                
            # Re-read the modified BEF to get updated type list
            output_bef_path = out_folder / f"{target_world.upper()}.BEF"
            if output_bef_path.exists():
                try:
                    updated_bef = BefFile.parse(output_bef_path)
                    old_types = bef_native_types.get(target_world, set())
                    new_types = updated_bef.type_ids()
                    added_types = new_types - old_types
                    
                    bef_native_types[target_world] = new_types
                    
                    if added_types:
                        added_names = [name_for_type(tid) for tid in sorted(added_types)]
                        print(f"    {target_world}: updated pool with {added_names}")
                        
                except Exception as e:
                    print(f"  [WARN] Failed to update native types for {target_world}: {e}")

        print()

        # Build a global template bank from ALL worlds
        global_enemy_templates: dict[int, "PsxRecord"] = {}
        for world_slot in world_templates.values():
            for tid, tpl in world_slot.items():
                if tid not in global_enemy_templates:
                    global_enemy_templates[tid] = tpl

        # For each world, borrow templates ONLY for types its BEF has
        for world in sorted(selected_worlds):
            slot = world_templates.setdefault(world, {})
            native = bef_native_types.get(world, set())
            # Types in BEF but without a template yet
            bef_enemies = native & set(WALKING_MELEE_POOL.keys())
            missing = bef_enemies - set(slot.keys()) - excluded_types
            borrowed = []
            for tid in missing:
                if tid in global_enemy_templates:
                    slot[tid] = global_enemy_templates[tid]
                    borrowed.append(tid)
            if borrowed:
                names = [name_for_type(t) for t in sorted(borrowed)]
                print(f"  cross-world [{world}]: +{len(borrowed)} borrowed "
                      f"templates -> {', '.join(names)}")

        # Override pool_for_world to use BEF-native types only
        def pool_for_world(world: str) -> set[int]:
            native = bef_native_types.get(world, set())
            pool = native & set(WALKING_MELEE_POOL.keys())
            pool = pool - excluded_types
            if spawn is not None:
                pool = pool - spawn.disabled_enemies(world)
            return pool

        print()


    summary = {
        "seed": seed,
        "modes": {
            "enemies": not args.no_enemies,
            "items": do_items,
            "chests": do_chests,
            "skills": do_skills,
            "columns": do_columns,
            "worlds": sorted(selected_worlds),
        },
        "files_processed": [],
        "errors": [],
    }

    for path in psx_files:
        # Boss-arena maps are scripted encounters — leave them alone, UNLESS
        # the experimental boss-duplication mode is enabled for this file.
        if path.name.upper() in BOSS_FILES:
            if dup_bosses and path.name.upper() in BOSS_DUPLICATION:
                try:
                    bpsx = PsxFile.parse(path)
                except Exception as e:
                    print(f"  [SKIP] {path.name}: {e}")
                    continue
                boss_world = BOSS_FILE_WORLD.get(path.name.upper())
                dup_count = _boss_clone_by_world.get(boss_world, _legacy_clones)
                boss_repl = duplicate_boss(bpsx, path.name, count=dup_count)
                if not boss_repl:
                    print(f"  [SKIP] {path.name}: boss arena (no clone slot, not written)")
                    continue
                out_path = out_folder / path.name
                try:
                    stats = bpsx.write_with_replacements(boss_repl, out_path)
                    print(f"  [BOSS] {path.name}: +{len(boss_repl)} boss clone(s) "
                          f"({boss_world}), shift={stats['total_shift']}b")
                    summary["files_processed"].append({
                        "file": path.name, "world": "boss",
                        "changes": len(boss_repl), "shift": stats["total_shift"],
                        "seed": seed, "mode": "boss_duplication",
                    })
                except Exception as e:
                    print(f"  [FAIL] {path.name}: {e}")
                    summary["errors"].append({"file": path.name, "error": str(e)})
                continue
            print(f"  [SKIP] {path.name}: boss arena (not randomized, not written)")
            continue
        # Each file gets its own seed derived from master
        file_seed = rng_master.randrange(1 << 30)
        psx = parsed_psx.get(path)
        if psx is None:
            try:
                psx = PsxFile.parse(path)
            except Exception as e:
                print(f"  [SKIP] {path.name}: {e}")
                summary["errors"].append({"file": path.name, "error": str(e)})
                continue

        world = get_world_from_bef_path(psx.bef_path)
        if world is None:
            # Unknown world — don't write filler files.
            print(f"  [SKIP] {path.name}: unknown world (not written to output)")
            continue

        if world not in selected_worlds:
            print(f"  [SKIP] {path.name}: world '{world}' not in selected set")
            continue

        # Build all replacements for this file
        all_repl = {}
        rng = random.Random(file_seed)

        # Per-file PROGRESSION protection: every record sharing an
        # instance_name with a progression-anchor record (Gold_Key,
        # Level_Column, Skeleton_Key) is part of a kill-group whose
        # membership the engine counts to spawn the key / finish the level /
        # open the gate. Type-swapping any such enemy breaks the trigger.
        progression_names = get_gold_key_protected_names(psx)

        # ============================================================
        # ALL-ENEMIES (chaos) MODE
        # ============================================================
        # Every eligible item / chest / structure / smashable becomes an
        # enemy. Gold_Keys are preserved. Enemies can drop Skeleton Keys and
        # the 1% Skeleton-Key / Gold-Key map lotteries still run so the game
        # stays beatable. Skips the normal enemy-swap + even-out passes
        # because the universal all-enemies pass already populates the map.
        if all_enemies_mode:
            enemy_pool = tuple(sorted(pool_for_world(world)))
            # extra_templates must contain BOTH the world enemy roster and
            # the Skeleton_Key / Gold_Key templates for the carve-outs.
            extra = dict(world_item_templates.get(world, {}))
            for tid, tpl in world_templates.get(world, {}).items():
                extra.setdefault(tid, tpl)

            item_seed = rng.randrange(1 << 30)
            item_repl = aggressive_item_randomize(
                psx, seed=item_seed,
                existing_repl=all_repl,
                extra_templates=extra,
                world=world,
                all_enemies=True,
                enemy_types=enemy_pool,
                all_enemies_weights=enemy_weight_table(world),
                excluded_types=excluded_types,
                weather_template=weather_template_for(world),
                randomize_gen_tier=getattr(args, "gen_tier", True),
                gate_mode=gate_mode,
                gate_templates=world_gate_templates.get(world),
            )
            for off, r in item_repl.items():
                all_repl[off] = r

            # Enemy post-processing on the freshly-created enemies:
            #   - Raven aggro boost
            #   - death-drop reroll (so enemies can drop Skeleton Keys etc.)
            #   - variant flags
            #   - force-clamp dangerous props (Axe iron-key, croc/sz wave idx)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                rec = _boost_raven_aggro(rec)
                all_repl[off] = rec
            drop_rng = random.Random(file_seed ^ 0xD150D250)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_drop(rec, drop_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            variant_rng = random.Random(file_seed ^ 0xBADBA17)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_variant(rec, variant_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = force_enemy_props(rec)
                if new_rec is not rec:
                    all_repl[off] = new_rec

            # START-OF-LEVEL Gold_Key guarantee (chaos mode too).
            placed_key_offsets: set[int] = set()
            if path.name.upper() in {k.upper() for k in START_GOLD_KEY_FILES}:
                key_repl = place_start_gold_key(
                    psx, path.name, global_gold_key_template,
                    existing_repl=all_repl,
                )
                for off, r in key_repl.items():
                    all_repl[off] = r
                    placed_key_offsets.add(off)

            # EXTRA mid-level keys (chaos mode too) — keep gated levels beatable.
            if path.name.upper() in {k.upper() for k in EXTRA_KEY_FILES}:
                extra_repl = place_extra_keys(
                    psx, path.name, global_gold_key_template,
                    global_skeleton_key_template,
                    avoid_offsets=placed_key_offsets,
                )
                for off, r in extra_repl.items():
                    all_repl[off] = r
                    placed_key_offsets.add(off)

            # FIXED-POSITION Gold_Keys (chaos mode too) — e.g. The Siege catapult.
            if path.name.upper() in {k.upper() for k in FIXED_GOLD_KEY_TARGETS}:
                fixed_repl = place_fixed_gold_keys(
                    psx, path.name, global_gold_key_template,
                    avoid_offsets=placed_key_offsets,
                )
                for off, r in fixed_repl.items():
                    all_repl[off] = r

            out_path = out_folder / path.name
            spawn_patch = spawn_patch_for(psx, path.name, file_seed)
            if not all_repl and not spawn_patch:
                print(f"  [SKIP] {path.name}: no swaps (world={world})")
                continue
            try:
                stats = psx.write_with_replacements(all_repl, out_path,
                                                    header_patches=spawn_patch)
                print(f"  [DONE] {path.name}: {len(all_repl)} -> enemies, "
                      f"world={world}, shift={stats['total_shift']}b"
                      f"{', spawn moved' if spawn_patch else ''}")
                summary["files_processed"].append({
                    "file": path.name, "world": world,
                    "changes": len(all_repl), "shift": stats["total_shift"],
                    "seed": file_seed, "mode": "all_enemies",
                    "spawn_moved": bool(spawn_patch),
                })
            except Exception as e:
                print(f"  [FAIL] {path.name}: {e}")
                summary["errors"].append({"file": path.name, "error": str(e)})
            continue  # done with this file in all-enemies mode

        # 1. Enemy randomization (if enabled). Use the GLOBAL per-world
        # template registry so even maps with a thin native enemy roster
        # (Ghost+Raven+Torso only, etc.) can swap in any walking-melee
        # type the world supports.
        enemy_swap_delta = 0  # net byte change from enemy type swaps
        # (progression_names already computed above for this file.)
        if not args.no_enemies:
            pool_ids = pool_for_world(world)
            templates = world_templates.get(world, {})

            # SOURCE set: which records count as swappable enemies. Includes
            # types DISABLED in the spawn config so their vanilla instances get
            # retyped into an enabled enemy instead of remaining in the map.
            # DESTINATIONS come from `available` (templates) which already
            # excludes disabled types — so disabled enemies never spawn.
            melee_source = set(pool_ids)
            if spawn is not None:
                melee_source |= spawn.disabled_enemies(world)

            if templates:
                available = list(templates.keys())
                # Equal-weight uniform pick. Each per-record pool excludes the
                # enemy's own type so every eligible enemy guaranteed swaps.
                for r in psx.records:
                    if r.type_id not in melee_source:
                        continue
                    if is_event_tied_enemy(r.instance_name):
                        continue
                    if r.instance_name in progression_names:
                        continue
                    pool_excl = [t for t in available if t != r.type_id]
                    if not pool_excl:
                        continue
                    new_type = rng.choice(pool_excl)
                    new_rec = make_record_with_template(
                        templates[new_type], r, r.offset
                    )
                    enemy_swap_delta += new_rec.size - r.size
                    all_repl[r.offset] = new_rec

        # 2. Item randomization (if enabled). Pass the enemy swaps in so
        # tier-randomization (step 6 inside aggressive_item_randomize) can
        # re-roll the tier of POST-swap enemies (e.g. a Ghost->Basic_Skeleton
        # gets a fresh tier 0/1/2 instead of being stuck at the template's
        # tier).
        if any_item_pipeline:
            item_seed = rng.randrange(1 << 30)
            # Per-world destination exclusions: global excludes (e.g.
            # Dark_Knight) plus any enemies disabled in the spawn config.
            world_excluded = set(excluded_types)
            uni_weights = None
            tier_weights = None
            if spawn is not None:
                world_excluded |= spawn.disabled_enemies(world)
                uni_weights = spawn.universal_weights(world)
                tier_weights = spawn.enemy_tier_weights_for_world(world)
            # Exclude enemy types not defined in this world's BEF.
            # Without this, seeds that roll a world-specific enemy (e.g.
            # Goat_Devil 0x13 in a Grave-world file) cause the engine to
            # look up a null mesh pointer and crash — the intermittent
            # "crash when loading a level" bug. This covers BOTH enemy types
            # AND world-specific props (smashables, lanterns, statues, etc.)
            # in UNIVERSAL_ITEM_POOL — all are BEF-managed and will crash if
            # placed in a file whose BEF doesn't define them. bef_native_types
            # is empty for a world only if its BEF failed to parse; skip the
            # filter in that case so the tool doesn't crash itself.
            native = bef_native_types.get(world)
            if native:
                world_excluded |= ALL_POOL_BEF_TYPES - native
            item_repl = aggressive_item_randomize(
                psx, seed=item_seed,
                initial_budget_deficit=max(0, enemy_swap_delta),
                existing_repl=all_repl,
                extra_templates=world_item_templates.get(world, {}),
                world=world,
                world_gen_templates=world_gen_templates.get(world),
                excluded_types=world_excluded,
                do_items=do_items,
                do_chests=do_chests,
                do_skills=do_skills,
                do_columns=do_columns,
                universal_weights=uni_weights,
                tier_weights=tier_weights,
                weather_template=weather_template_for(world),
                chest_special_chances=(forced_chest_special if forced_chest_special
                                       is not None else
                                       (spawn.chest_special_chances()
                                        if spawn is not None else None)),
                lift_protection=(path.name.upper() in LIFT_PROTECTION_FILES),
                randomize_gen_tier=getattr(args, "gen_tier", True),
                gate_mode=gate_mode,
                gate_templates=world_gate_templates.get(world),
                preserve_chests=preserve_chests,
                preserve_iron_keys=preserve_iron_keys,
            )
            for off, r in item_repl.items():
                all_repl[off] = r

        # 3. EVEN-OUT pass: guarantee a perfectly even distribution across
        # every walking-melee enemy in the world's pool.
        #
        # Both step 1 (cli.py uniform enemy swap) and step 2 (items rolling
        # into enemies via the universal pool) introduce statistical bias —
        # some enemies sit in size classes that fit budget more often, some
        # share sizes with popular items, etc. This final pass collects every
        # walking-melee record that ended up in the file (vanilla survivors
        # + items that became enemies) and reassigns their type using a
        # shuffled balanced bag so each pool member appears the same number
        # of times (off by one when the count doesn't divide evenly).
        # Event-tied enemies are excluded from rebalancing.
        if not args.no_enemies:
            pool_ids = pool_for_world(world)
            templates = world_templates.get(world, {})
            if templates:
                pool_list = sorted(templates.keys())  # deterministic
                # Final state of every record in the file (post-step-1+2)
                final_state: dict[int, "PsxRecord"] = {
                    r.offset: all_repl.get(r.offset, r) for r in psx.records
                }
                # Plus any newly-created enemy records that came in via items
                for off, rec in all_repl.items():
                    if off not in final_state:
                        final_state[off] = rec

                # Eligible enemy slots — ones we're allowed to retype.
                eligible: list["PsxRecord"] = []
                for off, rec in final_state.items():
                    if rec.type_id not in pool_ids:
                        continue
                    if is_event_tied_enemy(rec.instance_name):
                        continue
                    if rec.instance_name in progression_names:
                        continue
                    eligible.append(rec)

                if eligible and len(pool_list) > 1:
                    # Build a WEIGHTED balanced bag.
                    # Per-type weights come from the spawn config (when the
                    # user supplied one) or the stock WALKING_MELEE_WEIGHTS —
                    # certain types (Raven/Ghost/Torso/Guard) default to a
                    # reduced weight so they appear less often. Slots are
                    # allocated proportionally and rounded with leftover slots
                    # going to the highest-weight types.
                    ewt = enemy_weight_table(world)
                    N = len(eligible)
                    weights = [ewt.get(t, 1.0)
                               for t in pool_list]
                    total_w = sum(weights) or 1.0
                    raw = [N * w / total_w for w in weights]
                    counts = [int(x) for x in raw]
                    leftover = N - sum(counts)
                    # Distribute the leftover seats by largest fractional part
                    # (standard "Hamilton" allocation).
                    if leftover > 0:
                        fracs = sorted(
                            range(len(pool_list)),
                            key=lambda i: (raw[i] - counts[i], weights[i]),
                            reverse=True,
                        )
                        for i in fracs[:leftover]:
                            counts[i] += 1
                    bag: list[int] = []
                    for tid, c in zip(pool_list, counts):
                        bag.extend([tid] * c)
                    rng.shuffle(bag)

                    # Compute current net size delta (what the file would
                    # grow by if we wrote it now). Negative = we have free
                    # headroom; positive = already over-budget. We use that
                    # as the budget for the even-out pass so it can roll
                    # bigger types when there's headroom available.
                    orig_sizes = {r.offset: r.size for r in psx.records}
                    current_delta = sum(
                        rec.size - orig_sizes.get(off, rec.size)
                        for off, rec in all_repl.items()
                    )
                    # Allow the even-out pass to consume headroom up to a
                    # safety margin to avoid PSX-rewrite overshoot.
                    SAFETY = 256
                    headroom = max(0, -current_delta - SAFETY)

                    # Process eligible slots in size-DESCENDING order so big
                    # records get retyped first. When a big slot rolls a
                    # small target we shrink, releasing byte budget that
                    # later small slots can use to grow into bigger types.
                    eligible.sort(key=lambda r: -r.size)

                    # Pair each eligible record with a target type from the
                    # bag and reassign. Skip if the record already has the
                    # rolled type (no-op) or if the swap would overshoot the
                    # file's byte budget.
                    for rec, new_type in zip(eligible, bag):
                        if rec.type_id == new_type:
                            continue
                        tpl = templates[new_type]
                        delta = tpl.size - rec.size
                        if delta > headroom:
                            # Would overshoot. Pick a RANDOM affordable
                            # alternative weighted by the bag weights so
                            # reduced types stay reduced.
                            affordable = [
                                cand for cand in pool_list
                                if cand != rec.type_id
                                and (templates[cand].size - rec.size) <= headroom
                            ]
                            if not affordable:
                                continue  # can't safely retype
                            ww = [ewt.get(c, 1.0)
                                  for c in affordable]
                            new_type = rng.choices(affordable, weights=ww, k=1)[0]
                            tpl = templates[new_type]
                            delta = tpl.size - rec.size
                        headroom -= delta
                        new_rec = make_record_with_template(
                            tpl, rec, rec.offset
                        )
                        # The template's tier (prop[0]) is whatever the
                        # template happened to have — usually 0. Roll a fresh
                        # tier so step-3 reassignments don't all stick on T0.
                        new_rec = reroll_enemy_tier(
                            new_rec, rng, world=world,
                            tier_weights_per_type=enemy_tier_table(world))
                        all_repl[rec.offset] = new_rec

            # 4. Boost aggro range on every Raven in the final state.
            # Vanilla Ravens have a tiny 15-38 unit sight zone that makes
            # them feel passive — they only react when the player is
            # right under them. Bump every Raven's trigger volume to a
            # normal melee-style detection range so they actually engage.
            for r in psx.records:
                cur = all_repl.get(r.offset, r)
                if cur.type_id == 0x37:
                    new_rec = _boost_raven_aggro(cur)
                    if new_rec is not cur:
                        all_repl[r.offset] = new_rec
            # Also handle Ravens at offsets that didn't exist in vanilla
            # (e.g. created by the universal item randomizer at coin slots).
            for off, rec in list(all_repl.items()):
                if rec.type_id == 0x37:
                    new_rec = _boost_raven_aggro(rec)
                    if new_rec is not rec:
                        all_repl[off] = new_rec

            # 5. Roll a fresh death-drop kind on every walking-melee enemy.
            # Without this, every enemy of a given type inherits the
            # template's prop[5] value (so they all drop the same thing —
            # e.g. every Axe_Skeleton spawns a Skeleton_Key on death because
            # the chosen template happened to come from a HUB record where
            # prop[5] was 1). The drop randomizer rolls 0/1/2/3/4 from the
            # type's VANILLA-WEIGHTED pool (mirrors per-type frequency) so
            # most enemies still drop nothing but ~25% drop a key/ability/
            # health/gold and Iron Keys (val=1 on Axe_Skeleton) stay rare.
            drop_rng = random.Random(file_seed ^ 0xD150D250)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_drop(rec, drop_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            # And any vanilla survivors not in all_repl yet.
            for r in psx.records:
                if r.offset in all_repl:
                    continue
                new_rec = reroll_enemy_drop(r, drop_rng)
                if new_rec is not r:
                    all_repl[r.offset] = new_rec

            # 6. Roll a fresh BOOLEAN VARIANT FLAG on every enemy that has
            # one — Ghost (Poltergeist vs Blue Ghost), Basic_Zombie (normal
            # vs splittable), Basic_Skeleton (variant flag). cli.py picks
            # ONE template per (world, type), so without this every enemy
            # of that type inherits the template's variant — e.g. Grave is
            # 100% Poltergeist (no Blue Ghost) and Under is 100% non-
            # splittable Zombie (the corpse never spawns a Torso_Zombie).
            # Using vanilla-derived weights restores the natural mix in
            # every world.
            variant_rng = random.Random(file_seed ^ 0xBADBA17)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_variant(rec, variant_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            for r in psx.records:
                if r.offset in all_repl:
                    continue
                new_rec = reroll_enemy_variant(r, variant_rng)
                if new_rec is not r:
                    all_repl[r.offset] = new_rec

            # 7. FINAL NORMALIZATION (enemy-block portion): hard-clamp
            # known-bad props on every enemy in the final state. The
            # cli.py template-selection filter tries to pick "neutral"
            # templates (p5=0 etc.) but if the world only has HUB-style
            # records for a given type, the filter falls back to a HUB
            # record and every cloned enemy inherits the HUB phase value.
            # Most painful symptom: every Axe_Skeleton drops an Iron Key
            # on death because HUB Axe_Skeletons all have prop[5]=1 which
            # the engine reads as "drop Skeleton_Key". `force_enemy_props`
            # runs unconditionally on every record after everything else
            # and clamps these dangerous slots back to 0.
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = force_enemy_props(rec)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            for r in psx.records:
                if r.offset in all_repl:
                    continue
                new_rec = force_enemy_props(r)
                if new_rec is not r:
                    all_repl[r.offset] = new_rec

        # FINAL NORMALIZATION (always runs, even when --no-enemies). Items
        # mode can still produce enemies via item-to-enemy rolls in the
        # universal pool, and those need force_enemy_props too. This second
        # pass is idempotent — rerunning on already-clamped records is a
        # no-op.
        for off in list(all_repl.keys()):
            rec = all_repl[off]
            new_rec = force_enemy_props(rec)
            if new_rec is not rec:
                all_repl[off] = new_rec
        for r in psx.records:
            if r.offset in all_repl:
                continue
            new_rec = force_enemy_props(r)
            if new_rec is not r:
                all_repl[r.offset] = new_rec

        # START-OF-LEVEL Gold_Key guarantee. The four named levels in
        # START_GOLD_KEY_FILES must always have a collectable Gold_Key
        # right at the player's spawn. This OVERRIDES whatever the nearest
        # filler-item slot rolled into, so it runs last.
        placed_key_offsets: set[int] = set()
        if path.name.upper() in {k.upper() for k in START_GOLD_KEY_FILES}:
            key_repl = place_start_gold_key(
                psx, path.name, global_gold_key_template,
                existing_repl=all_repl,
            )
            for off, r in key_repl.items():
                all_repl[off] = r
                placed_key_offsets.add(off)

        # EXTRA mid-level Gold_Keys + extra Skeleton_Keys for gated levels
        # (e.g. Dungeon of Despair). Must be able to OVERRIDE universal swap
        # slots (the universal pass has usually already consumed every filler
        # pickup), so we only avoid the start-key slot — NOT every swap.
        if path.name.upper() in {k.upper() for k in EXTRA_KEY_FILES}:
            extra_repl = place_extra_keys(
                psx, path.name, global_gold_key_template,
                global_skeleton_key_template,
                avoid_offsets=placed_key_offsets,
            )
            for off, r in extra_repl.items():
                all_repl[off] = r
                placed_key_offsets.add(off)
                placed_key_offsets.add(off)

        # FIXED-POSITION Gold_Keys at specific gates that would otherwise
        # soft-lock (e.g. The Siege's catapult gate). This must be able to
        # OVERRIDE a universal swap slot (every nearby filler pickup has
        # usually already been swapped into an enemy/item), so we only avoid
        # the slots we used for the start / extra keys above.
        if path.name.upper() in {k.upper() for k in FIXED_GOLD_KEY_TARGETS}:
            fixed_repl = place_fixed_gold_keys(
                psx, path.name, global_gold_key_template,
                avoid_offsets=placed_key_offsets,
            )
            for off, r in fixed_repl.items():
                all_repl[off] = r

        out_path = out_folder / path.name
        spawn_patch = spawn_patch_for(psx, path.name, file_seed)
        if not all_repl and not spawn_patch:
            print(f"  [SKIP] {path.name}: no swaps (world={world}, not written)")
            continue

        try:
            stats = psx.write_with_replacements(all_repl, out_path,
                                                header_patches=spawn_patch)
            print(f"  [DONE] {path.name}: {len(all_repl)} changes, world={world}, "
                  f"shift={stats['total_shift']}b"
                  f"{', spawn moved' if spawn_patch else ''}")
            summary["files_processed"].append({
                "file": path.name,
                "world": world,
                "changes": len(all_repl),
                "shift": stats["total_shift"],
                "seed": file_seed,
                "spawn_moved": bool(spawn_patch),
            })
        except Exception as e:
            print(f"  [FAIL] {path.name}: {e} — writing vanilla fallback")
            summary["errors"].append({"file": path.name, "error": str(e)})
            # CRITICAL: always write a vanilla copy to out_path so
            # write_patched_assets doesn't write a stale/partial file
            # from a previous run into the ISO, corrupting that sector.
            import shutil as _shutil
            _shutil.copy2(path, out_path)

    # SLUS (ELF) patches. The executable is patched and written to out_folder
    # when EITHER:
    #   - columns are randomized (level-title '???' patch, world-aware), OR
    #   - skills are randomized (ability/skill DROP pool patch — lets every
    #     world drop every sword enchantment AND every skill, not just its
    #     vanilla ones).
    # Both patches are applied to ONE bytearray and written once.
    do_sword_drops = do_skills
    damage_taken = getattr(args, "damage_taken", None) or "normal"
    damage_dealt = getattr(args, "damage_dealt", None) or "normal"
    if harder_mode:
        # Harder mode forces Maximo to take 2x and deal 0.5x damage.
        damage_taken = "2x"
        damage_dealt = "0.50"
    # Starting inventory (new-game GameVarTbl). None = leave vanilla.
    start_gold = getattr(args, "start_gold", None)
    start_lives = getattr(args, "start_lives", None)
    start_keys = getattr(args, "start_keys", None)
    start_deathcoins = getattr(args, "start_deathcoins", None)
    sword_enchant = getattr(args, "sword_enchant", None)
    elemental_shield = getattr(args, "elemental_shield", None)
    # Starting skills: accept a comma-string (CLI) or an iterable (GUI/ISO).
    _ss_raw = getattr(args, "start_skills", None)
    if isinstance(_ss_raw, str):
        start_skills = {s.strip() for s in _ss_raw.split(",") if s.strip()}
    else:
        start_skills = set(_ss_raw or ())
    if getattr(args, "randomize_start_inv", False):
        # Roll seeded starting values (overrides any manual entries). Uses the
        # run seed so the same seed reproduces the same loadout.
        rng = random.Random((seed if seed is not None else 0) ^ 0x57A47125)
        start_gold = rng.choice([0, 0, 50, 100, 200, 350, 500, 750, 1000])
        start_lives = rng.randint(1, 9)
        start_keys = rng.randint(0, 3)
        start_deathcoins = rng.randint(1, 5)
        sword_enchant = rng.randint(0, 4)
        elemental_shield = rng.randint(0, 3)
        start_skills = {s for s in STARTING_SKILL_MASKS if rng.random() < 0.5}
        print(f"Starting inventory randomized: gold={start_gold} "
              f"lives={start_lives} keys={start_keys} deathcoins={start_deathcoins} "
              f"sword_enchant={sword_enchant} elemental_shield={elemental_shield} "
              f"skills={sorted(start_skills) or 'none'}")
    do_start_inv = any(v is not None for v in
                       (start_gold, start_lives, start_keys, start_deathcoins,
                        sword_enchant, elemental_shield))
    do_start_skills = bool(start_skills)
    do_rand_levels = bool(getattr(args, "randomize_levels", False))
    if (do_columns or do_sword_drops or damage_taken != "normal"
            or damage_dealt != "normal" or do_start_inv or do_start_skills
            or do_rand_levels):
        # Region-aware: patch whichever boot executable the disc shipped
        # (US SLUS_200.17 / JP SLPM_621.27 / ...). All patches below are located
        # by signature or anchor, so they apply to any region. The output keeps
        # the SAME filename so the ISO re-injection map matches.
        elf_src = find_executable(in_folder)
        if elf_src is not None:
            elf_out = out_folder / elf_src.name
            print(f"\nELF patch: target executable {elf_src.name}")
            try:
                data = bytearray(elf_src.read_bytes())
                if do_columns:
                    tres = apply_level_titles(data, worlds=selected_worlds)
                    print(f"\nELF patch: level titles ({len(tres['patched'])} slots)")
                    bad = [s for s in tres["skipped"] if "actual" in s]
                    if bad:
                        print(f"  WARNING: skipped {len(bad)} title slot(s) "
                              f"(text mismatch — wrong ELF version?)")
                if do_sword_drops:
                    sres = patch_ability_drops(data, seed)
                    if sres["found"]:
                        print(f"ELF patch: ability/skill drops randomized across "
                              f"{sres['tables']} levels ({sres['abilities']} "
                              f"abilities) — any world can drop any sword "
                              f"enchantment and skill")
                    else:
                        print("ELF patch: ability-drop table NOT found "
                              "(unexpected SLUS version?) — drops unchanged")
                if damage_taken != "normal":
                    dres = patch_damage_taken(data, damage_taken)
                    if dres.get("applied"):
                        print(f"ELF patch: Maximo damage-taken set to "
                              f"'{damage_taken}'")
                    else:
                        print(f"ELF patch: damage-taken patch NOT applied "
                              f"({dres.get('reason', 'site not found')})")
                if damage_dealt != "normal":
                    ddres = patch_damage_dealt(data, damage_dealt)
                    if ddres.get("applied"):
                        print(f"ELF patch: Maximo damage-dealt set to "
                              f"'{damage_dealt}'")
                    else:
                        print(f"ELF patch: damage-dealt patch NOT applied "
                              f"({ddres.get('reason', 'site not found')})")
                if do_start_inv:
                    sinv = patch_starting_inventory(
                        data, gold=start_gold, lives=start_lives,
                        keys=start_keys, deathcoins=start_deathcoins,
                        sword_enchant=sword_enchant,
                        elemental_shield=elemental_shield)
                    if sinv.get("applied"):
                        print(f"ELF patch: starting inventory set "
                              f"{sinv['changed']}")
                    else:
                        print(f"ELF patch: starting-inventory patch NOT applied "
                              f"({sinv.get('reason', 'GameVarTbl not found')})")
                if do_start_skills:
                    sskl = patch_starting_skills(data, start_skills)
                    if sskl.get("applied"):
                        print(f"ELF patch: starting skills set "
                              f"{sorted(sskl['skills'])}")
                    else:
                        print(f"ELF patch: starting-skills patch NOT applied "
                              f"({sskl.get('reason', 'InitVars site not found')})")
                if do_rand_levels:
                    rlv = patch_randomize_levels(
                        data, seed,
                        cross_world=bool(getattr(args, "randomize_levels_cross", False)))
                    if rlv.get("applied"):
                        if rlv.get("cross_world"):
                            print("ELF patch: WHOLE-WORLD swap (experimental) — "
                                  "entire worlds permuted:")
                            for dst, src in rlv.get("mapping", []):
                                if dst != src:
                                    print(f"    entering {dst} now plays {src}")
                            excl = rlv.get("excluded_worlds")
                            if excl:
                                print(f"    (kept vanilla, non-standard boss layout: "
                                      f"{', '.join(excl)})")
                        else:
                            print(f"ELF patch: levels RANDOMIZED — "
                                  f"{rlv['sublevels']} sub-levels shuffled within their worlds")
                            for dst, src in rlv.get("mapping", []):
                                if dst != src:
                                    print(f"    {dst}  loads  {src}")
                    else:
                        print(f"ELF patch: level-randomize patch NOT applied "
                              f"({rlv.get('reason', 'LevelTable not found')})")
                elf_out.write_bytes(bytes(data))
                print(f"ELF patch: wrote {elf_out}")
            except Exception as e:
                print(f"\nELF patch FAILED: {e}")
        else:
            print(f"\nELF patch SKIPPED: no boot executable "
                  f"({' / '.join(KNOWN_EXECUTABLES)}) found in source folder.")

    summary_path = out_folder / "randomizer_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\nSummary written to: {summary_path}")
    print(f"Total processed: {len(summary['files_processed'])}, errors: {len(summary['errors'])}")


def _resolve_worlds(value: str | None) -> set[str]:
    """Parse the --worlds argument. Returns a set of canonical world names.

    Accepted values:
      None or "all"            -> every world
      "grave,under,..."        -> only those worlds
      "grave"                  -> just one
    """
    if not value or value.lower() == "all":
        return set(ALL_WORLDS)
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    bad = [p for p in parts if p not in ALL_WORLDS]
    if bad:
        print(f"ERROR: unknown world(s): {bad}. Valid: {', '.join(ALL_WORLDS)}")
        sys.exit(1)
    return set(parts)


def main():
    parser = argparse.ArgumentParser(
        prog="randomizer",
        description="Maximo: Ghosts to Glory enemy randomizer",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="Show statistics for a PSX file")
    p_stats.add_argument("psx_file", help="Path to a PSX file")
    p_stats.set_defaults(func=cmd_stats)

    p_list = sub.add_parser("list-types", help="List all entity types in a folder of PSX files")
    p_list.add_argument("psx_folder", help="Folder containing PSX files")
    p_list.set_defaults(func=cmd_list_types)

    p_rand = sub.add_parser("randomize", help="Randomize enemies/items in a folder of PSX files")
    p_rand.add_argument("psx_folder", help="Folder containing source PSX files")
    p_rand.add_argument("--output", "-o", required=True, help="Output folder")
    p_rand.add_argument("--seed", type=int, help="RNG seed (default: random)")
    p_rand.add_argument("--items", action="store_true", help="Randomize items/structures (universal type swap), plus collectors, enemy tiers, generators, and weather")
    p_rand.add_argument("--chests", action="store_true", default=None, help="Randomize chest contents and gold amounts")
    p_rand.add_argument("--skills", action="store_true", default=None, help="Randomize ability/skill pickup IDs")
    p_rand.add_argument("--columns", action="store_true", default=None, help="Randomize HUB level-select columns (blind select + '???' level titles)")
    p_rand.add_argument("--no-gen-tier", action="store_false", dest="gen_tier",
                        default=True,
                        help="Disable monster-generator class-level randomization "
                             "(on by default — generators spawn base/upgraded/elite variants)")
    p_rand.add_argument("--damage-taken", dest="damage_taken", default="normal",
                        choices=["0.25", "0.50", "normal", "2x", "4x"],
                        help="Scale the damage Maximo takes from enemies "
                             "(0.25/0.50/normal/2x/4x). Default normal.")
    p_rand.add_argument("--damage-dealt", dest="damage_dealt", default="normal",
                        choices=["0.25", "0.50", "normal", "2x", "4x"],
                        help="Scale the melee damage Maximo deals to enemies "
                             "(0.25/0.50/normal/2x/4x). Default normal.")
    p_rand.add_argument("--start-gold", dest="start_gold", type=int, default=None,
                        help="Starting koins for a new game (0-9999). "
                             "Omit to keep vanilla (0).")
    p_rand.add_argument("--start-lives", dest="start_lives", type=int, default=None,
                        help="Starting lives for a new game (0-99). "
                             "Omit to keep vanilla (4).")
    p_rand.add_argument("--start-keys", dest="start_keys", type=int, default=None,
                        help="Starting keys for a new game (0-9). "
                             "Omit to keep vanilla (0).")
    p_rand.add_argument("--start-deathcoins", dest="start_deathcoins", type=int,
                        default=None,
                        help="Starting death coins for a new game (0-99). "
                             "Omit to keep vanilla (1).")
    p_rand.add_argument("--sword-enchant", dest="sword_enchant", type=int,
                        default=None,
                        help="Starting sword enchant tier: 0=normal, 1=Fire, "
                             "2=Ice, 3=Sun, 4=Armageddon. Omit to keep vanilla.")
    p_rand.add_argument("--elemental-shield", dest="elemental_shield", type=int,
                        default=None,
                        help="Starting elemental shield: 0=none, 1=Wind, "
                             "2=Magnetic, 3=Lightning. Omit to keep vanilla.")
    p_rand.add_argument("--start-skills", dest="start_skills", default=None,
                        help="Comma-separated new-game starting sword skills: "
                             "sword720,double_slash,mighty_blow,masquerade.")
    p_rand.add_argument("--randomize-start-inv", action="store_true",
                        dest="randomize_start_inv",
                        help="Roll seeded random starting gold/lives/keys/"
                             "deathcoins/skills (overrides the manual values).")
    p_rand.add_argument("--randomize-levels", action="store_true",
                        dest="randomize_levels",
                        help="Shuffle which level loads in each playable "
                             "sub-level slot, within each world (repoints the "
                             "engine's LevelTable file pointers; progression is "
                             "preserved).")
    p_rand.add_argument("--randomize-levels-cross", action="store_true",
                        dest="randomize_levels_cross",
                        help="EXPERIMENTAL: with --randomize-levels, permute "
                             "ENTIRE worlds (whole-world swap) so each hub plays "
                             "a different world end-to-end, instead of shuffling "
                             "sub-levels within each world.")
    p_rand.add_argument("--preserve-chests", action="store_true", dest="preserve_chests",
                        help="Keep every vanilla chest in place (type/position) "
                             "but randomize the contents inside them. Composes "
                             "with --harder-mode (opt-in add-on, not forced off).")
    p_rand.add_argument("--preserve-iron-keys", action="store_true", dest="preserve_iron_keys",
                        help="Keep every vanilla Iron Key (Skeleton_Key) in its "
                             "original position/count — keys are never moved, "
                             "removed, or added at random. Composes with "
                             "--harder-mode (opt-in add-on, not forced off).")
    p_rand.add_argument("--harder-mode", action="store_true", dest="harder_mode",
                        help="Enable HARDER MODE: koin-only economy (gold/gem/"
                             "health/life disabled), structures disabled and "
                             "spawned as enemies, rarer keys/collectors/skills, "
                             "more item->enemy rolls, mostly-empty enemy drops, "
                             "all enemies at max class, 1-koin chests, "
                             "mimic/wizard 33%% each, boss x2 (Grave+Swamp), "
                             "Maximo takes 2x / deals 0.5x damage.")
    p_rand.add_argument("--gate-mode", dest="gate_mode", default=None,
                        choices=["isolated", "pool"],
                        help="Gate randomizer: 'isolated' swaps gates among gate "
                             "types only; 'pool' adds gates to the item pool so "
                             "they can appear anywhere. Omit to leave gates alone.")
    p_rand.add_argument("--spawn-location", action="store_true", dest="spawn_location",
                        help="Randomize Maximo's level-start (spawn) position — moved to a random item/structure/enemy location")
    p_rand.add_argument("--spawn-config", dest="spawn_config_path", default=None,
                        help="Path to a spawn-rate JSON config (per-tag/per-world weights + enable flags). "
                             "Omit to use the stock spawn rates.")
    p_rand.add_argument("--no-enemies", action="store_true", help="Don't randomize enemies (only items if --items)")
    p_rand.add_argument(
        "--all-enemies", action="store_true",
        help="CHAOS mode: transform every item, chest, structure and smashable "
             "into an enemy. Gold_Keys are preserved. Enemies can drop Skeleton "
             "Keys, and the 1%% Skeleton-Key / Gold-Key map lotteries still run "
             "so the game stays beatable.",
    )
    p_rand.add_argument(
        "--duplicate-bosses", action="store_true",
        help="EXPERIMENTAL: clone the boss in supported boss arenas (currently "
             "Grave / GraveDigger and Swamp / BokorLaBas) so the fight has "
             "multiple bosses. May be unstable — boss death/victory triggers "
             "are scripted for one boss.",
    )
    p_rand.add_argument(
        "--boss-clones", type=int, default=1,
        help="Legacy: extra boss clones for ALL bosses when --duplicate-bosses "
             "is set. Overridden by --boss-clones-grave / --boss-clones-swamp.",
    )
    p_rand.add_argument(
        "--boss-clones-grave", type=int, default=None,
        help="Extra GraveDigger clones (Grave) when --duplicate-bosses is set "
             "(max 4; 4 clones = 5 bosses).",
    )
    p_rand.add_argument(
        "--boss-clones-swamp", type=int, default=None,
        help="Extra BokorLaBas clones (Swamp) when --duplicate-bosses is set "
             "(max 6; 6 clones = 7 bosses).",
    )
    p_rand.add_argument(
        "--no-dark-knight", action="store_true", dest="exclude_dark_knight",
        help="Exclude Dark_Knight (Castle enemy) from every randomization "
             "pool, keeping only the vanilla-placed ones. Dark_Knight has a "
             "kill-event quirk that can SOFT-LOCK a level if it lands on a "
             "progression slot, so this is the safe option.",
    )
    p_rand.add_argument(
        "--worlds", default="all",
        help="Comma-separated worlds to randomize: all, grave, under, swamp, ice, castle. "
             "Examples: --worlds all (default), --worlds grave, --worlds grave,under",
    )
    p_rand.add_argument(
        "--cross-world", action="store_true",
        help="Use the universal enemy pool for all worlds. Requires BEF injection "
             "(done automatically by iso_patcher --cross-world). In folder mode, "
             "the BEF files in the input folder must already have been expanded. "
             "Enables ANY enemy type to spawn in ANY world.",
    )
    p_rand.set_defaults(func=cmd_randomize)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
