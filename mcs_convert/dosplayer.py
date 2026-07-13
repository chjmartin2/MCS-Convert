"""Build a standalone DOS `.COM` that plays a Song on real/emulated hardware.

The `.COM` is a tiny 16-bit real-mode program: a hand-assembled player engine
followed by an appended event stream. No assembler toolchain is needed at build
time — a minimal two-pass assembler (`_Asm`) resolves labels here in Python.

Targets (phase 1):
  * "tandy"  — the TI SN76489 PSG on a Tandy 1000 / PCjr (I/O port 0xC0): three
               real square voices. We use up to 3 tone channels.
  * "1voice" — the PC speaker (8253 timer channel 2 -> port 0x61): one square
               voice, so the song is reduced to its top monophonic line.

Engine: reprogram PIT channel 0 to fire IRQ0 (INT 8) at (a submultiple of) the
song's 32nd-tick rate; the ISR walks a time-sorted stream of (port, value)
register writes — one record per tick — poking the chip. The whole musical
decision (which registers, when) is precomputed here in Python; the ISR is a
dumb player. Press any key to quit; the program restores the timer, the INT 8
vector, and silences the hardware before returning to DOS.

Phase 2 (not here yet): "4voice" — a 1-bit PWM multiplex of the PC speaker.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Tuple

from .audio import _allocate_voices, _note_events, midi_to_freq
from .mcs.reader import tick_seconds_for
from .model import Song

MODES = ("tandy", "1voice")

# --- I/O ports ---------------------------------------------------------------
_SN76489 = 0xC0          # Tandy/PCjr PSG (write-only)
_PIT_CH0 = 0x40          # system timer, drives IRQ0 (tempo)
_PIT_CH2 = 0x42          # gated to the PC speaker
_PIT_CMD = 0x43
_SPEAKER = 0x61          # bit0 = timer2 gate, bit1 = speaker data enable
_PIT_HZ = 1193182.0
_SN_HZ = 3579545.0       # SN76489 reference clock (÷32 per step)


# --- register encoders (one drum hit / note -> (port, value) writes) --------

def _tandy_note_on(ch: int, freq: float) -> List[Tuple[int, int]]:
    """SN76489: set channel `ch` (0..2) to `freq` at full volume."""
    n = max(1, min(1023, round(_SN_HZ / (32.0 * freq))))
    latch = 0x80 | (ch << 5) | (n & 0x0F)          # 1 cc 0 dddd : freq low nibble
    data = (n >> 4) & 0x3F                          # 0 dddddd     : freq high bits
    att = 0x80 | (ch << 5) | 0x10 | 0x00            # 1 cc 1 0000  : attenuation 0
    return [(_SN76489, latch), (_SN76489, data), (_SN76489, att)]


def _tandy_note_off(ch: int) -> List[Tuple[int, int]]:
    return [(_SN76489, 0x80 | (ch << 5) | 0x10 | 0x0F)]   # attenuation 15 = silent


def _spk_note_on(freq: float) -> List[Tuple[int, int]]:
    div = max(1, min(65535, round(_PIT_HZ / freq)))
    return [(_PIT_CMD, 0xB6),                       # ch2, lo/hi, mode 3 (square)
            (_PIT_CH2, div & 0xFF), (_PIT_CH2, (div >> 8) & 0xFF),
            (_SPEAKER, 0x03)]                        # gate + data enable
    #                                                  (0x61 writes preserve the
    #                                                   high bits — see the ISR)


def _spk_note_off() -> List[Tuple[int, int]]:
    return [(_SPEAKER, 0x00)]                        # clear the low 2 bits -> quiet


# --- event stream ------------------------------------------------------------

def _tandy_stream(song: Song) -> Dict[int, List[Tuple[int, int]]]:
    """Per-tick (port,value) writes for the 3 SN76489 tone voices."""
    per_track = [_note_events(t.notes) for t in song.tracks]
    voices = _allocate_voices(per_track, n=3)
    by_tick: Dict[int, List[Tuple[int, int]]] = {}
    for ch, voice in enumerate(voices):
        starts = {e[0] for e in voice}
        for start, dur, midi in voice:
            by_tick.setdefault(start, []).extend(
                _tandy_note_on(ch, midi_to_freq(midi)))
            off = start + dur
            if off not in starts:                   # a new note here would re-set it
                by_tick.setdefault(off, []).extend(_tandy_note_off(ch))
    return by_tick


def _mono_stream(song: Song) -> Dict[int, List[Tuple[int, int]]]:
    """Per-tick writes for the single PC-speaker voice: at each tick the highest
    pitch sounding wins; emit only when that pitch changes (a rest = note off)."""
    events = [e for t in song.tracks for e in _note_events(t.notes)]
    total = max((s + d for s, d, _ in events), default=0)
    by_tick: Dict[int, List[Tuple[int, int]]] = {}
    prev = None
    for t in range(total):
        top = max((m for s, d, m in events if s <= t < s + d), default=None)
        if top == prev:
            continue
        prev = top
        by_tick[t] = _spk_note_off() if top is None else _spk_note_on(midi_to_freq(top))
    return by_tick


def _build_stream(song: Song, mode: str) -> Tuple[bytes, int]:
    """(stream bytes, total_ticks). One record per tick: [pair_count][port,val]*."""
    by_tick = _tandy_stream(song) if mode == "tandy" else _mono_stream(song)
    total = max((n.end_tick for t in song.tracks for n in t.notes), default=0)
    out = bytearray()
    for t in range(total):
        pairs = by_tick.get(t, [])
        if len(pairs) > 255:
            raise ValueError(f"tick {t} has {len(pairs)} register writes (max 255)")
        out.append(len(pairs))
        for port, value in pairs:
            out += bytes([port & 0xFF, value & 0xFF])
    return bytes(out), total


# --- minimal two-pass assembler ---------------------------------------------

class _Asm:
    """Emit 16-bit machine code with symbolic labels. Absolute refs (`abs16`)
    become the label's in-segment offset (org 0x100); `rel8`/`rel16` become
    signed displacements from the end of the operand."""

    def __init__(self, org: int = 0x100):
        self.org = org
        self.buf = bytearray()
        self.labels: Dict[str, int] = {}
        self.fixups: List[Tuple[int, str, str]] = []

    def label(self, name: str) -> None:
        self.labels[name] = self.org + len(self.buf)

    def db(self, *b: int) -> "_Asm":
        self.buf += bytes(b)
        return self

    def abs16(self, name: str) -> "_Asm":
        self.fixups.append((len(self.buf), "abs16", name))
        self.buf += b"\x00\x00"
        return self

    def rel8(self, name: str) -> "_Asm":
        self.fixups.append((len(self.buf), "rel8", name))
        self.buf += b"\x00"
        return self

    def rel16(self, name: str) -> "_Asm":
        self.fixups.append((len(self.buf), "rel16", name))
        self.buf += b"\x00\x00"
        return self

    def bytes(self, raw: bytes) -> "_Asm":
        self.buf += raw
        return self

    def resolve(self) -> bytes:
        for pos, kind, name in self.fixups:
            target = self.labels[name]
            if kind == "abs16":
                self.buf[pos:pos + 2] = struct.pack("<H", target)
            elif kind == "rel8":
                disp = target - (self.org + pos + 1)
                if not -128 <= disp <= 127:
                    raise ValueError(f"rel8 to {name} out of range ({disp})")
                self.buf[pos] = disp & 0xFF
            else:                                    # rel16
                disp = target - (self.org + pos + 2)
                self.buf[pos:pos + 2] = struct.pack("<h", disp)
        return bytes(self.buf)


def _assemble(divider: int, subdiv: int, total_ticks: int,
              silence: bytes, stream: bytes) -> bytes:
    """The engine: install ISR, run the timer, wait for a key, tear down; then
    the appended silence record and per-tick event stream."""
    a = _Asm()
    # ---- start: save + hook INT 8, program the tick timer -------------------
    a.db(0xFA)                                       # cli
    a.db(0x31, 0xC0).db(0x8E, 0xC0)                  # xor ax,ax ; mov es,ax
    a.db(0x26, 0xA1, 0x20, 0x00).db(0xA3).abs16("old_off")   # mov ax,[es:0x20];mov[old_off],ax
    a.db(0x26, 0xA1, 0x22, 0x00).db(0xA3).abs16("old_seg")   # mov ax,[es:0x22];mov[old_seg],ax
    a.db(0x26, 0xC7, 0x06, 0x20, 0x00).abs16("isr")  # mov word[es:0x20], isr
    a.db(0x26, 0x8C, 0x0E, 0x22, 0x00)               # mov [es:0x22], cs
    a.db(0xB8).abs16("stream").db(0xA3).abs16("streamptr")   # mov ax,stream;mov[streamptr],ax
    a.db(0xB8).bytes(struct.pack("<H", total_ticks)).db(0xA3).abs16("ticksleft")
    a.db(0xB0, 0x36).db(0xE6, _PIT_CMD)              # mov al,0x36 ; out 0x43,al
    a.db(0xB8).bytes(struct.pack("<H", divider))     # mov ax, divider
    a.db(0xE6, _PIT_CH0).db(0x88, 0xE0).db(0xE6, _PIT_CH0)   # out 0x40,al;mov al,ah;out 0x40,al
    a.db(0xBE).abs16("silence").db(0xE8).rel16("playrec")    # silence the chip now
    a.db(0xFB)                                       # sti
    # ---- wait for a keypress ------------------------------------------------
    a.label("wait")
    a.db(0xB4, 0x01).db(0xCD, 0x16)                  # mov ah,1 ; int 0x16
    a.db(0x74).rel8("wait")                          # jz wait  (ZF=1: no key)
    a.db(0x30, 0xE4).db(0xCD, 0x16)                  # xor ah,ah ; int 0x16 (consume)
    # ---- finish: silence, restore timer + vector, exit ----------------------
    a.db(0xFA)                                       # cli
    a.db(0xBE).abs16("silence").db(0xE8).rel16("playrec")    # mov si,silence;call playrec
    a.db(0xB0, 0x36).db(0xE6, _PIT_CMD)              # mov al,0x36;out 0x43,al
    a.db(0x30, 0xC0).db(0xE6, _PIT_CH0).db(0xE6, _PIT_CH0)   # xor al,al;out40;out40 (div 65536)
    a.db(0x31, 0xC0).db(0x8E, 0xC0)                  # xor ax,ax;mov es,ax
    a.db(0xA1).abs16("old_off").db(0x26, 0xA3, 0x20, 0x00)   # mov ax,[old_off];mov[es:0x20],ax
    a.db(0xA1).abs16("old_seg").db(0x26, 0xA3, 0x22, 0x00)   # mov ax,[old_seg];mov[es:0x22],ax
    a.db(0xFB)                                       # sti
    a.db(0xB8, 0x00, 0x4C).db(0xCD, 0x21)            # mov ax,0x4C00 ; int 0x21
    # ---- playrec: output one [count][port,val]* record at DS:SI -------------
    a.label("playrec")
    a.db(0xAC).db(0x88, 0xC1).db(0x30, 0xED)         # lodsb;mov cl,al;xor ch,ch
    a.db(0xE3).rel8("pr_done")                       # jcxz pr_done
    a.label("pr_loop")
    a.db(0xAC).db(0x88, 0xC2).db(0x30, 0xF6).db(0xAC)        # lodsb;mov dl,al;xor dh,dh;lodsb
    a.db(0x80, 0xFA, _SPEAKER)                       # cmp dl,0x61
    a.db(0x75).rel8("pr_out")                        # jne pr_out
    a.db(0x88, 0xC4).db(0xE4, _SPEAKER).db(0x24, 0xFC).db(0x08, 0xE0)  # mov ah,al;in al,0x61;and al,0xFC;or al,ah
    a.label("pr_out")
    a.db(0xEE)                                       # out dx,al
    a.db(0xE2).rel8("pr_loop")                       # loop pr_loop
    a.label("pr_done")
    a.db(0xC3)                                       # ret
    # ---- isr: every `subdiv`th IRQ0, play one tick record -------------------
    a.label("isr")
    a.db(0x50, 0x51, 0x52, 0x56, 0x1E, 0x0E, 0x1F)   # push ax,cx,dx,si,ds ; push cs;pop ds
    a.db(0xFE, 0x0E).abs16("subcount")               # dec byte [subcount]
    a.db(0x75).rel8("isr_eoi")                       # jnz isr_eoi
    a.db(0xC6, 0x06).abs16("subcount").db(subdiv & 0xFF)     # mov byte[subcount], subdiv
    a.db(0x83, 0x3E).abs16("ticksleft").db(0x00)     # cmp word [ticksleft],0
    a.db(0x74).rel8("isr_eoi")                       # je isr_eoi
    a.db(0x8B, 0x36).abs16("streamptr")              # mov si,[streamptr]
    a.db(0xE8).rel16("playrec")                      # call playrec
    a.db(0x89, 0x36).abs16("streamptr")              # mov [streamptr],si
    a.db(0xFF, 0x0E).abs16("ticksleft")              # dec word [ticksleft]
    a.label("isr_eoi")
    a.db(0xB0, 0x20).db(0xE6, 0x20)                  # mov al,0x20 ; out 0x20,al (EOI)
    a.db(0x1F, 0x5E, 0x5A, 0x59, 0x58, 0xCF)         # pop ds,si,dx,cx,ax ; iret
    # ---- variables ----------------------------------------------------------
    a.label("old_off"); a.db(0x00, 0x00)
    a.label("old_seg"); a.db(0x00, 0x00)
    a.label("streamptr"); a.db(0x00, 0x00)
    a.label("ticksleft"); a.db(0x00, 0x00)
    a.label("subcount"); a.db(subdiv & 0xFF)
    # ---- appended data ------------------------------------------------------
    a.label("silence"); a.bytes(silence)
    a.label("stream"); a.bytes(stream)
    return a.resolve()


def _tandy_silence() -> List[Tuple[int, int]]:
    return [(_SN76489, 0x9F), (_SN76489, 0xBF),      # attenuate tone 0,1,2
            (_SN76489, 0xDF), (_SN76489, 0xFF)]      # and the noise channel


def build_com(song: Song, mode: str, tempo_byte0: int) -> bytes:
    """Assemble a `.COM` that plays `song` in the given mode at the MCS tempo."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    stream, total = _build_stream(song, mode)
    if total == 0:
        raise ValueError("nothing to play (no notes)")
    # Timer: fire at a submultiple of the tick rate so the divider fits 16 bits
    # (slow tempos exceed one PIT period); the ISR advances music every `subdiv`.
    tick_s = tick_seconds_for(tempo_byte0)
    subdiv = 1
    while round(_PIT_HZ * tick_s / subdiv) > 65535:
        subdiv += 1
    divider = round(_PIT_HZ * tick_s / subdiv)

    sil = _tandy_silence() if mode == "tandy" else _spk_note_off()
    sil_bytes = bytes([len(sil)]) + b"".join(bytes([p, v]) for p, v in sil)
    # append the silence record as one extra tick so the song self-silences at
    # its end instead of hanging the final chord until a key is pressed
    stream += sil_bytes
    total += 1
    com = _assemble(divider, subdiv, total, sil_bytes, stream)
    if len(com) > 0xFF00:
        raise ValueError(f".COM is {len(com)} bytes — too big for one segment; "
                         "shorten the song or split it")
    return com
