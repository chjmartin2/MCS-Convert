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

MODES = ("tandy", "1voice", "4voice")

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

# --- 4-voice PC-speaker player (software 1-bit mixing) -----------------------
# The PC speaker is 1 bit, so N voices are summed in software and delta-sigma
# modulated onto that single bit. A sample-rate timer ISR (PIT ch0 at ~Fs)
# advances one PHASE ACCUMULATOR per voice (acc += inc each sample; the square
# wave is the accumulator's top bit), sums the top bits, and PWMs the sum. The
# speaker is driven directly (port 0x61 bit 1) with timer 2's gate off so its
# OUT is forced high and the cone follows the data bit. Note events (per sub-
# tick) reload the voices' `inc`. Phase 1 = 3 tone voices; the 4th (noise) voice
# just swaps its accumulator-bit for an LFSR bit -- same mixer.
_SPK4_DIV = 100                  # default PIT ch0 divider -> Fs = 1193182/100 ≈ 11.9 kHz
_SPK4_FS = _PIT_HZ / _SPK4_DIV
# Mixing-rate range the user may request (any value in between is fine):
#   ~1 kHz (div 1200) for very slow targets .. ~48 kHz (div 25) for an ultrasonic
#   carrier on a fast CPU / DOSBox cycles=max, where the PWM carrier is inaudible
#   and only the audio band survives -- the highest quality the mixer can reach.
_SPK4_DIV_MIN, _SPK4_DIV_MAX = 25, 1200
_SPK4_TONES = 3                  # square-wave voices
_SPK4_VOICES = 4                 # + 1 noise voice = delta-sigma threshold
_SPK4_LFSR = 0xB400              # 16-bit maximal Galois LFSR taps (noise source)
_SPK4_NOISE_BRIGHT_HZ = 5500     # hi-hat: fast LFSR clock (hiss)
_SPK4_NOISE_DARK_HZ = 1200       # kick: slow LFSR clock (rumble)

# "static screen" -- the XT answer: there's no runtime CPU for a live display, so
# render the WHOLE song ONCE as a picture. A piano roll (pitch x time, coloured by
# voice) is built here in Python, packed into a CGA mode-4 (320x200x4) bitmap, and
# baked into the .COM; the player just blits it once and then does nothing but play
# + poll the keyboard. Palette: black / bright-green / bright-red / yellow.
_CGA_GREEN, _CGA_RED, _CGA_YELLOW = 1, 2, 3        # palette-0 high-intensity indices


def _pack_cga4(px: List[List[int]]) -> bytes:
    """Pack a 200x320 array of 2-bit pixels into the CGA mode-4 framebuffer layout:
    even scanlines at offset 0, odd at 0x2000, 80 bytes/line, 4 pixels/byte MSB
    first. The framebuffer is 16 KB (0x4000) -- two 8 KB interleaved planes."""
    buf = bytearray(0x4000)
    for y in range(200):
        base = (0x2000 if (y & 1) else 0) + (y // 2) * 80
        row = px[y]
        for bx in range(80):
            x = bx * 4
            buf[base + bx] = (row[x] << 6) | (row[x + 1] << 4) \
                | (row[x + 2] << 2) | row[x + 3]
    return bytes(buf)


def _render_static_poster(song: Song) -> bytes:
    """The whole song as a 320x200x4 piano roll: pitch (vertical) x time (across),
    each voice a colour -- lead=yellow, 2nd=green, bass=red -- with dotted octave
    gridlines and a drum-hit strip along the bottom. Returns the packed CGA bitmap
    (16000 bytes). The colour legend text is drawn by the player via the BIOS."""
    W, H = 320, 200
    px = [[0] * W for _ in range(H)]
    tone = [[], [], []]                             # (start, end, midi) per tone voice
    tone_tracks = [t for t in song.tracks
                   if getattr(t, "kind", "tone") == "tone"]
    for vi, t in enumerate(tone_tracks[:3]):
        for n in t.notes:
            if not n.is_rest and n.midi_note and not n.percussive:
                tone[vi].append((n.start_tick, n.end_tick, n.midi_note))
    perc = [n.start_tick for t in song.tracks for n in t.notes if not n.is_rest
            and (n.percussive or getattr(t, "kind", "tone") in ("noise", "drum"))]
    allnotes = [x for v in tone for x in v]
    if not allnotes:
        return bytes(0x4000)
    tot = max(e for _, e, _ in allnotes) or 1
    mn = min(m for _, _, m in allnotes)
    mx = max((m for _, _, m in allnotes), default=mn) + 1
    roll_top, roll_bot, roll_l, roll_r = 12, 188, 2, 317
    drum_y0, drum_y1 = 191, 197

    def xt(t):
        return roll_l + int(t / tot * (roll_r - roll_l))

    def yp(m):
        m = max(mn, min(mx, m))
        return roll_bot - int((m - mn) / (mx - mn) * (roll_bot - roll_top))

    def plot(x, y, c):
        if 0 <= x < W and 0 <= y < H:
            px[y][x] = c

    def hbar(x0, x1, y, c):
        for x in range(max(0, x0), min(W, x1 + 1)):
            plot(x, y, c)
            plot(x, y - 1, c)                       # 2 px thick

    m = mn - (mn % 12)                              # dotted octave gridlines (at each C)
    while m <= mx:
        if m >= mn:
            y = yp(m)
            for x in range(roll_l, roll_r, 6):
                plot(x, y, _CGA_GREEN)
        m += 12
    # bass (red), then 2nd voice (green), then lead (yellow) on top
    for vi, col in ((2, _CGA_RED), (1, _CGA_GREEN), (0, _CGA_YELLOW)):
        for s, e, mid in tone[vi]:
            hbar(xt(s), xt(e), yp(mid), col)
    for s in perc:                                  # drum-hit strip along the bottom
        x = xt(s)
        for y in range(drum_y0, drum_y1 + 1):
            plot(x, y, _CGA_YELLOW)
    return _pack_cga4(px)


def _spk4_div_for(mix_rate) -> int:
    """PIT ch0 divider for a requested mixing rate in Hz (clamped), or the default
    when `mix_rate` is falsy. Fs = 1193182 / divider."""
    if not mix_rate:
        return _SPK4_DIV
    return max(_SPK4_DIV_MIN, min(_SPK4_DIV_MAX, round(_PIT_HZ / mix_rate)))


def _spk4_inc(freq: float, fs: float = _SPK4_FS) -> int:
    """Phase-accumulator increment for `freq` at sample rate `fs`: the 16-bit
    accumulator wraps once per cycle, so inc = freq * 2^16 / Fs. Its top bit is
    the square wave (for the noise voice it's the overflow that clocks the LFSR)."""
    return max(1, min(65535, round(freq * 65536.0 / fs)))


def _spk4_noise_inc(bright: bool, fs: float = _SPK4_FS) -> int:
    """Accumulator increment for the noise voice -- it sets how fast the LFSR is
    clocked, i.e. the noise brightness (hi-hat vs kick)."""
    return _spk4_inc(_SPK4_NOISE_BRIGHT_HZ if bright else _SPK4_NOISE_DARK_HZ, fs)


def _spk4_events(song: Song, fs: float = _SPK4_FS) -> Dict[int, List[Tuple[int, int, int]]]:
    """sub-tick -> [(voice, inc, viz)] changes: each note-on sets its voice's
    increment (at sample rate `fs`), each articulated note-off sets it to 0
    (silent). `viz` is the on-screen scope period (0 = silent) that the text
    visualisations read -- same units as the Tandy player, so the scope renderers
    are reused verbatim. Voices 0-2 are the tone squares; voice 3 is the noise
    channel from percussion."""
    per_track, perc = _split_notes(song)
    voices = _allocate_voices(per_track, n=3)
    # note velocities, so the SoundBlaster engine can play a quiet note quietly
    # (the 1-bit speaker engines ignore the level -- they have no volume)
    vel = {(n.start_tick, n.midi_note): n.velocity
           for t in song.tracks for n in t.notes if not n.is_rest}
    events: Dict[int, List[Tuple[int, int, int, int]]] = {}
    for v, voice in enumerate(voices):
        for start, dur, midi in voice:
            on = start * _SUBTICKS
            off = _artic_off(on, dur)
            freq = midi_to_freq(midi)
            lvl = _sb_level(vel.get((start, midi), 100))
            events.setdefault(on, []).append(
                (v, _spk4_inc(freq, fs), _viz_period(freq), lvl))
            events.setdefault(off, []).append((v, 0, 0, 0))
    seen = set()
    for start, midi in perc:                        # drums -> noise voice (3)
        on = start * _SUBTICKS
        if on in seen:                              # one hit per sub-tick
            continue
        seen.add(on)
        off = on + max(1, _SUBTICKS - 1)
        events.setdefault(on, []).append(
            (3, _spk4_noise_inc(midi >= _DRUM_BRIGHT_MIDI, fs), _NOISE_VIZ_P,
             _SB_LEVELS - 1))
        events.setdefault(off, []).append((3, 0, 0, 0))
    return events


def _build_spk4_stream(song: Song, fs: float = _SPK4_FS) -> Tuple[bytes, int]:
    """(stream bytes, total sub-ticks). One record per sub-tick:
    [nchanges][voice|level<<4, inc_lo, inc_hi, viz]* -- only voices that CHANGE
    are listed, so a held note emits nothing and keeps its phase. The voice byte
    is 0-3, so its high nibble carries the note's VOLUME level for free (the
    SoundBlaster engine reads it; the 1-bit engines mask it off). A final all-off
    record silences the voices before the loop restarts."""
    events = _spk4_events(song, fs)
    ticks = max((n.end_tick for t in song.tracks for n in t.notes), default=0)
    total = ticks * _SUBTICKS
    out = bytearray()
    for s in range(total):
        ch = events.get(s, [])
        out.append(len(ch))
        for v, inc, viz, lvl in ch:
            out += bytes([(v & 0x0F) | ((lvl & 0x0F) << 4),
                          inc & 0xFF, (inc >> 8) & 0xFF, viz & 0xFF])
    out.append(_SPK4_VOICES)                        # trailing all-off sub-tick
    for v in range(_SPK4_VOICES):
        out += bytes([v, 0, 0, 0])
    total += 1
    return bytes(out), total


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
    """(per-track pitched events, percussion hits [(start_tick, midi)]).

    Universal-tracker aware: kind="noise"/"drum" tracks are percussion sources
    wholesale (their notes' pitch carries the bright/dark split), and percussive
    notes on tone tracks join them; everything else is pitched material."""
    per_track, perc = [], []
    for t in song.tracks:
        if getattr(t, "kind", "tone") in ("noise", "drum"):
            for n in t.notes:
                if not n.is_rest:
                    perc.append((n.start_tick, n.midi_note))
            continue
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
_GAMP = 20                      # graphics-scope amplitude in scanlines (cen - hi)
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

# VGA mode 13h (320x200x256) port of the graphics scope: a LINEAR framebuffer at
# A000 (1 byte/pixel), so no banks/packing -- and universal (any VGA card), unlike
# the Tandy-only mode-9. Same 2x2-scopes+master layout and logic; only the pixel
# writer, the blit and the colours change. Each mode-9 "column" (0..159) becomes 2
# pixels so the 160-wide layout fills the full 320. Default-palette colour indices:
_CH13 = [(*_TOP, 14, 4),         # ch0 yellow       top-left
         (*_TOP, 12, 86),        # ch1 light red    top-right
         (*_BOT, 11, 4),         # ch2 light cyan   bottom-left
         (*_BOT, 10, 86)]        # ch3 noise green  bottom-right
_VGA_WHITE = 15                  # frames / master
_ROWADDR13 = [y * 320 for y in range(200)]


def _w(v: int) -> bytes:
    return struct.pack("<H", v)


def _emit_ploty13(a: "_Asm") -> None:
    """VGA mode-13h pixel writer (emitted under the 'ploty' label so vline/hline/
    the channel draws call it unchanged): plot column BX (0..159) as TWO pixels at
    rowaddr[CX]*+BX*2, colour DL. Clobbers SI, DI."""
    a.label("ploty")
    a.db(0x89, 0xCE).db(0x01, 0xF6)                     # mov si,cx; add si,si (y*2)
    a.db(0x8B, 0xBC).abs16("rowaddr")                   # mov di,[rowaddr+si]
    a.db(0x01, 0xDF).db(0x01, 0xDF)                     # add di,bx; add di,bx (bx*2)
    a.db(0x26, 0x88, 0x15)                              # mov es:[di],dl
    a.db(0x26, 0x88, 0x55, 0x01)                        # mov es:[di+1],dl
    a.db(0xC3)                                          # ret


def _emit_blit13(a: "_Asm") -> None:
    """Copy the 64000-byte linear back buffer to the VGA screen at A000."""
    a.label("blit")
    a.db(0x1E)                                          # push ds
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xD8)           # mov ax,[bufseg]; mov ds,ax
    a.db(0xB8, 0x00, 0xA0).db(0x8E, 0xC0)               # mov ax,0xA000; mov es,ax
    a.db(0x31, 0xF6).db(0x31, 0xFF)                     # xor si,si; xor di,di
    a.db(0xB9).bytes(_w(32000)).db(0xF3, 0xA5)          # mov cx,32000; rep movsw (64000 bytes)
    a.db(0x1F).db(0xC3)                                 # pop ds; ret


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


