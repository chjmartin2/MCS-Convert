"""The RCT effects engine: patterns -> per-sub-tick pitch/volume arrays.

This module is the ONE semantics authority for tracker effects. `flatten()`
walks an RctSong's order list and expands every pattern, effect, and ornament
into flat per-channel arrays with one entry per SUB-TICK (a quarter of the MCS
32nd-note tick — 8-26 ms depending on tempo). Everything downstream renders
those arrays:

  * the Windows preview (render_flat -> PCM),
  * the PERF stream compilers (streams.py),
  * every .COM / .MCS export.

So what you hear in RCTracker is, by construction, what DOS plays.

Effect semantics (spec: docs/RCT-FORMAT.md):
  Axy arpeggio     cycle +0/+x/+y semitones per sub-tick (row-scoped)
  1xx slide up     +xx*4 cents per sub-tick (row-scoped)
  2xx slide down   -xx*4 cents per sub-tick (row-scoped)
  3xx portamento   glide toward the row's note at xx*4 cents/sub-tick; no retrigger
  4xy vibrato      sine, depth y*12.5 cents, speed x/32 cycles per sub-tick
  Vxy vol slide    +x / -y volume per sub-tick (x wins), clamped 0-15
  Cxx note cut     silence after xx sub-ticks of the row
  Dxx note delay   onset happens xx sub-ticks into the row
  Oxx ornament     select ornament (persists on the channel; 0 = off)
  Fxx set speed    sub-ticks per row, from this row on
  Bxx break        after this row, jump to row xx of the next order position

Pitch is carried as FRACTIONAL MIDI (semitones; 1 cent = 0.01) so slides and
vibrato survive; each backend converts to its own divider/increment domain.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .audio import midi_to_freq
from .mcs.reader import tick_seconds_for
from .rct import (FX_ARP, FX_BREAK, FX_CUT, FX_DELAY, FX_NONE, FX_ORNAMENT,
                  FX_PORTA, FX_SLIDE_DOWN, FX_SLIDE_UP, FX_SPEED, FX_VIBRATO,
                  FX_VOLSLIDE, NOTE_OFF, RctSong, note_to_midi)

_CENT = 0.01                     # one cent in fractional-midi units


@dataclass
class FlatChannel:
    """One channel's flattened performance: parallel per-sub-tick arrays."""
    kind: str                                        # "tone" | "noise"
    pitch: List[Optional[float]] = field(default_factory=list)   # frac midi | None
    vol: List[int] = field(default_factory=list)     # 0-15
    onset: List[bool] = field(default_factory=list)  # new attack this sub-tick
    wave: List[str] = field(default_factory=list)    # waveform name ('' silent)


@dataclass
class FlatSong:
    channels: List[FlatChannel]
    tempo_byte0: int
    subtick_s: float = 0.0       # exact period (free BPM); 0 = MCS byte grid

    @property
    def total_subs(self) -> int:
        return len(self.channels[0].pitch) if self.channels else 0

    @property
    def subtick_seconds(self) -> float:
        return self.subtick_s or tick_seconds_for(self.tempo_byte0) / 4.0


class _ChanState:
    """Per-channel walker state carried across rows."""

    def __init__(self):
        self.midi: Optional[float] = None            # sounding base pitch
        self.target: Optional[float] = None          # portamento goal
        self.inst = 1
        self.vol = 15
        self.orn = 0                                 # active ornament index
        self.orn_pos = 0
        self.vib_phase = 0.0
        self.sounding = False


