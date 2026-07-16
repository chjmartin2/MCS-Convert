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


# A 32nd-tick is split into this many sub-ticks. The engine runs at the sub-tick
# rate so it can ARTICULATE each note: a note sounds for most of its span then
# goes silent briefly, so the SN76489 (no attack envelope) re-attacks the next
# note instead of merging back-to-back (legato) notes into one held drone.
_SUBTICKS = 4
_NOISE_CH = 3                    # SN76489 channel 3 = noise
_DRUM_BRIGHT_MIDI = 72          # drum pitch at/above this -> bright hi-hat noise


def _artic_off(on: int, dur_ticks: int) -> int:
    """Sub-tick a note goes silent: cut ~1/8 of its span (min 1 sub-tick) so the
    gap SCALES with note length. A fixed 1-sub-tick cut was inaudible on long
    legato notes (Zelda's held triangle bass ran together); a proportional cut
    separates long notes cleanly while keeping fast repeats crisp."""
    span = dur_ticks * _SUBTICKS
    return on + max(1, span - max(1, span // 8))


# --- register encoders (one note/hit -> (port, value) writes) ----------------

def _sn_divider(freq: float) -> int:
    """SN76489 10-bit divider for `freq`, octave-shifting UP any note below the
    chip's ~109 Hz floor (divider > 1023) so it stays in TUNE (same pitch class,
    an octave higher) instead of clamping to a wrong pitch. The NES triangle bass
    routinely goes below 109 Hz, which is what made it sound out of tune."""
    while freq > 0 and round(_SN_HZ / (32.0 * freq)) > 1023:
        freq *= 2.0
    return max(1, min(1023, round(_SN_HZ / (32.0 * freq))))


def _tandy_note_on(ch: int, freq: float) -> List[Tuple[int, int]]:
    """SN76489: set tone channel `ch` (0..2) to `freq` at full volume."""
    n = _sn_divider(freq)
    latch = 0x80 | (ch << 5) | (n & 0x0F)          # 1 cc 0 dddd : freq low nibble
    data = (n >> 4) & 0x3F                          # 0 dddddd     : freq high bits
    att = 0x80 | (ch << 5) | 0x10 | 0x00            # 1 cc 1 0000  : attenuation 0
    return [(_SN76489, latch), (_SN76489, data), (_SN76489, att)]


def _tandy_note_off(ch: int) -> List[Tuple[int, int]]:
    return [(_SN76489, 0x80 | (ch << 5) | 0x10 | 0x0F)]   # attenuation 15 = silent


def _tandy_noise_on(bright: bool) -> List[Tuple[int, int]]:
    """SN76489 noise channel: white noise, bright (hi-hat, fast /512 shift) or
    dark (kick, slow /2048 shift). Re-latching the control byte retriggers the
    LFSR, giving each drum a fresh attack."""
    ctrl = 0xE0 | 0x04 | (0x00 if bright else 0x02)  # 1110 0 1 rr : white, rate
    return [(_SN76489, ctrl), (_SN76489, 0xF0)]      # + channel-3 attenuation 0
    #                                                  (0xF0 = 1 11 1 0000)


def _tandy_noise_off() -> List[Tuple[int, int]]:
    return [(_SN76489, 0xFF)]                        # channel-3 attenuation 15


def _spk_note_on(freq: float) -> List[Tuple[int, int]]:
    div = max(1, min(65535, round(_PIT_HZ / freq)))
    return [(_PIT_CMD, 0xB6),                       # ch2, lo/hi, mode 3 (square)
            (_PIT_CH2, div & 0xFF), (_PIT_CH2, (div >> 8) & 0xFF),
            (_SPEAKER, 0x03)]                        # gate + data enable
    #                                                  (0x61 writes preserve the
    #                                                   high bits — see the ISR)


def _spk_note_off() -> List[Tuple[int, int]]:
    return [(_SPEAKER, 0x00)]                        # clear the low 2 bits -> quiet


# --- event stream (all in SUB-ticks) -----------------------------------------

def _split_notes(song: Song):
    """(per-track pitched events, percussion hits [(start_tick, midi)])."""
    per_track, perc = [], []
    for t in song.tracks:
        per_track.append(_note_events([n for n in t.notes if not n.percussive]))
        for n in t.notes:
            if n.percussive and not n.is_rest:
                perc.append((n.start_tick, n.midi_note))
    return per_track, perc


# Scope "viz" records ride the same stream as audio: a pair (0xF0|ch, P) sets
# channel ch's on-screen square-wave half-period to P pixels (0 = silent). The
# player stores these to a table instead of OUTing them (ports 0xF0-0xF3 are
# never real hardware). P is derived from the note's frequency so higher notes
# draw as tighter waves; the noise channel gets a fixed buzzy period.
_VIZ_PORT = 0xF0
_NOISE_VIZ_P = 3


def _viz_period(freq: float) -> int:
    """On-screen square half-period in COLUMNS (each column = 2 screen pixels) for
    a tone of `freq`: proportional to the SN76489 divider, so pitch maps to wave
    tightness. Clamped to a drawable range."""
    n = max(1, min(1023, round(_SN_HZ / (32.0 * freq))))
    return max(1, min(50, n >> 5))


def _tandy_stream(song: Song, scope: bool = False) -> Dict[int, List[Tuple[int, int]]]:
    """Per-sub-tick writes: 3 SN76489 tone voices + the noise channel for drums.
    Every note is articulated (a short cut before the next), so fast repeats
    rat-tat-tat instead of merging. With `scope`, also emit viz records so the
    on-screen dot scopes track each channel."""
    per_track, perc = _split_notes(song)
    voices = _allocate_voices(per_track, n=3)
    by: Dict[int, List[Tuple[int, int]]] = {}
    for ch, voice in enumerate(voices):
        for start, dur, midi in voice:
            on = start * _SUBTICKS
            off = _artic_off(on, dur)               # silence briefly before the next note
            by.setdefault(on, []).extend(_tandy_note_on(ch, midi_to_freq(midi)))
            by.setdefault(off, []).extend(_tandy_note_off(ch))
            if scope:
                by.setdefault(on, []).append((_VIZ_PORT | ch, _viz_period(midi_to_freq(midi))))
                by.setdefault(off, []).append((_VIZ_PORT | ch, 0))
    seen = set()
    for start, midi in perc:                        # drums -> noise channel
        on = start * _SUBTICKS
        if on in seen:                              # one noise hit per sub-tick
            continue
        seen.add(on)
        off = on + max(1, _SUBTICKS - 1)
        by.setdefault(on, []).extend(_tandy_noise_on(midi >= _DRUM_BRIGHT_MIDI))
        by.setdefault(off, []).extend(_tandy_noise_off())
        if scope:
            by.setdefault(on, []).append((_VIZ_PORT | _NOISE_CH, _NOISE_VIZ_P))
            by.setdefault(off, []).append((_VIZ_PORT | _NOISE_CH, 0))
    return by


def _mono_stream(song: Song) -> Dict[int, List[Tuple[int, int]]]:
    """Per-sub-tick writes for the single PC-speaker voice: play the highest
    voice (channel 0), articulated so repeated notes re-attack."""
    per_track, _ = _split_notes(song)
    voice = _allocate_voices(per_track, n=3)[0]     # channel 0 = the top line
    by: Dict[int, List[Tuple[int, int]]] = {}
    for start, dur, midi in voice:
        on = start * _SUBTICKS
        off = _artic_off(on, dur)
        by.setdefault(on, []).extend(_spk_note_on(midi_to_freq(midi)))
        by.setdefault(off, []).extend(_spk_note_off())
    return by


def _build_stream(song: Song, mode: str, scope: bool = False) -> Tuple[bytes, int]:
    """(stream bytes, total_subticks). One record per sub-tick: [n][port,val]*."""
    by = _tandy_stream(song, scope) if mode == "tandy" else _mono_stream(song)
    ticks = max((n.end_tick for t in song.tracks for n in t.notes), default=0)
    total = ticks * _SUBTICKS
    out = bytearray()
    for s in range(total):
        pairs = by.get(s, [])
        if len(pairs) > 255:
            raise ValueError(f"sub-tick {s} has {len(pairs)} writes (max 255)")
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

    def abs16(self, name: str, off: int = 0) -> "_Asm":
        self.fixups.append((len(self.buf), "abs16", name, off))
        self.buf += b"\x00\x00"
        return self

    def rel8(self, name: str) -> "_Asm":
        self.fixups.append((len(self.buf), "rel8", name, 0))
        self.buf += b"\x00"
        return self

    def rel16(self, name: str) -> "_Asm":
        self.fixups.append((len(self.buf), "rel16", name, 0))
        self.buf += b"\x00\x00"
        return self

    def bytes(self, raw: bytes) -> "_Asm":
        self.buf += raw
        return self

    def resolve(self) -> bytes:
        for pos, kind, name, off in self.fixups:
            target = self.labels[name] + off
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


# Scope layout: a spaced 2x2 GRID of the four channel scopes, each inside a white
# frame with black margins, then a wide master trace framed below (like the GUI
# player's scope view). Drawing is DIRECT into the Tandy mode-9 packed/banked
# back buffer at BYTE granularity: each sample is one byte = two pixels, both
# nibbles the same colour, so a plot is a single store. rowaddr[y] (baked table)
# gives a scanline's byte offset (bank y&3 at (y>>2)*160). Waves are drawn by
# connecting consecutive samples with a vertical line (vline) -- the same way for
# every scope, so square edges stay crisp.
_SCROLL_SPEED = 2               # columns the waves scroll per frame
_NOISE_SEED = 0xACE1
_CHW = 70                       # channel scope width in byte columns; L = 0..69
_TOP = (10, 50, 30)             # top-row band: hi_y, lo_y, cen_y (drawn y 10..50)
_BOT = (70, 110, 90)            # bottom-row band
# (hi_y, lo_y, cen_y, packed colour, left column). left col 4, right col 86.
_CH = [(*_TOP, 0xEE, 4),        # ch0 yellow      top-left
       (*_TOP, 0x44, 86),       # ch1 dark red    top-right
       (*_BOT, 0x11, 4),        # ch2 dark blue   bottom-left
       (*_BOT, 0xAA, 86)]       # ch3 noise bright green  bottom-right
_NOISE_CEN = 90                 # noise band centre (spikes go both ways from here)
_MASTER_CEN_Y = 158
# Master = the 3 tone levels (±1 each) summed, each divided by 3 -> discrete
# bands: y = 158 - sum*12, so sum -3/-1/+1/+3 lands on 4 levels (194/170/146/122),
# max only when all three are high, min when all three are low. Noise is excluded.
_MASTER_K = 12
# white frames (x0, x1, y0, y1): the four scopes + the master (aligned to the grid
# on both sides: left x=2, right x=157).
_FRAMES = [(2, 75, 6, 54), (84, 157, 6, 54),
           (2, 75, 66, 114), (84, 157, 66, 114),
           (2, 157, 120, 196)]
_ROWADDR = [(y & 3) * 0x2000 + (y >> 2) * 160 for y in range(200)]


def _w(v: int) -> bytes:
    return struct.pack("<H", v)


def _emit_ploty(a: "_Asm") -> None:
    """Plot one packed sample: byte at rowaddr[CX] + BX = DL. (CX=y, DL=packed
    colour, BX=column 0..159, ES=back buffer.) Clobbers SI, DI only."""
    a.label("ploty")
    a.db(0x89, 0xCE).db(0x01, 0xF6)                     # mov si,cx; add si,si (y*2)
    a.db(0x8B, 0xBC).abs16("rowaddr")                   # mov di,[rowaddr+si]
    a.db(0x01, 0xDF).db(0x26, 0x88, 0x15)               # add di,bx; mov es:[di],dl
    a.db(0xC3)                                          # ret


def _emit_vline(a: "_Asm") -> None:
    """Vertical line from [vtop] down to [vbot] (inclusive, vtop<=vbot) at column
    BX, colour DL. Clobbers AX, CX, SI, DI."""
    a.label("vline")
    a.db(0xA1).abs16("vtop")                            # mov ax,[vtop]
    a.label("vl_lp")
    a.db(0x89, 0xC1).db(0xE8).rel16("ploty")            # mov cx,ax; call ploty
    a.db(0x40).db(0x3B, 0x06).abs16("vbot")             # inc ax; cmp ax,[vbot]
    a.db(0x76).rel8("vl_lp")                            # jbe vl_lp
    a.db(0xC3)                                          # ret


def _emit_hline(a: "_Asm") -> None:
    """Horizontal line at y=CX from column BX to [hend]-1, colour DL. Clobbers
    BX, SI, DI (CX/y preserved)."""
    a.label("hline")
    a.label("hl_lp")
    a.db(0xE8).rel16("ploty")                           # call ploty
    a.db(0x43).db(0x3B, 0x1E).abs16("hend")             # inc bx; cmp bx,[hend]
    a.db(0x72).rel8("hl_lp")                            # jb hl_lp
    a.db(0xC3)                                          # ret


def _emit_frame(a: "_Asm", x0: int, x1: int, y0: int, y1: int) -> None:
    """Draw a white rectangle outline (top/bottom via hline, sides via vline)."""
    a.db(0xB2, 0xFF)                                    # mov dl, white
    a.db(0xC7, 0x06).abs16("hend").bytes(_w(x1 + 1))    # mov word[hend], x1+1
    a.db(0xBB).bytes(_w(x0)).db(0xB9).bytes(_w(y0)).db(0xE8).rel16("hline")   # top
    a.db(0xBB).bytes(_w(x0)).db(0xB9).bytes(_w(y1)).db(0xE8).rel16("hline")   # bottom
    a.db(0xC7, 0x06).abs16("vtop").bytes(_w(y0))        # mov word[vtop], y0
    a.db(0xC7, 0x06).abs16("vbot").bytes(_w(y1))        # mov word[vbot], y1
    a.db(0xBB).bytes(_w(x0)).db(0xE8).rel16("vline")    # left
    a.db(0xBB).bytes(_w(x1)).db(0xE8).rel16("vline")    # right


def _emit_channel_draw(a: "_Asm", ch: int) -> None:
    """One tone channel per column (BX = its screen column): pick this column's y
    (HI/LO while sounding, CENTRE when silent), add ±1 to the master sum (HI =
    up = +1), advance its scroll counter, then connect the previous column's y to
    this one with a vline -- so the square is solid and edges stay crisp."""
    hi, lo, cen, packed, _cs = _CH[ch]
    a.db(0xB2, packed)                                  # mov dl, packed colour
    a.db(0x80, 0x3E).abs16("viz", ch).db(0x00)          # cmp byte[viz+ch],0
    a.db(0x75).rel8(f"c{ch}_s")                         # jne snd
    a.db(0xB9).bytes(_w(cen))                           # mov cx,cen_y
    a.db(0xEB).rel8(f"c{ch}_y")                         # jmp have_y
    a.label(f"c{ch}_s")
    a.db(0x80, 0x3E).abs16("lev", ch).db(0x00)          # cmp byte[lev+ch],0
    a.db(0x74).rel8(f"c{ch}_lo")                        # je lo
    a.db(0xB9).bytes(_w(hi)).db(0xFE, 0x06).abs16("msum")   # mov cx,hi_y; inc byte[msum] (up)
    a.db(0xEB).rel8(f"c{ch}_a")                         # jmp adv
    a.label(f"c{ch}_lo")
    a.db(0xB9).bytes(_w(lo)).db(0xFE, 0x0E).abs16("msum")   # mov cx,lo_y; dec byte[msum]
    a.label(f"c{ch}_a")
    a.db(0xFE, 0x0E).abs16("cnt", ch)                   # dec byte[cnt+ch]
    a.db(0x75).rel8(f"c{ch}_y")                         # jnz have_y
    a.db(0x80, 0x36).abs16("lev", ch).db(0x01)          # xor byte[lev+ch],1 (flip)
    a.db(0xA0).abs16("viz", ch).db(0xA2).abs16("cnt", ch)   # mov al,[viz+ch];mov[cnt+ch],al
    a.label(f"c{ch}_y")
    a.db(0xA1).abs16("prevy", ch * 2)                   # mov ax,[prevy+ch] (previous y)
    a.db(0x89, 0x0E).abs16("prevy", ch * 2)             # mov [prevy+ch],cx (save this y)
    a.db(0x39, 0xC8).db(0x76).rel8(f"c{ch}_o").db(0x91)  # cmp ax,cx; jbe o; xchg ax,cx
    a.label(f"c{ch}_o")
    a.db(0xA3).abs16("vtop").db(0x89, 0x0E).abs16("vbot")   # vtop=top; vbot=bottom
    a.db(0xE8).rel16("vline")                           # call vline


def _emit_noise_draw(a: "_Asm") -> None:
    """Channel 3 is NOISE: a single-colour spike from the band centre to a random
    y that swings BOTH ways (an evolving LCG seed makes it shimmer). Silent -> a
    flat centre line. Contributes its spike direction (±1) to the master sum."""
    _, _, cen, packed, _cs = _CH[3]
    a.db(0xB2, packed)                                  # mov dl, packed colour
    a.db(0x80, 0x3E).abs16("viz", 3).db(0x00)           # cmp byte[viz+3],0
    a.db(0x75).rel8("n_act")                            # jne active
    a.db(0xB9).bytes(_w(cen)).db(0xE8).rel16("ploty")   # mov cx,cen; call ploty (flat)
    a.db(0xEB).rel8("n_done")                           # jmp done
    a.label("n_act")
    a.db(0xA1).abs16("seed").db(0xB9, 0x55, 0x62).db(0xF7, 0xE1)  # mov ax,[seed];mov cx,25173;mul cx
    a.db(0x05, 0x19, 0x36).db(0xA3).abs16("seed")       # add ax,13849; mov [seed],ax
    a.db(0x88, 0xE1).db(0x80, 0xE1, 0x1F).db(0x30, 0xED)   # mov cl,ah; and cl,0x1F (0..31); xor ch,ch
    a.db(0x81, 0xE9).bytes(_w(16))                      # sub cx,16  (-16..15)
    a.db(0x81, 0xC1).bytes(_w(cen))                     # add cx,cen (y = cen-16..cen+15)
    a.db(0xB8).bytes(_w(cen))                           # mov ax,cen  (noise is NOT in the master sum)
    a.db(0x39, 0xC8).db(0x76).rel8("n_o").db(0x91)      # cmp ax,cx; jbe o; xchg ax,cx
    a.label("n_o")
    a.db(0xA3).abs16("vtop").db(0x89, 0x0E).abs16("vbot")   # vtop=top; vbot=bottom
    a.db(0xB2, packed)                                  # mov dl,packed (LCG's mul clobbered DL!)
    a.db(0xE8).rel16("vline")                           # call vline (spike)
    a.label("n_done")


def _emit_master_draw(a: "_Asm") -> None:
    """Master (framed, below the grid): y = 158 - (sum of the 3 tone levels)*12,
    so it steps between discrete bands, connected column to column. Drawn 2 wide
    (10+2L, 10+2L+1; BP = L = 0..69), centred in its frame."""
    a.db(0xA0).abs16("msum").db(0x98)                   # mov al,[msum]; cbw
    a.db(0xB9).bytes(_w(_MASTER_K)).db(0xF7, 0xE9)      # mov cx,12; imul cx (ax=sum*12)
    a.db(0xB9).bytes(_w(_MASTER_CEN_Y)).db(0x29, 0xC1)  # mov cx,158; sub cx,ax (cx=y)
    a.db(0xA1).abs16("prev_my").db(0x89, 0x0E).abs16("prev_my")  # mov ax,[prev_my];mov[prev_my],cx
    a.db(0x39, 0xC8).db(0x76).rel8("m_o").db(0x91)      # cmp ax,cx; jbe m_o; xchg ax,cx
    a.label("m_o")
    a.db(0xA3).abs16("vtop").db(0x89, 0x0E).abs16("vbot")   # vtop=top; vbot=bottom
    a.db(0xB2, 0xFF)                                    # mov dl,white (imul clobbered DL!)
    a.db(0x89, 0xEB).db(0x01, 0xEB).db(0x83, 0xC3, 0x0A)    # mov bx,bp; add bx,bp; add bx,10 (=10+2L)
    a.db(0xE8).rel16("vline")                           # call vline (col 10+2L)
    a.db(0x43).db(0xE8).rel16("vline")                  # inc bx; call vline (col 10+2L+1)


def _emit_blit(a: "_Asm") -> None:
    """Copy the packed back buffer straight to the Tandy screen at B800 (both are
    already in mode-9 format, so it's a plain word copy — no packing)."""
    a.label("blit")
    a.db(0x1E)                                          # push ds
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xD8)           # mov ax,[bufseg]; mov ds,ax
    a.db(0xB8, 0x00, 0xB8).db(0x8E, 0xC0)               # mov ax,0xB800; mov es,ax
    a.db(0x31, 0xF6).db(0x31, 0xFF)                     # xor si,si; xor di,di
    a.db(0xB9).bytes(_w(16384)).db(0xF3, 0xA5)          # mov cx,16384; rep movsw (all 4 banks, 32 KB)
    a.db(0x1F).db(0xC3)                                 # pop ds; ret


def _emit_drawframe(a: "_Asm") -> None:
    """Redraw all five scopes straight into the packed back buffer, per column
    (0..159). Tone channels scroll as solid squares; ch3 is noise; the master is
    the summed trace."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # mov ax,[bufseg]; mov es,ax
    a.db(0x31, 0xFF).db(0x31, 0xC0)                     # xor di,di; xor ax,ax
    a.db(0xB9).bytes(_w(16384)).db(0xF3, 0xAB)          # mov cx,16384; rep stosw (clear all 4 banks)
    a.db(0x83, 0x06).abs16("scroll").db(_SCROLL_SPEED)  # add word[scroll], speed
    for ch in range(3):                                 # phase each tone channel by scroll
        a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)      # mov al,[viz+ch]; or al,al
        a.db(0x75).rel8(f"rst{ch}").db(0xB0, 0x01)      # jnz rst; mov al,1 (avoid /0)
        a.label(f"rst{ch}")
        a.db(0x30, 0xE4).db(0x89, 0xC3)                 # xor ah,ah; mov bx,ax (bx = period P)
        a.db(0x31, 0xD2).db(0xA1).abs16("scroll").db(0xF7, 0xF3)   # xor dx,dx;mov ax,[scroll];div bx
        a.db(0x24, 0x01).db(0xA2).abs16("lev", ch)      # and al,1; mov [lev+ch],al (quotient&1)
        a.db(0x89, 0xD8).db(0x29, 0xD0).db(0xA2).abs16("cnt", ch)  # mov ax,bx;sub ax,dx;mov[cnt+ch],al (P-rem)
        a.db(0xC7, 0x06).abs16("prevy", ch * 2).bytes(_w(_CH[ch][2]))  # prevy[ch]=cen_y
    a.db(0xC7, 0x06).abs16("prev_my").bytes(_w(_MASTER_CEN_Y))  # prev_my=158
    a.db(0x31, 0xED)                                    # xor bp,bp  (L = column 0..69)
    a.label("xloop")
    a.db(0xC6, 0x06).abs16("msum").db(0x00)             # mov byte[msum],0
    for ch in range(3):
        a.db(0xBB).bytes(_w(_CH[ch][4])).db(0x01, 0xEB)  # mov bx,col_start; add bx,bp
        _emit_channel_draw(a, ch)
    a.db(0xBB).bytes(_w(_CH[3][4])).db(0x01, 0xEB)     # mov bx,86; add bx,bp (noise)
    _emit_noise_draw(a)
    _emit_master_draw(a)
    a.db(0x45).db(0x83, 0xFD, _CHW)                    # inc bp; cmp bp,70
    a.db(0x73).rel8("xdone").db(0xE9).rel16("xloop")    # jae xdone; jmp xloop
    a.label("xdone")
    for fr in _FRAMES:                                  # white frames on top
        _emit_frame(a, *fr)
    a.db(0xC3)                                          # ret


# ---- text-mode (80x25) block scopes -----------------------------------------
# Five stacked bands (ch0-2 tone, ch3 noise, master), each 5 rows; the wave
# fills from its band's centre row toward the level with a full-block glyph, so
# it's cheap (a few hundred cell writes/frame) -- 60 fps on real Tandy hardware.
# A cell is a word: char (0xDB) in the low byte, colour attribute in the high.
_TBLK = 0xDB                    # full-block glyph
_TCEN = (2, 7, 12, 17, 22)      # band centre rows (ch0-3 + master)
_TATTR = (0x0E, 0x04, 0x01, 0x0A, 0x0F)   # yellow/dark-red/dark-blue/bright-green/white
_TAMP = 2                       # wave amplitude in rows (± from centre)
_TEXTROW = [r * 160 for r in range(25)]   # byte offset of each text row


def _tcolor(i: int) -> int:
    return _TBLK | (_TATTR[i] << 8)


def _emit_tcell(a: "_Asm") -> None:
    """Write one text cell: char+attr AX at (row CX, col BX). Clobbers SI, DI."""
    a.label("tcell")
    a.db(0x89, 0xCE).db(0x01, 0xF6)                     # mov si,cx; add si,si
    a.db(0x8B, 0xBC).abs16("textrow")                   # mov di,[textrow+si]
    a.db(0x01, 0xDF).db(0x01, 0xDF)                     # add di,bx; add di,bx (col*2)
    a.db(0x26, 0x89, 0x05).db(0xC3)                     # mov es:[di],ax ; ret


def _emit_tfill(a: "_Asm") -> None:
    """Fill rows [ftop]..[fbot] of column BX with char+attr AX."""
    a.label("tfill")
    a.db(0x8B, 0x0E).abs16("ftop")                      # mov cx,[ftop]
    a.label("tf_lp")
    a.db(0xE8).rel16("tcell")                           # call tcell
    a.db(0x41).db(0x3B, 0x0E).abs16("fbot")             # inc cx; cmp cx,[fbot]
    a.db(0x76).rel8("tf_lp")                            # jbe tf_lp
    a.db(0xC3)                                          # ret


def _emit_text_channel(a: "_Asm", ch: int) -> None:
    """One tone channel column (BP): pick HI/LO/CENTRE row from its scroll
    counter, add ±1 to the master sum, fill from centre to that row."""
    rc = _TCEN[ch]
    a.db(0x80, 0x3E).abs16("viz", ch).db(0x00)          # cmp byte[viz+ch],0
    a.db(0x75).rel8(f"tc{ch}_s")                        # jne snd
    a.db(0xB9).bytes(_w(rc))                            # mov cx,centre
    a.db(0xEB).rel8(f"tc{ch}_f")                        # jmp fill
    a.label(f"tc{ch}_s")
    a.db(0x80, 0x3E).abs16("lev", ch).db(0x00)          # cmp byte[lev+ch],0
    a.db(0x74).rel8(f"tc{ch}_lo")                       # je lo
    a.db(0xB9).bytes(_w(rc - _TAMP)).db(0xFE, 0x06).abs16("msum")   # mov cx,hi; inc [msum]
    a.db(0xEB).rel8(f"tc{ch}_a")                        # jmp adv
    a.label(f"tc{ch}_lo")
    a.db(0xB9).bytes(_w(rc + _TAMP)).db(0xFE, 0x0E).abs16("msum")   # mov cx,lo; dec [msum]
    a.label(f"tc{ch}_a")
    a.db(0xFE, 0x0E).abs16("cnt", ch)                   # dec byte[cnt+ch]
    a.db(0x75).rel8(f"tc{ch}_f")                        # jnz fill
    a.db(0x80, 0x36).abs16("lev", ch).db(0x01)          # xor byte[lev+ch],1
    a.db(0xA0).abs16("viz", ch).db(0xA2).abs16("cnt", ch)
    a.label(f"tc{ch}_f")
    a.db(0xB8).bytes(_w(rc))                            # mov ax,centre
    a.db(0x39, 0xC8).db(0x76).rel8(f"tc{ch}_o").db(0x91)  # cmp ax,cx; jbe o; xchg
    a.label(f"tc{ch}_o")
    a.db(0xA3).abs16("ftop").db(0x89, 0x0E).abs16("fbot")   # ftop=top; fbot=bottom
    a.db(0x89, 0xEB).db(0xB8).bytes(_w(_tcolor(ch))).db(0xE8).rel16("tfill")  # bx=col;ax=cc;call tfill


def _emit_text_noise(a: "_Asm") -> None:
    """Noise band: fill from centre to a random nearby row (shimmers)."""
    rc = _TCEN[3]
    a.db(0x80, 0x3E).abs16("viz", 3).db(0x00)           # cmp byte[viz+3],0
    a.db(0x75).rel8("tn_a")                             # jne active
    a.db(0xB9).bytes(_w(rc))                            # mov cx,centre
    a.db(0xEB).rel8("tn_f")                             # jmp fill
    a.label("tn_a")
    a.db(0xA1).abs16("seed").db(0xB9, 0x55, 0x62).db(0xF7, 0xE1)  # LCG
    a.db(0x05, 0x19, 0x36).db(0xA3).abs16("seed")
    a.db(0x88, 0xE1).db(0x80, 0xE1, 0x03).db(0x30, 0xED)   # cl=ah; and cl,3; xor ch,ch
    a.db(0x81, 0xE9, 0x02, 0x00)                        # sub cx,2  (-2..1)
    a.db(0x81, 0xC1).bytes(_w(rc))                      # add cx,centre
    a.label("tn_f")
    a.db(0xB8).bytes(_w(rc))                            # mov ax,centre
    a.db(0x39, 0xC8).db(0x76).rel8("tn_o").db(0x91)     # cmp ax,cx; jbe o; xchg
    a.label("tn_o")
    a.db(0xA3).abs16("ftop").db(0x89, 0x0E).abs16("fbot")
    a.db(0x89, 0xEB).db(0xB8).bytes(_w(_tcolor(3))).db(0xE8).rel16("tfill")


def _emit_text_master(a: "_Asm") -> None:
    """Master band: level = centre - clamp(sum of 3 tone levels, -2..2) -> discrete
    bands; fill from centre."""
    rc = _TCEN[4]
    a.db(0xA0).abs16("msum").db(0x98)                   # mov al,[msum]; cbw
    a.db(0x3D, 0x02, 0x00).db(0x7E, 0x03).db(0xB8, 0x02, 0x00)  # cmp ax,2; jle .c1; mov ax,2
    a.db(0x3D, 0xFE, 0xFF).db(0x7D, 0x03).db(0xB8, 0xFE, 0xFF)  # cmp ax,-2; jge .c2; mov ax,-2
    a.db(0xB9).bytes(_w(rc)).db(0x29, 0xC1)             # mov cx,centre; sub cx,ax (level)
    a.db(0xB8).bytes(_w(rc))                            # mov ax,centre
    a.db(0x39, 0xC8).db(0x76).rel8("tm_o").db(0x91)     # cmp ax,cx; jbe o; xchg
    a.label("tm_o")
    a.db(0xA3).abs16("ftop").db(0x89, 0x0E).abs16("fbot")
    a.db(0x89, 0xEB).db(0xB8).bytes(_w(_tcolor(4))).db(0xE8).rel16("tfill")


def _emit_text_drawframe(a: "_Asm") -> None:
    """Redraw the five text-mode block scopes into the 80x25 back buffer."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # es=bufseg
    a.db(0xB8, 0x20, 0x07).db(0x31, 0xFF)               # mov ax,0x0720 (space); xor di,di
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xAB)               # mov cx,2000; rep stosw (clear 80x25)
    a.db(0x83, 0x06).abs16("scroll").db(_SCROLL_SPEED)  # scroll += speed
    for ch in range(3):                                 # phase each tone channel by scroll
        a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)
        a.db(0x75).rel8(f"tr{ch}").db(0xB0, 0x01)
        a.label(f"tr{ch}")
        a.db(0x30, 0xE4).db(0x89, 0xC3)
        a.db(0x31, 0xD2).db(0xA1).abs16("scroll").db(0xF7, 0xF3)
        a.db(0x24, 0x01).db(0xA2).abs16("lev", ch)
        a.db(0x89, 0xD8).db(0x29, 0xD0).db(0xA2).abs16("cnt", ch)
    a.db(0x31, 0xED)                                    # xor bp,bp (col 0)
    a.label("txloop")
    a.db(0xC6, 0x06).abs16("msum").db(0x00)             # msum=0
    for ch in range(3):
        _emit_text_channel(a, ch)
    _emit_text_noise(a)
    _emit_text_master(a)
    a.db(0x45).db(0x83, 0xFD, 80)                       # inc bp; cmp bp,80
    a.db(0x73).rel8("txdone").db(0xE9).rel16("txloop")  # jae; jmp txloop
    a.label("txdone")
    a.db(0xC3)                                          # ret