def _emit_frame(a: "_Asm", x0: int, x1: int, y0: int, y1: int, white: int = 0xFF) -> None:
    """Draw a white rectangle outline (top/bottom via hline, sides via vline)."""
    a.db(0xB2, white)                                  # mov dl, white/border colour
    a.db(0xC7, 0x06).abs16("hend").bytes(_w(x1 + 1))    # mov word[hend], x1+1
    a.db(0xBB).bytes(_w(x0)).db(0xB9).bytes(_w(y0)).db(0xE8).rel16("hline")   # top
    a.db(0xBB).bytes(_w(x0)).db(0xB9).bytes(_w(y1)).db(0xE8).rel16("hline")   # bottom
    a.db(0xC7, 0x06).abs16("vtop").bytes(_w(y0))        # mov word[vtop], y0
    a.db(0xC7, 0x06).abs16("vbot").bytes(_w(y1))        # mov word[vbot], y1
    a.db(0xBB).bytes(_w(x0)).db(0xE8).rel16("vline")    # left
    a.db(0xBB).bytes(_w(x1)).db(0xE8).rel16("vline")    # right


def _emit_channel_draw(a: "_Asm", ch: int, colors=_CH) -> None:
    """One tone channel per column (BX = its screen column): pick this column's y
    (HI/LO while sounding, CENTRE when silent), add ±1 to the master sum (HI =
    up = +1), advance its scroll counter, then connect the previous column's y to
    this one with a vline -- so the square is solid and edges stay crisp."""
    hi, lo, cen, packed, _cs = colors[ch]
    a.db(0xB2, packed)                                  # mov dl, packed colour
    _emit_wave_pick(a, ch, cen, f"c{ch}", shape="wshapeg")
    a.db(0xA1).abs16("prevy", ch * 2)                   # mov ax,[prevy+ch] (previous y)
    a.db(0x89, 0x0E).abs16("prevy", ch * 2)             # mov [prevy+ch],cx (save this y)
    a.db(0x39, 0xC8).db(0x76).rel8(f"c{ch}_o").db(0x91)  # cmp ax,cx; jbe o; xchg ax,cx
    a.label(f"c{ch}_o")
    a.db(0xA3).abs16("vtop").db(0x89, 0x0E).abs16("vbot")   # vtop=top; vbot=bottom
    a.db(0xE8).rel16("vline")                           # call vline


def _emit_noise_draw(a: "_Asm", colors=_CH) -> None:
    """Channel 3 is NOISE: a single-colour spike from the band centre to a random
    y that swings BOTH ways (an evolving LCG seed makes it shimmer). Silent -> a
    flat centre line. Contributes its spike direction (±1) to the master sum."""
    _, _, cen, packed, _cs = colors[3]
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


def _emit_master_draw(a: "_Asm", white: int = 0xFF) -> None:
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
    a.db(0xB2, white)                                  # mov dl,white (imul clobbered DL!)
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


