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
# rate so it can ARTICULATE each note: a note sounds for all but its final
# sub-tick, leaving a ~1/_SUBTICKS-tick silence before the next onset. Without
# it the SN76489 (no attack envelope) merges back-to-back notes into one held
# tone — the "rat-tat-tat" of fast repeats blurs into a drone.
_SUBTICKS = 4
_NOISE_CH = 3                    # SN76489 channel 3 = noise
_DRUM_BRIGHT_MIDI = 72          # drum pitch at/above this -> bright hi-hat noise


# --- register encoders (one note/hit -> (port, value) writes) ----------------

def _tandy_note_on(ch: int, freq: float) -> List[Tuple[int, int]]:
    """SN76489: set tone channel `ch` (0..2) to `freq` at full volume."""
    n = max(1, min(1023, round(_SN_HZ / (32.0 * freq))))
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


def _tandy_stream(song: Song) -> Dict[int, List[Tuple[int, int]]]:
    """Per-sub-tick writes: 3 SN76489 tone voices + the noise channel for drums.
    Every note is articulated (a short cut before the next), so fast repeats
    rat-tat-tat instead of merging."""
    per_track, perc = _split_notes(song)
    voices = _allocate_voices(per_track, n=3)
    by: Dict[int, List[Tuple[int, int]]] = {}
    for ch, voice in enumerate(voices):
        for start, dur, midi in voice:
            on = start * _SUBTICKS
            off = on + max(1, dur * _SUBTICKS - 1)  # cut 1 sub-tick early
            by.setdefault(on, []).extend(_tandy_note_on(ch, midi_to_freq(midi)))
            by.setdefault(off, []).extend(_tandy_note_off(ch))
    seen = set()
    for start, midi in perc:                        # drums -> noise channel
        on = start * _SUBTICKS
        if on in seen:                              # one noise hit per sub-tick
            continue
        seen.add(on)
        by.setdefault(on, []).extend(_tandy_noise_on(midi >= _DRUM_BRIGHT_MIDI))
        by.setdefault(on + max(1, _SUBTICKS - 1), []).extend(_tandy_noise_off())
    return by


def _mono_stream(song: Song) -> Dict[int, List[Tuple[int, int]]]:
    """Per-sub-tick writes for the single PC-speaker voice: play the highest
    voice (channel 0), articulated so repeated notes re-attack."""
    per_track, _ = _split_notes(song)
    voice = _allocate_voices(per_track, n=3)[0]     # channel 0 = the top line
    by: Dict[int, List[Tuple[int, int]]] = {}
    for start, dur, midi in voice:
        on = start * _SUBTICKS
        off = on + max(1, dur * _SUBTICKS - 1)
        by.setdefault(on, []).extend(_spk_note_on(midi_to_freq(midi)))
        by.setdefault(off, []).extend(_spk_note_off())
    return by


def _build_stream(song: Song, mode: str) -> Tuple[bytes, int]:
    """(stream bytes, total_subticks). One record per sub-tick: [n][port,val]*."""
    by = _tandy_stream(song) if mode == "tandy" else _mono_stream(song)
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
    com = _assemble(divider, subdiv, total, sil_bytes, stream)
    if len(com) > 0xFF00:
        raise ValueError(f".COM is {len(com)} bytes — too big for one segment; "
                         "shorten the song or split it")
    return com
