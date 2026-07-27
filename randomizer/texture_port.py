"""
Cross-world enemy texture porting for .PRT files.

Background: see research/CROSSWORLD_TEXTURE_RESEARCH.md, Part 3, for the full
reverse-engineering trail (disassembly of PTextureManager::Create, confirmed
via real assert-condition strings pulled from the debug ELF).

Scope of what's SOLVED and supported here: indexed (PSMT8 / 8-bit paletted)
texture entries -- the format essentially every creature/enemy sprite uses,
confirmed by GRAVE.PRT (35/35 entries) and the first 54 of CASTLE.PRT's 55
entries. This case round-trips byte-perfectly and has been verified with a
real end-to-end port (Grave -> Castle) confirming: pixel data byte-identical,
new CLUT slot byte-identical to the source palette, all pre-existing CLUTs
and texture-stream bytes in the target file completely untouched.

Scope of what's NOT supported: direct-color (PSMCT24/32) entries with the
TEXTURE_FLAG_OPACITY_MAP flag set. These appear to be background/environment
art (not enemy sprites, based on every sample seen so far) and still have an
unresolved sizing detail -- the pixel data for at least one observed entry
continues well past where the documented header-size + derived-opacity-size
formula says it should stop. Extraction functions here will raise
PRTPortError with a clear message if asked to cross such an entry, rather
than silently producing a corrupt file.

Key design insight that simplifies injection a lot: appending a new texture
to a WORLD's PRT does NOT require re-parsing every existing entry in that
file. The texture stream is a flat, back-to-back sequence with no directory,
so "append at the end" only needs:
  - the current file length (trivial)
  - the header's count_a / count_b fields (trivial, first 16 bytes)
Extraction (reading a specific entry OUT of a SOURCE world) is the part that
needs the sequential walk, since we don't know an entry's byte offset without
walking every entry before it.

Growing the file is safe (see research doc Part 3): the format is a flat
sequential stream with no absolute-offset directory read by
PTextureManager::Create (only sequential file->Read() calls -- no seeks).
The only other place that needs to know about the new size is the ISO 9660
directory record, which randomizer/iso_patcher.py's existing grow/relocate
machinery (write_patched_assets) already handles generically for any file
placed in the patched-output folder -- .PRT needs to be added to
ASSET_EXTENSIONS there (see bottom of this file's docstring / iso_patcher.py).
"""
from __future__ import annotations
import struct
from dataclasses import dataclass

MAGIC = 0x04030201
CLUT_TABLE_SIZE = 175
CLUT_ENTRY_SIZE = 1024  # 256 colors x RGBA
HEADER_SIZE = 16
FMT_BPP = {0x00: 4, 0x01: 3, 0x13: 1, 0x14: 1}
FMT_PSMT8 = 0x13
FLAG_OPACITY_MAP = 0x0002
RESYNC_WINDOW = 0x20000  # bounded forward scan, for benign padding gaps only


class PRTPortError(Exception):
    pass


@dataclass
class PortableTexture:
    """Everything needed to inject a texture into another world's PRT."""
    header_bytes: bytes    # 16-byte TEXTURE_HEADER, verbatim from the source
    pixel_data: bytes       # raw pixel bytes (nSourceImageSize long)
    clut_bytes: bytes | None  # 1024-byte palette, if this is a PSMT8 (indexed) texture
    source_world: str
    source_index: int

    @property
    def is_indexed(self) -> bool:
        return self.clut_bytes is not None


def _read_header_fields(data: bytes, off: int) -> dict | None:
    if off + HEADER_SIZE > len(data):
        return None
    log2_w, log2_h = struct.unpack_from('<HH', data, off)
    if not (0 < log2_w <= 10 and 0 < log2_h <= 10):
        return None
    fmt = data[off + 4]
    if fmt not in FMT_BPP:
        return None
    size = struct.unpack_from('<I', data, off + 8)[0]
    if size != (1 << log2_w) * (1 << log2_h) * FMT_BPP[fmt]:
        return None
    flags = struct.unpack_from('<H', data, off + 6)[0]
    if flags & ~FLAG_OPACITY_MAP:
        return None
    clut_index = struct.unpack_from('<h', data, off + 14)[0]
    if fmt == FMT_PSMT8 and not (0 <= clut_index < CLUT_TABLE_SIZE):
        return None
    return dict(log2_w=log2_w, log2_h=log2_h, fmt=fmt, flags=flags,
                size=size, clut_index=clut_index)


