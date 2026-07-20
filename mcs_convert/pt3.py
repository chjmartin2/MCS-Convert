"""Import ProTracker 3 / Vortex Tracker II modules (.pt3) into the Song model.

PT3 is the ZX Spectrum / Atari ST scene's standard tracker format for the
AY-3-8910 — three pure tone channels, note-based patterns, no PCM samples.
That makes it the best-quality source for MCS conversion: the notes are stored
explicitly (no CPU emulation, no pitch detection), and the chip's constraints
already match MCS's.

This is a NOTE extractor, not a player: PT3 "samples" (per-frame volume/detune
tables), envelopes, and effects shape timbre on real hardware but don't change
which notes exist, so they are parsed past and dropped. Ornaments (per-frame
semitone-offset tables — the AY arpeggio mechanism) are read but only their
base offset is applied; a fast-cycling ornament chord collapses to its root.

Format reference: Vince Weaver's "How to decode a Vortex Tracker II PT3 File"
(deater.net) and the AY_emul source it derives from.

Timing: a pattern row lasts `delay` frames at 50 Hz. We map each row to 1, 2,
4, or 8 thirty-second ticks — whichever lets an MCS tempo byte (header byte 0)
match the row rate best — so converted songs play at their original speed.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from . import drums
from .model import NoteEvent, Song, Track

# Two signatures exist in the wild for the same header layout: ProTracker's own,
# and Vortex Tracker II's (e.g. "Vortex Tracker II 1.0 module: ...").
_MAGICS = (b"ProTracker 3.", b"Vortex Tracker II")

# Effect parameter bytes consumed AFTER the line-ending byte, by on-disk code.
_FX_PARAMS = {0x01: 3, 0x02: 5, 0x03: 1, 0x04: 1, 0x05: 2, 0x08: 3, 0x09: 1}

_FX_SET_SPEED = 0x09

# AY noise period is 0..31; a LOW period clocks the LFSR fast = bright high hiss
# (hi-hat), a high period = dark low rumble (kick). Split the range in half when
# a drum note carries an explicit noise period (rare — most PT3 drums are
# sample-driven, so click_pitches falls back to the sample's tone/noise duty).
_AY_NOISE_BRIGHT = 15


class PT3Error(ValueError):
    pass


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("latin-1").strip()


def _u16(d: bytes, off: int) -> int:
    return struct.unpack_from("<H", d, off)[0]


class _Channel:
    """One AY channel's decoder state across the whole pattern order."""

    def __init__(self) -> None:
        self.row = 0                 # absolute row position
        self.skip = 1                # rows per line-event
        self.ornament = 0
        self.sample = 1              # current PT3 sample (instrument table)
        self.last_noise = None       # last noise period set on this channel
        self.envelope = None         # active hardware-envelope shape (None = off)
        self.volume = 15             # channel volume 0-15 ($C1-$CF)
        self.notes: List[Tuple] = []             # (row, note_index, sample, noise, fx)
        self.offs: List[int] = []                # rows where the channel went silent
        self.noise_cmds = 0          # $20-$3F noise sets seen (percussion signal)

    def note_on(self, row: int, index: int, orn_base: int,
                fx: Optional[dict] = None) -> None:
        # Capture the universal-tracker nuances alongside the note: ornament,
        # hardware envelope, channel volume, and any line effects (see model.py
        # for the nomenclature).
        eff = dict(fx or {})
        if self.ornament:
            eff["orn"] = self.ornament
        if self.envelope is not None:
            eff["env"] = self.envelope
        self.notes.append((row, index + orn_base, self.sample, self.last_noise,
                           self.volume, eff))

    def note_off(self, row: int) -> None:
        self.offs.append(row)


def _sample_is_drum(data: bytes, idx: int) -> bool:
    """True when PT3 sample `idx` keeps the noise generator on for MOST of its
    audible frames — the AY percussion recipe. Frame byte 1: bit 4 set = tone
    disabled, bit 7 set = noise disabled, low 4 bits = amplitude (verified
    against deater's pt3_lib.c mixer code, `(b1>>1) & 0x48`).

    The duty cycle matters, not the tone flag: a snare is often tone+noise+
    envelope on EVERY frame (ALF's sample 1), while a slap-bass is a single
    noise ATTACK frame on an otherwise pure tone (ALF's sample 5) and must
    stay melodic — so count noise frames against audible frames."""
    duty, _ = _sample_noise(data, idx)
    return duty > 0.5


