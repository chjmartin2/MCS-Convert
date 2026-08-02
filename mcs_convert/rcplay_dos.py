"""RCPLAY.COM — the universal DOS .RCT player (8088+, real mode).

One .COM that plays any .RCT file:

    RCPLAY SONG.RCT [/T|/1|/4] [/F|/X] [/V5|/V6|/V8]

A hand-assembled LOADER parses the command line, opens the file (int 21h),
walks the chunk table to the PERF stream for the chosen target, loads the
stream into a shared heap inside the .COM's segment, patches the chosen
engine's labeled immediates (total sub-ticks, PIT divider, samples/sub-tick,
calibration constants), and jumps into it. The engines are the SAME proven
player bodies the .COM exports use — assembled here with `org`/`stream_at` so
they live at fixed offsets and read the loaded stream.

Scalability: with no /F or /X flag the loader times a calibration loop against
PIT channel 0. A slow CPU (XT class) gets the calibrated FOREGROUND engine
(audio-only, pitch-correct at any speed); a fast one gets the ISR engine with
a live visualization (text5 combined monitor by default, /V8 VGA scopes,
/V6 VU meters). /T and /1 pick the Tandy (text3 scopes) and 1-voice (text2)
players instead; the default target comes from the file's own hint byte.

Embedded engine builds:
    0  4voice foreground (no display — the XT path)
    1  4voice ISR + text5 combined monitor
    2  4voice ISR + VGA mode 13h scopes
    3  4voice ISR + VU meters
    4  Tandy SN76489 + text3 scopes
    5  1-voice PC speaker + text2 scope
"""

from __future__ import annotations

import struct
from typing import Dict, List, Tuple

from . import dosplayer as D
from .dosplayer import _Asm, _w

_LOADER_SIZE = 0x600             # loader area, padded (code is ~0x300)
_CAL_THRESH = 6000               # PIT counts for 2048 iterations: above = XT-slow

#: (mode, vis, foreground) for each embedded build, in patch-table order
BUILDS = (("4voice", "", True),
          ("4voice", "text5", False),
          ("4voice", "vga", False),
          ("4voice", "vu", False),
          ("tandy", "text3", False),
          ("1voice", "text2", False))


def _assemble_build(mode: str, vis: str, fg: bool, org: int,
                    stream_at: int, capture: dict) -> bytes:
    """One engine body at `org` reading the shared heap; label map captured."""
    real = _Asm.resolve

    def grab(self):
        out = real(self)
        capture["labels"] = dict(self.labels)
        return out

    _Asm.resolve = grab
    try:
        if fg:
            return D._assemble_spk4_fg(1193, 1, b"", D._FG_FS, org=org,
                                       stream_at=stream_at)
        if mode == "4voice":
            return D._assemble_spk4(100, 160, 1, b"", vis,
                                    D._draw_skip_for(vis, None), org=org,
                                    stream_at=stream_at)
        sil = D._tandy_silence() if mode == "tandy" else D._spk_note_off()
        sil_bytes = bytes([len(sil)]) + b"".join(bytes([p, v]) for p, v in sil)
        return D._assemble(1193, 1, 1, sil_bytes, b"", vis,
                           D._draw_skip_for(vis, None), org=org,
                           stream_at=stream_at)
    finally:
        _Asm.resolve = real


def build_rcplay() -> bytes:
    """Assemble the complete RCPLAY.COM."""
    # pass 1: sizes (label placement is size-independent of org/stream_at)
    sizes = []
    for mode, vis, fg in BUILDS:
        body = _assemble_build(mode, vis, fg, 0x100, 0, {})
        sizes.append(len(body))
    org0 = 0x100 + _LOADER_SIZE
    orgs = []
    pos = org0
    for n in sizes:
        orgs.append(pos)
        pos += n
    heap = (pos + 15) & ~15                          # paragraph-align the heap
    max_stream = 0xFFF0 - heap
    if max_stream < 16 * 1024:
        raise ValueError("engine builds leave too little heap for streams")

    # pass 2: final bodies + label maps at their real addresses
    bodies, labels = [], []
    for (mode, vis, fg), org in zip(BUILDS, orgs):
        cap: dict = {}
        bodies.append(_assemble_build(mode, vis, fg, org, heap, cap))
        labels.append(cap["labels"])

    loader = _build_loader(labels, orgs, heap, max_stream)
    if len(loader) > _LOADER_SIZE:
        raise ValueError(f"loader is {len(loader)} bytes (> {_LOADER_SIZE})")
    loader += bytes(_LOADER_SIZE - len(loader))
    com = loader + b"".join(bodies)
    com += bytes(heap - 0x100 - len(com))            # pad up to the aligned heap
    assert len(com) == heap - 0x100
    return com