def _emit_drawframe(a: "_Asm", colors=_CH, white: int = 0xFF,
                    clear_words: int = 16384) -> None:
    """Redraw all five scopes straight into the back buffer, per column (0..159).
    Tone channels scroll as solid squares; ch3 is noise; the master is the summed
    trace. `colors`/`white`/`clear_words` retarget it (mode-9 packed vs VGA 256)."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # mov ax,[bufseg]; mov es,ax
    a.db(0x31, 0xFF).db(0x31, 0xC0)                     # xor di,di; xor ax,ax
    a.db(0xB9).bytes(_w(clear_words)).db(0xF3, 0xAB)    # mov cx,N; rep stosw (clear buffer)
    a.db(0x83, 0x06).abs16("scroll").db(_SCROLL_SPEED)  # add word[scroll], speed
    for ch in range(3):                                 # phase each tone channel by scroll
        _emit_wave_setup(a, ch, "rst")
        a.db(0xC7, 0x06).abs16("prevy", ch * 2).bytes(_w(colors[ch][2]))  # prevy[ch]=cen_y
    a.db(0xC7, 0x06).abs16("prev_my").bytes(_w(_MASTER_CEN_Y))  # prev_my=158
    a.db(0x31, 0xED)                                    # xor bp,bp  (L = column 0..69)
    a.label("xloop")
    a.db(0xC6, 0x06).abs16("msum").db(0x00)             # mov byte[msum],0
    for ch in range(3):
        a.db(0xBB).bytes(_w(colors[ch][4])).db(0x01, 0xEB)  # mov bx,col_start; add bx,bp
        _emit_channel_draw(a, ch, colors)
    a.db(0xBB).bytes(_w(colors[3][4])).db(0x01, 0xEB)  # mov bx,86; add bx,bp (noise)
    _emit_noise_draw(a, colors)
    _emit_master_draw(a, white)
    a.db(0x45).db(0x83, 0xFD, _CHW)                    # inc bp; cmp bp,70
    a.db(0x73).rel8("xdone").db(0xE9).rel16("xloop")    # jae xdone; jmp xloop
    a.label("xdone")
    for fr in _FRAMES:                                  # frames on top
        _emit_frame(a, *fr, white)
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

# ---- waveform-aware scope traces --------------------------------------------
# The scopes used to draw a two-level square: each column picked HI or LO from a
# flip-flop. That's a lie whenever the build isn't sounding squares (a
# SoundBlaster wavetable, or the speaker's high-rate PWM modelling), where a
# sine and a 12.5% pulse looked identical. Now each channel runs a PHASE
# ACCUMULATOR (one full cycle per 2*period columns) and reads its height from a
# baked shape table, so the trace has the real waveform's contour. A square's
# table is still exactly +/-amp, so square builds look as they always did.
_WSHAPE_N = 16                  # phase steps per cycle (top 4 bits of the phase)


def _wave_value(waveform: str, ph: float) -> float:
    """One cycle of `waveform` at phase `ph` (0..1), in -1..+1."""
    import math
    if waveform in _WAVE_DUTIES:
        return 1.0 if ph < _WAVE_DUTIES[waveform] else -1.0
    if waveform == "triangle":
        return 4.0 * ph - 1.0 if ph < 0.5 else 3.0 - 4.0 * ph
    if waveform == "nestri":                          # 4-bit stepped triangle
        t = 4.0 * ph - 1.0 if ph < 0.5 else 3.0 - 4.0 * ph
        return round((t + 1.0) * 7.5) / 7.5 - 1.0
    if waveform == "sine":
        return math.sin(2 * math.pi * ph)
    return 1.0 if ph < 0.5 else -1.0                  # square


def _wave_shape(waveform: str, amp: int, n: int = _WSHAPE_N) -> bytes:
    """`n` signed offsets over one cycle, scaled to +/-amp. NEGATIVE is UP the
    screen (a row/scanline above the centre), matching the drawing code."""
    return bytes(round(-_wave_value(waveform, (i + 0.5) / n) * amp) & 0xFF
                 for i in range(n))


def _emit_wave_setup(a: "_Asm", ch: int, pfx: str) -> None:
    """Once per frame: this channel's phase STEP (a full cycle every 2*period
    columns) and its starting phase, carried from the scroll counter so the
    trace keeps sliding smoothly from frame to frame."""
    a.db(0xA0).abs16("viz", ch).db(0x08, 0xC0)          # mov al,[viz+ch]; or al,al
    a.db(0x75).rel8(f"{pfx}{ch}").db(0xB0, 0x01)        # jnz ok; mov al,1 (avoid /0)
    a.label(f"{pfx}{ch}")
    a.db(0x30, 0xE4).db(0x89, 0xC3).db(0x01, 0xDB)      # xor ah,ah; bx=ax; bx+=bx (2P)
    a.db(0xBA, 0x01, 0x00).db(0x31, 0xC0)               # dx=1; ax=0  (0x10000)
    a.db(0xF7, 0xF3)                                    # div bx -> ax = 65536/(2P)
    a.db(0xA3).abs16("wstep", ch * 2)                   # [wstep+ch] = step
    a.db(0xF7, 0x26).abs16("scroll")                    # mul word[scroll] (dx:ax)
    a.db(0xA3).abs16("wphase", ch * 2)                  # [wphase+ch] = scroll*step


def _emit_wave_pick(a: "_Asm", ch: int, rc: int, pfx: str,
                    shape: str = "wshape", msum: bool = True,
                    quarters: bool = False) -> None:
    """Per column: CX = the row/scanline this channel's wave sits on. Advances
    the phase, reads the baked shape, and feeds the master sum the wave's
    direction. A silent channel flatlines on its centre."""
    a.db(0x80, 0x3E).abs16("viz", ch).db(0x00)          # cmp byte[viz+ch],0
    a.db(0x75).rel8(f"{pfx}_s")                         # jne sounding
    if quarters:
        # a silent channel has NO leftover: without this the cap state survives
        # from the previous channel and paints a stray glyph into its band
        a.db(0xC6, 0x06).abs16("wrem").db(0x00)         # mov byte[wrem],0
    a.db(0xB9).bytes(_w(rc))                            # mov cx,centre
    a.db(0xEB).rel8(f"{pfx}_d")                         # jmp done
    a.label(f"{pfx}_s")
    a.db(0xA1).abs16("wphase", ch * 2)                  # mov ax,[wphase+ch]
    a.db(0x03, 0x06).abs16("wstep", ch * 2)             # add ax,[wstep+ch]
    a.db(0xA3).abs16("wphase", ch * 2)                  # mov [wphase+ch],ax
    a.db(0x88, 0xE0)                                    # mov al,ah (phase high byte)
    a.db(0xD0, 0xE8, 0xD0, 0xE8, 0xD0, 0xE8, 0xD0, 0xE8)   # shr al,1 x4 -> 0..15
    a.db(0x30, 0xE4).db(0x89, 0xC6)                     # xor ah,ah; mov si,ax
    a.db(0x8A, 0x84).abs16(shape)                       # mov al,[si+shape]
    a.db(0x98)                                          # cbw (signed offset)
    if quarters:
        # ax = signed QUARTER rows -> whole rows in ax, leftover in [wrem],
        # travel direction in [wdir] (1 = up the screen)
        a.db(0xC6, 0x06).abs16("wdir").db(0x00)         # mov byte[wdir],0
        a.db(0x09, 0xC0).db(0x79).rel8(f"{pfx}_qp")     # or ax,ax; jns positive
        a.db(0xF7, 0xD8)                                # neg ax (|quarters|)
        a.db(0xC6, 0x06).abs16("wdir").db(0x01)         # mov byte[wdir],1 (upward)
        a.label(f"{pfx}_qp")
        a.db(0x50)                                      # push ax (|quarters|)
        a.db(0x24, 0x03).db(0xA2).abs16("wrem")         # and al,3; [wrem]=leftover
        a.db(0x58)                                      # pop ax
        a.db(0xD1, 0xE8).db(0xD1, 0xE8)                 # shr ax,1 x2 (whole rows)
        a.db(0x80, 0x3E).abs16("wdir").db(0x00)         # cmp byte[wdir],0
        a.db(0x74).rel8(f"{pfx}_qd")                    # je downward
        a.db(0xF7, 0xD8)                                # neg ax (back above centre)
        a.label(f"{pfx}_qd")
    a.db(0xB9).bytes(_w(rc)).db(0x01, 0xC1)             # mov cx,centre; add cx,ax
    if quarters:
        a.db(0x89, 0x0E).abs16("wrow")                  # [wrow]=cx (last full row)
    if msum:                                            # feed the master trace
        a.db(0x09, 0xC0).db(0x74).rel8(f"{pfx}_d")      # or ax,ax; jz done
        a.db(0x78).rel8(f"{pfx}_u")                     # js up (above centre)
        a.db(0xFE, 0x0E).abs16("msum")                  # dec byte[msum]
        a.db(0xEB).rel8(f"{pfx}_d")                     # jmp done
        a.label(f"{pfx}_u")
        a.db(0xFE, 0x06).abs16("msum")                  # inc byte[msum]
    a.label(f"{pfx}_d")
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
    """One tone channel column (BP): fill from the centre row to the wave's
    height, then CAP the column with a partial glyph for the leftover QUARTER
    rows — so an 80x25 text screen resolves ~17 heights in a 5-row band instead
    of 5. The cap reads as a real sub-cell step: a half block (▀/▄) for the
    half, and the shading glyphs (░ / ▓) for the quarter and three-quarter."""
    rc = _TCEN[ch]
    _emit_wave_pick(a, ch, rc, f"tc{ch}", shape="wshapeq", quarters=True)
    # CX = the last FULL row, [wrem] = leftover quarters, [wdir] = 1 when the
    # wave runs UP the screen (the cap then sits one row further that way).
    a.db(0xB8).bytes(_w(rc))                            # mov ax,centre
    a.db(0x39, 0xC8).db(0x76).rel8(f"tc{ch}_o").db(0x91)  # cmp ax,cx; jbe o; xchg
    a.label(f"tc{ch}_o")
    a.db(0xA3).abs16("ftop").db(0x89, 0x0E).abs16("fbot")   # ftop=top; fbot=bottom
    a.db(0x89, 0xEB).db(0xB8).bytes(_w(_tcolor(ch))).db(0xE8).rel16("tfill")  # bx=col;ax=cc;call tfill
    _emit_wave_cap(a, ch, f"tc{ch}")


#: Sub-cell cap glyphs: a quarter of a row reads as light shading, a half as a
#: real half block (oriented by travel), three quarters as dark shading.
_CAP_QUARTER, _CAP_THREE = 0xB0, 0xB2      # ░ ▓
_CAP_HALF_UP, _CAP_HALF_DOWN = 0xDC, 0xDF  # ▄ (filled at the bottom) / ▀


def _emit_wave_cap(a: "_Asm", ch: int, pfx: str) -> None:
    """Draw the partial cell just past the filled column, when the wave's height
    carries leftover quarter-rows. BP is the column; clobbers AX/BX/CX."""
    a.db(0x80, 0x3E).abs16("wrem").db(0x00)             # cmp byte[wrem],0
    a.db(0x74).rel8(f"{pfx}_nc")                        # je no-cap
    a.db(0x8B, 0x0E).abs16("wrow")                      # mov cx,[wrow] (last full row)
    a.db(0x80, 0x3E).abs16("wdir").db(0x00)             # cmp byte[wdir],0
    a.db(0x74).rel8(f"{pfx}_cd")                        # je going-down
    a.db(0x49)                                          # dec cx (the cap sits above)
    a.db(0xB0, _CAP_HALF_UP).db(0xEB).rel8(f"{pfx}_ch")  # mov al,half-up; jmp have
    a.label(f"{pfx}_cd")
    a.db(0x41)                                          # inc cx (the cap sits below)
    a.db(0xB0, _CAP_HALF_DOWN)                          # mov al,half-down
    a.label(f"{pfx}_ch")
    # a half-row uses the block; a quarter/three-quarter uses shading instead
    a.db(0x80, 0x3E).abs16("wrem").db(0x02)             # cmp byte[wrem],2
    a.db(0x74).rel8(f"{pfx}_cw")                        # je write (keep the half block)
    a.db(0xB0, _CAP_QUARTER)                            # mov al,light shade
    a.db(0x80, 0x3E).abs16("wrem").db(0x01)             # cmp byte[wrem],1
    a.db(0x74).rel8(f"{pfx}_cw")                        # je write
    a.db(0xB0, _CAP_THREE)                              # mov al,dark shade
    a.label(f"{pfx}_cw")
    a.db(0xB4, _TATTR[ch])                              # mov ah,attr (channel colour)
    a.db(0x89, 0xEB)                                    # mov bx,bp (column)
    a.db(0xE8).rel16("tcell")
    a.label(f"{pfx}_nc")


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
        _emit_wave_setup(a, ch, "tr")
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
    _emit_wave_pick(a, ch, rc, f"u{ch}")
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
        _emit_wave_setup(a, ch, "ur")
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
    _emit_wave_pick(a, ch, rc, f"v{ch}")
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
        _emit_wave_setup(a, ch, "w3")
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
# box-drawing junctions for the one unified grid (where │ and ─ meet)
_BOX_CROSS, _BOX_TDOWN, _BOX_TUP = 0xC5, 0xC2, 0xC1     # ┼ ┬ ┴
_BOX_TRIGHT, _BOX_TLEFT = 0xC3, 0xB4                    # ├ ┤
_T5_VD = 39                          # the single full-height vertical divider column
_T5_HD = (6, 12)                     # horizontal divider rows (scope mid, scope/bottom)
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
    _emit_wave_pick(a, ch, cen, f"z{ch}", msum=False)
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
    """Draw one spectrum column BX: [sbar_h] blocks up from row 23, coloured by row
    via srowcol, with a ▄ half-block top edge. (No peak cap -- the spectrum bars
    read cleaner without the white markers; the VU meters keep their peaks.)"""
    a.label("t5sdraw")
    a.db(0xA0).abs16("sbar_h")                          # al=[sbar_h]
    a.db(0x08, 0xC0).db(0x74).rel8("t5s_done")          # or al,al; jz done (empty)
    a.db(0x88, 0xC6).db(0xD0, 0xEE)                     # dh=al; shr dh,1 (full rows)
    a.db(0xB2, _T5_SPEC_BASE)                           # dl=baseline row
    a.label("t5s_full")
    a.db(0x08, 0xF6).db(0x74).rel8("t5s_part")          # or dh,dh; jz partial
    a.db(0x88, 0xD1).db(0x30, 0xED)                     # cx=row
    a.db(0x89, 0xCE).db(0x8A, 0xA4).abs16("srowcol")    # si=cx; ah=[si+srowcol]
    a.db(0xB0, _BLK_FULL).db(0xE8).rel16("tcell")       # █
    a.db(0xFE, 0xCA).db(0xFE, 0xCE).db(0xEB).rel8("t5s_full")   # dec dl; dec dh; loop
    a.label("t5s_part")
    a.db(0xA0).abs16("sbar_h").db(0xA8, 0x01).db(0x74).rel8("t5s_done")   # test 1; jz done
    a.db(0x88, 0xD1).db(0x30, 0xED)
    a.db(0x89, 0xCE).db(0x8A, 0xA4).abs16("srowcol")
    a.db(0xB0, _BLK_LOWER).db(0xE8).rel16("tcell")      # ▄
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


def _emit_t5grid(a: "_Asm") -> None:
    """Draw ONE grid for the whole screen: a full outer square (0,0)-(79,24) with
    a full-height vertical divider at col 39 and horizontal dividers at rows 6 and
    12, joined with box-drawing junctions (┼ ┬ ┴ ├ ┤). That partitions the screen
    into the six cells -- four scopes, spectrum, VU -- with a single clean frame
    instead of six overlapping boxes. Grey; uses tcell (AH=colour survives)."""
    vd, (h1, h2) = _T5_VD, _T5_HD
    a.label("t5grid")
    a.db(0xB4, _T3_FRAME_ATTR)                          # mov ah, grey
    # -- top edge (row 0): ┌ ─ ┬ ─ ┐ --
    a.db(0xB9, 0x00, 0x00).db(0xBB, 0x00, 0x00)         # cx=0; bx=0
    a.label("g_top")
    a.db(0xB0, _BOX_H)                                  # al=─
    a.db(0x83, 0xFB, 0x00).db(0x75).rel8("g_t1").db(0xB0, _BOX_TL)     # bx==0 -> ┌
    a.label("g_t1")
    a.db(0x83, 0xFB, 79).db(0x75).rel8("g_t2").db(0xB0, _BOX_TR)       # bx==79 -> ┐
    a.label("g_t2")
    a.db(0x83, 0xFB, vd).db(0x75).rel8("g_t3").db(0xB0, _BOX_TDOWN)    # bx==vd -> ┬
    a.label("g_t3")
    a.db(0xE8).rel16("tcell")
    a.db(0x43).db(0x83, 0xFB, 80).db(0x72).rel8("g_top")   # inc bx; cmp bx,80; jb
    # -- bottom edge (row 24): └ ─ ┴ ─ ┘ --
    a.db(0xB9, 24, 0x00).db(0xBB, 0x00, 0x00)
    a.label("g_bot")
    a.db(0xB0, _BOX_H)
    a.db(0x83, 0xFB, 0x00).db(0x75).rel8("g_b1").db(0xB0, _BOX_BL)
    a.label("g_b1")
    a.db(0x83, 0xFB, 79).db(0x75).rel8("g_b2").db(0xB0, _BOX_BR)
    a.label("g_b2")
    a.db(0x83, 0xFB, vd).db(0x75).rel8("g_b3").db(0xB0, _BOX_TUP)
    a.label("g_b3")
    a.db(0xE8).rel16("tcell")
    a.db(0x43).db(0x83, 0xFB, 80).db(0x72).rel8("g_bot")
    # -- left edge (col 0), rows 1..23: │ with ├ at the divider rows --
    a.db(0xBB, 0x00, 0x00).db(0xB9, 0x01, 0x00)
    a.label("g_left")
    a.db(0xB0, _BOX_V)
    a.db(0x83, 0xF9, h1).db(0x74).rel8("g_lj")          # cx==6 -> ├
    a.db(0x83, 0xF9, h2).db(0x75).rel8("g_l1")          # cx!=12 -> keep │
    a.label("g_lj")
    a.db(0xB0, _BOX_TRIGHT)
    a.label("g_l1")
    a.db(0xE8).rel16("tcell")
    a.db(0x41).db(0x83, 0xF9, 24).db(0x72).rel8("g_left")   # inc cx; cmp cx,24; jb
    # -- right edge (col 79), rows 1..23: │ with ┤ at the divider rows --
    a.db(0xBB, 79, 0x00).db(0xB9, 0x01, 0x00)
    a.label("g_right")
    a.db(0xB0, _BOX_V)
    a.db(0x83, 0xF9, h1).db(0x74).rel8("g_rj")
    a.db(0x83, 0xF9, h2).db(0x75).rel8("g_r1")
    a.label("g_rj")
    a.db(0xB0, _BOX_TLEFT)
    a.label("g_r1")
    a.db(0xE8).rel16("tcell")
    a.db(0x41).db(0x83, 0xF9, 24).db(0x72).rel8("g_right")
    # -- horizontal dividers (rows 6, 12), cols 1..78: ─ with ┼ at the divider col --
    for tag, hd in (("h1", h1), ("h2", h2)):
        a.db(0xB9, hd, 0x00).db(0xBB, 0x01, 0x00)       # cx=hd; bx=1
        a.label(f"g_{tag}")
        a.db(0xB0, _BOX_H)
        a.db(0x83, 0xFB, vd).db(0x75).rel8(f"g_{tag}a").db(0xB0, _BOX_CROSS)   # bx==vd -> ┼
        a.label(f"g_{tag}a")
        a.db(0xE8).rel16("tcell")
        a.db(0x43).db(0x83, 0xFB, 79).db(0x72).rel8(f"g_{tag}")   # inc bx; cmp bx,79; jb
    # -- vertical divider (col 39), rows 1..23: │, skipping the ┼ crossings --
    a.db(0xBB, vd, 0x00).db(0xB9, 0x01, 0x00)           # bx=vd; cx=1
    a.label("g_v")
    a.db(0x83, 0xF9, h1).db(0x74).rel8("g_vs")          # cx==6 -> skip (┼ drawn already)
    a.db(0x83, 0xF9, h2).db(0x74).rel8("g_vs")          # cx==12 -> skip
    a.db(0xB0, _BOX_V).db(0xE8).rel16("tcell")
    a.label("g_vs")
    a.db(0x41).db(0x83, 0xF9, 24).db(0x72).rel8("g_v")
    a.db(0xC3)                                          # ret


def _emit_text5_drawframe(a: "_Asm") -> None:
    """The whole combined frame: clear, the 2x2 scope grid, the spectrum, the VU
    meters, then the one unified border grid."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)           # es=bufseg
    a.db(0xB8, 0x20, 0x07).db(0x31, 0xFF)               # ax=0x0720; di=0
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xAB)               # cx=2000; rep stosw (clear)
    a.db(0x83, 0x06).abs16("scroll").db(_SCROLL_SPEED)  # scroll += speed
    # ---- top: 2x2 scope grid ---------------------------------------------------
    for ch in range(3):                                 # phase each tone by scroll
        _emit_wave_setup(a, ch, "w5")
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
    a.db(0x88, 0x26).abs16("sbar_h")                    # sbar_h = ah (height to draw; no peak)
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
    # ---- one unified grid: outer square + cross-joined dividers ----------------
    a.db(0xE8).rel16("t5grid")
    a.db(0xC3)                                          # ret


