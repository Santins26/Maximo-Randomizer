"""
PRS mesh/skeleton/shader/render-pass parser and in-place texture-id patcher.

Reverse-engineered via disassembly of PResourceManager's loader chain
(LoadMeshResTable -> PMeshNode::Load / PSkeleton::Load -> PBone::Load ->
PMesh::Load -> PMeshFragment::Load -> PShader::Load -> PRenderPass::Load),
cross-referenced against real bytes in CASTLE.PRS. See
research/CROSSWORLD_TEXTURE_RESEARCH.md Part 4 for the full trail, including
two backwards-tag-semantics bugs that were found and fixed by testing
against real file bytes:
  - PMeshNode: tag 1 = another child follows (recurse), tag 2 = terminal/stop
  - PBone:     tag 8 = another child follows (recurse), tag 9 = terminal/stop
Both are easy to get backwards from reading disassembly alone; both were
only caught by running the parser against real bytes and watching where a
long, clean run of plausible data suddenly turned to garbage.

Verified result: parses CASTLE.PRS's entire 59-entry mesh table, extracting
600+ real texture-cache-index references (PRenderPass.tex_id) across every
top-level object in the file (props, doors, and character skeletons with
deeply nested bone hierarchies).

Dark_Knight identification (see research doc): his BEF blob (type 0x53 in
CASTLE.BEF) has the value pair (6, 25) packed as two u16s at header offset
+20 -- 25 matches mesh-table entry 25 exactly, which is one of only 13
character skeletons in the file, uses exactly 2 distinct textures (33, 34)
reused across 31 render passes (a classic "armor plating reuses a couple of
textures across many pieces" pattern), and no other candidate fits as well.

Known-unsolved edge cases (rare; the parser tolerates them via bounded
resync rather than being blocked by them -- see _resync_* helpers):
  - PVertexAnim's exact count field/formula isn't fully nailed down.
  - PMeshNode's tag==4 special case (rare; possibly camera/particle
    attachment) isn't traced -- raises clearly if hit.
  - The very last entry in a mesh table has occasionally needed a resync
    too; not fully root-caused.
None of these affected mesh[25] (Dark_Knight) -- his subtree parses 100%
cleanly with no resyncs needed.

Usage:
    from randomizer.prs_mesh_parser import Reader, load_mesh_res_table, \\
        render_passes, find_render_passes_under, patch_texture_ids

    data = open('CASTLE.PRS', 'rb').read()
    r = Reader(data, 0x20)
    load_mesh_res_table(r)   # populates the module-level render_passes list

    # Inspect what's under a specific top-level entry:
    dark_knight_passes = find_render_passes_under('mesh[25]')

    # Patch: remap Dark_Knight's textures 33->NEW_A, 34->NEW_B in place.
    new_data, patched = patch_texture_ids(data, 'mesh[25]', {33: NEW_A, 34: NEW_B})
    # `patched` lists every (offset, old_id, new_id, path) that changed.
    # File length is unchanged -- write new_data back to CASTLE.PRS as-is.
"""
import struct

VERBOSE = False  # set True for a full per-node/bone/fragment trace to stdout


class PRSParseError(Exception):
    pass


class Reader:
    def __init__(self, data, off=0):
        self.data = data
        self.off = off

    def read(self, n):
        if self.off + n > len(self.data):
            raise PRSParseError(f"EOF at 0x{self.off:X}, wanted {n} bytes")
        b = self.data[self.off:self.off + n]
        self.off += n
        return b

    def u32(self):
        return struct.unpack('<I', self.read(4))[0]

    def i32(self):
        return struct.unpack('<i', self.read(4))[0]

    def u16(self):
        return struct.unpack('<H', self.read(2))[0]

    def peek_u32(self):
        return struct.unpack_from('<I', self.data, self.off)[0]


def _log(msg):
    if VERBOSE:
        print(msg)


render_passes = []  # collected (offset, texture_id, path) for every PRenderPass seen


def load_st_anim_channel(r, path):
    start = r.off
    hdr = r.read(16)
    count = struct.unpack_from('<I', hdr, 0)[0]
    if not (0 <= count <= 10000):
        raise PRSParseError(f"0x{start:X}: implausible STAnimChannel count={count} path={path}")
    r.read(count * 8)


