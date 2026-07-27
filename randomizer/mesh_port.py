"""
Cross-world mesh porting for .PRS files, built on prs_mesh_parser.py.

Extracts a full top-level mesh-table entry (a PMeshNode or PSkeleton subtree,
including every nested bone/fragment/shader/render-pass byte) from a source
world's .PRS and inserts it into a target world's .PRS, growing the file.

Safety: the mesh table is immediately followed by the Anim table with no
gap and no offset directory anywhere in the file (confirmed: PTextureManager
and PResourceManager's loaders only ever call sequential file->Read, never
Seek). Inserting new bytes right at the mesh-table/anim-table boundary and
incrementing the mesh count field is therefore safe -- everything after the
insertion point simply shifts later in the file, and nothing references it
by absolute offset.

Verified end-to-end (see research/CROSSWORLD_TEXTURE_RESEARCH.md Session 6):
ported Dark_Knight's full mesh subtree (mesh[25], 132,900 bytes) from
CASTLE.PRS into GRAVE.PRS. Confirmed via full byte-level diff:
  - Only the mesh-count field (offset 0x20) and the newly-inserted region
    changed; every other byte, before and after the insertion point, is
    provably identical to the original file (just shifted after the
    insertion point).
  - The inserted bytes exactly match the extracted source entry.
  - The patched file re-parses cleanly in full (all original entries plus
    the new one), and the new entry's texture references (33, 34) are
    intact.
  - The corresponding BEF blob's mesh-index field (offset 22, a u16) was
    repatched to point at the new index and verified round-trip correct.
"""
import struct
from .prs_mesh_parser import Reader, PRSParseError, load_mesh_node, load_skeleton, _resync_toplevel_tag


def find_entry_bounds(data, index):
    """Return (start_offset, end_offset, tag) for mesh-table entry `index`,
    where start_offset is the position of that entry's own leading tag."""
    r = Reader(data, 0x20)
    count = r.u32()
    if index >= count:
        raise ValueError(f"entry {index} out of range (table has {count} entries)")
    for i in range(count):
        entry_start = r.off
        tag = r.u32()
        path = f"mesh[{i}]@0x{entry_start:X}"
        try:
            if tag == 1:
                load_mesh_node(r, path)
            elif tag == 6:
                load_skeleton(r, path)
            else:
                raise PRSParseError(f"unknown tag {tag}")
        except PRSParseError:
            r.off = _resync_toplevel_tag(r, path)
        if i == index:
            return entry_start, r.off, tag
    raise ValueError(f"never reached entry {index}")


def extract_mesh_entry(source_path, index):
    """Return the raw bytes of mesh-table entry `index` (including its
    leading tag) from a source world's .PRS file."""
    data = open(source_path, 'rb').read()
    start, end, tag = find_entry_bounds(data, index)
    return data[start:end], tag


def insert_mesh_entry(target_data, entry_bytes):
    """Insert `entry_bytes` (as returned by extract_mesh_entry) into a
    target world's .PRS bytes, right at the end of its mesh table (== the
    start of its Anim table). Increments the mesh count at file offset
    0x20. Returns (new_data, new_index) where new_index is the position
    the ported entry now occupies in the target's mesh table.
    """
    count = struct.unpack_from('<I', target_data, 0x20)[0]
    # Find where the target's own mesh table ends the same way we found
    # the source's entry bounds -- walk every existing entry.
    r = Reader(target_data, 0x20)
    r.u32()  # count, already have it
    for i in range(count):
        entry_start = r.off
        tag = r.u32()
        path = f"mesh[{i}]@0x{entry_start:X}"
        try:
            if tag == 1:
                load_mesh_node(r, path)
            elif tag == 6:
                load_skeleton(r, path)
            else:
                raise PRSParseError(f"unknown tag {tag}")
        except PRSParseError:
            r.off = _resync_toplevel_tag(r, path)
    insert_at = r.off  # end of mesh table == start of Anim table

    new_data = target_data[:insert_at] + entry_bytes + target_data[insert_at:]
    new_data = bytearray(new_data)
    struct.pack_into('<I', new_data, 0x20, count + 1)
    return bytes(new_data), count  # new entry lands at index == old count


def port_mesh_entry(source_path, source_index, target_path):
    """Full port: extract `source_index` from `source_path` and insert it
    into `target_path`'s bytes (read from disk). Returns (new_target_bytes,
    new_index, tag) -- caller writes new_target_bytes back to target_path
    (or a copy of it) and must update any BEF blob referencing the enemy to
    point at `new_index` instead of `source_index` (see
    patch_bef_mesh_index below).
    """
    entry_bytes, tag = extract_mesh_entry(source_path, source_index)
    target_data = open(target_path, 'rb').read()
    new_data, new_index = insert_mesh_entry(target_data, entry_bytes)
    return new_data, new_index, tag


MESH_INDEX_OFFSET = 22  # u16, within a BEF blob's fixed header


def get_bef_mesh_index(blob):
    """Read the mesh-table index a BEF blob's header points at (offset 22,
    u16). Confirmed for Dark_Knight (type 0x53 in CASTLE.BEF): this field
    held the value 25, exactly matching mesh[25] in CASTLE.PRS -- see
    research doc Session 5/6 for the full identification trail."""
    return struct.unpack_from('<H', blob, MESH_INDEX_OFFSET)[0]


def patch_bef_mesh_index(blob, new_index):
    """Return a copy of a BEF blob with its mesh-table index field
    (offset 22, u16) rewritten to `new_index`. Fixed-size field, pure
    in-place overwrite -- blob length is unchanged."""
    out = bytearray(blob)
    struct.pack_into('<H', out, MESH_INDEX_OFFSET, new_index)
    return bytes(out)


def port_enemy_mesh_and_reindex(source_prs_path, source_mesh_index,
                                 target_prs_path, bef_blob):
    """Full pipeline for one enemy: port their mesh subtree from the source
    world's .PRS into the target world's .PRS, and return a patched copy of
    their BEF blob pointing at the new mesh index.

    Returns (new_target_prs_bytes, new_bef_blob, new_mesh_index).
    Caller is responsible for writing new_target_prs_bytes to the target
    world's .PRS (via the same patched-output-folder mechanism
    iso_patcher.py already uses for other grown assets) and using
    new_bef_blob in place of the original when injecting this enemy's BEF
    entry into the target world (see bef.py's inject_cross_world_enemies).

    NOTE on textures: this does NOT port textures. Confirmed for
    Dark_Knight (mesh[25] in CASTLE.PRS): his render-pass texture indices
    (33, 34) are small enough to near-certainly be GLOBAL.PRT indices
    (GLOBAL's ~160 textures are loaded first in the shared cache, before
    any world-local ones), meaning they're already present in every
    world's texture cache automatically -- no texture porting needed for
    him. This assumption should be re-checked per-enemy: if a ported
    enemy's texture ids turn out to be large (>= GLOBAL's texture count),
    they're world-local indices instead, and texture_port.py's PRT porting
    (or prs_mesh_parser.patch_texture_ids to remap them to existing target
    textures) is needed too.
    """
    entry_bytes, tag = extract_mesh_entry(source_prs_path, source_mesh_index)
    target_data = open(target_prs_path, 'rb').read()
    new_target_data, new_index = insert_mesh_entry(target_data, entry_bytes)
    new_blob = patch_bef_mesh_index(bef_blob, new_index)
    return new_target_data, new_blob, new_index