# ---- lightweight VU-only display (XT-friendly) ------------------------------
# Just four full-width horizontal VU meters + a border -- a few hundred cell
# writes/frame, so it keeps up alongside the audio ISR on a real 4.77 MHz XT
# (where the full scopes/spectrum are too heavy). Onset-kick ballistics, same as
# text 5's meters, but roomier and labelled.
_VU_ROWS = (6, 11, 16, 21)            # one meter per channel
_VU_LABELS = ("Pulse 1 ", "Pulse 2 ", "Triangle", "Noise   ")   # 8 chars each
_VU_LABEL_COL = 3
_VU_COL = 13                          # bar start column
_VU_LEN = 62                          # bar length in cells (col 13..74)
_VU_MAX = 2 * _VU_LEN                 # full level in half-cells (kick target)
_VU_GREEN, _VU_YELLOW = 42, 52        # cell thresholds: <42 green, <52 yellow, else red
_VU_FALL, _VU_PEAK_FALL = 4, 1


def _emit_vu_colour(a: "_Asm", tag: str) -> None:
    """AH = green/yellow/red from the cell index in DL."""
    a.db(0xB4, _S4_GREEN)
    a.db(0x80, 0xFA, _VU_GREEN).db(0x72).rel8(tag)     # cmp dl,42; jb have
    a.db(0xB4, _S4_YELLOW)
    a.db(0x80, 0xFA, _VU_YELLOW).db(0x72).rel8(tag)    # cmp dl,52; jb have
    a.db(0xB4, _S4_RED)
    a.label(tag)


def _emit_vu_draw(a: "_Asm") -> None:
    """Draw one horizontal VU meter on row [vurow]: [vu_h] half-cells growing
    right (full block, ▌ for the odd half), coloured by cell, then a white │ peak
    tick at [vupeak_h]."""
    a.label("vudraw")
    a.db(0xA0).abs16("vu_h").db(0x88, 0xC6).db(0xD0, 0xEE)   # al=vu_h; dh=al; shr dh,1 (cells)
    a.db(0x8B, 0x0E).abs16("vurow")                    # cx=row
    a.db(0x30, 0xD2)                                   # xor dl,dl (cell index)
    a.label("vu_full")
    a.db(0x08, 0xF6).db(0x74).rel8("vu_part")          # or dh,dh; jz partial
    _emit_vu_colour(a, "vu_c1")
    a.db(0x88, 0xD3).db(0x80, 0xC3, _VU_COL).db(0x30, 0xFF)   # bl=dl; add bl,col; bh=0
    a.db(0xB0, _BLK_FULL).db(0xE8).rel16("tcell")      # █
    a.db(0xFE, 0xC2).db(0xFE, 0xCE).db(0xEB).rel8("vu_full")  # inc dl; dec dh; loop
    a.label("vu_part")
    a.db(0xA0).abs16("vu_h").db(0xA8, 0x01).db(0x74).rel8("vu_peak")   # test vu_h,1; jz peak
    _emit_vu_colour(a, "vu_c2")
    a.db(0x88, 0xD3).db(0x80, 0xC3, _VU_COL).db(0x30, 0xFF)
    a.db(0xB0, _BLK_LEFT).db(0xE8).rel16("tcell")      # ▌ (partial right edge)
    a.label("vu_peak")
    a.db(0xA0).abs16("vupeak_h").db(0x08, 0xC0).db(0x74).rel8("vu_done")   # or al,al; jz done
    a.db(0xD0, 0xE8)                                   # shr al,1 (peak cell)
    a.db(0x88, 0xC3).db(0x80, 0xC3, _VU_COL).db(0x30, 0xFF)
    a.db(0xB4, _S4_PEAK_ATTR).db(0xB0, _BOX_V).db(0xE8).rel16("tcell")   # white │
    a.label("vu_done")
    a.db(0xC3)


def _emit_vu_channel(a: "_Asm", ch: int, row: int) -> None:
    """One meter: onset-kick (strike -> full) or slow release, peak-hold, label,
    then draw the bar."""
    lbl = _VU_LABELS[ch]
    a.db(0xA0).abs16("strike", ch).db(0x08, 0xC0)      # al=[strike+ch]; or al,al
    a.db(0x74).rel8(f"vq{ch}d")                        # jz decay
    a.db(0xC6, 0x06).abs16("strike", ch).db(0x00)      # strike[ch]=0
    a.db(0xC6, 0x06).abs16("vu", ch).db(_VU_MAX)       # vu[ch]=MAX (kick)
    a.db(0xEB).rel8(f"vq{ch}p")
    a.label(f"vq{ch}d")
    a.db(0xA0).abs16("vu", ch).db(0x2C, _VU_FALL)      # al=vu[ch]; sub al,FALL
    a.db(0x73).rel8(f"vq{ch}s").db(0x30, 0xC0)         # jnc set; xor al,al
    a.label(f"vq{ch}s")
    a.db(0xA2).abs16("vu", ch)                         # vu[ch]=al
    a.label(f"vq{ch}p")
    a.db(0x8A, 0x26).abs16("vu", ch)                   # ah=vu[ch]
    a.db(0xA0).abs16("vupeak", ch)                     # al=vupeak[ch]
    a.db(0x38, 0xC4).db(0x76).rel8(f"vq{ch}pd")        # cmp ah,al; jbe pdec
    a.db(0x88, 0x26).abs16("vupeak", ch).db(0xEB).rel8(f"vq{ch}dr")   # vupeak=vu
    a.label(f"vq{ch}pd")
    a.db(0x2C, _VU_PEAK_FALL).db(0x73).rel8(f"vq{ch}ps").db(0x30, 0xC0)
    a.label(f"vq{ch}ps")
    a.db(0xA2).abs16("vupeak", ch)                     # vupeak[ch]=al
    a.label(f"vq{ch}dr")
    a.db(0xB9).bytes(_w(row)).db(0xBB).bytes(_w(_VU_LABEL_COL))   # cx=row; bx=label col
    a.db(0xB4, _TATTR[ch])                             # ah=channel colour
    for chch in lbl:
        a.db(0xB0, ord(chch)).db(0xE8).rel16("tcell").db(0x43)   # al=char; tcell; inc bx
    a.db(0xA0).abs16("vu", ch).db(0xA2).abs16("vu_h")            # vu_h = vu[ch]
    a.db(0xA0).abs16("vupeak", ch).db(0xA2).abs16("vupeak_h")   # vupeak_h = vupeak[ch]
    a.db(0xC7, 0x06).abs16("vurow").bytes(_w(row))              # vurow = row
    a.db(0xE8).rel16("vudraw")


def _emit_vu_drawframe(a: "_Asm") -> None:
    """Clear, draw the four labelled meters, then the border."""
    a.label("drawframe")
    a.db(0xA1).abs16("bufseg").db(0x8E, 0xC0)          # es=bufseg
    a.db(0xB8, 0x20, 0x07).db(0x31, 0xFF)              # ax=0x0720; di=0
    a.db(0xB9, 0xD0, 0x07).db(0xF3, 0xAB)              # cx=2000; rep stosw (clear)
    for ch in range(4):
        _emit_vu_channel(a, ch, _VU_ROWS[ch])
    a.db(0xC7, 0x06).abs16("fx0").bytes(_w(0))         # full-screen border
    a.db(0xC7, 0x06).abs16("fx1").bytes(_w(79))
    a.db(0xC7, 0x06).abs16("fy0").bytes(_w(0))
    a.db(0xC7, 0x06).abs16("fy1").bytes(_w(24))
    a.db(0xE8).rel16("t3frame")
    a.db(0xC3)


