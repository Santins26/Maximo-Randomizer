Maximo: Ghosts to Glory — Randomizer
=====================================

Quick start
-----------

1. Double-click MaximoRandomizer.exe to open the program.

2. In the "Patch ISO" tab:
   - Click Browse... next to "Maximo disc image" and pick your Maximo
     disc file (.iso, .bin, or .cue).
   - Optional: set an Output path. Leave blank to patch the source
     in place (a .backup copy is created automatically).
   - Pick which worlds to randomize (default: all five).
   - Optional: set a Seed if you want a specific run, or leave blank
     for a random seed.
   - Click "Run randomizer".

3. Boot the patched disc image in PCSX2.
   IMPORTANT: do not load a save state taken before patching.

What it does
------------

- Shuffles enemies, items, chests, smashables, decorations across the
  five worlds (Grave, Under, Swamp, Ice, Castle).
- Shuffles the level order in each HUB and renames the column labels
  (and the loading-screen titles) to "???" so the level select is blind.
- Randomizes chest contents, gold amounts, ability drops, collector
  drops, enemy tier values, and Monster_Generator parameters.
- Protected by default: gate-key chests, Gold_Key entities, the
  GraveDigger boss, and the boss room itself stay vanilla so the game
  is always beatable.

Supported disc formats
----------------------

  .iso         (2048-byte sectors, plain DVD format)
  .bin         (2352-byte raw CD sectors, with or without a .cue)
  .cue + .bin  (CUE sheet pointing at a BIN — the BIN is patched)

The format is auto-detected. For raw CD images, only the user-data
portion of each sector is overwritten — sync, header, and EDC/ECC
fields are preserved bit-for-bit. PCSX2 reads patched images normally.

Folder mode (advanced)
----------------------

If you've already extracted the level files yourself (G_INTRO.PSX,
SLUS_200.17, etc.), use the "Folder mode" tab instead — it operates
on a folder of files instead of a disc image.

Notes
-----

- The randomizer needs no Python install. The .exe contains everything.
- The seed used for a run is always printed in the log so you can
  reproduce it later.
- A backup is created automatically when patching in place. To skip
  it, uncheck "Create backup" in the Patch ISO tab.
- All output files end up next to the input disc (or at the path you
  set in Output) — the program never writes anywhere else.