_local_n = 0


def _je_far(a: _Asm, target: str) -> None:
    """jump-if-EQUAL to a possibly-far label: jne over a near jmp."""
    global _local_n
    _local_n += 1
    skip = f"_lj{_local_n}"
    a.db(0x75).rel8(skip)                            # jne skip
    a.db(0xE9).rel16(target)                         # jmp near target
    a.label(skip)


def _jne_far(a: _Asm, target: str) -> None:
    """jump-if-NOT-equal to a possibly-far label: je over a near jmp."""
    global _local_n
    _local_n += 1
    skip = f"_lk{_local_n}"
    a.db(0x74).rel8(skip)                            # je skip
    a.db(0xE9).rel16(target)                         # jmp near target
    a.label(skip)


def _set_if_eq(a: _Asm, imm: int, var: str, val: int) -> None:
    """if al == imm: byte[var] = val (local-label skip, no hand-counted offsets)."""
    global _local_n
    _local_n += 1
    skip = f"_ls{_local_n}"
    a.db(0x3C, imm)                                  # cmp al, imm
    a.db(0x75).rel8(skip)                            # jne skip
    a.db(0xC6, 0x06).abs16(var).db(val)              # mov byte[var], val
    a.label(skip)


def _build_loader(labels: List[dict], orgs: List[int], heap: int,
                  max_stream: int) -> bytes:
    """The loader: cmdline -> file -> PERF -> patch -> jump. All branches to
    far-away code go through `jne+jmp near` pairs, so nothing depends on
    hand-counted displacements."""
    a = _Asm(0x100)
    FG, T5, VGA, VU, TAN, PC1 = range(6)

    # ---- parse the command line (PSP: length at 0x80, text at 0x81) ---------
    a.db(0xBE, 0x81, 0x00)                           # mov si, 0x81
    a.db(0x8A, 0x0E, 0x80, 0x00).db(0x30, 0xED)      # mov cl,[0x80]; xor ch,ch
    a.db(0xE3).rel8("j_usage")                       # jcxz -> usage
    a.label("skipsp")
    a.db(0xAC)                                       # lodsb
    a.db(0x3C, 0x20).db(0x75).rel8("gotname")        # not a space -> the name
    a.db(0xE2).rel8("skipsp")                        # loop skipsp
    a.label("j_usage")
    a.db(0xE9).rel16("h_usage")                      # all spaces -> usage
    a.label("gotname")
    a.db(0xBF).abs16("fname")                        # mov di, fname
    a.label("copyf")
    a.db(0x3C, 0x0D).db(0x74).rel8("args")           # CR -> flags/done
    a.db(0x3C, 0x20).db(0x74).rel8("args")           # space -> flags/done
    a.db(0xAA)                                       # stosb
    a.db(0xAC)                                       # lodsb
    a.db(0xE2).rel8("copyf")                         # loop copyf
    a.db(0xEB).rel8("argsdone")                      # cx ran out
    # ---- flags: /T /1 /4 (target)  /F /X (engine)  /V5 /V6 /V8 (viz) --------
    # EVERY lodsb pairs with a dec cx: the counter tracks characters consumed,
    # or the scan runs off the end of the command line into raw memory and
    # random 0x2F bytes there get parsed as flags (the "/1 played the Tandy
    # stream" bug -- a stray '/T'-looking pair always won).
    a.label("args")
    a.db(0xE3).rel8("argsdone")                      # jcxz argsdone
    a.db(0xAC).db(0x49)                              # lodsb; dec cx
    a.db(0x3C, 0x2F).db(0x75).rel8("argnext")        # only care past a '/'
    a.db(0xE3).rel8("argsdone")
    a.db(0xAC).db(0x49)                              # lodsb (the flag letter); dec cx
    _set_if_eq(a, ord("1"), "v_target", 0x02)        # /1 -> 1voice
    _set_if_eq(a, ord("4"), "v_target", 0x03)        # /4 -> 4voice
    a.db(0x24, 0xDF)                                 # and al,0xDF (uppercase)
    _set_if_eq(a, ord("T"), "v_target", 0x01)        # /T -> tandy
    _set_if_eq(a, ord("F"), "v_force", 0x01)         # /F -> foreground
    _set_if_eq(a, ord("X"), "v_force", 0x02)         # /X -> ISR
    a.db(0x3C, 0x56).db(0x75).rel8("argnext")        # 'V' + digit?
    a.db(0xE3).rel8("argsdone")
    a.db(0xAC).db(0x49)                              # lodsb (digit); dec cx
    a.db(0x2C, 0x30)                                 # sub al,'0'
    a.db(0xA2).abs16("v_viz")                        # [v_viz] = digit
    a.label("argnext")
    a.db(0xE3).rel8("argsdone").db(0xEB).rel8("args")   # more?
    a.label("argsdone")
    a.db(0xC6, 0x05, 0x00)                           # mov byte[di],0 (terminate name)

    # ---- open + header ------------------------------------------------------
    a.db(0xBA).abs16("fname").db(0xB8, 0x00, 0x3D).db(0xCD, 0x21)   # open r/o
    a.db(0x73, 0x03).db(0xE9).rel16("h_e_open")      # jnc +3; jmp near
    a.db(0xA3).abs16("handle")
    _read(a, "fhdr", 16, "h_e_read")
    a.db(0x81, 0x3E).abs16("fhdr").bytes(b"RC")      # cmp word[fhdr],'RC'
    _jne_far(a, "h_e_fmt")
    a.db(0x81, 0x3E).abs16("fhdr", 2).bytes(b"T!")
    _jne_far(a, "h_e_fmt")
    a.db(0x80, 0x3E).abs16("fhdr", 4).db(0x01)       # version 1?
    _jne_far(a, "h_e_fmt")
    # default target from the file's hint byte when no /flag chose one
    a.db(0x80, 0x3E).abs16("v_target").db(0x00)
    a.db(0x75).rel8("findperf")                      # explicit flag wins
    a.db(0xC6, 0x06).abs16("v_target").db(0x03)      # default 4voice
    a.db(0xA0).abs16("fhdr", 6)                      # al = target hint
    _set_if_eq(a, 0x02, "v_target", 0x01)            # hint tandy
    _set_if_eq(a, 0x03, "v_target", 0x02)            # hint 1voice

    # ---- chunk walk: find PERF with target == v_target ----------------------
    a.label("findperf")
    _read(a, "chdr", 8, "h_e_read", exact_to="h_e_nostream")
    a.db(0x81, 0x3E).abs16("chdr").bytes(b"PE")
    a.db(0x75).rel8("skipchunk")
    a.db(0x81, 0x3E).abs16("chdr", 2).bytes(b"RF")
    a.db(0x75).rel8("skipchunk")
    _read(a, "phdr", 16, "h_e_read")
    a.db(0xA0).abs16("phdr")                         # al = PERF target
    a.db(0x3A, 0x06).abs16("v_target")               # cmp al,[v_target]
    a.db(0x74).rel8("gotperf")
    # not ours: seek past the remaining (size-16) bytes
    a.db(0x8B, 0x16).abs16("chdr", 4)                # dx = size low
    a.db(0x83, 0xEA, 0x10)                           # sub dx,16
    a.db(0x8B, 0x0E).abs16("chdr", 6)                # cx = size high
    a.db(0x83, 0xD9, 0x00)                           # sbb cx,0
    _seek_cur(a, "h_e_read")
    a.db(0xE9).rel16("findperf")
    a.label("skipchunk")
    a.db(0x8B, 0x16).abs16("chdr", 4)                # dx/cx = full size
    a.db(0x8B, 0x0E).abs16("chdr", 6)
    _seek_cur(a, "h_e_read")
    a.db(0xE9).rel16("findperf")

    a.label("gotperf")
    a.db(0x80, 0x3E).abs16("phdr", 1).db(0x01)       # stream version 1?
    _jne_far(a, "h_e_fmt")
    a.db(0x83, 0x3E).abs16("phdr", 14).db(0x00)      # length high word == 0?
    _jne_far(a, "h_e_big")
    a.db(0xA1).abs16("phdr", 12)                     # ax = length low
    a.db(0x3D).bytes(_w(max_stream))                 # cmp ax, max_stream
    a.db(0x76, 0x03).db(0xE9).rel16("h_e_big")       # jbe +3; jmp near (ja)
    a.db(0x89, 0xC1)                                 # cx = length
    a.db(0xBA).bytes(_w(heap))                       # dx = heap
    a.db(0x8B, 0x1E).abs16("handle").db(0xB4, 0x3F).db(0xCD, 0x21)   # read stream
    a.db(0x73, 0x03).db(0xE9).rel16("h_e_read")      # jnc +3; jmp near
    a.db(0x8B, 0x1E).abs16("handle").db(0xB4, 0x3E).db(0xCD, 0x21)   # close

    # ---- choose + patch + go ------------------------------------------------
    a.db(0xA0).abs16("v_target")
    a.db(0x3C, 0x01)                                 # tandy?
    _je_far(a, "go_tandy")                         # (inverted: je via jne-skip)
    a.db(0x3C, 0x02)
    _je_far(a, "go_1voice")
    # 4voice: /F fg, /X isr, else calibrate the CPU
    a.db(0xA0).abs16("v_force")
    a.db(0x3C, 0x01)
    _je_far(a, "go_fg")
    a.db(0x3C, 0x02)
    _je_far(a, "go_isr")
    _emit_calibrate(a)                               # AX = PIT counts elapsed
    a.db(0x3D).bytes(_w(_CAL_THRESH))                # cmp ax, THRESH
    a.db(0x76, 0x03)                                 # jbe +3 (fast -> ISR)
    a.db(0xE9).rel16("go_fg")                        # slow CPU -> foreground
    a.db(0xE9).rel16("go_isr")

    a.label("go_tandy")
    _emit_patch_pv(a, labels[TAN], orgs[TAN])
    a.label("go_1voice")
    _emit_patch_pv(a, labels[PC1], orgs[PC1])
    a.label("go_fg")
    _emit_patch_fg(a, labels[FG], orgs[FG])
    a.label("go_isr")
    # viz select: /V8 vga, /V6 vu, else text5
    a.db(0xA0).abs16("v_viz")
    a.db(0x3C, 0x08)
    _je_far(a, "go_vga")
    a.db(0x3C, 0x06)
    _je_far(a, "go_vu")
    _emit_patch_isr(a, labels[T5], orgs[T5])
    a.label("go_vga")
    _emit_patch_isr(a, labels[VGA], orgs[VGA])
    a.label("go_vu")
    _emit_patch_isr(a, labels[VU], orgs[VU])

    # ---- error handlers -----------------------------------------------------
    for name, msg in (("usage", "RCPLAY 1.0 - RetroComputerist Tracker player\r\n"
                       "usage: RCPLAY SONG.RCT [/T|/1|/4] [/F|/X] [/V5|/V6|/V8]\r\n$"),
                      ("e_open", "cannot open file$"),
                      ("e_fmt", "not an RCT v1 file$"),
                      ("e_read", "read error$"),
                      ("e_big", "stream too large for this player$"),
                      ("e_nostream", "no performance stream for this target "
                       "(re-save the file)$")):
        a.label("h_" + name)
        a.db(0xBA).abs16("m_" + name)                # mov dx, msg
        a.db(0xB4, 0x09).db(0xCD, 0x21)              # print $-string
        a.db(0xB8, 0x01, 0x4C).db(0xCD, 0x21)        # exit(1)
        a.label("m_" + name)
        a.bytes(msg.encode("cp437"))

    # ---- variables ----------------------------------------------------------
    a.label("handle"); a.db(0, 0)
    a.label("v_target"); a.db(0)                     # 0 auto / 1 tandy / 2 1v / 3 4v
    a.label("v_force"); a.db(0)                      # 0 auto / 1 fg / 2 isr
    a.label("v_viz"); a.db(0)                        # 0 default / 5 / 6 / 8
    a.label("fhdr"); a.bytes(bytes(16))
    a.label("chdr"); a.bytes(bytes(8))
    a.label("phdr"); a.bytes(bytes(16))
    a.label("fname"); a.bytes(bytes(80))
    return a.resolve()