def _emit_scope_code(a: "_Asm", vis: str) -> None:
    """Emit the scope renderer routines + baked tables for `vis`. Audio-agnostic:
    they only read the viz[]/strike[] tables and draw to the back buffer, so both
    the Tandy engine and the 4-voice engine share them."""
    if vis == "graphics":
        _emit_ploty(a)
        _emit_vline(a)
        _emit_hline(a)
        _emit_drawframe(a)
        _emit_blit(a)
        a.label("rowaddr")                           # mode-9 byte offset of each scanline
        a.bytes(struct.pack(f"<{len(_ROWADDR)}H", *_ROWADDR))
    elif vis == "vga":                               # 320x200x256 VGA (universal)
        _emit_ploty13(a)                             # linear pixel writer (label 'ploty')
        _emit_vline(a)
        _emit_hline(a)
        _emit_drawframe(a, _CH13, _VGA_WHITE, 32000)
        _emit_blit13(a)
        a.label("rowaddr")                           # linear byte offset of each scanline
        a.bytes(struct.pack(f"<{len(_ROWADDR13)}H", *_ROWADDR13))
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
        _emit_t5grid(a)                              # one unified border grid
        _emit_t5spec_drawcol(a)
        _emit_t5vu_draw(a)
        _emit_text5_drawframe(a)
        _emit_text_blit(a)
        a.label("textrow")
        a.bytes(struct.pack(f"<{len(_TEXTROW)}H", *_TEXTROW))
        a.label("sharm"); a.bytes(_t5_spec_harm())   # spectrum harmonic bins (18 bars)
        a.label("srowcol"); a.bytes(_t5_spec_rowcol())   # spectrum row colours
    elif vis == "vu":
        _emit_tcell(a)
        _emit_t3frame(a)                             # the box-drawing border
        _emit_vu_draw(a)
        _emit_vu_drawframe(a)
        _emit_text_blit(a)
        a.label("textrow")
        a.bytes(struct.pack(f"<{len(_TEXTROW)}H", *_TEXTROW))


def _emit_scope_vars(a: "_Asm", vis: str, wave: str = "square") -> None:
    """Emit the shared draw state + the per-vis scope variables. (viz[]/strike[]
    are emitted by the caller, right before this.) `wave` is the waveform the
    build actually sounds, baked into the trace shape tables."""
    if vis:                                          # shared draw state
        a.label("bufseg"); a.db(0x00, 0x00)              # back-buffer segment
        a.label("msum"); a.db(0x00)                       # master-sum accumulator
        a.label("scroll"); a.db(0x00, 0x00)              # horizontal scroll phase
        a.label("seed"); a.bytes(struct.pack("<H", _NOISE_SEED))   # noise PRNG state
        # one wave cycle per channel: phase + per-column step, and the contour
        a.label("wphase"); a.bytes(bytes(8))
        a.label("wstep"); a.bytes(bytes(8))
        a.label("wshape"); a.bytes(_wave_shape(wave, _TAMP))       # text rows
        a.label("wshapeq"); a.bytes(_wave_shape(wave, _TAMP * 4))  # quarter rows
        a.label("wrem"); a.db(0x00)                      # leftover quarters
        a.label("wdir"); a.db(0x00)                      # 1 = wave runs up
        a.label("wrow"); a.db(0x00, 0x00)                # last fully-filled row
        if vis in ("graphics", "vga"):
            a.label("wshapeg"); a.bytes(_wave_shape(wave, _GAMP))  # scanlines
    if vis in ("graphics", "vga"):                   # mode-9 / VGA vline/frame state
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
        a.label("stgt"); a.bytes(bytes(_T5_SPEC_N))      # spectrum target/current
        a.label("sbar"); a.bytes(bytes(_T5_SPEC_N))
        a.label("sbar_h"); a.db(0x00)                    # spectrum drawcol height
        a.label("vu"); a.db(0x00, 0x00, 0x00, 0x00)      # VU level/peak per channel
        a.label("vupeak"); a.db(0x00, 0x00, 0x00, 0x00)
        a.label("vu_h"); a.db(0x00)                      # VU drawcol level/peak
        a.label("vupeak_h"); a.db(0x00)
        a.label("t5row"); a.db(0x00, 0x00)               # current VU meter row
    elif vis == "vu":                                # lightweight VU-only state
        a.label("vu"); a.db(0x00, 0x00, 0x00, 0x00)      # VU level per channel
        a.label("vupeak"); a.db(0x00, 0x00, 0x00, 0x00)  # peak-hold per channel
        a.label("vu_h"); a.db(0x00)                      # drawcol level/peak
        a.label("vupeak_h"); a.db(0x00)
        a.label("vurow"); a.db(0x00, 0x00)               # current meter row
        a.label("fx0"); a.db(0x00, 0x00)                 # border rectangle
        a.label("fx1"); a.db(0x00, 0x00)
        a.label("fy0"); a.db(0x00, 0x00)
        a.label("fy1"); a.db(0x00, 0x00)


def _emit_draw_wait(a: "_Asm", draw_skip: int) -> None:
    """Foreground scope loop (label 'wait'): render the frame off-screen, wait
    `draw_skip` vertical retraces (the throttle -- 1 = every frame/60 fps, higher
    = lighter/slower), blit on retrace, then poll for a quit key. Shared by both
    engines; calls the `drawframe`/`blit` routines the vis emitted."""
    a.label("wait")
    a.db(0xE8).rel16("drawframe")                    # render the whole frame off-screen
    if draw_skip > 1:
        a.db(0xB9).bytes(_w(draw_skip))              # mov cx, draw_skip (retraces to wait)
        a.label("dpace")
    a.db(0xBA, 0xDA, 0x03)                           # mov dx,0x3DA (CRTC status)
    a.label("vs1"); a.db(0xEC).db(0xA8, 0x08).db(0x75).rel8("vs1")   # wait end of retrace
    a.label("vs2"); a.db(0xEC).db(0xA8, 0x08).db(0x74).rel8("vs2")   # wait retrace start
    if draw_skip > 1:
        a.db(0xE2).rel8("dpace")                     # loop dpace (wait draw_skip retraces)
    a.db(0xE8).rel16("blit")                         # copy back buffer -> screen
    a.db(0xB4, 0x01).db(0xCD, 0x16).db(0x74).rel8("wait")   # int16 ah=1; jz wait
    a.db(0x30, 0xE4).db(0xCD, 0x16)                  # consume the key


def _assemble(divider: int, subdiv: int, total_ticks: int, silence: bytes,
              stream: bytes, vis: str = "", draw_skip: int = 1,
              wave: str = "square") -> bytes:
    """The engine: (optionally set a graphics/text video mode), install the timer
    ISR, run — waiting for a key or redrawing the scopes — then tear down.
    Followed by the silence record and per-sub-tick event stream. `vis` is "",
    "graphics" (mode 9) or "text" (mode 3)."""
    a = _Asm()
    if vis:
        video_mode = (0x03 if vis.startswith("text") else 0x13 if vis == "vga"
                      else 0x09)                        # text3 / VGA13h / Tandy9
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
        _emit_draw_wait(a, draw_skip)
    else:
        a.label("wait")
        a.db(0xF4)                                   # hlt: sleep until the next IRQ
        #  (don't spin int 0x16 -- a tight poll on a very fast CPU / DOSBox
        #   cycles=max starves the timer and makes playback skip)
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
    _emit_scope_code(a, vis)                          # scope renderers (shared)
    # ---- variables ----------------------------------------------------------
    a.label("old_off"); a.db(0x00, 0x00)
    a.label("old_seg"); a.db(0x00, 0x00)
    a.label("streamptr"); a.db(0x00, 0x00)
    a.label("ticksleft"); a.db(0x00, 0x00)
    a.label("subcount"); a.db(subdiv & 0xFF)
    a.label("viz"); a.db(0x00, 0x00, 0x00, 0x00)     # per-channel scope periods
    a.label("strike"); a.db(0x00, 0x00, 0x00, 0x00)  # per-channel note-on latch (VU)
    _emit_scope_vars(a, vis, wave)                    # shared + per-vis draw state
    # ---- appended data ------------------------------------------------------
    a.label("silence"); a.bytes(silence)
    a.label("stream"); a.bytes(stream)
    return a.resolve()


def _tandy_silence() -> List[Tuple[int, int]]:
    return [(_SN76489, 0x9F), (_SN76489, 0xBF),      # attenuate tone 0,1,2
            (_SN76489, 0xDF), (_SN76489, 0xFF)]      # and the noise channel


# static-screen legend: text drawn once via the BIOS over the baked bitmap.
# (col in 40x25 text cells, CGA colour, label) -- colour matches the voice.
_STATIC_LEGEND = ((1, _CGA_YELLOW, "P1"), (6, _CGA_GREEN, "P2"),
                  (11, _CGA_RED, "Tri"), (17, _CGA_YELLOW, "Drums"))


def _emit_static_setup(a: "_Asm") -> None:
    """One-time setup for the static-screen player: CGA mode 4, palette 0 +
    intensity (yellow/green/red/black), blit the baked poster to B800, then write
    the colour legend across the top with the BIOS. After this the foreground just
    plays + polls the keyboard -- no display updates at all."""
    a.db(0xB8, 0x04, 0x00).db(0xCD, 0x10)            # mov ax,0x0004; int 0x10 (CGA 320x200x4)
    a.db(0xBA, 0xD9, 0x03).db(0xB0, 0x10).db(0xEE)   # mov dx,0x3D9; al=0x10; out (palette0+intensity)
    a.db(0xB8, 0x00, 0xB8).db(0x8E, 0xC0)            # es = B800
    a.db(0xBE).abs16("poster")                       # mov si, poster (ds=cs)
    a.db(0x31, 0xFF).db(0xB9).bytes(_w(0x2000)).db(0xF3, 0xA5)   # di=0; cx=8192; rep movsw (16 KB)
    for col, colour, text in _STATIC_LEGEND:         # BIOS legend text over the bitmap
        a.db(0xB4, 0x02).db(0x30, 0xFF).db(0xBA, col, 0x00).db(0xCD, 0x10)  # set cursor row0,col
        for chch in text:
            a.db(0xB4, 0x0E).db(0xB3, colour).db(0xB0, ord(chch)).db(0xCD, 0x10)  # teletype char


_SPK4_MCS_PULSE = 24             # timer-2 one-shot pulse width in PIT ticks (MCS drive)

# SoundBlaster output: a real 8-bit DAC, so we drop the 1-bit PWM tricks entirely
# and mix the voices at full amplitude (the same phase accumulators, but summed as
# signed levels and written to the DSP), which reproduces the NES/PT3 waveform far
# more faithfully than the 1-bit speaker. Uses DSP "direct DAC" (command 0x10), one
# sample per timer interrupt -- no DMA -- so it drops straight into the ISR engine.
_SB_PORT = 0x220                 # default SoundBlaster base I/O port (BLASTER A220)
_SB_AMP = 30                     # per-voice amplitude in the 8-bit (0..255) mix