def _emit_text_blit(a: "_Asm") -> None:
    """Copy the 80x25 text back buffer to the video page at B800."""
    a.label("blit")
    a.db(0x1E)                                          # push ds
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xD8)           # ds=bufseg
    a.db(0xB8, 0x00, 0xB8).db(0x8E, 0xC0)               # es=0xB800
    a.db(0x31, 0xF6).db(0x31, 0xFF)                     # si=0; di=0
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xA5)               # mov cx,2000; rep movsw (4000 bytes)
    a.db(0x1F).db(0xC3)                                 # pop ds; ret


# ---- text-mode 2: box-drawing (line) oscilloscope trace ---------------------
# Same 5-band layout as text 1, but the wave is a CONNECTED LINE built from
# CP437 box glyphs (179..218): each column links the previous row to this one
# with a horizontal run, a vertical edge, and corners -- a clean scope trace.
_BOX_H, _BOX_V = 0xC4, 0xB3          # - and |
_BOX_TL, _BOX_TR = 0xDA, 0xBF        # top-left (down+right), top-right (down+left)
_BOX_BL, _BOX_BR = 0xC0, 0xD9        # bottom-left (up+right), bottom-right (up+left)


def _emit_t2seg(a: "_Asm") -> None:
    """Draw the box-glyph segment linking [t2prev] to [t2cur] at column BP, colour
    [t2attr]: flat -> a horizontal run; a jump -> corners + a vertical edge."""
    a.label("t2seg")
    a.db(0x89, 0xEB)                                    # mov bx,bp (col)
    a.db(0x8A, 0x26).abs16("t2attr")                    # mov ah,[t2attr]
    a.db(0x8B, 0x0E).abs16("t2cur")                     # mov cx,[t2cur]
    a.db(0x3B, 0x0E).abs16("t2prev")                    # cmp cx,[t2prev]
    a.db(0x74).rel8("t2_flat")                          # je flat
    a.db(0x72).rel8("t2_rise")                          # jb rising (cur<prev)
    # falling: prev is top, cur is bottom
    a.db(0x8B, 0x16).abs16("t2prev")                    # mov dx,[t2prev]
    a.db(0x89, 0x16).abs16("t2top").db(0x89, 0x0E).abs16("t2bot")  # t2top=dx; t2bot=cx
    a.db(0x51).db(0x89, 0xD1).db(0xB0, _BOX_TR).db(0xE8).rel16("tcell").db(0x59)  # push cx;cx=dx;al=┐;tcell;pop cx
    a.db(0xB0, _BOX_BL).db(0xE8).rel16("tcell")         # al=└; tcell (at cur/bot)
    a.db(0xEB).rel8("t2_vfill")                         # jmp vfill
    a.label("t2_rise")                                  # rising: cur is top
    a.db(0x8B, 0x16).abs16("t2prev")                    # mov dx,[t2prev]
    a.db(0x89, 0x0E).abs16("t2top").db(0x89, 0x16).abs16("t2bot")  # t2top=cx; t2bot=dx
    a.db(0xB0, _BOX_TL).db(0xE8).rel16("tcell")         # al=┌; tcell (at cur/top)
    a.db(0x51).db(0x89, 0xD1).db(0xB0, _BOX_BR).db(0xE8).rel16("tcell").db(0x59)  # push cx;cx=dx;al=┘;tcell;pop cx
    a.label("t2_vfill")
    a.db(0x8B, 0x0E).abs16("t2top").db(0x41)            # mov cx,[t2top]; inc cx
    a.label("t2_vl")
    a.db(0x3B, 0x0E).abs16("t2bot").db(0x73).rel8("t2_done")  # cmp cx,[t2bot]; jae done
    a.db(0xB0, _BOX_V).db(0xE8).rel16("tcell")          # al=│; tcell
    a.db(0x41).db(0xEB).rel8("t2_vl")                   # inc cx; jmp vl
    a.label("t2_done")
    a.db(0xC3)                                          # ret
    a.label("t2_flat")
    a.db(0xB0, _BOX_H).db(0xE8).rel16("tcell").db(0xC3)  # al=─; tcell; ret