def _read(a: _Asm, buf: str, n: int, err: str, exact_to: str = None) -> None:
    """int 21h/3F read of n bytes into `buf`; carry -> err; short read -> err
    or `exact_to` (EOF handling for the chunk walk). Far-safe branches."""
    a.db(0x8B, 0x1E).abs16("handle")                 # bx = handle
    a.db(0xB9).bytes(_w(n))                          # cx = n
    a.db(0xBA).abs16(buf)                            # dx = buf
    a.db(0xB4, 0x3F).db(0xCD, 0x21)                  # ah=0x3F; int 21
    a.db(0x73, 0x03).db(0xE9).rel16(err)             # jnc +3; jmp near err
    a.db(0x3D).bytes(_w(n))                          # cmp ax, n
    _jne_far(a, exact_to or err)                     # short read / EOF


def _seek_cur(a: _Asm, err: str) -> None:
    """int 21h/42 sub 1 (seek from current) by CX:DX. Far-safe branch."""
    a.db(0x8B, 0x1E).abs16("handle")
    a.db(0xB8, 0x01, 0x42).db(0xCD, 0x21)            # ax=0x4201
    a.db(0x73, 0x03).db(0xE9).rel16(err)             # jnc +3; jmp near err


def _emit_calibrate(a: _Asm) -> None:
    """Time 2048 iterations of a small loop on PIT ch0 (mode 2, count-by-1).
    Returns the elapsed PIT count in AX (~15000 on a 4.77 MHz 8088, a few
    hundred on anything fast). Restores the 18.2 Hz system timer after."""
    a.db(0xFA)                                       # cli
    a.db(0xB0, 0x34).db(0xE6, 0x43)                  # ch0 mode 2
    a.db(0x30, 0xC0).db(0xE6, 0x40).db(0xE6, 0x40)   # divisor 0 (65536)
    a.db(0xB0, 0x00).db(0xE6, 0x43)                  # latch
    a.db(0xE4, 0x40).db(0x88, 0xC1)                  # cl = lo
    a.db(0xE4, 0x40).db(0x88, 0xC5)                  # ch = hi
    a.db(0x51)                                       # push cx (C0)
    a.db(0xBD).bytes(_w(2048))                       # mov bp, 2048
    a.label("calx")
    a.db(0x83, 0xEB, 0x01)                           # sub bx,1 (dummy work)
    a.db(0x4D)                                       # dec bp
    a.db(0x75).rel8("calx")                          # jnz
    a.db(0xB0, 0x00).db(0xE6, 0x43)                  # latch again
    a.db(0xE4, 0x40).db(0x88, 0xC1)
    a.db(0xE4, 0x40).db(0x88, 0xC5)
    a.db(0x58)                                       # pop ax (C0)
    a.db(0x29, 0xC8)                                 # sub ax,cx (elapsed)
    a.db(0x50)                                       # push ax
    a.db(0xB0, 0x36).db(0xE6, 0x43)                  # restore ch0 mode 3
    a.db(0x30, 0xC0).db(0xE6, 0x40).db(0xE6, 0x40)   # divisor 0 -> 18.2 Hz
    a.db(0x58)                                       # pop ax
    a.db(0xFB)                                       # sti