# Waveform tables: 256 entries spanning one oscillator cycle, indexed by the
# phase accumulator's HIGH BYTE (no shifting needed on the 8088 — the natural
# 256-entry table makes the lookup a single mov). The SB engine uses SIGNED
# entries (+/-_SB_AMP); the speaker's high-rate PWM path uses UNSIGNED levels
# 0.._SPK4_WAVE_FULL per voice, delta-sigma'd as a multi-level sum.
_SPK4_WAVE_FULL = 20             # per-voice full level in the PWM wavetable path
_SPK4_WAVE_MIN_RATE = 12000      # PWM waveform modeling needs an inaudible carrier
_WAVE_DUTIES = {"pulse12": 0.125, "pulse25": 0.25, "pulse50": 0.5,
                "pulse75": 0.75, "square": 0.5}


def _wave_table(waveform: str, amp: int, signed: bool) -> bytes:
    """One 256-entry cycle of `waveform` at +/-amp (signed) or 0..2*amp levels."""
    import math
    out = bytearray(256)
    for i in range(256):
        ph = i / 256.0
        if waveform in _WAVE_DUTIES:
            v = 1.0 if ph < _WAVE_DUTIES[waveform] else -1.0
        elif waveform == "triangle":
            v = 4.0 * ph - 1.0 if ph < 0.5 else 3.0 - 4.0 * ph
        elif waveform == "nestri":                    # 4-bit stepped triangle
            t = 4.0 * ph - 1.0 if ph < 0.5 else 3.0 - 4.0 * ph
            v = round((t + 1.0) * 7.5) / 7.5 - 1.0
        else:                                         # sine
            v = math.sin(2 * math.pi * ph)
        out[i] = (round(v * amp) & 0xFF) if signed else round((v + 1.0) * amp)
    return bytes(out)


# SoundBlaster VOLUME: the DAC can play a note quietly, so each voice indexes a
# table scaled to its note's velocity instead of blasting every note at full
# amplitude. _SB_LEVELS tables of 256 entries sit back to back, so a voice's
# table base is wavetbl + level*256 -- one pointer per voice, no per-sample
# multiply (an 8088 mul would cost more than the whole synth).
_SB_LEVELS = 8                   # volume steps 0 (silent) .. 7 (full _SB_AMP)


def _sb_wave_bank(waveform: str) -> bytes:
    """The volume-scaled wavetable bank: level v plays at v/7 of full amplitude."""
    return b"".join(_wave_table(waveform, round(_SB_AMP * v / (_SB_LEVELS - 1)),
                                signed=True)
                    for v in range(_SB_LEVELS))


def _sb_level(velocity: int) -> int:
    """Note velocity (0-127) -> a volume level. A sounding note never falls to
    level 0 (silence) -- that's what a note-OFF is for."""
    return max(1, min(_SB_LEVELS - 1, round(velocity * (_SB_LEVELS - 1) / 100.0)))


def _emit_sb_write(a: "_Asm", value: int) -> None:
    """Poll the DSP write-status port (base+0xC bit7) then write command `value`.
    DX must be base+0xC on entry (and is left there)."""
    a.db(0xEC).db(0xA8, 0x80).db(0x75, 0xFB)         # .w: in al,dx; test al,0x80; jnz .w (-5)
    a.db(0xB0, value).db(0xEE)                       # mov al, value; out dx, al


def _emit_sb_init(a: "_Asm", sb_port: int) -> None:
    """Reset the DSP, then turn the speaker (DAC output) on. No sample-rate setup:
    'direct DAC' plays each byte the instant we write it, so the timer sets the rate."""
    a.db(0xBA).bytes(_w(sb_port + 0x06))             # mov dx, base+6 (reset)
    a.db(0xB0, 0x01).db(0xEE)                        # al=1; out (assert reset)
    a.db(0xB9).bytes(_w(200)).db(0xE2, 0xFE)         # mov cx,200; .d: loop .d (~delay)
    a.db(0x30, 0xC0).db(0xEE)                        # al=0; out (release reset)
    a.db(0xBA).bytes(_w(sb_port + 0x0E))             # mov dx, base+0xE (read-status)
    a.db(0xEC).db(0xA8, 0x80).db(0x74, 0xFB)         # .r: in al,dx; test al,0x80; jz .r (wait ready)
    a.db(0xBA).bytes(_w(sb_port + 0x0A)).db(0xEC)    # mov dx, base+0xA; in al,dx (read 0xAA)
    a.db(0xBA).bytes(_w(sb_port + 0x0C))             # mov dx, base+0xC (write port)
    _emit_sb_write(a, 0xD1)                          # DSP cmd 0xD1: speaker on


def _emit_sb_synth_out(a: "_Asm", sb_port: int) -> None:
    """One 8-bit sample for the SoundBlaster DAC: a real MIX, not a voice count.

    Each voice looks its level up in its own volume-scaled wavetable (`wptr`
    points at wavetbl + level*256), so a voice contributes a continuous
    amplitude -- its actual waveform at its note's volume -- rather than a
    +/-AMP sign bit. The four levels sum in BX (16-bit, so the mix can't wrap
    the way a byte accumulator did) and the total lands in the DAC's range."""
    a.db(0x31, 0xDB)                                 # xor bx,bx (16-bit signed sum)
    for i in range(_SPK4_TONES):
        a.db(0xA1).abs16("acc", i * 2)               # mov ax,[acc+i]
        a.db(0x03, 0x06).abs16("inc", i * 2)         # add ax,[inc+i]
        a.db(0xA3).abs16("acc", i * 2)               # mov [acc+i],ax
        a.db(0x8B, 0x36).abs16("wptr", i * 2)        # mov si,[wptr+i] (volume table)
        a.db(0x88, 0xE0).db(0x30, 0xE4)              # mov al,ah; xor ah,ah (phase hi)
        a.db(0x01, 0xC6)                             # add si,ax (table + phase)
        a.db(0x8A, 0x04).db(0x98)                    # mov al,[si]; cbw (signed sample)
        a.db(0x01, 0xC3)                             # add bx,ax
    nz = _SPK4_TONES * 2                             # noise voice
    a.db(0xA1).abs16("inc", nz).db(0x09, 0xC0)       # ax=ninc; or ax,ax
    a.db(0x74).rel8("sb_nmute")                      # jz muted (contributes 0)
    a.db(0xA1).abs16("acc", nz).db(0x03, 0x06).abs16("inc", nz).db(0xA3).abs16("acc", nz)
    a.db(0x73).rel8("sb_nobit")                      # jnc: no overflow -> don't clock
    a.db(0xA1).abs16("nlfsr").db(0xD1, 0xE8)         # ax=[nlfsr]; shr ax,1
    a.db(0x73).rel8("sb_nf").db(0x35).bytes(_w(_SPK4_LFSR))   # jnc; xor ax,taps
    a.label("sb_nf")
    a.db(0xA3).abs16("nlfsr")
    a.label("sb_nobit")
    # the noise voice reads its own volume table too: a set LFSR bit takes the
    # table's high half (+amp), a clear bit the low half (-amp)
    a.db(0x8B, 0x36).abs16("wptr", nz)               # mov si,[wptr+3]
    a.db(0xF6, 0x06).abs16("nlfsr").db(0x01)         # test byte[nlfsr],1
    a.db(0x75).rel8("sb_nhi")                        # jnz high
    a.db(0x81, 0xC6).bytes(_w(128))                  # add si,128 (the -amp half)
    a.label("sb_nhi")
    a.db(0x8A, 0x04).db(0x98).db(0x01, 0xC3)         # mov al,[si]; cbw; add bx,ax
    a.label("sb_nmute")
    # sample = mix + 0x80 (signed level -> 0..255); write cmd 0x10 then the sample
    a.db(0x88, 0xD8).db(0x04, 0x80).db(0x88, 0xC4)   # al=bl; add al,0x80; ah=al
    a.db(0xBA).bytes(_w(sb_port + 0x0C))             # mov dx, base+0xC
    _emit_sb_write(a, 0x10)                          # DSP cmd 0x10 (direct DAC)
    a.db(0xEC).db(0xA8, 0x80).db(0x75, 0xFB)         # poll write-ready
    a.db(0x88, 0xE0).db(0xEE)                        # al=ah (the sample); out dx,al


def _emit_spk_wave_synth(a: "_Asm", mcs: bool) -> None:
    """The high-carrier PWM waveform path: each tone's LEVEL (0..2*FULL) comes
    from the unsigned `wavetbl`, the noise voice adds FULL when its LFSR bit is
    set, and the multi-level sum (0..80) is first-order delta-sigma'd onto the
    1-bit speaker. At an ultrasonic mix rate the carrier vanishes and the
    speaker genuinely plays sines/triangles — the PWM waveform modeling."""
    full = 4 * _SPK4_WAVE_FULL                       # the sum's full scale (80)
    a.db(0x31, 0xDB)                                 # xor bx,bx (bl = level sum)
    for i in range(_SPK4_TONES):
        a.db(0xA1).abs16("acc", i * 2)               # mov ax,[acc+i]
        a.db(0x03, 0x06).abs16("inc", i * 2)         # add ax,[inc+i]
        a.db(0xA3).abs16("acc", i * 2)               # mov [acc+i],ax
        a.db(0x88, 0xE2).db(0x30, 0xF6)              # mov dl,ah; xor dh,dh
        a.db(0x89, 0xD6)                             # mov si,dx (phase high byte)
        a.db(0x8A, 0x84).abs16("wavetbl")            # mov al,[si+wavetbl] (0..2*FULL)
        a.db(0x00, 0xC3)                             # add bl,al
    nz = _SPK4_TONES * 2                             # noise voice (LFSR level)
    a.db(0xA1).abs16("inc", nz).db(0x09, 0xC0)       # ax=ninc; or ax,ax
    a.db(0x74).rel8("wv_nmute")                      # jz muted
    a.db(0xA1).abs16("acc", nz).db(0x03, 0x06).abs16("inc", nz).db(0xA3).abs16("acc", nz)
    a.db(0x73).rel8("wv_nobit")                      # jnc: don't clock
    a.db(0xA1).abs16("nlfsr").db(0xD1, 0xE8)         # ax=[nlfsr]; shr ax,1
    a.db(0x73).rel8("wv_nf").db(0x35).bytes(_w(_SPK4_LFSR))
    a.label("wv_nf")
    a.db(0xA3).abs16("nlfsr")
    a.label("wv_nobit")
    a.db(0xF6, 0x06).abs16("nlfsr").db(0x01)         # test byte[nlfsr],1
    a.db(0x74).rel8("wv_nmute")                      # jz (low adds 0)
    a.db(0x80, 0xC3, _SPK4_WAVE_FULL)                # add bl, FULL
    a.label("wv_nmute")
    # multi-level delta-sigma: err += sum; err >= FULLSCALE -> high, err -= it
    highbit = 0x01 if mcs else 0x02
    a.db(0xA0).abs16("sderr").db(0x00, 0xD8)         # al=[sderr]; add al,bl
    a.db(0x30, 0xD2)                                 # xor dl,dl
    a.db(0x3C, full).db(0x72).rel8("wv_lo")          # cmp al,80; jb lo
    a.db(0x2C, full).db(0xB2, highbit)               # sub al,80; dl=highbit
    a.label("wv_lo")
    a.db(0xA2).abs16("sderr")                        # [sderr]=al
    if mcs:
        a.db(0xA0).abs16("base61").db(0x08, 0xD0).db(0xE6, _SPEAKER)   # gate edge
        a.db(0xA0).abs16("base61").db(0xE6, _SPEAKER)                  # gate low
    else:
        a.db(0xA0).abs16("base61").db(0x08, 0xD0).db(0xE6, _SPEAKER)   # data bit


