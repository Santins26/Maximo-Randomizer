"""
ISO 9660 reader and in-place patcher for the Maximo: Ghosts to Glory PS2 disc.

Supported formats:
  - .iso (2048-byte sectors, plain user data) — typical PS2 DVD format
  - .bin + .cue (2352-byte raw sectors, includes sync + header + EDC/ECC)
                — typical PS1/CD-format dumps
  - .bin (no .cue)        — sector format auto-detected from sync pattern

In-place patching strategy:
  Our randomizer pads every output file to its original size, so we seek to
  the file's LBA inside the disc image and overwrite the user-data portion
  of each sector. For 2352-byte BIN sectors, we write only the 2048-byte
  user-data window; the surrounding sync/header bytes are left untouched.

  We do NOT recalculate EDC/ECC after writing. PCSX2 ignores those fields by
  default. If a strict tool flags the image, run it through any standard
  CD-image fixer (cdrecord/cdmage/etc).

ISO 9660 layout reference:
  Sector 16:   Primary Volume Descriptor (PVD)
  PVD+156:     Root Directory Record (34 bytes)
    +2:  u32 LBA (LE; followed by BE duplicate)
    +10: u32 length (LE; followed by BE duplicate)
  Each Directory Record:
    +0:  u8  record length
    +2:  u32 LBA (LE)
    +10: u32 data length (LE)
    +25: u8  flags (bit 1 = directory)
    +32: u8  name length
    +33: name bytes (;1 version suffix optional)
"""
from __future__ import annotations
import os
import re
import struct
from pathlib import Path
from typing import Iterator

# Standard sector sizes
USER_DATA_SIZE = 2048               # ISO 9660 user-data per sector
RAW_SECTOR_SIZE = 2352              # Mode 1 / Mode 2 raw CD sector
MODE1_USER_OFFSET = 16              # 12 sync + 4 header before user data
MODE2_FORM1_USER_OFFSET = 24        # 12 sync + 4 header + 8 subheader

SYNC_PATTERN = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"  # 12 bytes