def _emit_t2row(a: "_Asm", ch: int) -> None:
    """Set up [t2cur]/[t2prev]/[t2attr] from the row in CX and call t2seg,
    advancing this band's previous-row so the line stays connected."""
    a.db(0x89, 0x0E).abs16("t2cur")                     # mov [t2cur],cx
    a.db(0xA1).abs16("prevrow", ch * 2).db(0xA3).abs16("t2prev")  # mov ax,[prevrow+ch];mov[t2prev],ax
    a.db(0x89, 0x0E).abs16("prevrow", ch * 2)           # mov [prevrow+ch],cx
    a.db(0xC6, 0x06).abs16("t2attr").db(_TATTR[ch])     # mov byte[t2attr],attr
    a.db(0xE8).rel16("t2seg")                           # call t2seg


def _emit_text2_channel(a: "_Asm", ch: int) -> None:
    """Tone channel: pick HI/LO/CENTRE row from its scroll counter, add ±1 to the
    master sum, draw the connected line segment."""
    rc = _TCEN[ch]
    a.db(0x80, 0x3E).abs16("viz", ch).db(0x00)          # cmp byte[viz+ch],0
    a.db(0x75).rel8(f"u{ch}_s")                         # jne snd
    a.db(0xB9).bytes(_w(rc)).db(0xEB).rel8(f"u{ch}_r")  # mov cx,centre; jmp row
    a.label(f"u{ch}_s")
    a.db(0x80, 0x3E).abs16("lev", ch).db(0x00)          # cmp byte[lev+ch],0
    a.db(0x74).rel8(f"u{ch}_lo")                        # je lo
    a.db(0xB9).bytes(_w(rc - _TAMP)).db(0xFE, 0x06).abs16("msum")   # mov cx,hi; inc [msum]
    a.db(0xEB).rel8(f"u{ch}_a")                         # jmp adv
    a.label(f"u{ch}_lo")
    a.db(0xB9).bytes(_w(rc + _TAMP)).db(0xFE, 0x0E).abs16("msum")   # mov cx,lo; dec [msum]
    a.label(f"u{ch}_a")
    a.db(0xFE, 0x0E).abs16("cnt", ch)                   # dec byte[cnt+ch]
    a.db(0x75).rel8(f"u{ch}_r")                         # jnz row
    a.db(0x80, 0x36).abs16("lev", ch).db(0x01)          # xor byte[lev+ch],1
    a.db(0xA0).abs16("viz", ch).db(0xA2).abs16("cnt", ch)
    a.label(f"u{ch}_r")
    _emit_t2row(a, ch)


def _emit_text2_noise(a: "_Asm") -> None:
    """Noise band: connect to a random nearby row (a jagged noise trace)."""
    rc = _TCEN[3]
    a.db(0x80, 0x3E).abs16("viz", 3).db(0x00)           # cmp byte[viz+3],0
    a.db(0x75).rel8("un_a")                             # jne active
    a.db(0xB9).bytes(_w(rc)).db(0xEB).rel8("un_r")      # mov cx,centre; jmp row
    a.label("un_a")
    a.db(0xA1).abs16("seed").db(0xB9, 0x55, 0x62).db(0xF7, 0xE1)  # LCG
    a.db(0x05, 0x19, 0x36).db(0xA3).abs16("seed")
    a.db(0x88, 0xE1).db(0x80, 0xE1, 0x03).db(0x30, 0xED)   # cl=ah; and cl,3; xor ch,ch
    a.db(0x81, 0xE9, 0x01, 0x00)                        # sub cx,1  (-1..2)
    a.db(0x81, 0xC1).bytes(_w(rc))                      # add cx,centre
    a.label("un_r")
    _emit_t2row(a, 3)


def _emit_text2_master(a: "_Asm") -> None:
    """Master band: row = centre - clamp(3-tone sum, -2..2) -> discrete line."""
    rc = _TCEN[4]
    a.db(0xA0).abs16("msum").db(0x98)                   # mov al,[msum]; cbw
    a.db(0x3D, 0x02, 0x00).db(0x7E, 0x03).db(0xB8, 0x02, 0x00)  # cmp ax,2; jle; mov ax,2
    a.db(0x3D, 0xFE, 0xFF).db(0x7D, 0x03).db(0xB8, 0xFE, 0xFF)  # cmp ax,-2; jge; mov ax,-2
    a.db(0xB9).bytes(_w(rc)).db(0x29, 0xC1)             # mov cx,centre; sub cx,ax (row)
    _emit_t2row(a, 4)