def _patch(a: _Asm, addr: int) -> None:
    a.db(0xA3).bytes(_w(addr))                       # mov [addr], ax


def _emit_patch_common(a: _Asm, lab: dict) -> None:
    a.db(0xA1).abs16("phdr", 6)                      # ax = total sub-ticks
    _patch(a, lab["imm_total_a"])
    _patch(a, lab["imm_total_b"])


def _emit_patch_pv(a: _Asm, lab: dict, entry: int) -> None:
    """Tandy / 1-voice: total + PIT divider, then jump in."""
    _emit_patch_common(a, lab)
    a.db(0xA1).abs16("phdr", 2)                      # ax = divider
    _patch(a, lab["imm_div"])
    a.db(0xB8).bytes(_w(entry)).db(0xFF, 0xE0)       # mov ax,entry; jmp ax


def _emit_patch_isr(a: _Asm, lab: dict, entry: int) -> None:
    """4voice ISR: total + divider + samples/sub-tick."""
    _emit_patch_common(a, lab)
    a.db(0xA1).abs16("phdr", 2)
    _patch(a, lab["imm_div"])
    a.db(0xA1).abs16("phdr", 4)                      # ax = samps_per_sub
    _patch(a, lab["sampsub"])
    a.db(0xB8).bytes(_w(entry)).db(0xFF, 0xE0)


def _emit_patch_fg(a: _Asm, lab: dict, entry: int) -> None:
    """Foreground: total; sub-tick divider = mix divider * samps; calibration
    window M = 30000/divider iterations, reference elapsed = M * divider."""
    _emit_patch_common(a, lab)
    a.db(0xA1).abs16("phdr", 2)                      # ax = mix divider
    a.db(0xF7, 0x26).abs16("phdr", 4)                # mul word[samps] -> DX:AX
    _patch(a, lab["imm_div"])                        # sub-tick divider
    a.db(0xB8).bytes(_w(30000))                      # calM = 30000 / divider
    a.db(0x31, 0xD2)                                 # xor dx,dx
    a.db(0xF7, 0x36).abs16("phdr", 2)                # div word[divider]
    _patch(a, lab["imm_calM"])
    a.db(0xF7, 0x26).abs16("phdr", 2)                # calref = calM * divider
    _patch(a, lab["imm_calref"])
    a.db(0xB8).bytes(_w(entry)).db(0xFF, 0xE0)


def save_rcplay(path: str) -> int:
    """Write RCPLAY.COM to `path`; returns its size."""
    com = build_rcplay()
    with open(path, "wb") as fh:
        fh.write(com)
    return len(com)
