"""RCPLAY.COM — the universal DOS .RCT player (8088+, real mode).

Two ways to run it:

    RCPLAY                     full-screen browser: lists the current
                               directory's *.RCT files, arrow keys + Enter to
                               play, S opens the settings screen (target /
                               engine / display), Esc quits. After a song you
                               land back in the browser.

    RCPLAY SONG.RCT [flags]    classic CLI: play one file and exit.
                               /T /1 /4  target (Tandy / 1-voice / 4-voice)
                               /F /X     engine (foreground / ISR, 4-voice)
                               /V5 /V6 /V8  display (text5 monitor / VU / VGA)

A hand-assembled LOADER parses the command line, opens the file (int 21h),
walks the chunk table to the PERF stream for the chosen target, loads the
stream into a shared heap inside the .COM's segment, patches the chosen
engine's labeled immediates (total sub-ticks, PIT divider, samples/sub-tick,
calibration constants), and jumps into it. The engines are the SAME proven
player bodies the .COM exports use — assembled here with `org`/`stream_at` so
they live at fixed offsets and read the loaded stream. Every engine's DOS
exit is retargeted at startup to a common `after_play`, which returns to the
browser in menu mode (and exits in CLI mode).

Scalability: with no /F or /X the loader times a calibration loop against PIT
channel 0 — a slow (XT-class) CPU gets the calibrated FOREGROUND engine, a
fast one gets the ISR engine with a live visualization. The VU display (/V6)
works on EVERY target; text5/VGA apply to the 4-voice ISR engine.

Embedded engine builds:
    0  4voice foreground (no display — the XT path)
    1  4voice ISR + text5 combined monitor      2  4voice ISR + VGA scopes
    3  4voice ISR + VU meters                   4  Tandy + text3 scopes
    5  Tandy + VU meters                        6  1-voice + text2 scope
    7  1-voice + VU meters
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import dosplayer as D
from .dosplayer import _Asm, _w

_LOADER_SIZE = 0x1000            # loader + browser + file table, padded
_CAL_THRESH = 6000               # PIT counts for 2048 iterations: above = XT-slow
_MAX_FILES = 64                  # browser capacity (13 bytes each)
_LIST_ROWS = 18                  # browser rows on screen

#: (mode, vis, foreground) for each embedded build
BUILDS = (("4voice", "", True),          # 0 FG
          ("4voice", "text5", False),    # 1 T5
          ("4voice", "vga", False),      # 2 VGA
          ("4voice", "vu", False),       # 3 VU4
          ("tandy", "text3", False),     # 4 TAN
          ("tandy", "vu", False),        # 5 TANVU
          ("1voice", "text2", False),    # 6 PC1
          ("1voice", "vu", False))       # 7 PC1VU


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
    """Assemble the complete RCPLAY.COM (two-pass: the loader is assembled
    once to fix its own label addresses, then again with the engine-exit
    retarget offsets that depend on them)."""
    sizes = []
    for mode, vis, fg in BUILDS:
        sizes.append(len(_assemble_build(mode, vis, fg, 0x100, 0, {})))
    orgs, pos = [], 0x100 + _LOADER_SIZE
    for n in sizes:
        orgs.append(pos)
        pos += n
    heap = (pos + 15) & ~15
    max_stream = 0xFFF0 - heap
    if max_stream < 16 * 1024:
        raise ValueError("engine builds leave too little heap for streams")

    bodies, labels = [], []
    for (mode, vis, fg), org in zip(BUILDS, orgs):
        cap: dict = {}
        bodies.append(_assemble_build(mode, vis, fg, org, heap, cap))
        labels.append(cap["labels"])

    # the foreground engine quits by NOPping its own back-jump; the browser
    # must restore the original bytes before replaying it
    fg_off = labels[0]["fgjmp"] - orgs[0]
    fg_orig = bodies[0][fg_off] | (bodies[0][fg_off + 1] << 8)

    pass_a, la = _build_loader(labels, orgs, heap, max_stream, fg_orig, None)
    loader, lb = _build_loader(labels, orgs, heap, max_stream, fg_orig, la)
    assert len(pass_a) == len(loader), "loader size must be pass-stable"
    if len(loader) > _LOADER_SIZE:
        raise ValueError(f"loader is {len(loader)} bytes (> {_LOADER_SIZE})")
    loader += bytes(_LOADER_SIZE - len(loader))
    com = loader + b"".join(bodies)
    com += bytes(heap - 0x100 - len(com))
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
    """if al == imm: byte[var] = val."""
    global _local_n
    _local_n += 1
    skip = f"_ls{_local_n}"
    a.db(0x3C, imm)                                  # cmp al, imm
    a.db(0x75).rel8(skip)                            # jne skip
    a.db(0xC6, 0x06).abs16(var).db(val)              # mov byte[var], val
    a.label(skip)


def _puts_at(a: _Asm, s: str, row: int, col: int, attr: int) -> None:
    """Emit: draw string label `s` at (row, col) with `attr` via `puts`."""
    a.db(0xBE).abs16(s)                              # mov si, str
    a.db(0xBF).bytes(_w((row * 80 + col) * 2))       # mov di, cell offset
    a.db(0xB4, attr)                                 # mov ah, attr
    a.db(0xE8).rel16("puts")                         # call puts


def _build_loader(labels: List[dict], orgs: List[int], heap: int,
                  max_stream: int, fg_orig: int,
                  lp: Optional[dict]) -> Tuple[bytes, dict]:
    """The loader + browser + settings UI. `lp` is the previous pass's label
    map (None on pass A); the engine-exit retargets need it."""
    a = _Asm(0x100)
    FG, T5, VGA, VU4, TAN, TANVU, PC1, PC1VU = range(8)
    after_play = (lp or {}).get("after_play", 0)

    # ---- retarget every engine's DOS exit to after_play ----------------------
    # exit21 is `mov ax,4C00 / int 21` (5 bytes); overwrite with jmp near + nops
    for lab in labels:
        rel = (after_play - (lab["exit21"] + 3)) & 0xFFFF
        a.db(0xC7, 0x06).bytes(_w(lab["exit21"]))
        a.bytes(_w(0xE9 | ((rel & 0xFF) << 8)))
        a.db(0xC7, 0x06).bytes(_w(lab["exit21"] + 2))
        a.bytes(_w(((rel >> 8) & 0xFF) | 0x9000))
        a.db(0xC6, 0x06).bytes(_w(lab["exit21"] + 4)).db(0x90)

    # ---- parse the command line (PSP: length at 0x80, text at 0x81) ---------
    a.db(0xBE, 0x81, 0x00)                           # mov si, 0x81
    a.db(0x8A, 0x0E, 0x80, 0x00).db(0x30, 0xED)      # mov cl,[0x80]; xor ch,ch
    a.db(0xE3).rel8("j_menu")                        # no args -> browser
    a.label("skipsp")
    a.db(0xAC)                                       # lodsb
    a.db(0x3C, 0x20).db(0x75).rel8("gotname")        # not a space -> the name
    a.db(0xE2).rel8("skipsp")                        # loop skipsp
    a.label("j_menu")
    a.db(0xC6, 0x06).abs16("menu_mode").db(0x01)
    a.db(0xE9).rel16("menu_entry")
    a.label("gotname")
    a.db(0xBF).abs16("fname")                        # mov di, fname
    a.label("copyf")
    a.db(0x3C, 0x0D).db(0x74).rel8("args")           # CR -> flags/done
    a.db(0x3C, 0x20).db(0x74).rel8("args")           # space -> flags/done
    a.db(0xAA)                                       # stosb
    a.db(0xAC)                                       # lodsb
    a.db(0xE2).rel8("copyf")                         # loop copyf
    a.db(0xEB).rel8("argsdone")                      # cx ran out
    # ---- flags (every lodsb pairs with dec cx -- see the /1-as-Tandy bug) ----
    a.label("args")
    a.db(0xE3).rel8("argsdone")                      # jcxz argsdone
    a.db(0xAC).db(0x49)                              # lodsb; dec cx
    a.db(0x3C, 0x2F).db(0x75).rel8("argnext")        # only care past a '/'
    a.db(0xE3).rel8("argsdone")
    a.db(0xAC).db(0x49)                              # lodsb (flag letter); dec cx
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
    a.db(0xE3).rel8("argsdone").db(0xEB).rel8("args")
    a.label("argsdone")
    a.db(0xC6, 0x05, 0x00)                           # terminate fname
    a.db(0x80, 0x3E).abs16("fname").db(0x00)         # empty name ->
    a.db(0x75, 0x03).db(0xE9).rel16("j_menu")        # ...browser (jne over jmp)

    # ---- playfile: open + header + PERF + choose engine ----------------------
    a.label("playfile")
    a.db(0xBA).abs16("fname").db(0xB8, 0x00, 0x3D).db(0xCD, 0x21)   # open r/o
    a.db(0x73, 0x03).db(0xE9).rel16("h_e_open")
    a.db(0xA3).abs16("handle")
    _read(a, "fhdr", 16, "h_e_read")
    a.db(0x81, 0x3E).abs16("fhdr").bytes(b"RC")
    _jne_far(a, "h_e_fmt")
    a.db(0x81, 0x3E).abs16("fhdr", 2).bytes(b"T!")
    _jne_far(a, "h_e_fmt")
    a.db(0x80, 0x3E).abs16("fhdr", 4).db(0x01)       # version 1?
    _jne_far(a, "h_e_fmt")
    # resolve the play target into p_target -- NEVER write v_target here (the
    # browser reuses it; auto must stay auto across plays)
    a.db(0xA0).abs16("v_target").db(0xA2).abs16("p_target")
    a.db(0x80, 0x3E).abs16("p_target").db(0x00)
    a.db(0x75).rel8("findperf")                      # explicit choice wins
    a.db(0xC6, 0x06).abs16("p_target").db(0x03)      # default 4voice
    a.db(0xA0).abs16("fhdr", 6)                      # al = target hint
    _set_if_eq(a, 0x02, "p_target", 0x01)            # hint tandy
    _set_if_eq(a, 0x03, "p_target", 0x02)            # hint 1voice

    a.label("findperf")
    _read(a, "chdr", 8, "h_e_read", exact_to="h_e_nostream")
    a.db(0x81, 0x3E).abs16("chdr").bytes(b"PE")
    a.db(0x75).rel8("skipchunk")
    a.db(0x81, 0x3E).abs16("chdr", 2).bytes(b"RF")
    a.db(0x75).rel8("skipchunk")
    _read(a, "phdr", 16, "h_e_read")
    a.db(0xA0).abs16("phdr")                         # al = PERF target
    a.db(0x3A, 0x06).abs16("p_target")               # cmp al,[p_target]
    a.db(0x74).rel8("gotperf")
    a.db(0x8B, 0x16).abs16("chdr", 4)                # dx = size low
    a.db(0x83, 0xEA, 0x10)                           # sub dx,16 (header read)
    a.db(0x8B, 0x0E).abs16("chdr", 6)                # cx = size high
    a.db(0x83, 0xD9, 0x00)                           # sbb cx,0
    _seek_cur(a, "h_e_read")
    a.db(0xE9).rel16("findperf")
    a.label("skipchunk")
    a.db(0x8B, 0x16).abs16("chdr", 4)
    a.db(0x8B, 0x0E).abs16("chdr", 6)
    _seek_cur(a, "h_e_read")
    a.db(0xE9).rel16("findperf")

    a.label("gotperf")
    a.db(0x80, 0x3E).abs16("phdr", 1).db(0x01)       # stream version 1?
    _jne_far(a, "h_e_fmt")
    a.db(0x83, 0x3E).abs16("phdr", 14).db(0x00)      # length high word == 0?
    _jne_far(a, "h_e_big")
    a.db(0xA1).abs16("phdr", 12)                     # ax = length low
    a.db(0x3D).bytes(_w(max_stream))
    a.db(0x76, 0x03).db(0xE9).rel16("h_e_big")       # ja h_e_big
    a.db(0x89, 0xC1)                                 # cx = length
    a.db(0xBA).bytes(_w(heap))                       # dx = heap
    a.db(0x8B, 0x1E).abs16("handle").db(0xB4, 0x3F).db(0xCD, 0x21)
    a.db(0x73, 0x03).db(0xE9).rel16("h_e_read")
    a.db(0x8B, 0x1E).abs16("handle").db(0xB4, 0x3E).db(0xCD, 0x21)   # close

    # ---- choose + patch + go -------------------------------------------------
    a.db(0xA0).abs16("p_target")
    a.db(0x3C, 0x01)
    _je_far(a, "go_tandy")
    a.db(0x3C, 0x02)
    _je_far(a, "go_1voice")
    a.db(0xA0).abs16("v_force")                      # 4voice: /F fg, /X isr
    a.db(0x3C, 0x01)
    _je_far(a, "go_fg")
    a.db(0x3C, 0x02)
    _je_far(a, "go_isr")
    _emit_calibrate(a)                               # AX = PIT counts elapsed
    a.db(0x3D).bytes(_w(_CAL_THRESH))
    a.db(0x76, 0x03)                                 # jbe +3 (fast -> ISR)
    a.db(0xE9).rel16("go_fg")                        # slow CPU -> foreground
    a.db(0xE9).rel16("go_isr")

    a.label("go_tandy")                              # VU display works here too
    a.db(0x80, 0x3E).abs16("v_viz").db(0x06)
    _je_far(a, "go_tanvu")
    _emit_patch_pv(a, labels[TAN], orgs[TAN])
    a.label("go_tanvu")
    _emit_patch_pv(a, labels[TANVU], orgs[TANVU])
    a.label("go_1voice")
    a.db(0x80, 0x3E).abs16("v_viz").db(0x06)
    _je_far(a, "go_1vvu")
    _emit_patch_pv(a, labels[PC1], orgs[PC1])
    a.label("go_1vvu")
    _emit_patch_pv(a, labels[PC1VU], orgs[PC1VU])
    a.label("go_fg")                                 # restore the quit-NOPped jmp
    a.db(0xC7, 0x06).bytes(_w(labels[FG]["fgjmp"])).bytes(_w(fg_orig))
    _emit_patch_fg(a, labels[FG], orgs[FG])
    a.label("go_isr")                                # /V8 vga, /V6 vu, else text5
    a.db(0xA0).abs16("v_viz")
    a.db(0x3C, 0x08)
    _je_far(a, "go_vga")
    a.db(0x3C, 0x06)
    _je_far(a, "go_vu4")
    _emit_patch_isr(a, labels[T5], orgs[T5])
    a.label("go_vga")
    _emit_patch_isr(a, labels[VGA], orgs[VGA])
    a.label("go_vu4")
    _emit_patch_isr(a, labels[VU4], orgs[VU4])

    # ---- after a song: back to the browser, or exit in CLI mode --------------
    a.label("after_play")
    a.db(0x80, 0x3E).abs16("menu_mode").db(0x01)
    a.db(0x74, 0x05)                                 # je +5 -> menu
    a.db(0xB8, 0x00, 0x4C).db(0xCD, 0x21)            # CLI: exit clean
    a.db(0xE9).rel16("menu_entry")

    # ---- the browser ---------------------------------------------------------
    a.label("menu_entry")
    a.db(0xB8, 0x03, 0x00).db(0xCD, 0x10)            # text mode 3 (clears)
    a.db(0xC7, 0x06).abs16("fcount").bytes(_w(0))
    a.db(0x1E, 0x07)                                 # push ds; pop es (movsb)
    a.db(0xBA).abs16("patstr")                       # dx = "*.RCT"
    a.db(0x31, 0xC9)                                 # cx = 0 (normal attrs)
    a.db(0xB4, 0x4E).db(0xCD, 0x21)                  # FindFirst
    a.db(0x73).rel8("mf_copy")                       # found one
    a.db(0xE9).rel16("m_none")
    a.label("mf_copy")
    a.db(0xA0).abs16("fcount")                       # al = fcount (< 64)
    a.db(0xB3, 13).db(0xF6, 0xE3)                    # mov bl,13; mul bl
    a.db(0x05).abs16("ftab")                         # add ax, ftab
    a.db(0x89, 0xC7)                                 # di = slot
    a.db(0xBE, 0x9E, 0x00)                           # si = DTA+0x1E (filename)
    a.db(0xB9, 13, 0x00).db(0xF3, 0xA4)              # cx=13; rep movsb
    a.db(0xFF, 0x06).abs16("fcount")                 # fcount++
    a.db(0x83, 0x3E).abs16("fcount").db(_MAX_FILES)
    a.db(0x73).rel8("mf_done")                       # table full
    a.db(0xB4, 0x4F).db(0xCD, 0x21)                  # FindNext
    a.db(0x73).rel8("mf_copy")
    a.label("mf_done")
    a.db(0xA1).abs16("sel")                          # clamp sel < fcount
    a.db(0x3B, 0x06).abs16("fcount")
    a.db(0x72).rel8("browser")
    a.db(0xA1).abs16("fcount").db(0x48)              # sel = fcount-1
    a.db(0xA3).abs16("sel")
    a.label("browser")
    a.db(0xE8).rel16("draw_browser")
    a.label("b_key")
    a.db(0x30, 0xE4).db(0xCD, 0x16)                  # int 16h: wait key
    a.db(0x3C, 0x1B)                                 # Esc?
    _je_far(a, "b_quit")
    a.db(0x3C, 0x0D)                                 # Enter?
    _je_far(a, "b_play")
    a.db(0x80, 0xFC, 0x48)                           # up arrow?
    _je_far(a, "b_up")
    a.db(0x80, 0xFC, 0x50)                           # down arrow?
    _je_far(a, "b_down")
    a.db(0x24, 0xDF).db(0x3C, ord("S"))              # S -> settings
    _je_far(a, "settings")
    a.db(0xEB).rel8("b_key")
    a.label("b_up")
    a.db(0xA1).abs16("sel").db(0x09, 0xC0)           # ax=sel; or ax,ax
    a.db(0x74).rel8("b_up0")
    a.db(0x48).db(0xA3).abs16("sel")                 # sel--
    a.label("b_up0")
    a.db(0xEB).rel8("browser")
    a.label("b_down")
    a.db(0xA1).abs16("sel").db(0x40)                 # ax=sel+1
    a.db(0x3B, 0x06).abs16("fcount")
    a.db(0x73).rel8("b_dn0")                         # already at the end
    a.db(0xA3).abs16("sel")
    a.label("b_dn0")
    a.db(0xEB).rel8("browser")
    a.label("b_play")
    a.db(0xA0).abs16("sel")                          # al = sel (< 64)
    a.db(0xB3, 13).db(0xF6, 0xE3)                    # * 13
    a.db(0x05).abs16("ftab")
    a.db(0x89, 0xC6)                                 # si = slot
    a.db(0xBF).abs16("fname")                        # di = fname
    a.db(0x1E, 0x07)                                 # es = ds
    a.db(0xB9, 13, 0x00).db(0xF3, 0xA4)              # copy the name
    a.db(0xE9).rel16("playfile")
    a.label("b_quit")
    a.db(0xB8, 0x03, 0x00).db(0xCD, 0x10)            # leave a clean screen
    a.db(0xB8, 0x00, 0x4C).db(0xCD, 0x21)

    # ---- the settings screen -------------------------------------------------
    a.label("settings")
    a.db(0xE8).rel16("draw_settings")
    a.label("s_key")
    a.db(0x30, 0xE4).db(0xCD, 0x16)
    a.db(0x3C, 0x1B)                                 # Esc -> browser
    _je_far(a, "browser")
    a.db(0x24, 0xDF)
    a.db(0x3C, ord("T")).db(0x75).rel8("s_e")
    a.db(0xA0).abs16("v_target").db(0xFE, 0xC0)      # target: 0..3 cycle
    a.db(0x24, 0x03).db(0xA2).abs16("v_target")
    a.db(0xEB).rel8("settings_j")
    a.label("s_e")
    a.db(0x3C, ord("E")).db(0x75).rel8("s_v")
    a.db(0xA0).abs16("v_force").db(0xFE, 0xC0)       # engine: 0..2 cycle
    a.db(0x3C, 0x03).db(0x72, 0x02).db(0xB0, 0x00)
    a.db(0xA2).abs16("v_force")
    a.db(0xEB).rel8("settings_j")
    a.label("s_v")
    a.db(0x3C, ord("V")).db(0x75).rel8("s_key_j")    # display: 0->5->6->8->0
    a.db(0xA0).abs16("v_viz")
    a.db(0x3C, 0x00).db(0x74).rel8("sv5")
    a.db(0x3C, 0x05).db(0x74).rel8("sv6")
    a.db(0x3C, 0x06).db(0x74).rel8("sv8")
    a.db(0xB0, 0x00).db(0xEB).rel8("svd")
    a.label("sv5"); a.db(0xB0, 0x05).db(0xEB).rel8("svd")
    a.label("sv6"); a.db(0xB0, 0x06).db(0xEB).rel8("svd")
    a.label("sv8"); a.db(0xB0, 0x08)
    a.label("svd")
    a.db(0xA2).abs16("v_viz")
    a.label("settings_j")
    a.db(0xE9).rel16("settings")
    a.label("s_key_j")
    a.db(0xEB).rel8("s_key")

    a.label("m_none")
    a.db(0xBA).abs16("m_nofiles").db(0xB4, 0x09).db(0xCD, 0x21)
    a.db(0xB8, 0x01, 0x4C).db(0xCD, 0x21)

    # ---- drawing routines ----------------------------------------------------
    a.label("cls")                                   # clear to grey-on-black
    a.db(0xB8, 0x00, 0xB8).db(0x8E, 0xC0)            # es = 0xB800
    a.db(0x31, 0xFF)                                 # di = 0
    a.db(0xB8, 0x20, 0x07)                           # ax = attr 07, space
    a.db(0xB9).bytes(_w(2000)).db(0xF3, 0xAB)        # rep stosw
    a.db(0xC3)

    a.label("puts")                                  # si=str di=cell ah=attr
    a.db(0x53)                                       # push bx (caller's)
    a.db(0xBB, 0x00, 0xB8).db(0x8E, 0xC3)            # es = 0xB800
    a.label("puts_l")
    a.db(0xAC).db(0x08, 0xC0)                        # lodsb; or al,al
    a.db(0x74).rel8("puts_d")
    a.db(0xAB)                                       # stosw (char + attr)
    a.db(0xEB).rel8("puts_l")
    a.label("puts_d")
    a.db(0x5B).db(0xC3)                              # pop bx; ret

    # value-string lookups: al = index -> si = table + al*7
    for name, table in (("vstr_t", "t_tgt"), ("vstr_e", "t_eng"),
                        ("vstr_v", "t_viz")):
        a.label(name)
        a.db(0xB4, 0x00)                             # ah = 0
        a.db(0xB2, 0x07).db(0xF6, 0xE2)              # mov dl,7; mul dl
        a.db(0x05).abs16(table)                      # add ax, table
        a.db(0x89, 0xC6)                             # si = ax
        a.db(0xC3)
    a.label("viz_idx")                               # al 0/5/6/8 -> 0..3
    a.db(0x3C, 0x05).db(0x74).rel8("vi1")
    a.db(0x3C, 0x06).db(0x74).rel8("vi2")
    a.db(0x3C, 0x08).db(0x74).rel8("vi3")
    a.db(0xB0, 0x00).db(0xC3)
    a.label("vi1"); a.db(0xB0, 0x01).db(0xC3)
    a.label("vi2"); a.db(0xB0, 0x02).db(0xC3)
    a.label("vi3"); a.db(0xB0, 0x03).db(0xC3)

    a.label("draw_status")                           # shared browser/settings
    a.db(0xA0).abs16("v_target")
    a.db(0xE8).rel16("vstr_t")
    a.db(0xBF).bytes(_w((24 * 80 + 9) * 2)).db(0xB4, 0x0E)
    a.db(0xE8).rel16("puts")
    a.db(0xA0).abs16("v_force")
    a.db(0xE8).rel16("vstr_e")
    a.db(0xBF).bytes(_w((24 * 80 + 26) * 2)).db(0xB4, 0x0E)
    a.db(0xE8).rel16("puts")
    a.db(0xA0).abs16("v_viz")
    a.db(0xE8).rel16("viz_idx")
    a.db(0xE8).rel16("vstr_v")
    a.db(0xBF).bytes(_w((24 * 80 + 42) * 2)).db(0xB4, 0x0E)
    a.db(0xE8).rel16("puts")
    a.db(0xC3)

    a.label("draw_browser")
    a.db(0xE8).rel16("cls")
    _puts_at(a, "s_title", 0, 2, 0x0F)
    _puts_at(a, "s_keys", 22, 2, 0x08)
    _puts_at(a, "s_stat", 24, 2, 0x08)
    a.db(0xE8).rel16("draw_status")
    # scroll window: top <= sel <= top+LIST_ROWS-1
    a.db(0xA1).abs16("sel")
    a.db(0x3B, 0x06).abs16("top")
    a.db(0x73).rel8("dwt1")                          # sel >= top: ok
    a.db(0xA3).abs16("top")
    a.label("dwt1")
    a.db(0xA1).abs16("top").db(0x05).bytes(_w(_LIST_ROWS - 1))
    a.db(0x3B, 0x06).abs16("sel")
    a.db(0x73).rel8("dwt2")                          # sel <= top+N-1: ok
    a.db(0xA1).abs16("sel").db(0x2D).bytes(_w(_LIST_ROWS - 1))
    a.db(0xA3).abs16("top")
    a.label("dwt2")
    a.db(0xBD, 0x00, 0x00)                           # bp = row i
    a.label("dw_row")
    a.db(0xA1).abs16("top").db(0x01, 0xE8)           # ax = top + i
    a.db(0x3B, 0x06).abs16("fcount")
    a.db(0x73).rel8("dw_done")                       # past the end
    a.db(0x50)                                       # push idx
    a.db(0xB3, 13).db(0xF6, 0xE3)                    # * 13
    a.db(0x05).abs16("ftab")
    a.db(0x89, 0xC6)                                 # si = name
    a.db(0x89, 0xE8)                                 # ax = bp (row i)
    a.db(0x05, 0x02, 0x00)                           # + 2 (list starts row 2)
    a.db(0xB2, 160).db(0xF6, 0xE2)                   # * 160 bytes/row
    a.db(0x05, 0x08, 0x00)                           # + col 4 (cell*2)
    a.db(0x89, 0xC7)                                 # di = cell
    a.db(0x58)                                       # pop idx
    a.db(0x3B, 0x06).abs16("sel")
    a.db(0x74).rel8("dw_sel")
    a.db(0xB4, 0x07)                                 # normal attr
    a.db(0xEB).rel8("dw_put")
    a.label("dw_sel")
    a.db(0x56).db(0x57)                              # push si, di
    a.db(0x83, 0xEF, 0x04)                           # marker two cells left
    a.db(0xBE).abs16("s_mark").db(0xB4, 0x0A)
    a.db(0xE8).rel16("puts")
    a.db(0x5F).db(0x5E)                              # pop di, si
    a.db(0xB4, 0x0F)                                 # bright attr
    a.label("dw_put")
    a.db(0xE8).rel16("puts")
    a.db(0x45)                                       # bp++
    a.db(0x83, 0xFD, _LIST_ROWS)                     # all rows drawn?
    a.db(0x72).rel8("dw_row")
    a.label("dw_done")
    a.db(0xC3)

    a.label("draw_settings")
    a.db(0xE8).rel16("cls")
    _puts_at(a, "s_settitle", 0, 2, 0x0F)
    _puts_at(a, "s_lt", 4, 4, 0x07)
    _puts_at(a, "s_le", 6, 4, 0x07)
    _puts_at(a, "s_lv", 8, 4, 0x07)
    _puts_at(a, "s_n1", 12, 4, 0x08)
    _puts_at(a, "s_n2", 13, 4, 0x08)
    _puts_at(a, "s_n3", 14, 4, 0x08)
    _puts_at(a, "s_back", 17, 4, 0x08)
    a.db(0xA0).abs16("v_target")
    a.db(0xE8).rel16("vstr_t")
    a.db(0xBF).bytes(_w((4 * 80 + 18) * 2)).db(0xB4, 0x0E)
    a.db(0xE8).rel16("puts")
    a.db(0xA0).abs16("v_force")
    a.db(0xE8).rel16("vstr_e")
    a.db(0xBF).bytes(_w((6 * 80 + 18) * 2)).db(0xB4, 0x0E)
    a.db(0xE8).rel16("puts")
    a.db(0xA0).abs16("v_viz")
    a.db(0xE8).rel16("viz_idx")
    a.db(0xE8).rel16("vstr_v")
    a.db(0xBF).bytes(_w((8 * 80 + 18) * 2)).db(0xB4, 0x0E)
    a.db(0xE8).rel16("puts")
    a.db(0xC3)

    # ---- error handlers (menu mode: show, wait, back to the browser) ---------
    for name, msg in (("usage", "usage: RCPLAY [SONG.RCT] [/T|/1|/4] [/F|/X] "
                       "[/V5|/V6|/V8]\r\n(no file: browse this directory)$"),
                      ("e_open", "cannot open file - press a key$"),
                      ("e_fmt", "not an RCT v1 file - press a key$"),
                      ("e_read", "read error - press a key$"),
                      ("e_big", "stream too large for this player - press a key$"),
                      ("e_nostream", "no performance stream for this target "
                       "(re-save the file) - press a key$")):
        a.label("h_" + name)
        a.db(0xB8, 0x03, 0x00).db(0xCD, 0x10)        # clean text screen
        a.db(0xBA).abs16("m_" + name)
        a.db(0xB4, 0x09).db(0xCD, 0x21)              # print
        a.db(0x80, 0x3E).abs16("menu_mode").db(0x01)
        a.db(0x75).rel8("hx_" + name)                # CLI -> exit(1)
        a.db(0x30, 0xE4).db(0xCD, 0x16)              # wait a key
        a.db(0xE9).rel16("menu_entry")
        a.label("hx_" + name)
        a.db(0xB8, 0x01, 0x4C).db(0xCD, 0x21)
        a.label("m_" + name)
        a.bytes(msg.encode("cp437"))

    # ---- strings + variables -------------------------------------------------
    def strz(label, text):
        a.label(label)
        a.bytes(text.encode("cp437") + b"\x00")

    strz("s_title", "RCPLAY 1.1 - RetroComputerist Tracker player")
    strz("s_mark", "\x10")                            # CP437 ► pointer
    strz("s_keys", "\x18\x19 select   ENTER play   S settings   ESC quit")
    strz("s_stat", "TARGET .......   ENGINE .....   DISPLAY .....")
    strz("s_settitle", "RCPLAY SETTINGS")
    strz("s_lt", "T)  target  :")
    strz("s_le", "E)  engine  :")
    strz("s_lv", "V)  display :")
    strz("s_n1", "target AUTO follows the song's own hint")
    strz("s_n2", "engine applies to the 4-voice target: AUTO measures the CPU")
    strz("s_n3", "display VU works on every target; TXT5/VGA are 4-voice")
    strz("s_back", "T / E / V change   ESC back")
    a.label("patstr")
    a.bytes(b"*.RCT\x00")
    a.label("m_nofiles")
    a.bytes(b"no .RCT files in this directory\r\n$")
    for label, vals in (("t_tgt", ("AUTO", "TANDY", "1VOICE", "4VOICE")),
                        ("t_eng", ("AUTO", "FGND", "ISR")),
                        ("t_viz", ("DFLT", "TXT5", "VU", "VGA"))):
        a.label(label)
        for v in vals:
            a.bytes(v.encode("ascii").ljust(6, b"\x20") + b"\x00")

    a.label("handle"); a.db(0, 0)
    a.label("menu_mode"); a.db(0)
    a.label("v_target"); a.db(0)                     # 0 auto / 1 tandy / 2 1v / 3 4v
    a.label("v_force"); a.db(0)                      # 0 auto / 1 fg / 2 isr
    a.label("v_viz"); a.db(0)                        # 0 default / 5 / 6 / 8
    a.label("p_target"); a.db(0)                     # per-play resolved target
    a.label("sel"); a.db(0, 0)
    a.label("top"); a.db(0, 0)
    a.label("fcount"); a.db(0, 0)
    a.label("fhdr"); a.bytes(bytes(16))
    a.label("chdr"); a.bytes(bytes(8))
    a.label("phdr"); a.bytes(bytes(16))
    a.label("fname"); a.bytes(bytes(80))
    a.label("ftab"); a.bytes(bytes(_MAX_FILES * 13))
    return a.resolve(), dict(a.labels)


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