def _assemble_spk4(divider: int, samps_per_sub: int, total_subs: int,
                   stream: bytes, vis: str = "", draw_skip: int = 1,
                   poster: bytes = b"", mcs: bool = False, sb: bool = False,
                   sb_port: int = _SB_PORT, wave_table: bytes = b"",
                   wave: str = "square") -> bytes:
    """The 4-voice software-mixed PC-speaker engine. PIT ch0 fires the ISR at Fs;
    each interrupt it advances the phase accumulators (3 squares + an LFSR noise
    voice), delta-sigma modulates the summed bits to the speaker, and every
    `samps_per_sub` samples applies the next sub-tick's changes (which also update
    viz[]/strike[] for the scopes). With `mcs`, the speaker is driven the way the
    original Music Construction Set does it -- timer 2 as a retriggerable one-shot,
    fired by a gate edge on each 'high' sample, so every high is a fixed-width click
    (a pulse-density DAC) instead of our direct data-bit level. `vis` picks the display: a live text-mode scope
    (drawframe/blit), the 'static' full-song CGA poster (drawn once, zero runtime
    cost -- the XT answer), or nothing. Auto-repeats; a keypress restores and exits."""
    a = _Asm()
    static = vis == "static"
    if static:                                       # draw the whole-song poster once
        _emit_static_setup(a)
    elif vis:                                         # live scope: set mode + back buffer
        video_mode = 0x13 if vis == "vga" else 0x03  # VGA 320x200x256 or 80x25 text
        a.db(0xB8, video_mode, 0x00).db(0xCD, 0x10)  # mov ax,000N; int 0x10
        a.db(0x8C, 0xC8).db(0x05, 0x00, 0x10)        # mov ax,cs; add ax,0x1000
        a.db(0xA3).abs16("bufseg")                   # [bufseg] = back-buffer segment
    # ---- start: hook INT 8, set up the speaker, program the sample timer -------
    a.db(0xFA)                                       # cli
    a.db(0x31, 0xC0).db(0x8E, 0xC0)                  # xor ax,ax; es=0
    a.db(0x26, 0xA1, 0x20, 0x00).db(0xA3).abs16("old_off")   # save INT8 vector
    a.db(0x26, 0xA1, 0x22, 0x00).db(0xA3).abs16("old_seg")
    a.db(0x26, 0xC7, 0x06, 0x20, 0x00).abs16("isr")  # [es:0x20] = isr
    a.db(0x26, 0x8C, 0x0E, 0x22, 0x00)               # [es:0x22] = cs
    if sb:                                           # SoundBlaster: reset DSP + speaker on
        _emit_sb_init(a, sb_port)
    else:
        a.db(0xE4, 0x61).db(0xA2).abs16("old61")     # in al,0x61; save
        if mcs:
            # MCS drive: timer 2 = retriggerable one-shot (mode 1), count = pulse
            # width; speaker DATA on (bit1=1), gate low (bit0=0). A gate rising edge
            # fires a fixed-width pulse on OUT2 -> a click. base61 = data on, gate low.
            a.db(0x24, 0xFC).db(0x0C, 0x02).db(0xA2).abs16("base61")   # and 0xFC; or 0x02
            a.db(0xB0, 0xB2).db(0xE6, _PIT_CMD)      # mov al,0xB2; out 0x43 (ch2 mode 1)
            a.db(0xB0, _SPK4_MCS_PULSE).db(0xE6, _PIT_CH2).db(0xB0, 0x00).db(0xE6, _PIT_CH2)
        else:
            # direct drive: timer 2 mode 3 + gate low forces OUT2 high, so the cone
            # follows data bit 1 -- our 1-bit level DAC.
            a.db(0x24, 0xFC).db(0xA2).abs16("base61")    # and al,0xFC; base61
            a.db(0xB0, 0xB6).db(0xE6, _PIT_CMD)      # mov al,0xB6; out 0x43 (ch2 mode3)
            a.db(0xB0, 0x01).db(0xE6, _PIT_CH2).db(0xB0, 0x00).db(0xE6, _PIT_CH2)   # count=1
        a.db(0xA0).abs16("base61").db(0xE6, _SPEAKER)    # apply base now
    # stream pointers; sampctr=1 so the first sub-tick applies immediately
    a.db(0xB8).abs16("stream").db(0xA3).abs16("streamptr")
    a.db(0xB8).bytes(_w(total_subs)).db(0xA3).abs16("ticksleft")
    a.db(0xB8).bytes(_w(1)).db(0xA3).abs16("sampctr")
    a.db(0xB0, 0x36).db(0xE6, _PIT_CMD)              # ch0 mode 3
    a.db(0xB8).bytes(_w(divider)).db(0xE6, _PIT_CH0).db(0x88, 0xE0).db(0xE6, _PIT_CH0)
    a.db(0xFB)                                        # sti
    # ---- foreground: a live scope redraws; static/none just wait for a key -----
    if vis and not static:
        _emit_draw_wait(a, draw_skip)
    else:
        # hlt (sleep until an IRQ; a tight spin on a fast CPU / DOSBox cycles=max
        # starves the timer and skips) -- but only poll the BIOS keyboard ~20x/sec.
        # At Fs the hlt wakes thousands of times/sec; calling int 0x16 on every one
        # is ~10% of a 4.77 MHz CPU, which we'd rather leave to the audio.
        kb_n = max(1, min(65535, round(_PIT_HZ / divider / 20)))
        a.label("wait")
        a.db(0xF4)                                   # hlt
        a.db(0xFF, 0x0E).abs16("kbctr").db(0x75).rel8("wait")   # dec[kbctr]; jnz wait
        a.db(0xC7, 0x06).abs16("kbctr").bytes(_w(kb_n))         # reload (~20/sec)
        a.db(0xB4, 0x01).db(0xCD, 0x16).db(0x74).rel8("wait")   # int16 ah=1; jz wait
        a.db(0x30, 0xE4).db(0xCD, 0x16)              # consume the key
    # ---- teardown: silence, restore timer + vector, exit -----------------------
    a.db(0xFA)                                        # cli
    if sb:                                            # SoundBlaster speaker off (cmd 0xD3)
        a.db(0xBA).bytes(_w(sb_port + 0x0C))
        _emit_sb_write(a, 0xD3)
    else:
        a.db(0xA0).abs16("old61").db(0xE6, _SPEAKER) # restore port 0x61
    a.db(0xB0, 0x36).db(0xE6, _PIT_CMD)              # ch0 mode 3
    a.db(0x30, 0xC0).db(0xE6, _PIT_CH0).db(0xE6, _PIT_CH0)   # divisor 0 (65536) = 18.2 Hz
    a.db(0x31, 0xC0).db(0x8E, 0xC0)                  # es=0
    a.db(0xA1).abs16("old_off").db(0x26, 0xA3, 0x20, 0x00)
    a.db(0xA1).abs16("old_seg").db(0x26, 0xA3, 0x22, 0x00)
    a.db(0xFB)                                        # sti
    if vis:
        a.db(0xB8, 0x03, 0x00).db(0xCD, 0x10)        # mov ax,0x0003; int 0x10 (clear/restore text)
    a.db(0xB8, 0x00, 0x4C).db(0xCD, 0x21)            # exit to DOS
    # ---- isr: one audio sample (+ a sub-tick's note changes when due) ----------
    a.label("isr")
    a.db(0x50, 0x53, 0x51, 0x52, 0x56)               # push ax,bx,cx,dx,si (ds already = cs)
    if sb:
        _emit_sb_synth_out(a, sb_port)               # volume-mixed 8-bit -> SB DAC
    elif wave_table:
        _emit_spk_wave_synth(a, mcs)                 # PWM waveform modeling
    else:
        # synth: bl = sum of the voices' bits (3 tone squares + 1 noise)
        a.db(0x31, 0xDB)                             # xor bx,bx
        for i in range(_SPK4_TONES):
            a.db(0xA1).abs16("acc", i * 2)           # mov ax,[acc+i]
            a.db(0x03, 0x06).abs16("inc", i * 2)     # add ax,[inc+i]
            a.db(0xA3).abs16("acc", i * 2)           # mov [acc+i],ax
            a.db(0x01, 0xC0).db(0x80, 0xD3, 0x00)    # add ax,ax (CF=bit15); adc bl,0
        # noise voice (index 3): clock a Galois LFSR each time its accumulator
        # overflows (the inc sets the clock rate = brightness); its bit0 joins the sum.
        nz = _SPK4_TONES * 2
        a.db(0xA1).abs16("inc", nz).db(0x09, 0xC0)   # ax=ninc; or ax,ax
        a.db(0x74).rel8("sp_nmute")                  # jz muted -> contributes 0
        a.db(0xA1).abs16("acc", nz).db(0x03, 0x06).abs16("inc", nz).db(0xA3).abs16("acc", nz)
        a.db(0x73).rel8("sp_nobit")                  # jnc: no overflow -> don't clock
        a.db(0xA1).abs16("nlfsr").db(0xD1, 0xE8)     # ax=[nlfsr]; shr ax,1 (CF=lsb)
        a.db(0x73).rel8("sp_nf").db(0x35).bytes(_w(_SPK4_LFSR))   # jnc; xor ax,taps
        a.label("sp_nf")
        a.db(0xA3).abs16("nlfsr")                    # [nlfsr]=ax
        a.label("sp_nobit")
        a.db(0xA0).abs16("nlfsr").db(0x24, 0x01).db(0x00, 0xC3)   # al=[nlfsr]; and al,1; add bl,al
        a.label("sp_nmute")
        # delta-sigma: err += sum; if err>=VOICES: err-=VOICES -> this sample is 'high'
        # (dl = the port bit to raise: gate bit0 for the MCS one-shot, data bit1 direct)
        highbit = 0x01 if mcs else 0x02
        a.db(0xA0).abs16("sderr").db(0x00, 0xD8)     # al=[sderr]; add al,bl
        a.db(0x30, 0xD2)                             # xor dl,dl
        a.db(0x3C, _SPK4_VOICES).db(0x72).rel8("sd_lo")  # cmp al,4; jb lo
        a.db(0x2C, _SPK4_VOICES).db(0xB2, highbit)   # sub al,4; dl=highbit
        a.label("sd_lo")
        a.db(0xA2).abs16("sderr")                    # [sderr]=al
        if mcs:
            # fire the one-shot on a 'high' sample: base|gate up (edge triggers a
            # fixed-width pulse), then gate back down for the next edge.
            a.db(0xA0).abs16("base61").db(0x08, 0xD0).db(0xE6, _SPEAKER)   # al=base|dl; out
            a.db(0xA0).abs16("base61").db(0xE6, _SPEAKER)                  # gate low; out
        else:
            a.db(0xA0).abs16("base61").db(0x08, 0xD0).db(0xE6, _SPEAKER)   # al=base|dl(bit1); out
    # timing: every samps_per_sub samples, apply the next sub-tick
    a.db(0xFF, 0x0E).abs16("sampctr")                # dec word[sampctr]
    a.db(0x75).rel8("isr_eoi")                       # jnz eoi (fast path skips di)
    a.db(0x57)                                        # push di (the event path uses it)
    a.db(0xA1).abs16("sampsub").db(0xA3).abs16("sampctr")   # reload sampctr
    a.db(0x83, 0x3E).abs16("ticksleft").db(0x00)     # cmp word[ticksleft],0
    a.db(0x75).rel8("sp_apply")                      # jne apply
    a.db(0xC7, 0x06).abs16("ticksleft").bytes(_w(total_subs))   # rewind (auto-repeat)
    a.db(0xC7, 0x06).abs16("streamptr").abs16("stream")
    a.label("sp_apply")
    a.db(0x8B, 0x36).abs16("streamptr")              # si=[streamptr]
    a.db(0xAC).db(0x88, 0xC1).db(0x30, 0xED)         # lodsb (nchanges); cl=al; ch=0
    a.db(0xE3).rel8("sp_noev")                       # jcxz noev
    a.label("sp_evl")
    a.db(0xAC)                                        # lodsb (voice | level<<4)
    a.db(0x88, 0xC2)                                 # mov dl,al (keep the packed byte)
    a.db(0x24, 0x0F).db(0x30, 0xE4)                  # and al,0x0F (voice); xor ah,ah
    a.db(0x89, 0xC7)                                 # mov di,ax (voice: byte index)
    a.db(0x89, 0xC3).db(0x01, 0xDB)                 # mov bx,ax; add bx,bx (voice*2: word)
    if sb and wave_table:
        # the note's volume level picks this voice's wavetable: base + level*256
        a.db(0xD0, 0xEA, 0xD0, 0xEA, 0xD0, 0xEA, 0xD0, 0xEA)   # shr dl,1 x4 -> level
        a.db(0x88, 0xD6).db(0x30, 0xD2)              # mov dh,dl; xor dl,dl (dx=level*256)
        a.db(0x81, 0xC2).abs16("wavetbl")            # add dx, wavetbl
        a.db(0x89, 0x97).abs16("wptr")               # [wptr+bx] = dx
    a.db(0xAD)                                        # lodsw (inc)
    a.db(0x89, 0x87).abs16("inc")                    # [inc+bx]=ax
    a.db(0xC7, 0x87).abs16("acc").bytes(_w(0))       # [acc+bx]=0 (reset phase on change)
    a.db(0xAC).db(0x88, 0x85).abs16("viz")           # lodsb (viz); [viz+di]=al
    a.db(0x08, 0xC0).db(0x74).rel8("sp_nostr")       # or al,al; jz (note-off, no strike)
    a.db(0xC6, 0x85).abs16("strike").db(0x01)        # [strike+di]=1 (VU onset)
    a.label("sp_nostr")
    a.db(0xE2).rel8("sp_evl")                        # loop
    a.label("sp_noev")
    a.db(0x89, 0x36).abs16("streamptr")              # [streamptr]=si
    a.db(0xFF, 0x0E).abs16("ticksleft")              # dec word[ticksleft]
    a.db(0x5F)                                        # pop di
    a.label("isr_eoi")
    a.db(0xB0, 0x20).db(0xE6, 0x20)                  # EOI
    a.db(0x5E, 0x5A, 0x59, 0x5B, 0x58).db(0xCF)      # pop si,dx,cx,bx,ax; iret
    if not static:                                   # the static poster needs no renderer
        _emit_scope_code(a, vis)                      # scope renderers (shared)
    # ---- variables + data ------------------------------------------------------
    a.label("old_off"); a.db(0x00, 0x00)
    a.label("old_seg"); a.db(0x00, 0x00)
    a.label("old61"); a.db(0x00)
    a.label("base61"); a.db(0x00)
    a.label("streamptr"); a.db(0x00, 0x00)
    a.label("ticksleft"); a.db(0x00, 0x00)
    a.label("sampctr"); a.db(0x00, 0x00)
    a.label("sampsub"); a.bytes(_w(samps_per_sub))   # samples per sub-tick (const)
    a.label("sderr"); a.db(0x00)                     # delta-sigma error
    a.label("nlfsr"); a.bytes(_w(_NOISE_SEED))       # noise LFSR state (nonzero)
    a.label("acc"); a.bytes(bytes(2 * _SPK4_VOICES))  # phase accumulators (0-2 tone, 3 noise)
    a.label("inc"); a.bytes(bytes(2 * _SPK4_VOICES))  # phase increments
    a.label("viz"); a.db(0x00, 0x00, 0x00, 0x00)     # per-voice scope period (for the scopes)
    a.label("strike"); a.db(0x00, 0x00, 0x00, 0x00)  # per-voice note-on latch (VU)
    a.label("kbctr"); a.db(0x01, 0x00)               # countdown to the next keyboard poll
    if sb:                                           # per-voice volume-table pointers
        a.label("wptr")                              # start on level 0 = silence,
        for _ in range(_SPK4_VOICES):                # so a voice is quiet until its
            a.abs16("wavetbl")                       # first note sets its level
    if wave_table:
        a.label("wavetbl"); a.bytes(wave_table)      # 256-entry oscillator cycle
    if static:
        a.label("poster"); a.bytes(poster)               # the baked 320x200x4 song picture
    else:
        _emit_scope_vars(a, vis, wave)                # shared + per-vis draw state
    a.label("stream"); a.bytes(stream)
    return a.resolve()


