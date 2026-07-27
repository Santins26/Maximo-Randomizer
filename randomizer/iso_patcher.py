"""
End-to-end ISO patching for the Maximo: Ghosts to Glory randomizer.

Workflow:
  1. Take a vanilla Maximo ISO as input.
  2. Optionally make a backup copy.
  3. Extract the files we'll randomize (PSX maps + SLUS executable).
  4. Run the randomizer on those files.
  5. Patch the randomized output back into the ISO in-place.

Because our randomizer pads every modified file to its ORIGINAL size, no
ISO rebuilding is needed. We seek to each file's LBA and overwrite the bytes.
"""
from __future__ import annotations
import argparse
import random
import shutil
import sys
import tempfile
from pathlib import Path

from .iso import IsoFile, find_maximo_files, SLUS_FILENAME


# NOTE: this tuple is NOT used anywhere in this module -- write_patched_assets()
# below iterates `file_map`, which comes from extract_assets() -> 
# find_maximo_files() (iso.py), and that already collects .PSX/.BEF/.PRS/
# .PRT/.TEX generically. Kept only as documentation of what's expected to
# round-trip; delete once confirmed nothing external imports it.
ASSET_EXTENSIONS = (".PSX", ".BEF", ".PRS", ".PRT")


def extract_assets(iso_path: Path, out_dir: Path) -> dict[str, tuple[int, int]]:
    """Extract every PSX and BEF file (plus SLUS_200.17) from the ISO into
    `out_dir`. Returns the LBA/length map so we can write back in-place later.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    file_map: dict[str, tuple[int, int]] = {}
    with IsoFile(iso_path, writable=False) as iso:
        files = find_maximo_files(iso)
        for upper_name, (lba, length) in files.items():
            data = iso.read_file(lba, length)
            (out_dir / upper_name).write_bytes(data)
            file_map[upper_name] = (lba, length)
    return file_map


def write_patched_assets(
    iso_path: Path,
    patched_dir: Path,
    file_map: dict[str, tuple[int, int]],
    progress=None,
) -> dict:
    """Write randomized files back into the ISO in-place.

    `file_map`: {uppercase filename: (lba, length)} from `extract_assets`.
    `patched_dir`: folder containing patched files (typically the randomizer's
                   output folder). Files NOT in patched_dir are left as-is.
    `progress`: optional callable receiving (filename, written_bytes, total).

    Files that grew beyond their sector slot (e.g. BEFs with injected enemy
    assets) are relocated to the end of the disc and their directory entries
    updated to point to the new LBA.
    """
    SECTOR = 2048  # ISO 9660 user-data sector size; matches USER_DATA_SIZE
    written = 0
    skipped = []
    grown: list[tuple[str, int, int]] = []  # (name, old_len, new_len)
    relocated: list[tuple[str, int, int, int]] = []  # (name, old_lba, new_lba, new_len)
    with IsoFile(iso_path, writable=True) as iso:
        # Pre-compute every file's parent directory location so we can
        # update the data-length field of the directory record when a
        # patched file outgrew its original length.
        parents = iso.file_parents()
        for upper_name, (lba, length) in file_map.items():
            patched = patched_dir / upper_name
            if not patched.exists():
                # Try lower-cased / mixed-case match
                for candidate in patched_dir.iterdir():
                    if candidate.name.upper() == upper_name:
                        patched = candidate
                        break
            if not patched.exists():
                skipped.append(upper_name)
                continue
            data = patched.read_bytes()
            # The on-disc slot for each file is `length` rounded UP to the
            # next sector boundary. iso.write_file accepts data up to that
            # padded size and zero-fills the tail.
            slack = (-length) % SECTOR
            slot_size = length + slack
            if len(data) > slot_size:
                # File grew beyond its sector slot — relocate to end of disc.
                parent = parents.get(lba)
                if parent is None:
                    raise RuntimeError(
                        f"{upper_name}: grew beyond sector slot but parent "
                        f"directory unknown; cannot relocate."
                    )
                new_lba = iso.append_file(data)
                ok = iso.update_directory_entry_lba_and_length(
                    parent[0], parent[1], lba, new_lba, len(data),
                )
                if not ok:
                    raise RuntimeError(
                        f"{upper_name}: could not update directory record "
                        f"for relocation (parent_dir_lba=0x{parent[0]:X})"
                    )
                relocated.append((upper_name, lba, new_lba, len(data)))
            else:
                iso.write_file(lba, data, length)
                # If the file grew (within slack), patch its directory record so
                # ISO 9660 reports the new length and the engine reads the
                # appended bytes. Same-size writes leave the directory untouched.
                if len(data) > length:
                    parent = parents.get(lba)
                    if parent is not None:
                        ok = iso.update_directory_entry_length(
                            parent[0], parent[1], lba, len(data),
                        )
                        if not ok:
                            raise RuntimeError(
                                f"{upper_name}: could not update directory "
                                f"record (parent_dir_lba=0x{parent[0]:X})"
                            )
                        grown.append((upper_name, length, len(data)))
                    else:
                        raise RuntimeError(
                            f"{upper_name}: grew but parent directory unknown; "
                            f"cannot update ISO 9660 length field."
                        )
            written += 1
            if progress:
                progress(upper_name, written, len(file_map))
    return {"written": written, "skipped": skipped, "grown": grown,
            "relocated": relocated, "total": len(file_map)}


def patch_iso(
    iso_path: Path | str,
    seed: int | None = None,
    *,
    items: bool = True,
    chests: bool | None = None,
    skills: bool | None = None,
    columns: bool | None = None,
    spawn_location: bool = False,
    gen_tier: bool = True,
    gate_mode: str | None = None,
    damage_taken: str = "normal",
    damage_dealt: str = "normal",
    start_gold: int | None = None,
    start_lives: int | None = None,
    start_keys: int | None = None,
    start_deathcoins: int | None = None,
    sword_enchant: int | None = None,
    elemental_shield: int | None = None,
    start_skills=None,
    randomize_start_inv: bool = False,
    randomize_levels: bool = False,
    randomize_levels_cross: bool = False,
    harder_mode: bool = False,
    preserve_chests: bool = False,
    preserve_iron_keys: bool = False,
    spawn_config_path: str | None = None,
    enemies: bool = True,
    all_enemies: bool = False,
    cross_world: bool = False,
    worlds: set[str] | None = None,
    duplicate_bosses: bool = False,
    exclude_dark_knight: bool = False,
    boss_clones: int = 1,
    boss_clones_grave: int | None = None,
    boss_clones_swamp: int | None = None,
    boss_clones_ice: int | None = None,
    boss_clones_under: int | None = None,
    boss_clones_castle: int | None = None,
    output_iso: Path | str | None = None,
    backup: bool = True,
    workdir: Path | None = None,
    log=print,
) -> dict:
    """Run the full disc-image randomization pipeline.

    Args:
      iso_path:    Path to the Maximo disc image. Accepts .iso, .bin, or .cue.
                   For .cue, the referenced .bin is used.
      seed:        RNG seed (None = random).
      items:       Randomize items / chests / abilities / level columns.
      enemies:     Randomize walking-melee enemies.
      all_enemies: CHAOS mode — turn every item / structure into an enemy.
                   Overrides `items` / `enemies`. Gold Keys preserved.
      cross_world: Inject enemy assets across worlds so ANY enemy can appear
                   in ANY world. Modifies BEF files (grows them ~30-48KB each).
      worlds:      Set of worlds to randomize (subset of grave/under/swamp/ice/castle).
                   None means all worlds.
      output_iso:  Optional path for the patched output. When provided:
                     - The source disc is copied to this location FIRST.
                     - All patches are applied to the COPY, leaving the source
                       byte-for-byte untouched.
                     - For .cue input, the companion .bin is copied beside the
                       output and the .cue is rewritten to point at it.
                   When omitted, the source disc is patched in place (the
                   classic behavior — backup is created if `backup=True`).
      backup:      Only used when `output_iso` is None. Copies the source to
                   <name>.backup before in-place patching.
      workdir:     Folder to extract / write patched assets into. None = temp.
      log:         Callable for progress lines (default: print).

    Returns a dict with seed, files_written, skipped, paths.
    """
    iso_path = Path(iso_path)
    if not iso_path.exists():
        raise FileNotFoundError(iso_path)

    # Resolve the actual data file we will patch (the BIN, when given a CUE).
    is_cue = iso_path.suffix.lower() == ".cue"
    if is_cue:
        from .iso import parse_cue
        source_bin = parse_cue(iso_path)
        log(f"CUE sheet references: {source_bin.name}")
    else:
        source_bin = iso_path

    # Determine where the patches actually land. Two modes:
    #   1. output_iso provided -> copy source to that path, patch the copy
    #   2. output_iso missing  -> patch source in place (with optional backup)
    backup_path = None
    if output_iso is not None:
        target_path = _prepare_output_copy(iso_path, source_bin, Path(output_iso),
                                           is_cue=is_cue, log=log)
    else:
        target_path = source_bin
        if backup:
            backup_path = target_path.with_suffix(target_path.suffix + ".backup")
            if not backup_path.exists():
                log(f"Creating backup: {backup_path.name}")
                shutil.copy2(target_path, backup_path)
            else:
                log(f"Backup already exists: {backup_path.name} (skipping)")

    if seed is None:
        seed = random.randint(1, 999999)

    # Set up working directories
    cleanup_workdir = False
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="maximo_rand_"))
        cleanup_workdir = True
    else:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    extract_dir = workdir / "extracted"
    output_dir = workdir / "patched"

    try:
        log(f"Extracting Maximo assets from {target_path.name}...")
        file_map = extract_assets(target_path, extract_dir)
        log(f"  Extracted {len(file_map)} files to {extract_dir}")

        # Cross-world mode: BEF injection is handled inside cmd_randomize.
        # inject_cross_world_enemies() in bef.py rebuilds each world's BEF
        # with foreign enemy blobs remapped to GLOBAL resource loading,
        # then writes them back to extract_dir. write_patched_assets() below
        # pushes the grown BEF files to the disc image automatically.
        if cross_world:
            log(f"Cross-world mode: BEF files will be expanded with foreign enemy assets...")

        from .cli import cmd_randomize, ALL_WORLDS

        if worlds is None:
            worlds_arg = "all"
        else:
            worlds_arg = ",".join(sorted(worlds))

        class _Args:
            pass
        args = _Args()
        args.psx_folder = str(extract_dir)
        args.output = str(output_dir)
        args.seed = seed
        args.items = items
        args.chests = chests
        args.skills = skills
        args.columns = columns
        args.spawn_location = spawn_location
        args.gen_tier = gen_tier
        args.gate_mode = gate_mode
        args.damage_taken = damage_taken
        args.damage_dealt = damage_dealt
        args.start_gold = start_gold
        args.start_lives = start_lives
        args.start_keys = start_keys
        args.start_deathcoins = start_deathcoins
        args.sword_enchant = sword_enchant
        args.elemental_shield = elemental_shield
        args.start_skills = start_skills
        args.randomize_start_inv = randomize_start_inv
        args.randomize_levels = randomize_levels
        args.randomize_levels_cross = randomize_levels_cross
        args.harder_mode = harder_mode
        args.preserve_chests = preserve_chests
        args.preserve_iron_keys = preserve_iron_keys
        args.spawn_config_path = spawn_config_path
        args.no_enemies = not enemies
        args.all_enemies = all_enemies
        args.worlds = worlds_arg
        args.duplicate_bosses = duplicate_bosses
        args.exclude_dark_knight = exclude_dark_knight
        args.boss_clones = boss_clones
        args.boss_clones_grave = boss_clones_grave
        args.boss_clones_swamp = boss_clones_swamp
        args.boss_clones_ice = boss_clones_ice
        args.boss_clones_under = boss_clones_under
        args.boss_clones_castle = boss_clones_castle
        args.cross_world = cross_world

        mode_str = "ALL-ENEMIES chaos" if all_enemies else f"items={items}, enemies={enemies}"
        log(f"Running randomizer (seed={seed}, worlds={worlds_arg}, {mode_str})...")
        cmd_randomize(args)

        log(f"Patching disc image...")
        def _progress(name, n, total):
            log(f"  [{n}/{total}] {name}")
        result = write_patched_assets(target_path, output_dir, file_map, progress=_progress)
        log(f"Done. Wrote {result['written']} of {result['total']} files into the disc.")
        if result["skipped"]:
            log(f"  Skipped (no patched version available): {len(result['skipped'])}")
        if result.get("grown"):
            log(f"  Files grown into slack: {len(result['grown'])}")
            for name, old_len, new_len in result["grown"]:
                log(f"    {name}: {old_len:,} -> {new_len:,} bytes "
                    f"(+{new_len - old_len}b)")
        if result.get("relocated"):
            log(f"  Files relocated (grew beyond sector slot): {len(result['relocated'])}")
            for name, old_lba, new_lba, new_len in result["relocated"]:
                log(f"    {name}: LBA 0x{old_lba:X} -> 0x{new_lba:X} "
                    f"({new_len:,} bytes)")
        return {
            "seed": seed,
            "iso": str(target_path),
            "input": str(iso_path),
            "output_iso": str(output_iso) if output_iso else None,
            "backup": str(backup_path) if backup_path else None,
            "worlds": worlds_arg,
            **result,
        }
    finally:
        if cleanup_workdir:
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass


def _prepare_output_copy(
    input_path: Path,
    source_bin: Path,
    output_path: Path,
    is_cue: bool,
    log,
) -> Path:
    """Copy the source disc image to a new output location and return the
    path to the binary file the patcher should operate on.

    Three forms of `output_path`:
      1. A directory  -> copy source files into that dir, keeping the names.
      2. A .cue path  -> copy the BIN beside it (auto-named) and rewrite the CUE.
      3. A .bin/.iso  -> copy the source binary directly to that path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # CASE 1: the user pointed at an existing directory (or one with no suffix).
    if output_path.is_dir() or (not output_path.suffix and not output_path.exists()):
        output_path.mkdir(parents=True, exist_ok=True)
        if is_cue:
            # Copy the .cue and the .bin both, preserving the original names so
            # the CUE's FILE reference still resolves.
            cue_dst = output_path / input_path.name
            bin_dst = output_path / source_bin.name
            log(f"Copying source disc image to: {output_path}")
            log(f"  {input_path.name} -> {cue_dst.name}")
            log(f"  {source_bin.name} -> {bin_dst.name}")
            shutil.copy2(input_path, cue_dst)
            shutil.copy2(source_bin, bin_dst)
            return bin_dst
        else:
            target = output_path / source_bin.name
            log(f"Copying source disc image to: {target}")
            shutil.copy2(source_bin, target)
            return target

    # CASE 2: explicit .cue output path.
    if output_path.suffix.lower() == ".cue":
        if not is_cue:
            raise ValueError(
                "Output path is a .cue but input is a single binary. "
                "Provide an .iso or .bin output path instead."
            )
        bin_dst = output_path.with_suffix(source_bin.suffix)
        log(f"Copying CUE+BIN to: {output_path.parent}")
        log(f"  {input_path.name} -> {output_path.name}")
        log(f"  {source_bin.name} -> {bin_dst.name}")
        # Rewrite the CUE's FILE line to point at the new BIN basename.
        cue_text = input_path.read_text(errors="replace")
        new_cue = _rewrite_cue_file(cue_text, bin_dst.name)
        output_path.write_text(new_cue)
        shutil.copy2(source_bin, bin_dst)
        return bin_dst

    # CASE 3: explicit .iso / .bin output path.
    log(f"Copying source disc image to: {output_path}")
    shutil.copy2(source_bin, output_path)
    return output_path


