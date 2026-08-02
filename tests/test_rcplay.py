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
