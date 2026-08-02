"""RCPLAY.COM: the universal DOS .RCT player — structural verification.

(The audio itself is verified by ear in DOSBox; here we pin the loader's
contract: header parse, chunk walk, patch targets, entry jumps, and that a
freshly saved .RCT carries streams the player can actually select.)"""

import struct

from mcs_convert import rct as R
from mcs_convert import rcplay_dos as RP
from mcs_convert.streams import perf_chunks


def _labels_and_orgs():
    sizes = [len(RP._assemble_build(m, v, f, 0x100, 0, {})) for m, v, f in RP.BUILDS]
    orgs, pos = [], 0x100 + RP._LOADER_SIZE
    for n in sizes:
        orgs.append(pos)
        pos += n
    heap = (pos + 15) & ~15
    labels = []
    for (m, v, f), org in zip(RP.BUILDS, orgs):
        cap = {}
        RP._assemble_build(m, v, f, org, heap, cap)
        labels.append(cap["labels"])
    return labels, orgs, heap


def test_rcplay_builds_and_is_com_sized():
    com = RP.build_rcplay()
    assert 8 * 1024 < len(com) < 20 * 1024            # loader + 6 engines
    assert 0xFFF0 - 0x100 - len(com) > 40 * 1024      # plenty of stream heap
    assert com[0] == 0xBE                             # mov si,0x81 (cmdline parse)
    assert b"RCPLAY" in com and b"RCT" in com         # usage text present


def test_every_build_reads_the_shared_heap():
    labels, orgs, heap = _labels_and_orgs()
    for lab in labels:
        assert lab["stream"] == heap                  # all engines -> one heap
    # engines are placed back to back after the loader
    assert orgs[0] == 0x100 + RP._LOADER_SIZE


def test_loader_patches_every_engine_immediate():
    from capstone import Cs, CS_ARCH_X86, CS_MODE_16
    labels, orgs, heap = _labels_and_orgs()
    com = RP.build_rcplay()
    md = Cs(CS_ARCH_X86, CS_MODE_16)
    lines = list(md.disasm(com[:RP._LOADER_SIZE], 0x100))
    stores, entries = set(), set()
    for j, ins in enumerate(lines):
        if (ins.mnemonic == "mov" and ins.op_str.startswith("word ptr [0x")
                and ins.op_str.endswith("], ax")):
            stores.add(int(ins.op_str.split("[")[1].split("]")[0], 16))
        if (ins.mnemonic == "mov" and ins.op_str.startswith("ax, 0x")
                and j + 1 < len(lines) and lines[j + 1].mnemonic == "jmp"
                and lines[j + 1].op_str == "ax"):
            entries.add(int(ins.op_str.split("0x")[1], 16))
    for (mode, vis, fg), lab in zip(RP.BUILDS, labels):
        assert {lab["imm_total_a"], lab["imm_total_b"], lab["imm_div"]} <= stores
        if fg:
            assert {lab["imm_calM"], lab["imm_calref"]} <= stores
        elif mode == "4voice":
            assert lab["sampsub"] in stores
    assert entries == set(orgs)                       # a jump into every engine


def test_saved_rct_carries_streams_rcplay_can_pick():
    # a full .RCT written with PERF chunks: every chunk parses back with the
    # target ids the loader matches on, and streams fit the player's heap
    s = R.RctSong(speed=4, tempo_byte0=0x80)
    pat = R.RctPattern(rows=8)
    pat.cell(0, 0).note = R.midi_to_note(69)
    pat.cell(0, 0).inst = 1
    pat.cell(0, 0).fx, pat.cell(0, 0).param = R.FX_VIBRATO, 0x4C
    pat.cell(2, 3).note = R.midi_to_note(84)
    s.patterns = {0: pat}
    s.order = [0]
    s.perf = perf_chunks(s)
    data = R.write_rct(s)
    com = RP.build_rcplay()
    heap_free = 0xFFF0 - 0x100 - len(com)
    back = R.read_rct(data)
    assert set(back.perf) == {R.PERF_TANDY, R.PERF_1VOICE, R.PERF_4VOICE}
    for target, payload in back.perf.items():
        p = R.parse_perf(payload)
        assert p["target"] == target
        assert len(p["stream"]) < heap_free           # loads into RCPLAY's heap
        assert p["divider"] > 0 and p["total_subs"] > 0
    # the on-disk chunk really is "PERF" + the id byte the loader compares
    idx = data.index(b"PERF")
    (size,) = struct.unpack_from("<I", data, idx + 4)
    assert data[idx + 8] in (1, 2, 3)                 # target byte first in payload


def test_loader_file_walk_simulated_on_a_real_rct(tmp_path):
    # A "paper DOS": replay the loader's EXACT int-21h operation sequence
    # (sequential reads + relative seeks, as assembled) over a real saved .RCT
    # and check it lands on the same PERF stream parse_perf extracts. This is
    # the math a real 8088 will execute -- off-by-one seeks would show here.
    import struct
    s = R.RctSong(speed=4, tempo_byte0=0x80)
    pat = R.RctPattern(rows=16)
    pat.cell(0, 0).note = R.midi_to_note(60)
    pat.cell(0, 0).inst = 1
    pat.cell(4, 3).note = R.midi_to_note(84)
    s.patterns = {0: pat}
    s.order = [0, 0]
    s.perf = perf_chunks(s)
    data = R.write_rct(s)

    def loader_walk(want_target):
        pos = 0

        def read(n):
            nonlocal pos
            chunk = data[pos:pos + n]
            pos += len(chunk)
            return chunk

        def seek_cur(off):
            nonlocal pos
            pos += off

        hdr = read(16)                       # file header
        assert hdr[:4] == b"RCT!" and hdr[4] == 1
        while True:
            ch = read(8)                     # chunk header
            if len(ch) < 8:
                return None                  # e_nostream
            size = struct.unpack_from("<I", ch, 4)[0]
            if ch[:4] != b"PERF":
                seek_cur(size)               # skipchunk
                continue
            ph = read(16)                    # PERF header
            if ph[0] != want_target:
                seek_cur(size - 16)          # skipperf
                continue
            assert ph[1] == 1                # stream version
            slen = struct.unpack_from("<I", ph, 12)[0]
            return read(slen)                # the stream RCPLAY loads

    for target in (R.PERF_TANDY, R.PERF_1VOICE, R.PERF_4VOICE):
        got = loader_walk(target)
        want = R.parse_perf(s.perf[target])["stream"]
        assert got == want, f"target {target}: loader walk diverged"
    assert loader_walk(99) is None           # unknown target -> e_nostream