def extract_texture(prt_path: str, index: int, world_name: str = "") -> PortableTexture:
    """Walk a world's PRT sequentially and pull out texture entry `index`
    (0-based, in file order) as a PortableTexture ready to inject elsewhere.

    Raises PRTPortError if entry `index` can't be reached -- either because
    the file ran out before it, or because an unsupported (opacity-map,
    non-indexed) entry sits in the way and its true length can't be
    determined yet (see module docstring).
    """
    with open(prt_path, 'rb') as f:
        data = f.read()
    magic = struct.unpack_from('<I', data, 0)[0]
    if magic != MAGIC:
        raise PRTPortError(f"{prt_path}: bad magic 0x{magic:08X}")
    count_a = struct.unpack_from('<I', data, 0x08)[0]
    off = 0x20 + count_a * CLUT_ENTRY_SIZE
    clut_pool = data[0x20:off]

    next_clut_expected = 0
    for i in range(index + 1):
        fields = _read_header_fields(data, off)
        if fields is None:
            found = None
            for cand in range(off, min(off + RESYNC_WINDOW, len(data) - HEADER_SIZE)):
                f2 = _read_header_fields(data, cand)
                if f2 is not None:
                    found = cand
                    fields = f2
                    break
            if fields is None:
                raise PRTPortError(
                    f"{prt_path}: couldn't locate entry {index} -- lost track "
                    f"at entry {i} (offset 0x{off:X}). Likely hit an "
                    f"unsupported opacity-map/non-indexed entry; see "
                    f"research/CROSSWORLD_TEXTURE_RESEARCH.md Part 3."
                )
            off = found

        is_indexed = fields['fmt'] == FMT_PSMT8
        if is_indexed and fields['clut_index'] != next_clut_expected:
            raise PRTPortError(
                f"{prt_path}: entry {i} CLUT index {fields['clut_index']} != "
                f"expected {next_clut_expected} -- parse likely desynced."
            )
        if fields['flags'] & FLAG_OPACITY_MAP and not is_indexed:
            raise PRTPortError(
                f"{prt_path}: entry {i} has TEXTURE_FLAG_OPACITY_MAP set on a "
                f"non-indexed ({fields['fmt']=}) texture -- this is the known "
                f"unsolved case (see research doc). Cannot safely determine "
                f"where entry {i+1} starts. Porting is only supported for "
                f"indexed (PSMT8) enemy-sprite-style textures right now."
            )

        header_bytes = bytes(data[off:off + HEADER_SIZE])
        body_start = off + HEADER_SIZE
        pixel_data = data[body_start: body_start + fields['size']]
        after = body_start + fields['size']
        # (opacity block would go here for non-indexed textures; unsupported, see above)

        if i == index:
            clut_bytes = None
            if is_indexed:
                clut_start = fields['clut_index'] * CLUT_ENTRY_SIZE
                clut_bytes = clut_pool[clut_start:clut_start + CLUT_ENTRY_SIZE]
                if len(clut_bytes) != CLUT_ENTRY_SIZE:
                    raise PRTPortError(
                        f"{prt_path}: entry {i} CLUT index {fields['clut_index']} "
                        f"out of range for this file's {count_a} CLUTs."
                    )
            return PortableTexture(
                header_bytes=header_bytes, pixel_data=pixel_data,
                clut_bytes=clut_bytes, source_world=world_name, source_index=index,
            )

        if is_indexed:
            next_clut_expected = fields['clut_index'] + 1
        off = after

    raise PRTPortError(f"{prt_path}: file ended before reaching entry {index}")