def _emit_text2_drawframe(a: "_Asm") -> None:
    """Redraw the five box-line scopes into the 80x25 back buffer."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # es=bufseg
    a.db(0xB8, 0x20, 0x07).db(0x31, 0xFF)               # mov ax,0x0720 (space); xor di,di
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xAB)               # mov cx,2000; rep stosw (clear)
    a.db(0x83, 0x06).abs16("scroll").db(_SCROLL_SPEED)  # scroll += speed
    for ch in range(3):                                 # phase each tone channel by scroll
        a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)
        a.db(0x75).rel8(f"ur{ch}").db(0xB0, 0x01)
        a.label(f"ur{ch}")
        a.db(0x30, 0xE4).db(0x89, 0xC3)
        a.db(0x31, 0xD2).db(0xA1).abs16("scroll").db(0xF7, 0xF3)
        a.db(0x24, 0x01).db(0xA2).abs16("lev", ch)
        a.db(0x89, 0xD8).db(0x29, 0xD0).db(0xA2).abs16("cnt", ch)
    for i in range(5):                                  # reset each band's previous row to its centre
        a.db(0xC7, 0x06).abs16("prevrow", i * 2).bytes(_w(_TCEN[i]))
    a.db(0x31, 0xED)                                    # xor bp,bp (col 0)
    a.label("txloop")
    a.db(0xC6, 0x06).abs16("msum").db(0x00)             # msum=0
    for ch in range(3):
        _emit_text2_channel(a, ch)
    _emit_text2_noise(a)
    _emit_text2_master(a)
    a.db(0x45).db(0x83, 0xFD, 80)                       # inc bp; cmp bp,80
    a.db(0x73).rel8("txdone").db(0xE9).rel16("txloop")  # jae; jmp
    a.label("txdone")
    a.db(0xC3)                                          # ret


# ---- text-mode 3: graphics-style 2x2 grid + full-width master ---------------
# The text twin of the graphics scope: the four channels in a 2x2 grid (ch0
# top-left, ch1 top-right, ch2 bottom-left, ch3/noise bottom-right) with a
# full-width master trace below, each a box-line scope inside a box-drawing
# frame. The wave rendering is text-2's connected line; only the LAYOUT changes
# -- bands sit side by side in quadrants instead of stacked full-width, so each
# needs its own centre row AND left column. The master is sampled at the
# quadrant width (38) and drawn at 2x horizontal scale to span the screen, the
# same trick the graphics master uses.
_T3_CEN = (4, 4, 13, 13, 21)          # centre row: TL, TR, BL, BR, master
_T3_COL0 = (1, 41, 1, 41)             # interior left column of each quadrant
_T3_QW = 38                           # quadrant interior width (columns)
# frames (x0, x1, y0, y1): the four quadrants + the full-width master
_T3_FRAMES = [(0, 39, 0, 8), (40, 79, 0, 8),
              (0, 39, 9, 17), (40, 79, 9, 17), (0, 79, 18, 24)]
_T3_FRAME_ATTR = 0x07                 # light grey box frames


def _emit_t3seg(a: "_Asm") -> None:
    """text-2's box-glyph segment, but at the column in [t3col] (not BP) so a band
    can live anywhere on the row -- links [t3prev] to [t3cur], colour [t3attr]."""
    a.label("t3seg")
    a.db(0x8B, 0x1E).abs16("t3col")                     # mov bx,[t3col] (col)
    a.db(0x8A, 0x26).abs16("t3attr")                    # mov ah,[t3attr]
    a.db(0x8B, 0x0E).abs16("t3cur")                     # mov cx,[t3cur]
    a.db(0x3B, 0x0E).abs16("t3prev")                    # cmp cx,[t3prev]
    a.db(0x74).rel8("t3_flat")                          # je flat
    a.db(0x72).rel8("t3_rise")                          # jb rising (cur<prev)
    # falling: prev is top, cur is bottom
    a.db(0x8B, 0x16).abs16("t3prev")                    # mov dx,[t3prev]
    a.db(0x89, 0x16).abs16("t3top").db(0x89, 0x0E).abs16("t3bot")  # t3top=dx; t3bot=cx
    a.db(0x51).db(0x89, 0xD1).db(0xB0, _BOX_TR).db(0xE8).rel16("tcell").db(0x59)  # ┐ at top
    a.db(0xB0, _BOX_BL).db(0xE8).rel16("tcell")         # └ at cur/bot
    a.db(0xEB).rel8("t3_vfill")                         # jmp vfill
    a.label("t3_rise")                                  # rising: cur is top
    a.db(0x8B, 0x16).abs16("t3prev")                    # mov dx,[t3prev]
    a.db(0x89, 0x0E).abs16("t3top").db(0x89, 0x16).abs16("t3bot")  # t3top=cx; t3bot=dx
    a.db(0xB0, _BOX_TL).db(0xE8).rel16("tcell")         # ┌ at cur/top
    a.db(0x51).db(0x89, 0xD1).db(0xB0, _BOX_BR).db(0xE8).rel16("tcell").db(0x59)  # ┘ at bot
    a.label("t3_vfill")
    a.db(0x8B, 0x0E).abs16("t3top").db(0x41)            # mov cx,[t3top]; inc cx
    a.label("t3_vl")
    a.db(0x3B, 0x0E).abs16("t3bot").db(0x73).rel8("t3_done")  # cmp cx,[t3bot]; jae done
    a.db(0xB0, _BOX_V).db(0xE8).rel16("tcell")          # al=│; tcell
    a.db(0x41).db(0xEB).rel8("t3_vl")                   # inc cx; jmp vl
    a.label("t3_done")
    a.db(0xC3)                                          # ret
    a.label("t3_flat")
    a.db(0xB0, _BOX_H).db(0xE8).rel16("tcell").db(0xC3)  # al=─; tcell; ret


def _emit_t3row(a: "_Asm", ch: int) -> None:
    """Row CX -> a connected segment for band `ch` at the pre-set [t3col]:
    remember it in prevrow[ch] so the next column links to it."""
    a.db(0x89, 0x0E).abs16("t3cur")                     # mov [t3cur],cx
    a.db(0xA1).abs16("prevrow", ch * 2).db(0xA3).abs16("t3prev")  # mov ax,[prevrow+ch];mov[t3prev],ax
    a.db(0x89, 0x0E).abs16("prevrow", ch * 2)           # mov [prevrow+ch],cx
    a.db(0xC6, 0x06).abs16("t3attr").db(_TATTR[ch])     # mov byte[t3attr],attr
    a.db(0xE8).rel16("t3seg")                           # call t3seg


def _t3_setcol(a: "_Asm", col0: int) -> None:
    """[t3col] = BP + col0 -- this band's screen column for the current sample."""
    a.db(0x89, 0xEB)                                    # mov bx,bp
    if col0 == 1:
        a.db(0x43)                                      # inc bx
    else:
        a.db(0x81, 0xC3).bytes(_w(col0))               # add bx,col0
    a.db(0x89, 0x1E).abs16("t3col")                     # mov [t3col],bx


def _emit_text3_channel(a: "_Asm", ch: int) -> None:
    """Tone channel in its quadrant: HI/LO/CENTRE row from the scroll counter,
    ±1 into the master sum, then the connected line segment at (BP + col0)."""
    rc = _T3_CEN[ch]
    a.db(0x80, 0x3E).abs16("viz", ch).db(0x00)          # cmp byte[viz+ch],0
    a.db(0x75).rel8(f"v{ch}_s")                         # jne snd
    a.db(0xB9).bytes(_w(rc)).db(0xEB).rel8(f"v{ch}_r")  # mov cx,centre; jmp row
    a.label(f"v{ch}_s")
    a.db(0x80, 0x3E).abs16("lev", ch).db(0x00)          # cmp byte[lev+ch],0
    a.db(0x74).rel8(f"v{ch}_lo")                        # je lo
    a.db(0xB9).bytes(_w(rc - _TAMP)).db(0xFE, 0x06).abs16("msum")   # mov cx,hi; inc [msum]
    a.db(0xEB).rel8(f"v{ch}_a")                         # jmp adv
    a.label(f"v{ch}_lo")
    a.db(0xB9).bytes(_w(rc + _TAMP)).db(0xFE, 0x0E).abs16("msum")   # mov cx,lo; dec [msum]
    a.label(f"v{ch}_a")
    a.db(0xFE, 0x0E).abs16("cnt", ch)                   # dec byte[cnt+ch]
    a.db(0x75).rel8(f"v{ch}_r")                         # jnz row
    a.db(0x80, 0x36).abs16("lev", ch).db(0x01)          # xor byte[lev+ch],1
    a.db(0xA0).abs16("viz", ch).db(0xA2).abs16("cnt", ch)
    a.label(f"v{ch}_r")
    _t3_setcol(a, _T3_COL0[ch])
    _emit_t3row(a, ch)


def _emit_text3_noise(a: "_Asm") -> None:
    """Noise quadrant (bottom-right): connect to a random nearby row."""
    rc = _T3_CEN[3]
    a.db(0x80, 0x3E).abs16("viz", 3).db(0x00)           # cmp byte[viz+3],0
    a.db(0x75).rel8("vn_a")                             # jne active
    a.db(0xB9).bytes(_w(rc)).db(0xEB).rel8("vn_r")      # mov cx,centre; jmp row
    a.label("vn_a")
    a.db(0xA1).abs16("seed").db(0xB9, 0x55, 0x62).db(0xF7, 0xE1)  # LCG
    a.db(0x05, 0x19, 0x36).db(0xA3).abs16("seed")
    a.db(0x88, 0xE1).db(0x80, 0xE1, 0x03).db(0x30, 0xED)   # cl=ah; and cl,3; xor ch,ch
    a.db(0x81, 0xE9, 0x01, 0x00)                        # sub cx,1  (-1..2)
    a.db(0x81, 0xC1).bytes(_w(rc))                      # add cx,centre
    a.label("vn_r")
    _t3_setcol(a, _T3_COL0[3])
    _emit_t3row(a, 3)


def _emit_t3frame(a: "_Asm") -> None:
    """Draw a box-drawing rectangle around [fx0,fy0]-[fx1,fy1] in grey. Corners,
    then the top/bottom (─) and left/right (│) edges. Uses tcell (preserves AX,
    so the attribute in AH survives the whole routine)."""
    a.label("t3frame")
    a.db(0xB4, _T3_FRAME_ATTR)                          # mov ah, grey
    a.db(0x8B, 0x0E).abs16("fy0").db(0x8B, 0x1E).abs16("fx0")  # cx=fy0; bx=fx0
    a.db(0xB0, _BOX_TL).db(0xE8).rel16("tcell")         # ┌ (fx0,fy0)
    a.db(0x8B, 0x1E).abs16("fx1").db(0xB0, _BOX_TR).db(0xE8).rel16("tcell")  # ┐ (fx1,fy0)
    a.db(0x8B, 0x0E).abs16("fy1").db(0xB0, _BOX_BR).db(0xE8).rel16("tcell")  # ┘ (fx1,fy1)
    a.db(0x8B, 0x1E).abs16("fx0").db(0xB0, _BOX_BL).db(0xE8).rel16("tcell")  # └ (fx0,fy1)
    # top edge: row fy0, cols fx0+1..fx1-1
    a.db(0xB0, _BOX_H).db(0x8B, 0x0E).abs16("fy0")      # al=─; cx=fy0
    a.db(0x8B, 0x1E).abs16("fx0").db(0x43)              # bx=fx0; inc bx
    a.label("t3f_t")
    a.db(0x3B, 0x1E).abs16("fx1").db(0x73).rel8("t3f_td")  # cmp bx,[fx1]; jae
    a.db(0xE8).rel16("tcell").db(0x43).db(0xEB).rel8("t3f_t")  # tcell; inc bx; jmp
    a.label("t3f_td")
    # bottom edge: row fy1
    a.db(0x8B, 0x0E).abs16("fy1")                       # cx=fy1
    a.db(0x8B, 0x1E).abs16("fx0").db(0x43)              # bx=fx0; inc bx
    a.label("t3f_b")
    a.db(0x3B, 0x1E).abs16("fx1").db(0x73).rel8("t3f_bd")
    a.db(0xE8).rel16("tcell").db(0x43).db(0xEB).rel8("t3f_b")
    a.label("t3f_bd")
    # left edge: col fx0, rows fy0+1..fy1-1
    a.db(0xB0, _BOX_V).db(0x8B, 0x1E).abs16("fx0")      # al=│; bx=fx0
    a.db(0x8B, 0x0E).abs16("fy0").db(0x41)              # cx=fy0; inc cx
    a.label("t3f_l")
    a.db(0x3B, 0x0E).abs16("fy1").db(0x73).rel8("t3f_ld")  # cmp cx,[fy1]; jae
    a.db(0xE8).rel16("tcell").db(0x41).db(0xEB).rel8("t3f_l")  # tcell; inc cx; jmp
    a.label("t3f_ld")
    # right edge: col fx1
    a.db(0x8B, 0x1E).abs16("fx1")                       # bx=fx1
    a.db(0x8B, 0x0E).abs16("fy0").db(0x41)              # cx=fy0; inc cx
    a.label("t3f_r")
    a.db(0x3B, 0x0E).abs16("fy1").db(0x73).rel8("t3f_rd")
    a.db(0xE8).rel16("tcell").db(0x41).db(0xEB).rel8("t3f_r")
    a.label("t3f_rd")
    a.db(0xC3)                                          # ret