def flatten(song: RctSong) -> FlatSong:
    """Expand order list + patterns + effects into per-sub-tick arrays."""
    chans = [FlatChannel(kind=song.channel_kind(c)) for c in range(4)]
    st = [_ChanState() for _ in range(4)]
    speed = max(1, song.speed)

    def inst_of(c: _ChanState):
        return song.instruments.get(c.inst) or next(iter(song.instruments.values()))

    order_pos = 0
    start_row = 0
    while order_pos < len(song.order):
        pat = song.patterns.get(song.order[order_pos])
        if pat is None:
            order_pos += 1
            continue
        row = start_row
        start_row = 0
        brk = None                                   # (next_start_row) when Bxx fires
        while row < pat.rows and brk is None:
            cells = pat.cells[row]
            # -- row setup: latch cell data + row-scoped effects per channel --
            row_fx = []
            for c in range(4):
                cell, s = cells[c], st[c]
                if cell.inst:
                    s.inst = cell.inst
                fx, pm = cell.fx, cell.param
                if fx == FX_SPEED and 1 <= pm <= 32:
                    speed = pm
                if fx == FX_BREAK:
                    brk = min(pm, 63)
                if fx == FX_ORNAMENT:
                    s.orn = pm if pm in song.ornaments else 0
                    s.orn_pos = 0
                delay = pm if fx == FX_DELAY else 0
                trigger = cell.note not in (0, NOTE_OFF)
                if cell.note == NOTE_OFF:
                    s.sounding = False
                    s.midi = None
                if trigger and fx == FX_PORTA and s.sounding:
                    s.target = float(note_to_midi(cell.note))   # glide, no attack
                    trigger = False
                if cell.vol:
                    s.vol = cell.vol - 1
                elif trigger and not cell.vol:
                    s.vol = inst_of(s).volume        # new note resets to inst volume
                row_fx.append((fx, pm, trigger, cell, delay))
            # -- emit `speed` sub-ticks for this row --
            for sub in range(speed):
                for c in range(4):
                    fx, pm, trigger, cell, delay = row_fx[c]
                    s = st[c]
                    onset = False
                    if trigger and sub == delay:
                        s.midi = float(note_to_midi(cell.note))
                        s.target = None
                        s.sounding = True
                        s.orn_pos = 0
                        s.vib_phase = 0.0
                        ins = inst_of(s)
                        if cell.fx != FX_ORNAMENT:   # explicit Oxx wins this row
                            s.orn = ins.ornament if ins.ornament in song.ornaments else s.orn
                            if cell.note and ins.ornament:
                                s.orn_pos = 0
                        onset = True
                    if fx == FX_CUT and sub == pm:
                        s.sounding = False
                        s.midi = None
                    if not s.sounding or s.midi is None:
                        chans[c].pitch.append(None)
                        chans[c].vol.append(0)
                        chans[c].onset.append(False)
                        chans[c].wave.append("")
                        continue
                    # per-sub-tick pitch motion
                    if fx == FX_SLIDE_UP:
                        s.midi += pm * 4 * _CENT
                    elif fx == FX_SLIDE_DOWN:
                        s.midi -= pm * 4 * _CENT
                    elif fx == FX_PORTA and s.target is not None:
                        step = pm * 4 * _CENT
                        if abs(s.target - s.midi) <= step:
                            s.midi = s.target
                        else:
                            s.midi += step if s.target > s.midi else -step
                    pitch = s.midi
                    if fx == FX_ARP:
                        offs = (0, (pm >> 4) & 0xF, pm & 0xF)
                        pitch += offs[(sub) % 3]
                    if s.orn in song.ornaments:
                        orn = song.ornaments[s.orn]
                        steps = orn.steps
                        i = s.orn_pos
                        if i >= len(steps):
                            i = (len(steps) - 1 if orn.loop < 0 else
                                 orn.loop + (i - orn.loop) % (len(steps) - orn.loop))
                        pitch += steps[min(i, len(steps) - 1)]
                        s.orn_pos += 1
                    if fx == FX_VIBRATO:
                        x, y = (pm >> 4) & 0xF, pm & 0xF
                        s.vib_phase += x / 32.0
                        pitch += math.sin(2 * math.pi * s.vib_phase) * y * 12.5 * _CENT
                    if fx == FX_VOLSLIDE:
                        x, y = (pm >> 4) & 0xF, pm & 0xF
                        s.vol = min(15, s.vol + x) if x else max(0, s.vol - y)
                    chans[c].pitch.append(pitch)
                    chans[c].vol.append(s.vol)
                    chans[c].onset.append(onset)
                    chans[c].wave.append(inst_of(s).waveform)
            row += 1
        order_pos += 1
        if brk is not None:
            start_row = brk
    return FlatSong(channels=chans, tempo_byte0=song.tempo_byte0,
                    subtick_s=song.subtick_seconds)


# --- preview rendering (full effect fidelity) --------------------------------

def render_flat(flat: FlatSong, sr: int = 44100):
    """FlatSong -> (master float32, [4 per-channel buffers]). Renders straight
    from the flattened arrays — slides really slide, vibrato really wobbles —
    so the preview is the export's ground truth."""
    from .audio import _wave                          # cycle-normalized oscillators
    n_sub = flat.total_subs
    sps = flat.subtick_seconds
    total = int(n_sub * sps * sr) + 1
    voices = [np.zeros(total, dtype=np.float32) for _ in range(4)]
    rng = np.random.default_rng(0x51C7)

    for c, ch in enumerate(flat.channels):
        buf = voices[c]
        phase = 0.0
        noise_hold = 0.0
        noise_val = 0.0
        for s in range(n_sub):
            a = int(s * sps * sr)
            b = min(total, int((s + 1) * sps * sr))
            if b <= a or ch.pitch[s] is None:
                continue
            amp = 0.22 * (ch.vol[s] / 15.0)
            if ch.kind == "noise":
                # brightness follows the pitch: high = hissy, low = rumbly
                hold = max(1, int(sr / max(200.0, midi_to_freq(ch.pitch[s]) * 8)))
                seg = np.empty(b - a, dtype=np.float32)
                for i in range(b - a):
                    if noise_hold <= 0:
                        noise_val = rng.uniform(-1, 1)
                        noise_hold = hold
                    seg[i] = noise_val
                    noise_hold -= 1
                buf[a:b] += (amp * seg)
                continue
            freq = midi_to_freq(ch.pitch[s])
            t = np.arange(b - a, dtype=np.float64)
            ph = phase + t * (freq / sr)
            phase = float(ph[-1] + freq / sr) % 1e9
            buf[a:b] += amp * _wave(np.mod(ph, 1.0), ch.wave[s] or "square")
        # de-click: 2 ms fades at every silence boundary
        _declick(buf, sr)

    master = np.clip(sum(voices), -1.0, 1.0).astype(np.float32)
    return master, voices


def _declick(buf: np.ndarray, sr: int, ms: float = 2.0) -> None:
    """Soften hard edges where the signal starts/stops (in place)."""
    n = max(1, int(sr * ms / 1000.0))
    nz = np.flatnonzero(buf)
    if not len(nz):
        return
    a, b = nz[0], nz[-1]
    buf[a:a + n] *= np.linspace(0.0, 1.0, min(n, len(buf) - a), dtype=np.float32)
    buf[max(0, b - n):b] *= np.linspace(1.0, 0.0, min(n, b), dtype=np.float32)[-min(n, b):]