def _to_bcd(n: int) -> int:
    """Convert an integer (0-99) to BCD byte for raw CD sector headers."""
    return ((n // 10) << 4) | (n % 10)


def parse_cue(cue_path: Path) -> Path:
    """Parse a CUE sheet and return the absolute path to the referenced BIN.

    Only handles the simple `FILE "name.bin" BINARY` form, which covers the
    vast majority of single-track PS2 dumps.
    """
    cue_path = Path(cue_path)
    text = cue_path.read_text(errors="replace")
    m = re.search(r'FILE\s+"([^"]+)"\s+BINARY', text, re.IGNORECASE)
    if m:
        bin_name = m.group(1)
    else:
        # Fallback: FILE name.bin BINARY (no quotes)
        m = re.search(r"FILE\s+(\S+)\s+BINARY", text, re.IGNORECASE)
        if not m:
            raise ValueError(f"{cue_path}: no FILE entry found in CUE sheet.")
        bin_name = m.group(1)
    bin_path = cue_path.parent / bin_name
    if not bin_path.exists():
        raise FileNotFoundError(
            f"CUE references {bin_name!r}, but {bin_path} does not exist."
        )
    return bin_path


class IsoFile:
    """Read/write disc image handle that supports both ISO (2048) and BIN
    (2352 raw) sector formats. The sector format is auto-detected.
    """

    def __init__(self, path: Path | str, writable: bool = False):
        self.path = Path(path)
        # Resolve .cue to its referenced .bin
        if self.path.suffix.lower() == ".cue":
            self.path = parse_cue(self.path)

        mode = "r+b" if writable else "rb"
        self.fh = open(self.path, mode)
        self._writable = writable

        # Detect sector format
        self._sector_size, self._user_offset = self._detect_format()

        # Parse PVD to find root directory
        pvd = self._read_user_sector(16)
        if pvd[1:6] != b"CD001":
            raise ValueError(
                f"{self.path}: not a valid ISO 9660 image (no CD001 magic). "
                f"Detected sector_size={self._sector_size}, "
                f"user_offset={self._user_offset}"
            )
        self._root_lba = struct.unpack_from("<I", pvd, 158)[0]
        self._root_length = struct.unpack_from("<I", pvd, 166)[0]

    # -------------------------------------------------- format detection
    def _detect_format(self) -> tuple[int, int]:
        """Determine (sector_size, user_offset) by inspecting the first sector.

        Returns:
          (2048, 0)   for plain ISO / DVD-format BIN
          (2352, 16)  for Mode 1 raw CD BIN
          (2352, 24)  for Mode 2 Form 1 raw CD BIN
        """
        suffix = self.path.suffix.lower()
        # If it's an .iso it's almost certainly 2048/0 — but verify CD001
        # at the right offset to be sure.
        self.fh.seek(0)
        first = self.fh.read(2400)
        if len(first) < 2400:
            raise ValueError(f"{self.path}: file too small ({len(first)} bytes)")

        # Probe: does sector 16 contain "CD001" assuming 2048-byte sectors?
        # That is, at file offset 16 * 2048 = 0x8000.
        if self._probe_cd001(2048, 0):
            return 2048, 0
        # Probe: 2352-byte raw with Mode 1 header (user data at +16)
        if self._probe_cd001(2352, 16):
            return 2352, 16
        # Probe: 2352-byte raw with Mode 2 Form 1 header (user data at +24)
        if self._probe_cd001(2352, 24):
            return 2352, 24

        # Header byte heuristic: at offset 15 in a 2352 sector, byte = mode
        first_sector_mode = first[15] if len(first) > 15 else 0
        raise ValueError(
            f"{self.path}: cannot detect sector format. "
            f"First-sector mode byte = 0x{first_sector_mode:02X}. "
            f"Supported formats are .iso (2048 b/sector) and .bin/.cue "
            f"(2352 b/sector Mode 1 or Mode 2 Form 1)."
        )

    def _probe_cd001(self, sector_size: int, user_offset: int) -> bool:
        """True if sector 16 user data starts with CD001 magic for the given format."""
        off = 16 * sector_size + user_offset + 1  # +1 for the type byte before CD001
        self.fh.seek(off)
        return self.fh.read(5) == b"CD001"

    # -------------------------------------------------- low-level access
    @property
    def sector_size(self) -> int:
        return self._sector_size

    @property
    def is_raw_cd(self) -> bool:
        return self._sector_size == RAW_SECTOR_SIZE

    def close(self) -> None:
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _user_sector_offset(self, lba: int) -> int:
        """File-byte offset of the USER DATA portion of sector `lba`."""
        return lba * self._sector_size + self._user_offset

    def _read_user_sector(self, lba: int) -> bytes:
        """Read the 2048-byte user-data portion of sector `lba`."""
        self.fh.seek(self._user_sector_offset(lba))
        return self.fh.read(USER_DATA_SIZE)

    def read_file(self, lba: int, length: int) -> bytes:
        """Read `length` bytes of user data starting at sector `lba`.

        For raw-CD (2352-byte) images, this transparently skips the sync/header
        and EDC/ECC bytes and returns only the file's user data.
        """
        if self._sector_size == USER_DATA_SIZE:
            self.fh.seek(lba * USER_DATA_SIZE)
            return self.fh.read(length)
        # Raw CD: read sector by sector, extracting only user data each time.
        out = bytearray()
        remaining = length
        cur_lba = lba
        while remaining > 0:
            data = self._read_user_sector(cur_lba)
            take = min(remaining, USER_DATA_SIZE)
            out.extend(data[:take])
            remaining -= take
            cur_lba += 1
        return bytes(out)

    def write_file(self, lba: int, data: bytes, original_length: int) -> None:
        """Overwrite the user-data portion of sectors starting at `lba`.

        For raw-CD images, only the user-data window of each sector is touched;
        sync/header/EDC/ECC bytes are preserved bit-for-bit. PCSX2 doesn't
        verify EDC/ECC, so this works without recalculation.

        Patched data may be larger than `original_length` provided it still
        fits within the on-disc slot (`original_length` rounded up to the
        next sector boundary). Files within their original length are
        written in place; oversize-but-within-slot writes need the caller
        to also call `update_directory_entry_length` so ISO 9660 reports
        the new length.
        """
        if not self._writable:
            raise RuntimeError("Disc image opened read-only.")

        # Slack: we may safely write up to the next sector boundary because
        # the file slot is already padded.
        slack = USER_DATA_SIZE - (original_length % USER_DATA_SIZE)
        if slack == USER_DATA_SIZE:
            slack = 0
        max_writable = original_length + slack
        if len(data) > max_writable:
            raise ValueError(
                f"Patched file too large: {len(data)} > {max_writable} "
                f"(original_length={original_length}, slack={slack}). "
                f"Cannot in-place patch without rebuilding the image. "
                f"Use write_file_relocated() for files that need to grow."
            )

        # Pad data so it always covers full sectors (zero-padded for the tail).
        padded = data + b"\x00" * (max_writable - len(data))

        # Effective write window: at least `original_length`, expanded into
        # the slack sector when the patched data grew. `actual_len` is the
        # caller's payload size; `write_total` is the on-disc footprint we
        # actually clear (rounded up to a full sector).
        actual_len = len(data)
        if actual_len > original_length:
            # Grew into slack; we must write the FULL slot's-worth of data
            # so the trailing bytes of the original (now overwritten) data
            # don't linger in the new region.
            write_total = max_writable
        else:
            write_total = original_length

        if self._sector_size == USER_DATA_SIZE:
            # Plain ISO: contiguous write
            self.fh.seek(lba * USER_DATA_SIZE)
            self.fh.write(padded[:write_total])
            return

        # Raw CD: write sector by sector into the user-data window.
        cur_lba = lba
        offset = 0
        while offset < write_total:
            chunk = padded[offset:offset + USER_DATA_SIZE]
            if len(chunk) < USER_DATA_SIZE:
                chunk = chunk + b"\x00" * (USER_DATA_SIZE - len(chunk))
            self.fh.seek(self._user_sector_offset(cur_lba))
            self.fh.write(chunk)
            offset += USER_DATA_SIZE
            cur_lba += 1

    def file_parents(self) -> dict[int, tuple[int, int]]:
        """Walk the entire ISO directory tree and return a map from
        file_lba -> (parent_dir_lba, parent_dir_length) for every file
        encountered.

        Used by callers that need to update a file's directory record
        (e.g. growing a BEF beyond its original length so the engine
        actually reads the appended bytes).
        """
        parents: dict[int, tuple[int, int]] = {}
        seen_dirs: set[int] = set()

        def walk(dir_lba: int, dir_length: int) -> None:
            if dir_lba in seen_dirs:
                return
            seen_dirs.add(dir_lba)
            for name, lba, length, is_dir in self.list_directory(dir_lba, dir_length):
                if is_dir:
                    walk(lba, length)
                else:
                    parents[lba] = (dir_lba, dir_length)

        walk(self._root_lba, self._root_length)
        return parents

    # -------------------------------------------------- directory walking
    def list_directory(self, lba: int, length: int) -> Iterator[tuple]:
        """Yield (name, file_lba, file_length, is_dir) for entries in a
        directory at the given LBA + length.
        """
        sectors_needed = (length + USER_DATA_SIZE - 1) // USER_DATA_SIZE
        data = b""
        for k in range(sectors_needed):
            data += self._read_user_sector(lba + k)
        offset = 0
        end = length
        while offset < end:
            rec_len = data[offset]
            if rec_len == 0:
                # Padding to next sector boundary
                next_sector = ((offset // USER_DATA_SIZE) + 1) * USER_DATA_SIZE
                offset = next_sector
                continue
            file_lba = struct.unpack_from("<I", data, offset + 2)[0]
            file_length = struct.unpack_from("<I", data, offset + 10)[0]
            flags = data[offset + 25]
            is_dir = bool(flags & 0x02)
            name_len = data[offset + 32]
            name_bytes = data[offset + 33: offset + 33 + name_len]
            if b";" in name_bytes:
                name_bytes = name_bytes[: name_bytes.index(b";")]
            try:
                name = name_bytes.decode("ascii")
            except UnicodeDecodeError:
                name = name_bytes.decode("latin-1", errors="replace")
            if name in ("\x00", "\x01"):
                offset += rec_len
                continue
            yield name, file_lba, file_length, is_dir
            offset += rec_len

    def update_directory_entry_length(
        self, parent_dir_lba: int, parent_dir_length: int,
        target_file_lba: int, new_length: int,
    ) -> bool:
        """Update the data-length field of one directory record, in place.

        Walks the directory at `parent_dir_lba` looking for the entry whose
        file_lba matches `target_file_lba`. When found, overwrites the LE
        and BE u32 length fields (offsets +10 and +14 inside the record)
        with `new_length`. Returns True on success.

        We match by LBA rather than by name because LBA is unique per file
        on the disc, while the same name could appear in multiple
        directories.
        """
        if not self._writable:
            raise RuntimeError("Disc image opened read-only.")
        sectors_needed = (parent_dir_length + USER_DATA_SIZE - 1) // USER_DATA_SIZE
        for k in range(sectors_needed):
            sector_lba = parent_dir_lba + k
            data = bytearray(self._read_user_sector(sector_lba))
            offset = 0
            modified = False
            while offset < len(data):
                rec_len = data[offset]
                if rec_len == 0:
                    break  # end-of-records padding
                file_lba = struct.unpack_from("<I", data, offset + 2)[0]
                if file_lba == target_file_lba:
                    # Patch LE and BE length fields
                    struct.pack_into("<I", data, offset + 10, new_length)
                    struct.pack_into(">I", data, offset + 14, new_length)
                    modified = True
                    break
                offset += rec_len
            if modified:
                # Write the patched sector back
                self.fh.seek(self._user_sector_offset(sector_lba))
                self.fh.write(bytes(data))
                return True
        return False

    def update_directory_entry_lba_and_length(
        self, parent_dir_lba: int, parent_dir_length: int,
        target_file_lba: int, new_lba: int, new_length: int,
    ) -> bool:
        """Update both the LBA and data-length of a directory record in place.

        Used when a file is relocated to the end of the disc because it grew
        beyond its original sector slot. The directory entry must point to the
        new LBA so the engine finds the file at its new location.
        """
        if not self._writable:
            raise RuntimeError("Disc image opened read-only.")
        sectors_needed = (parent_dir_length + USER_DATA_SIZE - 1) // USER_DATA_SIZE
        for k in range(sectors_needed):
            sector_lba = parent_dir_lba + k
            data = bytearray(self._read_user_sector(sector_lba))
            offset = 0
            modified = False
            while offset < len(data):
                rec_len = data[offset]
                if rec_len == 0:
                    break
                file_lba = struct.unpack_from("<I", data, offset + 2)[0]
                if file_lba == target_file_lba:
                    # Patch LBA (LE at +2, BE at +6)
                    struct.pack_into("<I", data, offset + 2, new_lba)
                    struct.pack_into(">I", data, offset + 6, new_lba)
                    # Patch length (LE at +10, BE at +14)
                    struct.pack_into("<I", data, offset + 10, new_length)
                    struct.pack_into(">I", data, offset + 14, new_length)
                    modified = True
                    break
                offset += rec_len
            if modified:
                self.fh.seek(self._user_sector_offset(sector_lba))
                self.fh.write(bytes(data))
                return True
        return False

    def append_file(self, data: bytes) -> int:
        """Append data to the end of the disc image. Returns the LBA where
        the data was written.

        Used for files that grew beyond their original sector allocation.
        The file is written at the end of the image, and the caller must
        update the directory entry to point to the new LBA.
        """
        if not self._writable:
            raise RuntimeError("Disc image opened read-only.")

        # Find the current end of the disc image
        self.fh.seek(0, 2)  # seek to end
        current_size = self.fh.tell()

        if self._sector_size == USER_DATA_SIZE:
            # Plain ISO: just append at the next sector
            padding = (-current_size) % USER_DATA_SIZE
            if padding:
                self.fh.write(b'\x00' * padding)
            new_lba = (current_size + padding) // USER_DATA_SIZE
            # Pad data to full sectors
            padded = data + b'\x00' * ((-len(data)) % USER_DATA_SIZE)
            self.fh.write(padded)
            return new_lba
        else:
            # Raw CD (2352-byte sectors): read an existing sector to use as
            # a framing template, then write new sectors with that framing
            # but with our user data. We copy the sync pattern and mode byte
            # from sector 16 (PVD — guaranteed to exist and be Mode 1).
            template_sector = bytearray(self._sector_size)
            self.fh.seek(16 * self._sector_size)
            template_sector = bytearray(self.fh.read(self._sector_size))

            # Align to sector boundary
            self.fh.seek(0, 2)
            current_size = self.fh.tell()
            padding = (-current_size) % self._sector_size
            if padding:
                self.fh.write(b'\x00' * padding)
                current_size += padding
            new_lba = current_size // self._sector_size

            # Write data sector by sector
            offset = 0
            cur_lba = new_lba
            while offset < len(data):
                chunk = data[offset:offset + USER_DATA_SIZE]
                if len(chunk) < USER_DATA_SIZE:
                    chunk = chunk + b'\x00' * (USER_DATA_SIZE - len(chunk))

                # Build sector: copy template framing, replace user data
                sector = bytearray(template_sector)
                # Update MSF in header (bytes 12-14 for Mode 1)
                lba_for_msf = cur_lba + 150  # 2-second pregap
                minute = lba_for_msf // (60 * 75)
                second = (lba_for_msf // 75) % 60
                frame = lba_for_msf % 75
                sector[12] = _to_bcd(minute)
                sector[13] = _to_bcd(second)
                sector[14] = _to_bcd(frame)
                # Mode byte stays as-is from template (Mode 1)

                # Write user data at the correct offset
                sector[self._user_offset:self._user_offset + USER_DATA_SIZE] = chunk

                # Zero out EDC/ECC fields (PCSX2 doesn't verify them)
                # For Mode 1: EDC at 2064, ECC at 2076
                if self._user_offset == 16:  # Mode 1
                    sector[2064:2076] = b'\x00' * 12  # EDC + zero
                    sector[2076:2352] = b'\x00' * 276  # ECC

                self.fh.write(bytes(sector))
                offset += USER_DATA_SIZE
                cur_lba += 1

            return new_lba

    def find(self, *parts: str) -> tuple[int, int] | None:
        """Find a file or directory by path. Returns (lba, length) or None.
        Path matching is case-insensitive (ISO 9660 stores names uppercase).
        """
        cur_lba, cur_len = self._root_lba, self._root_length
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            found = None
            for name, lba, length, is_dir in self.list_directory(cur_lba, cur_len):
                if name.upper() == part.upper():
                    found = (lba, length, is_dir)
                    break
            if found is None:
                return None
            if is_last:
                return found[0], found[1]
            if not found[2]:
                return None  # Path continues but this is a file
            cur_lba, cur_len = found[0], found[1]
        return None

    def list_directory_path(self, *parts: str) -> list[tuple] | None:
        """Resolve a directory path and return its entries, or None if missing."""
        if not parts:
            return list(self.list_directory(self._root_lba, self._root_length))
        loc = self.find(*parts)
        if loc is None:
            return None
        try:
            return list(self.list_directory(loc[0], loc[1]))
        except Exception:
            return None


# Maximo retail ISO layout (verified by UltraISO inspection of SLUS-20017):
#   /SLUS_200.17                                (root - boot executable)
#   /SYSTEM.CNF                                 (root)
#   /PSXDATA/CASTLE/    C_*.PSX, CASTLE.BEF, CASTLE_K.BEF, CASTLE_Q.BEF
#   /PSXDATA/GRAVE/     G_*.PSX, GRAVE.BEF, GRAVE_B.BEF, GRAVE.PRS, GRAVE.PRT
#   /PSXDATA/ICESHIP/   I_*.PSX, ICE.BEF, ICE_B.BEF
#   /PSXDATA/SWAMP/     S_*.PSX, SWAMP.BEF, SWAMP_B.BEF
#   /PSXDATA/UNDER/     U_*.PSX, UNDER.BEF, UNDER_B.BEF, UNDER.PRS, UNDER.PRT
#
# Note: the BEF reference inside PSX files points to "CDS/Runtime/Maximo/
# Materials/ResourceFiles/<...>.bef" — that's the dev path used at build
# time. The retail engine maps it back to /PSXDATA/<WORLD>/.

PSXDATA_DIR = "PSXDATA"
WORLD_FOLDER_NAMES = ("GRAVE", "UNDER", "SWAMP", "ICESHIP", "CASTLE")
SLUS_FILENAME = "SLUS_200.17"
# Boot-executable filenames per region (root of the disc). The randomizer
# detects whichever one is present. US: SLUS_200.17  JP: SLPM_621.27
EXECUTABLE_FILENAMES = ("SLUS_200.17", "SLPM_621.27")


def find_maximo_files(iso: IsoFile) -> dict[str, tuple[int, int]]:
    """Locate all Maximo asset files inside the disc image.

    Walks /PSXDATA/<WORLD>/ for each world and the root for the SLUS exe.
    Returns a dict mapping uppercase filename → (lba, length).

    Collects PSX (level instances), BEF (entity scripts), PRS (resource pool),
    and PRT (resource pool table) files. The randomizer modifies PSX + SLUS;
    PRS/PRT/BEF are extracted as well so template lookups have everything.
    """
    out: dict[str, tuple[int, int]] = {}

    # Boot executable at root (region-aware: SLUS_200.17 / SLPM_621.27 / ...)
    exe_names = {n.upper() for n in EXECUTABLE_FILENAMES}
    for name, lba, length, is_dir in iso.list_directory(iso._root_lba, iso._root_length):
        if name.upper() in exe_names:
            out[name.upper()] = (lba, length)
            break

    # Walk each world folder under /PSXDATA/
    found_worlds = []
    for world in WORLD_FOLDER_NAMES:
        entries = iso.list_directory_path(PSXDATA_DIR, world)
        if entries is None:
            continue
        found_worlds.append(world)
        for name, lba, length, is_dir in entries:
            if is_dir:
                continue
            upper = name.upper()
            if any(upper.endswith(ext) for ext in (".PSX", ".BEF", ".PRS", ".PRT", ".TEX")):
                if upper in out:
                    continue
                out[upper] = (lba, length)

    if not found_worlds:
        raise RuntimeError(
            f"Disc image does not contain /{PSXDATA_DIR}/<world>/ folders. "
            f"Looked for: {', '.join(WORLD_FOLDER_NAMES)}. "
            f"Is this the correct Maximo: Ghosts to Glory disc image?"
        )

    return out