def inject_texture(target_prt_bytes: bytes, tex: PortableTexture) -> bytes:
    """Append `tex` to a target world's PRT bytes, returning the new (grown)
    file. Growing the file is safe here (see research doc): the format is a
    flat sequential stream with no offset directory, and this function only
    ever appends -- it never touches any existing entry's bytes.

    If `tex` is indexed, its palette is appended as a NEW CLUT slot (this
    file's count_a is incremented) and the injected entry's header is patched
    to point at that new slot -- it does NOT try to reuse/guess an existing
    slot in the target world, to avoid silently corrupting an unrelated
    texture that already uses that slot.

    Raises PRTPortError if the target's CLUT_TABLE_SIZE (175) would be
    exceeded by adding this palette.
    """
    magic = struct.unpack_from('<I', target_prt_bytes, 0)[0]
    if magic != MAGIC:
        raise PRTPortError(f"target: bad magic 0x{magic:08X}")
    count_a, count_b = struct.unpack_from('<II', target_prt_bytes, 0x08)
    clut_pool_start = 0x20
    clut_pool_end = clut_pool_start + count_a * CLUT_ENTRY_SIZE
    clut_pool = bytearray(target_prt_bytes[clut_pool_start:clut_pool_end])
    stream = bytearray(target_prt_bytes[clut_pool_end:])

    header = bytearray(tex.header_bytes)
    new_count_a = count_a
    if tex.is_indexed:
        if count_a >= CLUT_TABLE_SIZE:
            raise PRTPortError(
                f"target world's CLUT table is full ({count_a}/{CLUT_TABLE_SIZE}); "
                f"cannot add another palette. Consider re-using an existing "
                f"slot with a visually close palette instead, or converting "
                f"the source texture to direct-color (loses palette sharing "
                f"but sidesteps the cap)."
            )
        new_clut_index = count_a
        clut_pool += tex.clut_bytes
        struct.pack_into('<h', header, 14, new_clut_index)
        new_count_a = count_a + 1

    stream += header
    stream += tex.pixel_data

    out = bytearray(target_prt_bytes[0:0x20])
    struct.pack_into('<I', out, 0x08, new_count_a)
    struct.pack_into('<I', out, 0x0C, count_b + 1)
    out += clut_pool
    out += stream
    return bytes(out)


def port_texture(source_prt_path: str, source_index: int, source_world: str,
                  target_prt_path: str) -> bytes:
    """Convenience wrapper: extract entry `source_index` from
    `source_prt_path` and return the bytes of `target_prt_path` with it
    appended. Caller is responsible for writing the result to disk (typically
    into the randomizer's patched-output folder so iso_patcher.py's existing
    growth machinery picks it up)."""
    tex = extract_texture(source_prt_path, source_index, source_world)
    with open(target_prt_path, 'rb') as f:
        target_bytes = f.read()
    return inject_texture(target_bytes, tex)


def list_entries(prt_path: str, limit: int | None = None) -> list[dict]:
    """Diagnostic helper: walk a PRT and return a summary of each entry
    (index, dims, format, clut index) up to the first unsupported one, or
    `limit` entries, whichever comes first. Useful for figuring out which
    entry index corresponds to a given enemy (see research doc Part 4 --
    the enemy-name -> texture-index mapping isn't automated yet)."""
    with open(prt_path, 'rb') as f:
        data = f.read()
    count_a, count_b = struct.unpack_from('<II', data, 0x08)
    off = 0x20 + count_a * CLUT_ENTRY_SIZE
    out = []
    next_clut_expected = 0
    i = 0
    cap = count_b if limit is None else min(count_b, limit)
    while i < cap:
        fields = _read_header_fields(data, off)
        if fields is None:
            found = None
            for cand in range(off, min(off + RESYNC_WINDOW, len(data) - HEADER_SIZE)):
                f2 = _read_header_fields(data, cand)
                if f2 is not None:
                    found = cand
                    fields = f2
                    break
            if fields is None:
                out.append({"index": i, "error": f"lost track at offset 0x{off:X}"})
                break
            off = found
        is_indexed = fields['fmt'] == FMT_PSMT8
        out.append({
            "index": i, "offset": off,
            "width": 1 << fields['log2_w'], "height": 1 << fields['log2_h'],
            "format": fields['fmt'], "flags": fields['flags'],
            "size": fields['size'], "clut_index": fields['clut_index'] if is_indexed else None,
        })
        if fields['flags'] & FLAG_OPACITY_MAP and not is_indexed:
            out.append({"index": i + 1, "error": "unsupported opacity/non-indexed entry, stopping walk"})
            break
        if is_indexed:
            next_clut_expected = fields['clut_index'] + 1
        off += HEADER_SIZE + fields['size']
        i += 1
    return out