def _emit_text3_drawframe(a: "_Asm") -> None:
    """Redraw the 2x2 grid + master into the 80x25 back buffer, then the frames."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # es=bufseg
    a.db(0xB8, 0x20, 0x07).db(0x31, 0xFF)               # ax=0x0720 (space); di=0
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xAB)               # cx=2000; rep stosw (clear)
    a.db(0x83, 0x06).abs16("scroll").db(_SCROLL_SPEED)  # scroll += speed
    for ch in range(3):                                 # phase each tone channel by scroll
        a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)
        a.db(0x75).rel8(f"w3{ch}").db(0xB0, 0x01)
        a.label(f"w3{ch}")
        a.db(0x30, 0xE4).db(0x89, 0xC3)
        a.db(0x31, 0xD2).db(0xA1).abs16("scroll").db(0xF7, 0xF3)
        a.db(0x24, 0x01).db(0xA2).abs16("lev", ch)
        a.db(0x89, 0xD8).db(0x29, 0xD0).db(0xA2).abs16("cnt", ch)
    for i in range(5):                                  # reset each band's previous row
        a.db(0xC7, 0x06).abs16("prevrow", i * 2).bytes(_w(_T3_CEN[i]))
    # ---- quadrant loop: 38 columns; store each column's master sum to mbuf ----
    a.db(0x31, 0xED)                                    # xor bp,bp
    a.label("q3loop")
    a.db(0xC6, 0x06).abs16("msum").db(0x00)             # msum=0
    for ch in range(3):
        _emit_text3_channel(a, ch)
    _emit_text3_noise(a)
    a.db(0xA0).abs16("msum").db(0x89, 0xEB).db(0x88, 0x87).abs16("mbuf")  # mov al,[msum];mov bx,bp;mov[mbuf+bx],al
    a.db(0x45).db(0x83, 0xFD, _T3_QW)                  # inc bp; cmp bp,38
    a.db(0x73).rel8("q3done").db(0xE9).rel16("q3loop")  # jae; jmp
    a.label("q3done")
    # ---- master loop: 76 columns (2x the sampled width), centred at col 2 ------
    a.db(0x31, 0xED)                                    # xor bp,bp
    a.label("m3loop")
    a.db(0x89, 0xEB).db(0xD1, 0xEB)                     # mov bx,bp; shr bx,1 (sample = bp/2)
    a.db(0x8A, 0x87).abs16("mbuf").db(0x98)             # mov al,[mbuf+bx]; cbw
    a.db(0x3D, 0x02, 0x00).db(0x7E, 0x03).db(0xB8, 0x02, 0x00)  # cmp ax,2; jle; mov ax,2
    a.db(0x3D, 0xFE, 0xFF).db(0x7D, 0x03).db(0xB8, 0xFE, 0xFF)  # cmp ax,-2; jge; mov ax,-2
    a.db(0xB9).bytes(_w(_T3_CEN[4])).db(0x29, 0xC1)     # mov cx,centre; sub cx,ax (row)
    a.db(0x89, 0xEB).db(0x83, 0xC3, 0x02).db(0x89, 0x1E).abs16("t3col")  # mov bx,bp;add bx,2;mov[t3col],bx
    _emit_t3row(a, 4)
    a.db(0x45).db(0x83, 0xFD, 2 * _T3_QW)             # inc bp; cmp bp,76
    a.db(0x73).rel8("m3done").db(0xE9).rel16("m3loop")  # jae; jmp
    a.label("m3done")
    # ---- frames on top ---------------------------------------------------------
    for x0, x1, y0, y1 in _T3_FRAMES:
        a.db(0xC7, 0x06).abs16("fx0").bytes(_w(x0))
        a.db(0xC7, 0x06).abs16("fx1").bytes(_w(x1))
        a.db(0xC7, 0x06).abs16("fy0").bytes(_w(y0))
        a.db(0xC7, 0x06).abs16("fy1").bytes(_w(y1))
        a.db(0xE8).rel16("t3frame")
    a.db(0xC3)                                          # ret


# ---- text-mode 4: faux spectrum analyzer ------------------------------------
# A bar-graph spectrum: each active tone deposits energy at its fundamental bin
# and its odd harmonics (3f, 5f, 7f at 1/3, 1/5, 1/7 -- the square wave's
# spectrum), noise fills the whole band, and the bars attack instantly / release
# slowly with a slow-falling peak cap. Bars are coloured by height (green ->
# yellow -> red) and use the half-height block (0xDC) for a sub-cell top edge, so
# motion is smooth. Frequency->bin and the harmonic offsets are baked into `harm`
# (a per-period table); the ISR just deposits and draws.
_S4_N = 20                            # number of frequency bars
_S4_FIRST_COL = 10                    # left column of the first bar
_S4_STRIDE = 3                        # columns per bar (2 wide + 1 gap)
_S4_BASE_ROW = 22                     # baseline (bars grow up from here)
_S4_TOP_ROW = 2
_S4_HMAX = 2 * (_S4_BASE_ROW - _S4_TOP_ROW)   # 40 half-units of range
_S4_FALL = 2                          # bar release, half-units/frame
_S4_PEAK_FALL = 1                     # peak cap release (slower)
_S4_HH = (32, 10, 6, 4)               # deposit height: fundamental, 3rd, 5th, 7th
_S4_NOISE_BASE = 8                    # noise floor height (+ 0..7 jitter)
_BLK_FULL, _BLK_LOWER, _BLK_UPPER = 0xDB, 0xDC, 0xDF   # █  ▄  ▀
_S4_GREEN, _S4_YELLOW, _S4_RED, _S4_PEAK_ATTR = 0x0A, 0x0E, 0x0C, 0x0F
_S4_FRAME = (0, 79, 0, 24)            # full-screen border


def _s4_rowcol() -> bytes:
    """Attribute for each screen row: green low, yellow mid, red high (0 outside
    the meter). A cell's colour is its ROW, so a tall bar passes through zones."""
    out = []
    for r in range(25):
        if r < _S4_TOP_ROW or r > _S4_BASE_ROW:
            out.append(0)
        elif r >= 15:
            out.append(_S4_GREEN)
        elif r >= 8:
            out.append(_S4_YELLOW)
        else:
            out.append(_S4_RED)
    return bytes(out)


def _s4_harm() -> bytes:
    """Per-period (0..50) table of 4 bin indices: the fundamental and the 3rd/5th/
    7th harmonic, each on a log-frequency axis (low period = high freq = high
    bin). A square wave's energy lives at exactly these odd harmonics."""
    import math
    n = _S4_N

    def binof(p: int) -> int:
        p = max(1, min(50, p))
        t = (math.log(50) - math.log(p)) / (math.log(50) - math.log(1))
        return max(0, min(n - 1, round(t * (n - 1))))

    out = bytearray()
    for p in range(51):
        if p == 0:
            out += bytes(4)
            continue
        for k in (1, 3, 5, 7):
            out.append(binof(max(1, round(p / k))))
    return bytes(out)


def _emit_s4_drawcol(a: "_Asm") -> None:
    """Draw one column BX of the meter: [bar_h] full/half blocks up from the
    baseline (coloured by row), then the [peak_h] white cap. Uses tcell (keeps
    BX and BP), scratches AX/CX/DX/SI/DI."""
    a.label("s4drawcol")
    a.db(0xA0).abs16("bar_h")                           # mov al,[bar_h]
    a.db(0x08, 0xC0).db(0x74).rel8("s4c_peak")          # or al,al; jz peak (empty bar)
    a.db(0x88, 0xC6).db(0xD0, 0xEE)                     # mov dh,al; shr dh,1 (dh = full rows)
    a.db(0xB2, _S4_BASE_ROW)                            # mov dl, baseline row
    a.label("s4c_full")
    a.db(0x08, 0xF6).db(0x74).rel8("s4c_part")          # or dh,dh; jz partial
    a.db(0x88, 0xD1).db(0x30, 0xED)                     # mov cl,dl; xor ch,ch (cx=row)
    a.db(0x89, 0xCE).db(0x8A, 0xA4).abs16("rowcol")     # mov si,cx; mov ah,[si+rowcol]
    a.db(0xB0, _BLK_FULL).db(0xE8).rel16("tcell")       # al=█; tcell
    a.db(0xFE, 0xCA).db(0xFE, 0xCE)                     # dec dl (up a row); dec dh
    a.db(0xEB).rel8("s4c_full")
    a.label("s4c_part")
    a.db(0xA0).abs16("bar_h").db(0xA8, 0x01).db(0x74).rel8("s4c_peak")  # test bar_h,1; jz peak
    a.db(0x88, 0xD1).db(0x30, 0xED)                     # cx=row (dl)
    a.db(0x89, 0xCE).db(0x8A, 0xA4).abs16("rowcol")     # si=cx; ah=[si+rowcol]
    a.db(0xB0, _BLK_LOWER).db(0xE8).rel16("tcell")      # al=▄ (lower half); tcell
    a.label("s4c_peak")
    a.db(0xA0).abs16("peak_h")                          # mov al,[peak_h]
    a.db(0x08, 0xC0).db(0x74).rel8("s4c_done")          # or al,al; jz done
    a.db(0xD0, 0xE8)                                    # shr al,1 (peak full-rows)
    a.db(0xB6, _S4_BASE_ROW).db(0x28, 0xC6)             # mov dh,baseline; sub dh,al (peak row)
    a.db(0x88, 0xF1).db(0x30, 0xED)                     # mov cl,dh; xor ch,ch (cx=row)
    a.db(0xB4, _S4_PEAK_ATTR).db(0xB0, _BLK_UPPER)      # ah=white; al=▀ (upper half cap)
    a.db(0xE8).rel16("tcell")
    a.label("s4c_done")
    a.db(0xC3)                                          # ret


def _emit_s4_drawframe(a: "_Asm") -> None:
    """One spectrum frame: build the target heights from the channels (fundamental
    + odd harmonics per tone, broadband for noise), advance each bar's attack/
    release and peak, draw the bars, then the border."""
    a.label("drawframe")
    # ---- clear the per-bar target heights --------------------------------------
    a.db(0x31, 0xF6)                                    # xor si,si
    a.label("s4_clr")
    a.db(0xC6, 0x84).abs16("tgt").db(0x00)              # mov byte[si+tgt],0
    a.db(0x46).db(0x83, 0xFE, _S4_N).db(0x72).rel8("s4_clr")  # inc si; cmp si,N; jb
    # ---- deposit each tone: fundamental + 3rd/5th/7th harmonic (max) ------------
    for ch in range(3):
        a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)      # mov al,[viz+ch]; or al,al
        a.db(0x74).rel8(f"s4d{ch}")                     # jz skip (silent)
        a.db(0x88, 0xC3).db(0x30, 0xFF).db(0x01, 0xDB).db(0x01, 0xDB)  # bl=al;bh=0;bx*=4 (period*4)
        for k in range(4):
            a.db(0x8A, 0x87).abs16("harm", k)           # mov al,[bx+harm+k] (bin)
            a.db(0x30, 0xE4).db(0x89, 0xC6)             # xor ah,ah; mov si,ax (si=bin)
            a.db(0xB0, _S4_HH[k])                       # mov al, deposit height
            a.db(0x3A, 0x84).abs16("tgt")               # cmp al,[si+tgt]
            a.db(0x76).rel8(f"s4n{ch}_{k}")             # jbe keep
            a.db(0x88, 0x84).abs16("tgt")               # mov [si+tgt],al (raise)
            a.label(f"s4n{ch}_{k}")
        a.label(f"s4d{ch}")
    # ---- deposit noise across every bar (a shimmering floor) --------------------
    a.db(0xA0).abs16("viz", 3).db(0x08, 0xC0)           # mov al,[viz+3]; or al,al
    a.db(0x74).rel8("s4nn")                             # jz no-noise
    a.db(0x31, 0xF6)                                    # xor si,si
    a.label("s4nl")
    a.db(0xA1).abs16("seed").db(0xB9, 0x55, 0x62).db(0xF7, 0xE1)  # LCG: ax=seed*25173
    a.db(0x05, 0x19, 0x36).db(0xA3).abs16("seed")       # +13849; store
    a.db(0x88, 0xE0).db(0x24, 0x07).db(0x04, _S4_NOISE_BASE)  # al=ah; and 7; add base
    a.db(0x3A, 0x84).abs16("tgt").db(0x76).rel8("s4n2")  # cmp al,[si+tgt]; jbe keep
    a.db(0x88, 0x84).abs16("tgt")                       # mov [si+tgt],al
    a.label("s4n2")
    a.db(0x46).db(0x83, 0xFE, _S4_N).db(0x72).rel8("s4nl")   # inc si; cmp si,N; jb
    a.label("s4nn")
    # ---- clear the screen back buffer ------------------------------------------
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # es=bufseg
    a.db(0xB8, 0x20, 0x07).db(0x31, 0xFF)               # ax=0x0720; di=0
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xAB)               # cx=2000; rep stosw
    # ---- per-bar attack/release + peak, then draw ------------------------------
    a.db(0x31, 0xED)                                    # xor bp,bp (bar index)
    a.label("s4bl")
    a.db(0x89, 0xEE)                                    # mov si,bp
    a.db(0x8A, 0x84).abs16("tgt")                       # al = target
    a.db(0x8A, 0xA4).abs16("bar")                       # ah = current
    a.db(0x38, 0xC4).db(0x72).rel8("s4atk")             # cmp ah,al; jb attack (rise instantly)
    a.db(0x80, 0xEC, _S4_FALL).db(0x73).rel8("s4set")   # sub ah,FALL; jnc set
    a.db(0x30, 0xE4).db(0xEB).rel8("s4set")             # xor ah,ah (floor 0); jmp set
    a.label("s4atk")
    a.db(0x88, 0xC4)                                    # mov ah,al (jump up to target)
    a.label("s4set")
    a.db(0x88, 0xA4).abs16("bar")                       # mov [si+bar],ah
    # peak: raise to bar, else fall slowly
    a.db(0x8A, 0x84).abs16("peak")                      # al = peak
    a.db(0x38, 0xC4).db(0x76).rel8("s4pdec")            # cmp ah,al; jbe decay
    a.db(0x88, 0xE0).db(0x88, 0x84).abs16("peak").db(0xEB).rel8("s4pdrw")  # peak=bar
    a.label("s4pdec")
    a.db(0x2C, _S4_PEAK_FALL).db(0x73).rel8("s4pset")   # sub al,PFALL; jnc
    a.db(0x30, 0xC0)                                    # xor al,al
    a.label("s4pset")
    a.db(0x88, 0x84).abs16("peak")                      # mov [si+peak],al
    a.label("s4pdrw")
    a.db(0x8A, 0x84).abs16("bar").db(0xA2).abs16("bar_h")    # bar_h = [si+bar]
    a.db(0x8A, 0x84).abs16("peak").db(0xA2).abs16("peak_h")  # peak_h = [si+peak]
    a.db(0x89, 0xE8).db(0x01, 0xE8).db(0x01, 0xE8)      # ax=bp; ax+=bp; ax+=bp (3*i)
    a.db(0x05).bytes(_w(_S4_FIRST_COL)).db(0x89, 0xC3)  # add ax,first_col; mov bx,ax (col)
    a.db(0xE8).rel16("s4drawcol")                       # draw left column
    a.db(0x43).db(0xE8).rel16("s4drawcol")              # inc bx; draw right column
    a.db(0x45).db(0x83, 0xFD, _S4_N)                   # inc bp; cmp bp,N
    a.db(0x73).rel8("s4bdone").db(0xE9).rel16("s4bl")   # jae; jmp
    a.label("s4bdone")
    # ---- border ----------------------------------------------------------------
    x0, x1, y0, y1 = _S4_FRAME
    a.db(0xC7, 0x06).abs16("fx0").bytes(_w(x0))
    a.db(0xC7, 0x06).abs16("fx1").bytes(_w(x1))
    a.db(0xC7, 0x06).abs16("fy0").bytes(_w(y0))
    a.db(0xC7, 0x06).abs16("fy1").bytes(_w(y1))
    a.db(0xE8).rel16("t3frame")
    a.db(0xC3)                                          # ret