def _vis_for(scope: bool, text_scope) -> str:
    """The vis string a `scope`/`text_scope` request selects: "" / "graphics" /
    "text1".."text5"."""
    if text_scope:
        ts = int(text_scope)
        return ("vga" if ts == 8 else "static" if ts == 7 else "vu" if ts == 6
                else "text5" if ts == 5 else "text4" if ts == 4
                else "text3" if ts == 3 else "text2" if ts == 2 else "text1")
    return "graphics" if scope else ""


def _native_waveform(song: Song) -> str:
    """The majority tone-track waveform — what "native" resolves to for the
    wavetable engines (NES imports carry their duties; MCS songs are square)."""
    counts: dict = {}
    for t in song.tracks:
        if getattr(t, "kind", "tone") == "tone" and t.waveform:
            counts[t.waveform] = counts.get(t.waveform, 0) + len(t.notes)
    return max(counts, key=counts.get) if counts else "square"


def build_com(song: Song, mode: str, tempo_byte0: int, scope: bool = False,
              text_scope: bool = False, mix_rate=None, draw_skip: int = 1,
              mcs: bool = False, sb: bool = False, sb_port: int = _SB_PORT,
              sb_wave: str = None, spk_wave: str = None) -> bytes:
    """Assemble a `.COM` that plays `song` in the given mode at the MCS tempo.
    `scope` adds the mode-9 graphics oscilloscopes (Tandy only); `text_scope` adds
    an 80x25 text-mode scope -- 1 = block bars, 2 = box-drawing line trace, 3 =
    box-line 2x2 grid + master, 4 = faux spectrum analyzer, 5 = combined monitor,
    6/"vu" = lightweight VU meters, 7 = static full-song poster, 8 = VGA mode 13h.
    `mix_rate` (Hz, 4-voice only, ~1000-48000) sets the software mixing sample
    rate -- 4000 for a real XT, ~24000 (ultrasonic) for best quality on a fast
    CPU. `mcs` (4-voice only) drives the speaker the original MCS way (timer-2
    one-shot pulses). `sb_wave` picks the SoundBlaster wavetable (sine/triangle/
    nestri/pulseNN/"native" = the song's own); `spk_wave` models a non-square
    waveform on the PC speaker via multi-level PWM -- it needs a >= 12 kHz mix
    rate so the carrier is inaudible. `draw_skip` redraws the scope every Nth
    frame (>=2 lightens a heavy scope)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if mcs and mode != "4voice":
        raise ValueError("the MCS speaker drive is only available with --4voice")
    if sb and mode != "4voice":
        raise ValueError("SoundBlaster output is only available with --4voice")
    if sb and mcs:
        raise ValueError("--sb and --mcs are different speaker drives; pick one")
    if spk_wave and (sb or mode != "4voice"):
        raise ValueError("spk_wave (PWM waveform modeling) is 4-voice speaker only")
    draw_skip = max(1, min(255, int(draw_skip)))
    if mode == "4voice":                             # software-mixed PC speaker
        if scope:
            raise ValueError("the graphics scope is Tandy-only; use a text scope "
                             "(--scope-text..) with --4voice")
        vis = _vis_for(False, text_scope)            # text scopes only
        div = _spk4_div_for(mix_rate)
        fs = _PIT_HZ / div
        # -- waveform table selection (the universal waveforms reach DOS here) --
        wave_table = b""
        if sb:
            # every SB voice mixes through the volume-scaled bank, so each one
            # contributes a real amplitude at its note's volume. "native" plays
            # the song's own waveforms (NES duties, the stepped triangle...).
            wf = sb_wave or "native"
            if wf == "native":
                wf = _native_waveform(song)
            wave_table = _sb_wave_bank(wf)
        elif spk_wave and spk_wave not in ("square", "native"):
            if fs < _SPK4_WAVE_MIN_RATE:
                raise ValueError(
                    f"PWM waveform modeling needs --mix-rate >= "
                    f"{_SPK4_WAVE_MIN_RATE} (the carrier must be inaudible); "
                    f"got {fs:.0f} Hz")
            wave_table = _wave_table(spk_wave, _SPK4_WAVE_FULL, signed=False)
        stream, total = _build_spk4_stream(song, fs)
        if total <= 1:
            raise ValueError("nothing to play (no notes)")
        if total > 65535:
            raise ValueError("song too long for the 4-voice player (sub-tick "
                             "count exceeds 16 bits)")
        subtick_s = tick_seconds_for(tempo_byte0) / _SUBTICKS
        samps = max(1, min(65535, round(fs * subtick_s)))
        poster = _render_static_poster(song) if vis == "static" else b""
        # the scopes draw the contour of what this build actually sounds: the
        # SB wavetable, the speaker's PWM-modelled shape, or a plain square
        scope_wave = (wf if sb else
                      (spk_wave if spk_wave and spk_wave != "native" else "square"))
        com = _assemble_spk4(div, samps, total, stream, vis, draw_skip, poster,
                             mcs, sb, sb_port, wave_table, scope_wave)
        if len(com) > 0xFF00:
            raise ValueError(f".COM is {len(com)} bytes — too big for one segment")
        return com
    vis = _vis_for(scope, text_scope)                # remaining modes: tandy / 1voice
    if vis == "static":
        raise ValueError("the 'static screen' poster is 4-voice only")
    if vis and mode != "tandy":
        raise ValueError("scopes are only available with --tandy or --4voice")
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
    com = _assemble(divider, subdiv, total, sil_bytes, stream, vis, draw_skip,
                    "square")                        # SN76489 / PIT: real squares
    if len(com) > 0xFF00:
        raise ValueError(f".COM is {len(com)} bytes — too big for one segment; "
                         "shorten the song or split it")
    return com