def load_st_anim(r, path):
    """16-byte header (channel count @ offset+2, u16) + that many PSTAnimChannel."""
    start = r.off
    hdr = r.read(16)
    count = struct.unpack_from('<H', hdr, 2)[0]
    if not (0 <= count <= 64):
        raise PRSParseError(f"0x{start:X}: implausible PSTAnim channel count={count} path={path}")
    for i in range(count):
        load_st_anim_channel(r, path + f".stchannel{i}")


def load_render_pass(r, path):
    """40-byte header; offset+4 (s32) is the texture-cache index (-1 = none);
    offset+20 bit0x0001 gates an optional PSTAnim (UV-scroll) block."""
    start = r.off
    hdr = r.read(40)
    tex_id = struct.unpack_from('<i', hdr, 4)[0]
    flags20 = struct.unpack_from('<I', hdr, 20)[0]
    render_passes.append((start, tex_id, path))
    if flags20 & 0x0001:
        load_st_anim(r, path + ".stanim")


def load_shader(r, path):
    """16-byte header; offset+0 (u32) is the layer count. Each layer is a
    full PRenderPass record."""
    start = r.off
    hdr = r.read(16)
    layer_count = struct.unpack_from('<I', hdr, 0)[0]
    if not (0 <= layer_count <= 64):
        raise PRSParseError(f"0x{start:X}: implausible PShader layer_count={layer_count} path={path}")
    for i in range(layer_count):
        load_render_pass(r, path + f".layer{i}")


def load_poly_table(r, path):
    start = r.off
    hdr = r.read(16)
    poly_count = struct.unpack_from('<i', hdr, 4)[0]
    if not (0 <= poly_count <= 100000):
        raise PRSParseError(f"0x{start:X}: implausible poly_count={poly_count} path={path}")
    r.read(poly_count * 32)


def load_vertex_anim(r, path):
    start = r.off
    hdr = r.read(16)
    count = struct.unpack_from('<I', hdr, 0)[0]
    if not (0 <= count <= 100000):
        raise PRSParseError(f"0x{start:X}: implausible PVertexAnim count={count} path={path}")
    r.read(count * 16)


def load_vertex_anims(r, count, path):
    for i in range(count):
        load_vertex_anim(r, path + f".vanim{i}")


def load_mesh_fragment(r, path):
    """PShader::Load(fragment) + 60-byte fragment header + optional
    poly-tables (vert_count of them) + optional vertex-anims + optional
    raw face/index buffer (size given directly by header offset+4)."""
    load_shader(r, path)
    hdr_start = r.off
    hdr = r.read(60)
    flags = struct.unpack_from('<I', hdr, 0)[0]
    face_buf_size = struct.unpack_from('<I', hdr, 4)[0]
    vert_count = struct.unpack_from('<H', hdr, 12)[0]
    _log(f"      [frag hdr@0x{hdr_start:X}] flags=0x{flags:X} "
         f"face_buf_size={face_buf_size} vert_count={vert_count} path={path}")
    if not (0 <= vert_count <= 200000):
        raise PRSParseError(f"0x{hdr_start:X}: implausible vert_count={vert_count} path={path}")
    for pt_i in range(vert_count):
        load_poly_table(r, path + f".polytable{pt_i}")
    vanim_count = struct.unpack_from('<I', hdr, 8)[0]
    if flags & 0x0002 and vanim_count != 0:
        load_vertex_anims(r, vanim_count, path + ".vertexanims")
    if flags & 0x0001:
        r.read(face_buf_size)


def _resync_any_tag(r, path, window=0x4000):
    start = r.off
    for cand in range(start, min(start + window, len(r.data) - 4), 4):
        v = struct.unpack_from('<I', r.data, cand)[0]
        if v in (1, 2, 8, 9):
            return cand
    raise PRSParseError(f"0x{start:X}: generic resync failed within {window} bytes path={path}")


def load_mesh(r, count, path):
    for i in range(count):
        try:
            load_mesh_fragment(r, path + f".frag{i}")
        except PRSParseError as e:
            _log(f"    !! fragment parse failed ({e}); resyncing at mesh level")
            r.off = _resync_any_tag(r, path)


def _resync_tag_bone(r, path, window=0x2000):
    start = r.off - 4
    for cand in range(start, min(start + window, len(r.data) - 4), 4):
        v = struct.unpack_from('<I', r.data, cand)[0]
        if v in (8, 9):
            return cand
    raise PRSParseError(f"0x{start:X}: bone resync failed within {window} bytes path={path}")