# ---- text-mode 5: combined monitor (2x2 scopes + spectrum + VU) -------------
# One screen, three instruments watching the same signal: the top half is the
# 2x2 box-line scope grid (no master), the bottom-left is the spectrum analyzer,
# the bottom-right is four onset-kicked VU meters. Reuses t3seg (scope traces),
# the text-4 deposit/attack/peak model (spectrum), and t3frame (all borders).
_BLK_LEFT = 0xDD                      # ▌ left-half block (horizontal VU smoothing)
# -- top: 2x2 scope grid --
_T5_SCEN = (3, 3, 9, 9)               # scope centre rows: ch0, ch1, ch2, noise
_T5_SCOL0 = (1, 41, 1, 41)            # scope left columns
_T5_SQW = 38                          # scope quadrant interior width
_T5_SFRAMES = [(0, 39, 0, 6), (40, 79, 0, 6), (0, 39, 6, 12), (40, 79, 6, 12)]
# -- bottom-left: spectrum --
_T5_SPEC_FRAME = (0, 39, 12, 24)
_T5_SPEC_N = 18                       # bars
_T5_SPEC_FIRST, _T5_SPEC_STRIDE = 2, 2
_T5_SPEC_BASE, _T5_SPEC_TOP = 23, 13
_T5_SPEC_HH = (18, 6, 4, 3)           # deposit: fundamental, 3rd, 5th, 7th
_T5_SPEC_NOISE = 5
# -- bottom-right: VU meters --
_T5_VU_FRAME = (40, 79, 12, 24)
_T5_VU_ROWS = (15, 17, 19, 21)        # one horizontal meter per channel
_T5_VU_LABELS = ("P1", "P2", "Tr", "Nz")
_T5_VU_LABEL_COL = 42
_T5_VU_COL = 46                       # bar start column
_T5_VU_LEN = 30                       # bar length in cells
_T5_VU_MAX = 2 * _T5_VU_LEN           # full level in half-cells (kick target)
_T5_VU_GREEN, _T5_VU_YELLOW = 20, 26  # cell thresholds: <20 green, <26 yellow, else red
_T5_VU_FALL, _T5_VU_PEAK_FALL = 4, 1
_T5_FRAMES = _T5_SFRAMES + [_T5_SPEC_FRAME, _T5_VU_FRAME]


def _t5_spec_rowcol() -> bytes:
    """Green (low) -> yellow -> red (high) attribute per row, for the bottom-left
    spectrum box (rows 13..23)."""
    out = []
    for r in range(25):
        if r < _T5_SPEC_TOP or r > _T5_SPEC_BASE:
            out.append(0)
        elif r >= 20:
            out.append(_S4_GREEN)
        elif r >= 16:
            out.append(_S4_YELLOW)
        else:
            out.append(_S4_RED)
    return bytes(out)


def _t5_spec_harm() -> bytes:
    """Per-period fundamental + 3rd/5th/7th harmonic bins for the 18-bar box."""
    import math
    n = _T5_SPEC_N

    def binof(p: int) -> int:
        p = max(1, min(50, p))
        t = (math.log(50) - math.log(p)) / (math.log(50) - math.log(1))
        return max(0, min(n - 1, round(t * (n - 1))))

    out = bytearray()
    for p in range(51):
        if p == 0:
            out += bytes(4)
            continue
        for k in (1, 3, 5, 7):
            out.append(binof(max(1, round(p / k))))
    return bytes(out)


def _emit_t5_scope_channel(a: "_Asm", ch: int, cen: int, col0: int) -> None:
    """Top-grid tone scope: HI/LO/CENTRE row from the scroll counter (no master
    sum), then the box-line segment at (BP + col0)."""
    a.db(0x80, 0x3E).abs16("viz", ch).db(0x00)          # cmp byte[viz+ch],0
    a.db(0x75).rel8(f"z{ch}_s")                         # jne snd
    a.db(0xB9).bytes(_w(cen)).db(0xEB).rel8(f"z{ch}_r")  # mov cx,cen; jmp row
    a.label(f"z{ch}_s")
    a.db(0x80, 0x3E).abs16("lev", ch).db(0x00)          # cmp byte[lev+ch],0
    a.db(0x74).rel8(f"z{ch}_lo")                        # je lo
    a.db(0xB9).bytes(_w(cen - 2)).db(0xEB).rel8(f"z{ch}_a")  # mov cx,hi; jmp adv
    a.label(f"z{ch}_lo")
    a.db(0xB9).bytes(_w(cen + 2))                       # mov cx,lo
    a.label(f"z{ch}_a")
    a.db(0xFE, 0x0E).abs16("cnt", ch)                   # dec byte[cnt+ch]
    a.db(0x75).rel8(f"z{ch}_r")                         # jnz row
    a.db(0x80, 0x36).abs16("lev", ch).db(0x01)          # xor byte[lev+ch],1
    a.db(0xA0).abs16("viz", ch).db(0xA2).abs16("cnt", ch)
    a.label(f"z{ch}_r")
    a.db(0x89, 0xEB)                                    # mov bx,bp
    if col0 == 1:
        a.db(0x43)                                      # inc bx
    else:
        a.db(0x81, 0xC3).bytes(_w(col0))               # add bx,col0
    a.db(0x89, 0x1E).abs16("t3col")                     # mov [t3col],bx
    _emit_t3row(a, ch)


def _emit_t5_scope_noise(a: "_Asm", cen: int, col0: int) -> None:
    """Top-grid noise scope: a jagged line near the centre."""
    a.db(0x80, 0x3E).abs16("viz", 3).db(0x00)           # cmp byte[viz+3],0
    a.db(0x75).rel8("zn_a")                             # jne active
    a.db(0xB9).bytes(_w(cen)).db(0xEB).rel8("zn_r")     # mov cx,cen; jmp row
    a.label("zn_a")
    a.db(0xA1).abs16("seed").db(0xB9, 0x55, 0x62).db(0xF7, 0xE1)   # LCG
    a.db(0x05, 0x19, 0x36).db(0xA3).abs16("seed")
    a.db(0x88, 0xE1).db(0x80, 0xE1, 0x03).db(0x30, 0xED)   # cl=ah; and cl,3; xor ch,ch
    a.db(0x81, 0xE9, 0x01, 0x00).db(0x81, 0xC1).bytes(_w(cen))   # sub cx,1; add cx,cen
    a.label("zn_r")
    a.db(0x89, 0xEB)                                    # mov bx,bp
    a.db(0x81, 0xC3).bytes(_w(col0))                   # add bx,col0
    a.db(0x89, 0x1E).abs16("t3col")                     # mov [t3col],bx
    _emit_t3row(a, 3)


def _emit_t5spec_drawcol(a: "_Asm") -> None:
    """Draw one spectrum column BX: [sbar_h] blocks up from row 23 (coloured by
    row via srowcol), then the [speak_h] white cap. Like s4drawcol, 1 col wide."""
    a.label("t5sdraw")
    a.db(0xA0).abs16("sbar_h")                          # al=[sbar_h]
    a.db(0x08, 0xC0).db(0x74).rel8("t5s_peak")          # or al,al; jz peak
    a.db(0x88, 0xC6).db(0xD0, 0xEE)                     # dh=al; shr dh,1 (full rows)
    a.db(0xB2, _T5_SPEC_BASE)                           # dl=baseline row
    a.label("t5s_full")
    a.db(0x08, 0xF6).db(0x74).rel8("t5s_part")          # or dh,dh; jz partial
    a.db(0x88, 0xD1).db(0x30, 0xED)                     # cx=row
    a.db(0x89, 0xCE).db(0x8A, 0xA4).abs16("srowcol")    # si=cx; ah=[si+srowcol]
    a.db(0xB0, _BLK_FULL).db(0xE8).rel16("tcell")       # █
    a.db(0xFE, 0xCA).db(0xFE, 0xCE).db(0xEB).rel8("t5s_full")   # dec dl; dec dh; loop
    a.label("t5s_part")
    a.db(0xA0).abs16("sbar_h").db(0xA8, 0x01).db(0x74).rel8("t5s_peak")   # test 1; jz peak
    a.db(0x88, 0xD1).db(0x30, 0xED)
    a.db(0x89, 0xCE).db(0x8A, 0xA4).abs16("srowcol")
    a.db(0xB0, _BLK_LOWER).db(0xE8).rel16("tcell")      # ▄
    a.label("t5s_peak")
    a.db(0xA0).abs16("speak_h")                         # al=[speak_h]
    a.db(0x08, 0xC0).db(0x74).rel8("t5s_done")          # or al,al; jz done
    a.db(0xD0, 0xE8)                                    # shr al,1
    a.db(0xB6, _T5_SPEC_BASE).db(0x28, 0xC6)            # dh=baseline; sub dh,al
    a.db(0x88, 0xF1).db(0x30, 0xED)
    a.db(0xB4, _S4_PEAK_ATTR).db(0xB0, _BLK_UPPER).db(0xE8).rel16("tcell")   # ▀ white
    a.label("t5s_done")
    a.db(0xC3)


def _emit_t5vu_colour(a: "_Asm", tag: str) -> None:
    """AH = green/yellow/red from the cell index in DL (green < 20, yellow < 26)."""
    a.db(0xB4, _S4_GREEN)                               # mov ah,green
    a.db(0x80, 0xFA, _T5_VU_GREEN).db(0x72).rel8(tag)   # cmp dl,20; jb have
    a.db(0xB4, _S4_YELLOW)                              # mov ah,yellow
    a.db(0x80, 0xFA, _T5_VU_YELLOW).db(0x72).rel8(tag)  # cmp dl,26; jb have
    a.db(0xB4, _S4_RED)                                 # mov ah,red
    a.label(tag)


def _emit_t5vu_draw(a: "_Asm") -> None:
    """Draw one horizontal VU meter on row [t5row]: [vu_h] half-cells growing
    right (full block, ▌ for the odd half), coloured by cell, then a white │ peak
    tick at [vupeak_h]. tcell keeps CX(row)/DX(index), scratches AX/BX/SI/DI."""
    a.label("t5vu")
    a.db(0xA0).abs16("vu_h").db(0x88, 0xC6).db(0xD0, 0xEE)   # al=vu_h; dh=al; shr dh,1 (cells)
    a.db(0x8B, 0x0E).abs16("t5row")                     # cx=row
    a.db(0x30, 0xD2)                                    # xor dl,dl (cell index)
    a.label("t5v_full")
    a.db(0x08, 0xF6).db(0x74).rel8("t5v_part")          # or dh,dh; jz partial
    _emit_t5vu_colour(a, "t5v_c1")
    a.db(0x88, 0xD3).db(0x80, 0xC3, _T5_VU_COL).db(0x30, 0xFF)   # bl=dl; add bl,col; bh=0
    a.db(0xB0, _BLK_FULL).db(0xE8).rel16("tcell")       # █
    a.db(0xFE, 0xC2).db(0xFE, 0xCE).db(0xEB).rel8("t5v_full")   # inc dl; dec dh; loop
    a.label("t5v_part")
    a.db(0xA0).abs16("vu_h").db(0xA8, 0x01).db(0x74).rel8("t5v_peak")   # test vu_h,1; jz peak
    _emit_t5vu_colour(a, "t5v_c2")
    a.db(0x88, 0xD3).db(0x80, 0xC3, _T5_VU_COL).db(0x30, 0xFF)
    a.db(0xB0, _BLK_LEFT).db(0xE8).rel16("tcell")       # ▌ (partial right edge)
    a.label("t5v_peak")
    a.db(0xA0).abs16("vupeak_h").db(0x08, 0xC0).db(0x74).rel8("t5v_done")   # or al,al; jz done
    a.db(0xD0, 0xE8)                                    # shr al,1 (peak cell)
    a.db(0x88, 0xC3).db(0x80, 0xC3, _T5_VU_COL).db(0x30, 0xFF)   # bl=al; add bl,col; bh=0
    a.db(0xB4, _S4_PEAK_ATTR).db(0xB0, _BOX_V).db(0xE8).rel16("tcell")   # white │
    a.label("t5v_done")
    a.db(0xC3)