def _rewrite_cue_file(cue_text: str, new_bin_name: str) -> str:
    """Replace the BIN filename inside the FILE directive of a CUE sheet."""
    import re
    # FILE "name.bin" BINARY  (with quotes) — most common form
    new_text, n = re.subn(
        r'(FILE\s+")[^"]+(")',
        rf'\g<1>{new_bin_name}\g<2>',
        cue_text, count=1, flags=re.IGNORECASE,
    )
    if n:
        return new_text
    # FILE name.bin BINARY  (no quotes)
    new_text, n = re.subn(
        r'(FILE\s+)\S+',
        rf'\g<1>{new_bin_name}',
        cue_text, count=1, flags=re.IGNORECASE,
    )
    return new_text if n else cue_text


def main():
    p = argparse.ArgumentParser(
        prog="randomizer.iso_patcher",
        description="Patch a Maximo: Ghosts to Glory disc image with a randomized seed.",
    )
    p.add_argument("iso", help="Path to the Maximo disc image (.iso/.bin/.cue)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed (default: random)")
    p.add_argument("--no-items", action="store_true", help="Skip item randomization")
    p.add_argument("--no-gen-tier", action="store_false", dest="gen_tier",
                   default=True,
                   help="Disable monster-generator class-level randomization (on by default)")
    p.add_argument("--gate-mode", dest="gate_mode", default=None,
                   choices=["isolated", "pool"],
                   help="Gate randomizer mode: 'isolated' or 'pool' (default: off)")
    p.add_argument("--damage-taken", dest="damage_taken", default="normal",
                   choices=["0.25", "0.50", "normal", "2x", "4x"],
                   help="Scale damage Maximo takes (default normal)")
    p.add_argument("--damage-dealt", dest="damage_dealt", default="normal",
                   choices=["0.25", "0.50", "normal", "2x", "4x"],
                   help="Scale melee damage Maximo deals to enemies (default normal)")
    p.add_argument("--harder-mode", action="store_true", dest="harder_mode",
                   help="Enable HARDER MODE curated difficulty profile.")
    p.add_argument("--preserve-chests", action="store_true", dest="preserve_chests",
                   help="Keep vanilla chests in place but randomize their contents.")
    p.add_argument("--preserve-iron-keys", action="store_true", dest="preserve_iron_keys",
                   help="Keep Iron Keys (Skeleton_Keys) in their vanilla positions "
                        "and don't add new ones — key count and placement stay vanilla.")
    p.add_argument("--spawn-location", action="store_true", dest="spawn_location",
                   help="Randomize Maximo's spawn position (moved to a random entity location)")
    p.add_argument("--spawn-config", dest="spawn_config_path", default=None,
                   help="Path to a spawn-rate JSON config (per-tag/per-world weights + "
                        "enable flags). Omit to use the stock spawn rates.")
    p.add_argument("--no-enemies", action="store_true", help="Skip enemy randomization")
    p.add_argument("--all-enemies", action="store_true",
                   help="CHAOS mode: turn every item/structure into an enemy "
                        "(Gold Keys preserved, enemies drop keys).")
    p.add_argument("--cross-world", action="store_true",
                   help="Inject enemy assets across worlds so ANY enemy type "
                        "can appear in ANY world. Expands BEF files with "
                        "assets from other worlds (~30-50KB growth per BEF). "
                        "Enables a universal enemy pool for all worlds.")
    p.add_argument(
        "--worlds", default="all",
        help="Comma-separated worlds: all, grave, under, swamp, ice, castle. Default: all",
    )
    p.add_argument("--duplicate-bosses", action="store_true",
                   help="EXPERIMENTAL: clone the boss in supported arenas "
                        "(Grave / GraveDigger and Swamp / BokorLaBas). May be unstable.")
    p.add_argument("--boss-clones", type=int, default=1,
                   help="Extra boss clones when --duplicate-bosses is set (default 1, "
                        "max 6; clamped per arena — GraveDigger caps at 4, BokorLaBas at 6).")
    p.add_argument("--no-dark-knight", action="store_true", dest="exclude_dark_knight",
                   help="Exclude Dark_Knight from randomizer pools (kept vanilla-only). "
                        "Dark_Knight can SOFT-LOCK a level if it lands on a progression "
                        "slot, so this is the safe option.")
    p.add_argument(
        "--output", "-o", default=None,
        help="Optional output path for the patched disc image. Can be a folder, "
             "an .iso/.bin path, or a .cue path (companion .bin is auto-named). "
             "When omitted, the source ISO is patched in place (with .backup).",
    )
    p.add_argument("--no-backup", action="store_true",
                   help="Don't create <name>.backup before in-place patching. "
                        "Has no effect when --output is used.")
    p.add_argument("--workdir", default=None,
                   help="Working directory for extracted/patched files (default: temp)")
    args = p.parse_args()

    # Parse worlds string
    if args.worlds.lower() == "all":
        worlds_set = None
    else:
        worlds_set = {w.strip().lower() for w in args.worlds.split(",") if w.strip()}

    try:
        patch_iso(
            args.iso,
            seed=args.seed,
            items=not args.no_items,
            spawn_location=args.spawn_location,
            gen_tier=args.gen_tier,
            gate_mode=args.gate_mode,
            damage_taken=args.damage_taken,
            damage_dealt=args.damage_dealt,
            harder_mode=args.harder_mode,
            preserve_chests=args.preserve_chests,
            preserve_iron_keys=args.preserve_iron_keys,
            spawn_config_path=args.spawn_config_path,
            enemies=not args.no_enemies,
            all_enemies=args.all_enemies,
            cross_world=args.cross_world,
            worlds=worlds_set,
            duplicate_bosses=args.duplicate_bosses,
            exclude_dark_knight=args.exclude_dark_knight,
            boss_clones=args.boss_clones,
            output_iso=args.output,
            backup=not args.no_backup,
            workdir=args.workdir,
        )
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