def load_bone(r, path, depth=0):
    """96 bytes (transform+name) + 16-byte mesh-header (field0 doubles as
    both 'has mesh' flag and fragment count) + optional attached PMesh +
    tag loop: 8=another child bone follows (recurse), 9=no more children."""
    if depth > 64:
        raise PRSParseError(f"0x{r.off:X}: bone recursion too deep, likely desynced path={path}")
    bone_start = r.off
    r.read(96)
    mesh_hdr_start = r.off
    mesh_hdr = r.read(16)
    has_mesh = struct.unpack_from('<I', mesh_hdr, 0)[0]
    _log(f"  [bone@0x{bone_start:X}] has_mesh={has_mesh} path={path}")
    if has_mesh:
        mesh_count = has_mesh
        if not (0 <= mesh_count <= 256):
            raise PRSParseError(f"0x{mesh_hdr_start:X}: implausible bone mesh_count={mesh_count} path={path}")
        load_mesh(r, mesh_count, path + ".mesh")
    tag = r.u32()
    if tag not in (8, 9):
        r.off = _resync_tag_bone(r, path)
        tag = r.u32()
    child_idx = 0
    while tag == 8:
        load_bone(r, path + f".child{child_idx}", depth + 1)
        tag = r.u32()
        if tag not in (8, 9):
            r.off = _resync_tag_bone(r, path)
            tag = r.u32()
        child_idx += 1
    if tag != 9:
        raise PRSParseError(f"0x{r.off-4:X}: bone tag loop ended with tag={tag}, expected 9 path={path}")


def load_shared_mesh(r, path):
    start = r.off
    hdr = r.read(16)
    has_mesh = struct.unpack_from('<I', hdr, 0)[0]
    if has_mesh:
        mesh_count = has_mesh
        if not (0 <= mesh_count <= 256):
            raise PRSParseError(f"0x{start:X}: implausible sharedmesh mesh_count={mesh_count} path={path}")
        load_mesh(r, mesh_count, path + ".sharedmesh")


def load_skeleton(r, path):
    """16-byte header (field0 gates LoadSharedMesh) + opening tag (must==8)
    + root PBone::Load (recursive) + optional LoadSharedMesh (BEFORE the
    closing tag, not after -- easy to get backwards) + closing tag (must==7)."""
    hdr = r.read(16)
    field0 = struct.unpack_from('<I', hdr, 0)[0]
    tag1 = r.u32()
    if tag1 != 8:
        raise PRSParseError(f"0x{r.off-4:X}: skeleton opening tag={tag1}, expected 8 path={path}")
    load_bone(r, path + ".root")
    if field0:
        load_shared_mesh(r, path + ".shared")
    tag2 = r.u32()
    if tag2 != 7:
        raise PRSParseError(f"0x{r.off-4:X}: skeleton closing tag={tag2}, expected 7 path={path}")


def _resync_tag(r, path, window=0x2000):
    """Bounded forward scan for the next plausible tag value (1 or 2),
    used only when the expected tag read comes back implausible. This lets
    the parser push past not-yet-understood edge cases rather than being
    fully blocked by them -- see module docstring for known unsolved cases."""
    start = r.off - 4
    for cand in range(start, min(start + window, len(r.data) - 4), 4):
        v = struct.unpack_from('<I', r.data, cand)[0]
        if v in (1, 2):
            return cand
    raise PRSParseError(f"0x{start:X}: resync failed to find a valid tag within {window} bytes path={path}")


def load_mesh_node(r, path, depth=0):
    """112-byte transform (NOTE: version-gated, 80 bytes on older files --
    not yet auto-detected, hardcoded to 112 here) + 16-byte mesh-header +
    optional attached PMesh + tag loop: 1=another child node follows
    (recurse), 2=no more children (terminal, with a notify call). tag==4
    is a rare, untraced special case (raises clearly if hit)."""
    if depth > 64:
        raise PRSParseError(f"0x{r.off:X}: meshnode recursion too deep path={path}")
    node_start = r.off
    r.read(112)
    mesh_hdr_start = r.off
    mesh_hdr = r.read(16)
    has_mesh = struct.unpack_from('<I', mesh_hdr, 0)[0]
    _log(f"  [node@0x{node_start:X}] has_mesh={has_mesh} path={path}")
    if has_mesh:
        mesh_count = has_mesh
        if not (0 <= mesh_count <= 256):
            raise PRSParseError(f"0x{mesh_hdr_start:X}: implausible node mesh_count={mesh_count} path={path}")
        load_mesh(r, mesh_count, path + ".mesh")
    tag = r.u32()
    if tag == 4:
        raise PRSParseError(f"0x{r.off-4:X}: meshnode tag==4 special case not traced path={path}")
    if tag not in (1, 2):
        r.off = _resync_tag(r, path)
        tag = r.u32()
    child_idx = 0
    while tag == 1:
        load_mesh_node(r, path + f".child{child_idx}", depth + 1)
        tag = r.u32()
        if tag not in (1, 2):
            r.off = _resync_tag(r, path)
            tag = r.u32()
        child_idx += 1
    if tag != 2:
        raise PRSParseError(f"0x{r.off-4:X}: meshnode tag loop ended with tag={tag}, expected 2 path={path}")