def _emit_t5vu_channel(a: "_Asm", ch: int, row: int) -> None:
    """One VU channel: onset-kick (strike -> full) or slow release, peak-hold,
    label, and draw the meter."""
    lbl = _T5_VU_LABELS[ch]
    # kick on a latched strike, else decay
    a.db(0xA0).abs16("strike", ch).db(0x08, 0xC0)       # al=[strike+ch]; or al,al
    a.db(0x74).rel8(f"vk{ch}d")                         # jz decay
    a.db(0xC6, 0x06).abs16("strike", ch).db(0x00)       # strike[ch]=0
    a.db(0xC6, 0x06).abs16("vu", ch).db(_T5_VU_MAX)     # vu[ch]=MAX (kick)
    a.db(0xEB).rel8(f"vk{ch}p")                         # jmp peak
    a.label(f"vk{ch}d")
    a.db(0xA0).abs16("vu", ch).db(0x2C, _T5_VU_FALL)    # al=vu[ch]; sub al,FALL
    a.db(0x73).rel8(f"vk{ch}s").db(0x30, 0xC0)          # jnc set; xor al,al
    a.label(f"vk{ch}s")
    a.db(0xA2).abs16("vu", ch)                          # vu[ch]=al
    a.label(f"vk{ch}p")
    a.db(0x8A, 0x26).abs16("vu", ch)                    # ah=vu[ch]
    a.db(0xA0).abs16("vupeak", ch)                      # al=vupeak[ch]
    a.db(0x38, 0xC4).db(0x76).rel8(f"vk{ch}pd")         # cmp ah,al; jbe pdec
    a.db(0x88, 0x26).abs16("vupeak", ch).db(0xEB).rel8(f"vk{ch}dr")   # vupeak=vu
    a.label(f"vk{ch}pd")
    a.db(0x2C, _T5_VU_PEAK_FALL).db(0x73).rel8(f"vk{ch}ps").db(0x30, 0xC0)   # sub al,PF; jnc; 0
    a.label(f"vk{ch}ps")
    a.db(0xA2).abs16("vupeak", ch)                      # vupeak[ch]=al
    a.label(f"vk{ch}dr")
    # label (2 chars in the channel colour), then the bar
    a.db(0xB9).bytes(_w(row)).db(0xBB).bytes(_w(_T5_VU_LABEL_COL))   # cx=row; bx=labelcol
    a.db(0xB4, _TATTR[ch])                              # ah=channel colour
    a.db(0xB0, ord(lbl[0])).db(0xE8).rel16("tcell")     # char 0
    a.db(0x43).db(0xB0, ord(lbl[1])).db(0xE8).rel16("tcell")   # inc bx; char 1
    a.db(0xA0).abs16("vu", ch).db(0xA2).abs16("vu_h")   # vu_h = vu[ch]
    a.db(0xA0).abs16("vupeak", ch).db(0xA2).abs16("vupeak_h")   # vupeak_h = vupeak[ch]
    a.db(0xC7, 0x06).abs16("t5row").bytes(_w(row))      # t5row = row
    a.db(0xE8).rel16("t5vu")