def _sample_noise(data: bytes, idx: int) -> Tuple[float, float]:
    """(noise_duty, tone_duty) over a sample's audible frames."""
    if not 0 <= idx < 32:
        return 0.0, 1.0
    addr = _u16(data, 0x69 + idx * 2)
    if not 0 < addr < len(data) - 2:
        return 0.0, 1.0
    length = min(data[addr + 1], (len(data) - addr - 2) // 4)
    audible = noise_on = tone_on = 0
    for k in range(length):
        b1 = data[addr + 2 + k * 4 + 1]
        if b1 & 0x0F == 0:
            continue                             # silent frame: doesn't count
        audible += 1
        noise_on += 0 if b1 & 0x80 else 1
        tone_on += 0 if b1 & 0x10 else 1
    if audible == 0:
        return 0.0, 1.0
    return noise_on / audible, tone_on / audible


def _decode_pattern(data: bytes, addr: int, ch: _Channel, orn_base,
                    speed_changes: List[Tuple[int, int]]) -> int:
    """Walk one channel's nul-terminated stream for one pattern, starting at
    absolute row `ch.row`. Returns the number of rows the stream covered."""
    pos = addr
    start_row = ch.row
    pending_fx: List[int] = []
    while True:
        b = data[pos]
        pos += 1
        if b == 0x00:
            break
        if 0x01 <= b <= 0x0F:
            pending_fx.append(b)
            continue
        if b == 0x10:
            ch.sample = data[pos] // 2
            pos += 1
            continue
        if 0x11 <= b <= 0x1F:                        # envelope shape + period + sample
            ch.envelope = b & 0x0F                   # the hardware-envelope shape
            ch.sample = data[pos + 3] // 2
            pos += 4
            continue
        if 0x20 <= b <= 0x3F:                        # noise period 0-31
            ch.last_noise = (b & 0x0F) + (16 if b >= 0x30 else 0)
            ch.noise_cmds += 1
            continue
        if 0x40 <= b <= 0x4F:                        # set ornament
            ch.ornament = b & 0x0F
            continue
        if 0xB0 == b:                                # env off, orn reset
            ch.envelope = None
            continue
        if b == 0xB1:
            ch.skip = data[pos]
            pos += 1
            continue
        if 0xB2 <= b <= 0xBF:                        # envelope shape + period
            ch.envelope = (b - 0xB1) & 0x0F
            pos += 2
            continue
        if 0xC1 <= b <= 0xCF:                        # channel volume 1-15
            ch.volume = b & 0x0F
            continue
        if 0xD1 <= b <= 0xEF:                        # set sample
            ch.sample = b - 0xD0
            continue
        if 0xF0 <= b <= 0xFF:                        # init ornament + sample
            ch.ornament = b & 0x0F
            ch.sample = data[pos] // 2
            pos += 1
            continue
        # --- line enders: a note, note-off, or empty line ---------------------
        note_row = None
        if 0x50 <= b <= 0xAF:
            note_row = (ch.row, b - 0x50)
        elif b == 0xC0:
            ch.note_off(ch.row)
        elif b != 0xD0:                              # $D0 = empty line (sustain)
            raise PT3Error(f"unknown pattern byte 0x{b:02x} at 0x{pos - 1:x}")
        # Effect parameter bytes follow the line ender; name them with the
        # universal nomenclature (model.py) so nothing the module expressed is
        # lost: 1=tone slide, 2=portamento, 3=sample offset, 4=ornament offset,
        # 5=vibrato, 8=envelope slide, 9=set speed.
        fx_named: dict = {}
        for fx in pending_fx:
            n = _FX_PARAMS.get(fx, 0)
            if fx == _FX_SET_SPEED:
                speed_changes.append((ch.row, data[pos]))
            elif fx == 0x01 and n >= 3:              # delay + signed 16-bit step
                step = int.from_bytes(data[pos + 1:pos + 3], "little", signed=True)
                fx_named["slide"] = step
            elif fx == 0x02:
                fx_named["porta"] = data[pos] if n else 0
            elif fx == 0x03:
                fx_named["sampfx"] = data[pos]
            elif fx == 0x04:
                fx_named["ornfx"] = data[pos]
            elif fx == 0x05 and n >= 2:
                fx_named["vib"] = (data[pos], data[pos + 1])
            elif fx == 0x08:
                fx_named["envslide"] = 1
            pos += n
        pending_fx = []
        if note_row is not None:
            ch.note_on(note_row[0], note_row[1], orn_base(ch.ornament), fx_named)
        ch.row += ch.skip
    return ch.row - start_row


def _tick_seconds(byte0: int) -> float:
    """A 32nd-tick's real duration at MCS tempo byte0 (0x77 fastest .. 0x92)."""
    step = (byte0 - 0x77) // 3
    return (0.067 + 0.016 * step) / 2.0


# The subdivisions a row may map to. The default (auto-detect) keeps the
# original power-of-two set so existing conversions are unchanged; Exhaustive
# Optimize adds 3 and 6 so triplet-feel modules can land on the beat too.
_TICK_SET = (1, 2, 4, 8)
_TICK_SET_WIDE = (1, 2, 3, 4, 6, 8)


def fit_row_grid(delay: int, target_byte0: Optional[int] = None,
                 tick_set: Tuple[int, ...] = _TICK_SET) -> Tuple[int, int, float]:
    """Fit a PT3 row (delay frames at 50 Hz) onto the 32nd-tick grid.

    Returns (ticks_per_row, mcs_tempo_byte0, rel_error). With target_byte0 the
    tempo is FIXED and only the subdivision is chosen (the "optimize to the
    selected tempo" case); otherwise both are searched for the least error."""
    row_seconds = max(1, delay) / 50.0
    if target_byte0 is not None:
        ts = _tick_seconds(target_byte0)
        ticks, err = min(((t, abs(t * ts - row_seconds) / row_seconds)
                          for t in tick_set), key=lambda x: x[1])
        return ticks, target_byte0, err
    best = (1, 0x77, 1e9)
    for ticks in tick_set:
        want = row_seconds / ticks
        step = round((2 * want - 0.067) / 0.016)
        if not 0 <= step <= 9:
            continue
        got = (0.067 + 0.016 * step) / 2.0
        err = abs(got - want) / want
        if err < best[2]:
            best = (ticks, 0x77 + 3 * step, err)
    return best


def row_ticks_and_tempo(delay: int) -> Tuple[int, int]:
    """Back-compat shim: (ticks_per_row, tempo_byte0) for the default grid."""
    ticks, byte0, _ = fit_row_grid(delay)
    return ticks, byte0


def _sample_audible_ticks(data: bytes, idx: int, ticks_per_row: int,
                          delay: int):
    """How many 32nd-ticks sample `idx` stays audible, or None if it sustains.

    MCS has no volume, so a decaying sample can only be expressed as TIME: a
    note whose amplitude table falls to zero (and whose loop region is silent)
    is audibly over at that point, however long the pattern holds it. Frames
    run at 50 Hz; one row = `delay` frames = `ticks_per_row` ticks."""
    if not 0 <= idx < 32:
        return None
    addr = _u16(data, 0x69 + idx * 2)
    if not 0 < addr < len(data) - 2:
        return None
    loop = data[addr]
    length = min(data[addr + 1], (len(data) - addr - 2) // 4)
    if length <= 0:
        return None
    amps = [data[addr + 2 + k * 4 + 1] & 0x0F for k in range(length)]
    if any(a > 0 for a in amps[min(loop, length - 1):]):
        return None                              # loops at audible volume
    last = max((k for k, a in enumerate(amps) if a > 0), default=-1)
    frames = last + 1
    return max(1, -(-frames * ticks_per_row // delay))     # ceil


def parse_pt3(data: bytes, percussion: str = "clicks",
              drum_sound: str = "auto",
              shape_durations: bool = False,
              grid: Optional[Tuple[int, int]] = None) -> Tuple[Song, int]:
    """Parse a .pt3 module. Returns (song, mcs_tempo_byte0); song ticks are 32nds.

    `percussion` decides what happens to drum-classified notes (see is_drum below):
      "clicks"  — synthesize them as 1-tick percussion hits (default);
      "pitched" — ignore the noise modifier and play their written pitches;
      "drop"    — silence them, keeping the channel's melodic notes;
      "mark"    — the UNIVERSAL capture: keep the written note, set percussive
                  and effects["drumbright"] (the two-tone verdict), and let the
                  export-side retrack decide clicks/pitched/drop + the palette.
    `drum_sound` picks the click pitch when percussion == "clicks":
      "auto"     — two-tone: bright drums (pure-noise samples / low AY noise
                   period) -> hi-hat E7, dark drums (tonal body) -> low bass B2;
      "low bass" — every hit is B2 (47), the lowest note: a kick thud;
      "hi-hat"   — every hit is E7 (100), the highest note: a bright tick;
      "block"    — a single mid-register D4 wood-block tick;
    `shape_durations` truncates each note to its sample's audible length (the
    frame where its volume table decays to permanent silence) — MCS's only way
    to express a pluck, since it has no volume control.
    `grid` = (ticks_per_row, tempo_byte0) forces a specific tempo mapping
    (used by optimize_pt3 / optimize_pt3_at); None auto-fits from the row rate.
    """
    if percussion not in ("clicks", "pitched", "drop", "mark"):
        raise ValueError(
            f"percussion must be clicks/pitched/drop/mark, not {percussion!r}")
    if drum_sound not in drums.CLICKS and drum_sound != "auto":
        raise ValueError(f"unknown drum_sound {drum_sound!r}")
    if not any(data.startswith(m) for m in _MAGICS):
        raise PT3Error("not a ProTracker 3 / Vortex Tracker module (bad magic)")
    title = _cstr(data[0x1E:0x3E])
    author = _cstr(data[0x42:0x62])
    delay = data[0x64]
    pat_table = _u16(data, 0x67)

    # ornament base offsets (value at the table's start; the arpeggio root)
    orn_bases = []
    for i in range(16):
        addr = _u16(data, 0xA9 + i * 2)
        if 0 < addr < len(data) - 2 and data[addr + 1] > 0:   # loop, length, values
            orn_bases.append(int.from_bytes(data[addr + 2:addr + 3], "big", signed=True))
        else:
            orn_bases.append(0)

    def orn_base(n: int) -> int:
        return orn_bases[n & 0x0F]

    # pattern order list at $C9, terminated by $FF, entries are pattern*3
    order = []
    pos = 0xC9
    while data[pos] != 0xFF:
        order.append(data[pos] // 3)
        pos += 1

    chans = [_Channel() for _ in range(3)]
    speed_changes: List[Tuple[int, int]] = []
    for pat in order:
        base = pat_table + pat * 6
        addrs = [_u16(data, base + k * 2) for k in range(3)]
        start = max(c.row for c in chans)
        covered = 0
        for ch, addr in zip(chans, addrs):
            ch.row = start
            covered = max(covered, _decode_pattern(data, addr, ch, orn_base,
                                                   speed_changes))
        for ch in chans:
            ch.row = start + covered              # keep channels in lockstep

    ticks_per_row, byte0 = grid if grid is not None else row_ticks_and_tempo(delay)

    song = Song(title=title or "PT3 module", source=f"pt3:{author}" if author else "pt3")
    total_rows = max(c.row for c in chans)

    # Drum classification needs USAGE, not just the sample table: a tone+noise
    # buzz hammered at 1-2 fixed pitches is a snare/kick (ALF's s1, Neverending's
    # s5), but the same recipe walking a scale would be a buzz-bass. Pure-noise
    # samples are drums unconditionally.
    usage: Dict[int, set] = {}
    for ch in chans:
        for row_, idx, sample, *_rest in ch.notes:
            usage.setdefault(sample, set()).add(idx)

    def is_drum(sample: int) -> bool:
        noise_duty, tone_duty = _sample_noise(data, sample)
        if noise_duty <= 0.5:
            return False
        return tone_duty <= 0.5 or len(usage.get(sample, ())) <= 3

    def click_pitches(sample: int, noise) -> Tuple[int, ...]:
        """Click pitch(es) for one drum hit. "auto" splits two-tone by
        BRIGHTNESS (like the NES noise-period split). The sample's tonal
        character decides first: a tone+noise sample has a pitched body -> a DARK
        kick/tom (low bass). A pure-noise sample is bright -> hi-hat, unless the
        pattern set a HIGH AY noise period (a low rumble), which pulls it dark.
        Fixed sounds ignore all that."""
        if drum_sound != "auto":
            return drums.CLICKS.get(drum_sound, drums.CLICKS["block"])
        return drums.two_tone(auto_bright(sample, noise))

    def auto_bright(sample: int, noise) -> bool:
        """The two-tone verdict for one drum hit: a tone+noise sample has a
        pitched body -> DARK kick/tom; pure noise -> bright hat, unless a HIGH
        AY noise period (a low rumble) pulls it dark."""
        _, tone_duty = _sample_noise(data, sample)
        if tone_duty >= 0.5:
            return False                         # pitched body = kick/tom
        if noise is not None:
            return noise <= _AY_NOISE_BRIGHT      # noise pitch sets hat vs rumble
        return True                              # pure noise, no period = hat

    drum_sample = [is_drum(s) for s in range(32)]
    for name, ch in zip("ABC", chans):
        track = Track(name=f"AY {name}", chip="ay-tone", waveform="square")
        drum_count = 0
        events = sorted(ch.notes)
        offs = sorted(ch.offs)
        for i, ev in enumerate(events):
            row, idx, sample, noise = ev[0], ev[1], ev[2], ev[3]
            volume = ev[4] if len(ev) > 4 else 15
            effects = dict(ev[5]) if len(ev) > 5 and ev[5] else {}
            nxt = events[i + 1][0] if i + 1 < len(events) else total_rows
            end = next((o for o in offs if row < o <= nxt), nxt)
            is_drum_note = 0 <= sample < 32 and drum_sample[sample]
            if is_drum_note:
                drum_count += 1
                if percussion == "drop":
                    continue
                if percussion == "clicks":
                    # AY percussion -> a 32nd-note click at a register-extreme
                    # pitch (B2 kick thud / E7 hi-hat tick). One note per hit so
                    # it costs a single position and event; "auto" picks the
                    # pitch two-tone (see click_pitches).
                    for midi in click_pitches(sample, noise):
                        track.add(NoteEvent(start_tick=row * ticks_per_row,
                                            duration_ticks=1, midi_note=midi,
                                            percussive=True))
                    continue
                if percussion == "mark":
                    # UNIVERSAL capture: keep the note exactly as written but
                    # MARK it as a drum, with the bright/dark verdict the click
                    # palette would use — clicks/pitched/drop and the click
                    # sound are decided at retrack/export time, not here.
                    effects["drumbright"] = int(auto_bright(sample, noise))
                # "pitched": fall through — the note plays as written
            midi = 24 + idx                       # PT3 note 0 = C-1 (~MIDI 24)
            dur = max(1, (end - row) * ticks_per_row)
            if shape_durations:
                audible = _sample_audible_ticks(data, sample, ticks_per_row, delay)
                if audible is not None:
                    dur = min(dur, audible)
            if noise is not None:
                effects.setdefault("aynoise", noise)   # explicit AY noise period
            if sample and sample != 1:
                effects.setdefault("sampfx", sample)   # non-default instrument
            track.add(NoteEvent(start_tick=row * ticks_per_row,
                                duration_ticks=dur, midi_note=midi,
                                velocity=max(10, round(volume * 100 / 15)),
                                percussive=(is_drum_note and percussion == "mark"),
                                effects=effects))
        track.meta["noise_cmds"] = ch.noise_cmds
        track.meta["drum_notes"] = drum_count
        if track.notes:
            song.add_track(track)
    if speed_changes:
        # MCS is fixed-tempo; converted at the initial speed.
        song.source += f" ({len(speed_changes)} speed changes ignored)"
    # keep the raw bytes + options so the dialog can re-quantize onto another
    # tempo grid (optimize_pt3 / optimize_pt3_at) without re-decoding by hand
    song.pt3_source = (data, percussion, drum_sound, shape_durations)
    return song, byte0


def optimize_pt3(song: Song):
    """Re-quantize a PT3-imported Song onto its best-fitting grid — searching a
    wider subdivision set (incl. triplet 3/6) for the least row-rate error.
    Returns (new_song, tempo_byte0, rel_error, speed_drift); the song unchanged
    with (0x80, 0, 0) if it wasn't PT3-imported."""
    src = getattr(song, "pt3_source", None)
    if not src:
        return song, 0x80, 0.0, 0.0
    data, percussion, drum_sound, shape = src
    ticks, byte0, err = fit_row_grid(data[0x64], tick_set=_TICK_SET_WIDE)
    new, byte0 = parse_pt3(data, percussion, drum_sound, shape, grid=(ticks, byte0))
    return new, byte0, err, err


def optimize_pt3_at(song: Song, target_byte0: int):
    """Re-quantize a PT3-imported Song to a CHOSEN MCS tempo: the row keeps the
    subdivision that best fits that tempo (finer at faster tempos). Returns
    (new_song, tempo_byte0, rel_error, speed_drift)."""
    src = getattr(song, "pt3_source", None)
    if not src:
        return song, target_byte0, 0.0, 0.0
    data, percussion, drum_sound, shape = src
    ticks, byte0, err = fit_row_grid(data[0x64], target_byte0=target_byte0,
                                     tick_set=_TICK_SET_WIDE)
    new, byte0 = parse_pt3(data, percussion, drum_sound, shape, grid=(ticks, byte0))
    return new, byte0, err, err