def _resync_toplevel_tag(r, path, window=0x8000):
    """Bounded scan for a position where the next 4 bytes are a plausible
    top-level tag (1 or 6), staying 4-byte aligned."""
    start = r.off
    for cand in range(start, min(start + window, len(r.data) - 4), 4):
        v = struct.unpack_from('<I', r.data, cand)[0]
        if v in (1, 6):
            return cand
    raise PRSParseError(f"0x{start:X}: top-level resync failed within {window} bytes path={path}")


def load_mesh_res_table(r):
    """Entry point: [u32 uNumMeshes][per entry: u32 tag; tag==1 ->
    PMeshNode::Load; tag==6 -> PSkeleton::Load]. Tolerates a bad entry via
    bounded top-level resync rather than aborting the whole table."""
    count = r.u32()
    _log(f"uNumMeshes = {count}")
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
                raise PRSParseError(f"0x{entry_start:X}: unknown mesh-table tag={tag} (expected 1 or 6) idx={i}")
        except PRSParseError as e:
            _log(f"  !! entry {i} failed ({e}); resyncing at top level")
            r.off = _resync_toplevel_tag(r, path)
            continue
        _log(f"  [{i}] tag={tag} consumed up to 0x{r.off:X}")


def parse_prs_mesh_table(prs_path):
    """Convenience entry point: parse a world's .PRS mesh table and return
    the list of (file_offset, texture_cache_id, path) tuples collected.
    Resets the module-level render_passes list first."""
    global render_passes
    render_passes = []
    with open(prs_path, 'rb') as f:
        data = f.read()
    r = Reader(data, 0x20)
    load_mesh_res_table(r)
    return render_passes


def find_render_passes_under(prefix):
    """Return every (offset, tex_id, path) already collected in the global
    render_passes list whose path starts with the given prefix (e.g.
    'mesh[25]' to get every render pass belonging to that top-level entry,
    including all nested bones/fragments/layers)."""
    return [(off, tid, p) for off, tid, p in render_passes if p.startswith(prefix)]


def patch_texture_ids(data, prefix, tex_id_map):
    """Return (new_bytes, patched) where new_bytes is `data` with every
    PRenderPass texture-id field under `prefix` remapped according to
    tex_id_map (old_tex_id -> new_tex_id), and patched is a list of
    (offset, old_tex_id, new_tex_id, path) for everything actually changed.
    Render passes whose current tex_id isn't a key in tex_id_map are left
    untouched.

    This is a pure in-place overwrite -- exactly 4 bytes change per
    affected render pass, at offset+4 of its 40-byte header. No other byte
    in the file moves or shifts, so there is no file-growth/resync concern
    here (unlike the .PRT texture-porting case in texture_port.py). The
    resulting bytes are the same length as the input and can be written
    straight back to the .PRS file.

    `render_passes` (the module-level list) must already be populated by a
    prior call to load_mesh_res_table()/parse_prs_mesh_table() against this
    same `data`.
    """
    out = bytearray(data)
    patched = []
    for off, tid, path in find_render_passes_under(prefix):
        if tid in tex_id_map:
            new_tid = tex_id_map[tid]
            struct.pack_into('<i', out, off + 4, new_tid)
            patched.append((off, tid, new_tid, path))
    return bytes(out), patched


if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'CASTLE.PRS'
    results = parse_prs_mesh_table(path)
    print(f"{path}: collected {len(results)} texture references")
    for off, tid, p in results[:20]:
        print(f"  0x{off:X}  tex_id={tid}  {p}")