def _emit_text5_drawframe(a: "_Asm") -> None:
    """The whole combined frame: clear, the 2x2 scope grid, the spectrum, the VU
    meters, then every border."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # es=bufseg
    a.db(0xB8, 0x20, 0x07).db(0x31, 0xFF)               # ax=0x0720; di=0
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xAB)               # cx=2000; rep stosw (clear)
    a.db(0x83, 0x06).abs16("scroll").db(_SCROLL_SPEED)  # scroll += speed
    # ---- top: 2x2 scope grid ---------------------------------------------------
    for ch in range(3):                                 # phase each tone by scroll
        a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)
        a.db(0x75).rel8(f"w5{ch}").db(0xB0, 0x01)
        a.label(f"w5{ch}")
        a.db(0x30, 0xE4).db(0x89, 0xC3)
        a.db(0x31, 0xD2).db(0xA1).abs16("scroll").db(0xF7, 0xF3)
        a.db(0x24, 0x01).db(0xA2).abs16("lev", ch)
        a.db(0x89, 0xD8).db(0x29, 0xD0).db(0xA2).abs16("cnt", ch)
    for i in range(4):
        a.db(0xC7, 0x06).abs16("prevrow", i * 2).bytes(_w(_T5_SCEN[i]))
    a.db(0x31, 0xED)                                    # xor bp,bp
    a.label("z5loop")
    _emit_t5_scope_channel(a, 0, _T5_SCEN[0], _T5_SCOL0[0])
    _emit_t5_scope_channel(a, 1, _T5_SCEN[1], _T5_SCOL0[1])
    _emit_t5_scope_channel(a, 2, _T5_SCEN[2], _T5_SCOL0[2])
    _emit_t5_scope_noise(a, _T5_SCEN[3], _T5_SCOL0[3])
    a.db(0x45).db(0x83, 0xFD, _T5_SQW)                 # inc bp; cmp bp,38
    a.db(0x73).rel8("z5done").db(0xE9).rel16("z5loop")
    a.label("z5done")
    # ---- bottom-left: spectrum -------------------------------------------------
    a.db(0x31, 0xF6)                                    # xor si,si
    a.label("qclr")
    a.db(0xC6, 0x84).abs16("stgt").db(0x00)             # mov byte[si+stgt],0
    a.db(0x46).db(0x83, 0xFE, _T5_SPEC_N).db(0x72).rel8("qclr")
    for ch in range(3):
        a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)
        a.db(0x74).rel8(f"qd{ch}")                      # jz skip
        a.db(0x88, 0xC3).db(0x30, 0xFF).db(0x01, 0xDB).db(0x01, 0xDB)   # bx = period*4
        for k in range(4):
            a.db(0x8A, 0x87).abs16("sharm", k)          # al=[bx+sharm+k]
            a.db(0x30, 0xE4).db(0x89, 0xC6)             # ah=0; si=ax (bin)
            a.db(0xB0, _T5_SPEC_HH[k])                  # al=height
            a.db(0x3A, 0x84).abs16("stgt").db(0x76).rel8(f"qn{ch}_{k}")   # cmp; jbe
            a.db(0x88, 0x84).abs16("stgt")              # [si+stgt]=al
            a.label(f"qn{ch}_{k}")
        a.label(f"qd{ch}")
    a.db(0xA0).abs16("viz", 3).db(0x08, 0xC0)           # noise floor
    a.db(0x74).rel8("qnn")
    a.db(0x31, 0xF6)
    a.label("qnl")
    a.db(0xA1).abs16("seed").db(0xB9, 0x55, 0x62).db(0xF7, 0xE1)
    a.db(0x05, 0x19, 0x36).db(0xA3).abs16("seed")
    a.db(0x88, 0xE0).db(0x24, 0x07).db(0x04, _T5_SPEC_NOISE)   # al=ah; and 7; add base
    a.db(0x3A, 0x84).abs16("stgt").db(0x76).rel8("qn2")
    a.db(0x88, 0x84).abs16("stgt")
    a.label("qn2")
    a.db(0x46).db(0x83, 0xFE, _T5_SPEC_N).db(0x72).rel8("qnl")
    a.label("qnn")
    a.db(0x31, 0xED)                                    # xor bp,bp (bar index)
    a.label("qbl")
    a.db(0x89, 0xEE)                                    # mov si,bp
    a.db(0x8A, 0x84).abs16("stgt").db(0x8A, 0xA4).abs16("sbar")   # al=target; ah=current
    a.db(0x38, 0xC4).db(0x72).rel8("qatk")              # cmp ah,al; jb attack
    a.db(0x80, 0xEC, _S4_FALL).db(0x73).rel8("qset").db(0x30, 0xE4).db(0xEB).rel8("qset")
    a.label("qatk")
    a.db(0x88, 0xC4)                                    # ah=al
    a.label("qset")
    a.db(0x88, 0xA4).abs16("sbar")                      # [si+sbar]=ah
    a.db(0x8A, 0x84).abs16("speak")                     # al=peak
    a.db(0x38, 0xC4).db(0x76).rel8("qpdec")             # cmp ah,al; jbe
    a.db(0x88, 0xE0).db(0x88, 0x84).abs16("speak").db(0xEB).rel8("qpdrw")
    a.label("qpdec")
    a.db(0x2C, _S4_PEAK_FALL).db(0x73).rel8("qpset").db(0x30, 0xC0)
    a.label("qpset")
    a.db(0x88, 0x84).abs16("speak")
    a.label("qpdrw")
    a.db(0x8A, 0x84).abs16("sbar").db(0xA2).abs16("sbar_h")
    a.db(0x8A, 0x84).abs16("speak").db(0xA2).abs16("speak_h")
    a.db(0x89, 0xE8)                                    # ax=bp
    for _ in range(_T5_SPEC_STRIDE - 1):
        a.db(0x01, 0xE8)                                # add ax,bp (stride*bp)
    a.db(0x05).bytes(_w(_T5_SPEC_FIRST)).db(0x89, 0xC3)   # add ax,first; bx=col
    a.db(0xE8).rel16("t5sdraw")
    a.db(0x45).db(0x83, 0xFD, _T5_SPEC_N)
    a.db(0x73).rel8("qbdone").db(0xE9).rel16("qbl")
    a.label("qbdone")
    # ---- bottom-right: VU meters ----------------------------------------------
    for ch in range(4):
        _emit_t5vu_channel(a, ch, _T5_VU_ROWS[ch])
    # ---- borders ---------------------------------------------------------------
    for x0, x1, y0, y1 in _T5_FRAMES:
        a.db(0xC7, 0x06).abs16("fx0").bytes(_w(x0))
        a.db(0xC7, 0x06).abs16("fx1").bytes(_w(x1))
        a.db(0xC7, 0x06).abs16("fy0").bytes(_w(y0))
        a.db(0xC7, 0x06).abs16("fy1").bytes(_w(y1))
        a.db(0xE8).rel16("t3frame")
    a.db(0xC3)                                          # ret


def _assemble(divider: int, subdiv: int, total_ticks: int,
              silence: bytes, stream: bytes, vis: str = "") -> bytes:
    """The engine: (optionally set a graphics/text video mode), install the timer
    ISR, run — waiting for a key or redrawing the scopes — then tear down.
    Followed by the silence record and per-sub-tick event stream. `vis` is "",
    "graphics" (mode 9) or "text" (mode 3)."""
    a = _Asm()
    if vis:
        video_mode = 0x03 if vis.startswith("text") else 0x09
        a.db(0xB8, video_mode, 0x00).db(0xCD, 0x10)     # mov ax,000N; int 0x10 (set video mode)
        # back buffer one 64 KB block past our program (a .COM owns all memory)
        a.db(0x8C, 0xC8).db(0x05, 0x00, 0x10)           # mov ax,cs; add ax,0x1000
        a.db(0xA3).abs16("bufseg")                      # mov [bufseg],ax
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
    # ---- main loop: redraw scopes (vis) or just wait, until a key ----------
    if vis:
        a.label("wait")
        a.db(0xE8).rel16("drawframe")                # render the whole frame off-screen
        a.db(0xBA, 0xDA, 0x03)                       # mov dx,0x3DA (CRTC status)
        a.label("vs1")
        a.db(0xEC).db(0xA8, 0x08).db(0x75).rel8("vs1")   # in al,dx;test al,8;jnz vs1 (wait end)
        a.label("vs2")
        a.db(0xEC).db(0xA8, 0x08).db(0x74).rel8("vs2")   # in al,dx;test al,8;jz vs2 (retrace)
        a.db(0xE8).rel16("blit")                     # pack+copy back buffer -> Tandy screen
        a.db(0xB4, 0x01).db(0xCD, 0x16)              # mov ah,1 ; int 0x16
        a.db(0x74).rel8("wait")                      # jz wait
        a.db(0x30, 0xE4).db(0xCD, 0x16)              # xor ah,ah ; int 0x16 (consume)
    else:
        a.label("wait")
        a.db(0xB4, 0x01).db(0xCD, 0x16)              # mov ah,1 ; int 0x16
        a.db(0x74).rel8("wait")                      # jz wait  (ZF=1: no key)
        a.db(0x30, 0xE4).db(0xCD, 0x16)              # xor ah,ah ; int 0x16 (consume)
    # ---- finish: silence, restore timer + vector, (text mode,) exit ---------
    a.db(0xFA)                                       # cli
    a.db(0xBE).abs16("silence").db(0xE8).rel16("playrec")    # mov si,silence;call playrec
    a.db(0xB0, 0x36).db(0xE6, _PIT_CMD)              # mov al,0x36;out 0x43,al
    a.db(0x30, 0xC0).db(0xE6, _PIT_CH0).db(0xE6, _PIT_CH0)   # xor al,al;out40;out40 (div 65536)
    a.db(0x31, 0xC0).db(0x8E, 0xC0)                  # xor ax,ax;mov es,ax
    a.db(0xA1).abs16("old_off").db(0x26, 0xA3, 0x20, 0x00)   # mov ax,[old_off];mov[es:0x20],ax
    a.db(0xA1).abs16("old_seg").db(0x26, 0xA3, 0x22, 0x00)   # mov ax,[old_seg];mov[es:0x22],ax
    a.db(0xFB)                                       # sti
    if vis:
        a.db(0xB8, 0x03, 0x00).db(0xCD, 0x10)        # mov ax,0x0003; int 0x10 (back to text mode)
    a.db(0xB8, 0x00, 0x4C).db(0xCD, 0x21)            # mov ax,0x4C00 ; int 0x21
    # ---- playrec: apply one [count][port,val]* record at DS:SI --------------
    a.label("playrec")
    a.db(0xAC).db(0x88, 0xC1).db(0x30, 0xED)         # lodsb;mov cl,al;xor ch,ch
    a.db(0xE3).rel8("pr_done")                       # jcxz pr_done
    a.label("pr_loop")
    a.db(0xAC).db(0x88, 0xC2).db(0x30, 0xF6).db(0xAC)        # lodsb;mov dl,al;xor dh,dh;lodsb
    a.db(0x80, 0xFA, _VIZ_PORT)                      # cmp dl,0xF0
    a.db(0x72).rel8("pr_hw")                         # jb pr_hw (real port)
    a.db(0x80, 0xE2, 0x03).db(0x89, 0xD7)            # and dl,3 ; mov di,dx
    a.db(0x88, 0x85).abs16("viz")                    # mov [viz+di],al
    # a non-zero viz write is a real note-on/drum hit -> latch a strike for ch di
    # (the VU meters in text5 read + clear it; other modes ignore the table).
    a.db(0x08, 0xC0).db(0x74).rel8("pr_next")        # or al,al; jz pr_next (note-off)
    a.db(0xC6, 0x85).abs16("strike").db(0x01)        # mov byte[strike+di],1
    a.db(0xEB).rel8("pr_next")                       # jmp pr_next
    a.label("pr_hw")
    a.db(0x80, 0xFA, _SPEAKER)                       # cmp dl,0x61
    a.db(0x75).rel8("pr_out")                        # jne pr_out
    a.db(0x88, 0xC4).db(0xE4, _SPEAKER).db(0x24, 0xFC).db(0x08, 0xE0)  # mov ah,al;in al,0x61;and al,0xFC;or al,ah
    a.label("pr_out")
    a.db(0xEE)                                       # out dx,al
    a.label("pr_next")
    a.db(0xE2).rel8("pr_loop")                       # loop pr_loop
    a.label("pr_done")
    a.db(0xC3)                                       # ret
    # ---- isr: every `subdiv`th IRQ0, apply one sub-tick record --------------
    a.label("isr")
    a.db(0x50, 0x51, 0x52, 0x56, 0x57, 0x1E, 0x0E, 0x1F)  # push ax,cx,dx,si,di,ds; push cs;pop ds
    a.db(0xFE, 0x0E).abs16("subcount")               # dec byte [subcount]
    a.db(0x75).rel8("isr_eoi")                       # jnz isr_eoi
    a.db(0xC6, 0x06).abs16("subcount").db(subdiv & 0xFF)     # mov byte[subcount], subdiv
    a.db(0x83, 0x3E).abs16("ticksleft").db(0x00)     # cmp word [ticksleft],0
    a.db(0x75).rel8("isr_play")                      # jne isr_play (still mid-song)
    # reached the end -> rewind to the top and keep playing (auto-repeat). The
    # stream's last tick is the silence record, so held notes are already off; the
    # loop just re-attacks from tick 0. A keypress in the main loop still quits.
    a.db(0xC7, 0x06).abs16("ticksleft").bytes(struct.pack("<H", total_ticks))  # mov word[ticksleft],total
    a.db(0xC7, 0x06).abs16("streamptr").abs16("stream")   # mov word[streamptr],stream
    a.label("isr_play")
    a.db(0x8B, 0x36).abs16("streamptr")              # mov si,[streamptr]
    a.db(0xE8).rel16("playrec")                      # call playrec
    a.db(0x89, 0x36).abs16("streamptr")              # mov [streamptr],si
    a.db(0xFF, 0x0E).abs16("ticksleft")              # dec word [ticksleft]
    a.label("isr_eoi")
    a.db(0xB0, 0x20).db(0xE6, 0x20)                  # mov al,0x20 ; out 0x20,al (EOI)
    a.db(0x1F, 0x5F, 0x5E, 0x5A, 0x59, 0x58, 0xCF)   # pop ds,di,si,dx,cx,ax ; iret
    if vis == "graphics":
        _emit_ploty(a)
        _emit_vline(a)
        _emit_hline(a)
        _emit_drawframe(a)
        _emit_blit(a)
        a.label("rowaddr")                           # mode-9 byte offset of each scanline
        a.bytes(struct.pack(f"<{len(_ROWADDR)}H", *_ROWADDR))
    elif vis == "text1":
        _emit_tcell(a)
        _emit_tfill(a)
        _emit_text_drawframe(a)
        _emit_text_blit(a)
        a.label("textrow")                           # byte offset of each text row
        a.bytes(struct.pack(f"<{len(_TEXTROW)}H", *_TEXTROW))
    elif vis == "text2":
        _emit_tcell(a)
        _emit_t2seg(a)
        _emit_text2_drawframe(a)
        _emit_text_blit(a)
        a.label("textrow")
        a.bytes(struct.pack(f"<{len(_TEXTROW)}H", *_TEXTROW))
    elif vis == "text3":
        _emit_tcell(a)
        _emit_t3seg(a)
        _emit_t3frame(a)
        _emit_text3_drawframe(a)
        _emit_text_blit(a)
        a.label("textrow")
        a.bytes(struct.pack(f"<{len(_TEXTROW)}H", *_TEXTROW))
    elif vis == "text4":
        _emit_tcell(a)
        _emit_t3frame(a)                             # reuse the box-drawing border
        _emit_s4_drawcol(a)
        _emit_s4_drawframe(a)
        _emit_text_blit(a)
        a.label("textrow")
        a.bytes(struct.pack(f"<{len(_TEXTROW)}H", *_TEXTROW))
        a.label("harm"); a.bytes(_s4_harm())         # per-period fundamental+harmonic bins
        a.label("rowcol"); a.bytes(_s4_rowcol())     # per-row green/yellow/red attribute
    elif vis == "text5":
        _emit_tcell(a)
        _emit_t3seg(a)                               # scope box-line trace
        _emit_t3frame(a)                             # all six borders
        _emit_t5spec_drawcol(a)
        _emit_t5vu_draw(a)
        _emit_text5_drawframe(a)
        _emit_text_blit(a)
        a.label("textrow")
        a.bytes(struct.pack(f"<{len(_TEXTROW)}H", *_TEXTROW))
        a.label("sharm"); a.bytes(_t5_spec_harm())   # spectrum harmonic bins (18 bars)
        a.label("srowcol"); a.bytes(_t5_spec_rowcol())   # spectrum row colours
    # ---- variables ----------------------------------------------------------
    a.label("old_off"); a.db(0x00, 0x00)
    a.label("old_seg"); a.db(0x00, 0x00)
    a.label("streamptr"); a.db(0x00, 0x00)
    a.label("ticksleft"); a.db(0x00, 0x00)
    a.label("subcount"); a.db(subdiv & 0xFF)
    a.label("viz"); a.db(0x00, 0x00, 0x00, 0x00)     # per-channel scope periods
    a.label("strike"); a.db(0x00, 0x00, 0x00, 0x00)  # per-channel note-on latch (VU)
    if vis:                                          # shared draw state
        a.label("bufseg"); a.db(0x00, 0x00)              # back-buffer segment
        a.label("cnt"); a.db(0x00, 0x00, 0x00, 0x00)     # draw-loop counters
        a.label("lev"); a.db(0x01, 0x01, 0x01, 0x01)     # draw-loop levels
        a.label("msum"); a.db(0x00)                       # master-sum accumulator
        a.label("scroll"); a.db(0x00, 0x00)              # horizontal scroll phase
        a.label("seed"); a.bytes(struct.pack("<H", _NOISE_SEED))   # noise PRNG state
    if vis == "graphics":                            # mode-9 vline/frame state
        a.label("prev_my"); a.db(0x00, 0x00)             # master's previous y (line)
        a.label("prevy"); a.db(0, 0, 0, 0, 0, 0)         # 3 tone channels' previous y
        a.label("vtop"); a.db(0x00, 0x00)                # vline top y
        a.label("vbot"); a.db(0x00, 0x00)                # vline bottom y
        a.label("hend"); a.db(0x00, 0x00)                # hline end column
    elif vis == "text1":                             # text-1 block-fill state
        a.label("ftop"); a.db(0x00, 0x00)                # tfill top row
        a.label("fbot"); a.db(0x00, 0x00)                # tfill bottom row
    elif vis == "text2":                             # text-2 line-trace state
        a.label("prevrow"); a.db(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)   # 5 bands' previous row
        a.label("t2cur"); a.db(0x00, 0x00)               # this column's row
        a.label("t2prev"); a.db(0x00, 0x00)              # previous column's row
        a.label("t2top"); a.db(0x00, 0x00)               # segment top row
        a.label("t2bot"); a.db(0x00, 0x00)               # segment bottom row
        a.label("t2attr"); a.db(0x00)                    # current band colour
    elif vis == "text3":                             # text-3 grid line-trace state
        a.label("prevrow"); a.db(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)   # 5 bands' previous row
        a.label("t3col"); a.db(0x00, 0x00)               # this band's screen column
        a.label("t3cur"); a.db(0x00, 0x00)               # this column's row
        a.label("t3prev"); a.db(0x00, 0x00)              # previous column's row
        a.label("t3top"); a.db(0x00, 0x00)               # segment top row
        a.label("t3bot"); a.db(0x00, 0x00)               # segment bottom row
        a.label("t3attr"); a.db(0x00)                    # current band colour
        a.label("fx0"); a.db(0x00, 0x00)                 # frame rectangle (x0,x1,y0,y1)
        a.label("fx1"); a.db(0x00, 0x00)
        a.label("fy0"); a.db(0x00, 0x00)
        a.label("fy1"); a.db(0x00, 0x00)
        a.label("mbuf"); a.bytes(bytes(_T3_QW))          # per-column master sum (38)
    elif vis == "text4":                             # spectrum-analyzer state
        a.label("tgt"); a.bytes(bytes(_S4_N))            # this frame's target heights
        a.label("bar"); a.bytes(bytes(_S4_N))            # current bar heights (persist)
        a.label("peak"); a.bytes(bytes(_S4_N))           # peak-hold heights (persist)
        a.label("bar_h"); a.db(0x00)                     # drawcol: this bar's height
        a.label("peak_h"); a.db(0x00)                    # drawcol: this bar's peak
        a.label("fx0"); a.db(0x00, 0x00)                 # border rectangle
        a.label("fx1"); a.db(0x00, 0x00)
        a.label("fy0"); a.db(0x00, 0x00)
        a.label("fy1"); a.db(0x00, 0x00)
    elif vis == "text5":                             # combined-monitor state
        a.label("prevrow"); a.db(0, 0, 0, 0, 0, 0, 0, 0)   # 4 scope bands' previous row
        a.label("t3col"); a.db(0x00, 0x00)               # scope segment column
        a.label("t3cur"); a.db(0x00, 0x00)
        a.label("t3prev"); a.db(0x00, 0x00)
        a.label("t3top"); a.db(0x00, 0x00)
        a.label("t3bot"); a.db(0x00, 0x00)
        a.label("t3attr"); a.db(0x00)
        a.label("stgt"); a.bytes(bytes(_T5_SPEC_N))      # spectrum target/current/peak
        a.label("sbar"); a.bytes(bytes(_T5_SPEC_N))
        a.label("speak"); a.bytes(bytes(_T5_SPEC_N))
        a.label("sbar_h"); a.db(0x00)                    # spectrum drawcol height/peak
        a.label("speak_h"); a.db(0x00)
        a.label("vu"); a.db(0x00, 0x00, 0x00, 0x00)      # VU level/peak per channel
        a.label("vupeak"); a.db(0x00, 0x00, 0x00, 0x00)
        a.label("vu_h"); a.db(0x00)                      # VU drawcol level/peak
        a.label("vupeak_h"); a.db(0x00)
        a.label("t5row"); a.db(0x00, 0x00)               # current VU meter row
        a.label("fx0"); a.db(0x00, 0x00)                 # border rectangle
        a.label("fx1"); a.db(0x00, 0x00)
        a.label("fy0"); a.db(0x00, 0x00)
        a.label("fy1"); a.db(0x00, 0x00)
    # ---- appended data ------------------------------------------------------
    a.label("silence"); a.bytes(silence)
    a.label("stream"); a.bytes(stream)
    return a.resolve()


def _tandy_silence() -> List[Tuple[int, int]]:
    return [(_SN76489, 0x9F), (_SN76489, 0xBF),      # attenuate tone 0,1,2
            (_SN76489, 0xDF), (_SN76489, 0xFF)]      # and the noise channel


def build_com(song: Song, mode: str, tempo_byte0: int, scope: bool = False,
              text_scope: bool = False) -> bytes:
    """Assemble a `.COM` that plays `song` in the given mode at the MCS tempo.
    `scope` (Tandy only) adds the mode-9 graphics oscilloscopes; `text_scope`
    (Tandy only) adds a lighter 80x25 text-mode scope instead -- 1 = block bars,
    2 = box-drawing line trace, 3 = box-line 2x2 grid + master (graphics layout),
    4 = faux spectrum analyzer (colour bars + peak caps), 5 = combined monitor
    (2x2 scopes + spectrum + VU meters)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if text_scope:
        ts = int(text_scope)
        vis = ("text5" if ts == 5 else "text4" if ts == 4 else
               "text3" if ts == 3 else "text2" if ts == 2 else "text1")
    elif scope:
        vis = "graphics"
    else:
        vis = ""
    if vis and mode != "tandy":
        raise ValueError("the scope display is only implemented for --tandy")
    stream, total = _build_stream(song, mode, bool(vis))
    if total == 0:
        raise ValueError("nothing to play (no notes)")
    # Timer fires once per SUB-tick (the stream's resolution). Should always fit
    # 16 bits since a sub-tick is well under one PIT period, but keep the subdiv
    # fallback for safety; the ISR advances the stream every `subdiv` interrupts.
    subtick_s = tick_seconds_for(tempo_byte0) / _SUBTICKS
    subdiv = 1
    while round(_PIT_HZ * subtick_s / subdiv) > 65535:
        subdiv += 1
    divider = round(_PIT_HZ * subtick_s / subdiv)

    sil = _tandy_silence() if mode == "tandy" else _spk_note_off()
    sil_bytes = bytes([len(sil)]) + b"".join(bytes([p, v]) for p, v in sil)
    # append the silence record as one extra tick so the song self-silences at
    # its end instead of hanging the final chord until a key is pressed
    stream += sil_bytes
    total += 1
    com = _assemble(divider, subdiv, total, sil_bytes, stream, vis)
    if len(com) > 0xFF00:
        raise ValueError(f".COM is {len(com)} bytes — too big for one segment; "
                         "shorten the song or split it")
    return com
