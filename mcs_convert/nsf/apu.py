"""APU register model: decode notes and drum hits from the player's register writes.

Five channels:

    pulse 1    $4000-$4003   pitched (11-bit timer; muted when period < 8)
    pulse 2    $4004-$4007   pitched
    triangle   $4008-$400B   pitched (timer/32; gated by the linear counter)
    noise      $400C-$400F   percussion — each $400F write keys a hit
    DPCM       $4010-$4013   percussion — a $4015 bit-4 rising edge starts a sample

The pitched channels are gated the way the hardware gates them: channel enable
($4015), the length counter (~120 Hz), the volume envelope (~240 Hz), and the
triangle's linear counter (~240 Hz) — that gating is what turns a register stream
back into articulate notes instead of wall-to-wall legato. Feed writes in via
`write`, call `end_frame()` once per 60 Hz frame, and read the channel states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from ..pitch import freq_to_midi_int, pulse_period_to_freq, triangle_period_to_freq

PULSE1, PULSE2, TRIANGLE, NOISE, DPCM = 0, 1, 2, 3, 4
STATUS = 0x4015

_LENGTH_TABLE = [10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14,
                 12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30]


@dataclass
class _Pulse:
    period: int = 0
    duty_reg: int = 0            # $4000: duty | halt | const | vol
    length: int = 0
    env_level: int = 0
    env_div: int = 0
    enabled: bool = False

    @property
    def halt(self) -> bool:
        return bool(self.duty_reg & 0x20)

    def volume(self) -> int:
        return self.duty_reg & 0x0F if self.duty_reg & 0x10 else self.env_level

    def clock_envelope(self) -> None:
        if self.env_div == 0:
            self.env_div = self.duty_reg & 0x0F
            if self.env_level > 0:
                self.env_level -= 1
            elif self.halt:                      # loop flag doubles as env loop
                self.env_level = 15
        else:
            self.env_div -= 1

    def clock_length(self) -> None:
        if not self.halt and self.length > 0:
            self.length -= 1

    def sounding(self) -> bool:
        return (self.enabled and self.length > 0 and self.period >= 8
                and self.volume() > 0)

    def freq(self) -> float:
        """Continuous frequency in Hz (0 when gated off) — the true pitch,
        including any per-frame vibrato/slide, before rounding to a note."""
        return pulse_period_to_freq(self.period) if self.sounding() else 0.0

    def midi(self):
        return freq_to_midi_int(self.freq()) if self.sounding() else None


@dataclass
class _Triangle:
    period: int = 0
    control: int = 0             # $4008: halt/control | linear reload value
    length: int = 0
    linear: int = 0
    linear_reload: bool = False
    enabled: bool = False

    def clock_linear(self) -> None:
        if self.linear_reload:
            self.linear = self.control & 0x7F
        elif self.linear > 0:
            self.linear -= 1
        if not self.control & 0x80:
            self.linear_reload = False

    def clock_length(self) -> None:
        if not self.control & 0x80 and self.length > 0:
            self.length -= 1

    def sounding(self) -> bool:
        return (self.enabled and self.length > 0 and self.linear > 0
                and self.period >= 2)

    def freq(self) -> float:
        return triangle_period_to_freq(self.period) if self.sounding() else 0.0

    def midi(self):
        return freq_to_midi_int(self.freq()) if self.sounding() else None


class APUState:
    """Accumulates register writes; end_frame() advances the 240 Hz machinery."""

    def __init__(self) -> None:
        self.frame = 0
        self.pulse = [_Pulse(), _Pulse()]
        self.triangle = _Triangle()
        self.noise_hits: List[Tuple[int, int]] = []   # (frame, period index)
        self.dpcm_hits: List[int] = []                # frames
        self._noise_period = 0
        self._noise_ctrl = 0                           # $400C: volume/envelope
        self._noise_enabled = False
        self._dmc_on = False
        self.writes: List[Tuple[int, int]] = []       # this frame's (addr, value)

    # ---- bus hook -----------------------------------------------------------
    def write(self, addr: int, value: int) -> None:
        value &= 0xFF
        self.writes.append((addr, value))
        if addr == STATUS:
            self.pulse[0].enabled = bool(value & 0x01)
            self.pulse[1].enabled = bool(value & 0x02)
            self.triangle.enabled = bool(value & 0x04)
            self._noise_enabled = bool(value & 0x08)
            if not value & 0x01:
                self.pulse[0].length = 0
            if not value & 0x02:
                self.pulse[1].length = 0
            if not value & 0x04:
                self.triangle.length = 0
            dmc = bool(value & 0x10)
            if dmc and not self._dmc_on:                  # rising edge = sample start
                self.dpcm_hits.append(self.frame)
            self._dmc_on = dmc
            return
        if 0x4000 <= addr <= 0x4007:                      # pulses
            ch = self.pulse[(addr - 0x4000) // 4]
            reg = addr & 3
            if reg == 0:
                ch.duty_reg = value
            elif reg == 2:
                ch.period = (ch.period & 0x0700) | value
            elif reg == 3:
                ch.period = (ch.period & 0x00FF) | ((value & 0x07) << 8)
                if ch.enabled:
                    ch.length = _LENGTH_TABLE[value >> 3]
                ch.env_level = 15                          # envelope restart
                ch.env_div = ch.duty_reg & 0x0F
        elif 0x4008 <= addr <= 0x400B:                     # triangle
            t = self.triangle
            if addr == 0x4008:
                t.control = value
            elif addr == 0x400A:
                t.period = (t.period & 0x0700) | value
            elif addr == 0x400B:
                t.period = (t.period & 0x00FF) | ((value & 0x07) << 8)
                if t.enabled:
                    t.length = _LENGTH_TABLE[value >> 3]
                t.linear_reload = True
        elif 0x400C <= addr <= 0x400F:                     # noise
            if addr == 0x400C:
                self._noise_ctrl = value
            elif addr == 0x400E:
                self._noise_period = value & 0x0F
            elif addr == 0x400F and self._noise_enabled:   # key-on
                # Only an AUDIBLE key-on is a drum hit. In constant-volume mode
                # (bit 4) a zero low-nibble is silent — SMB writes $400F with
                # volume 0 as a note-OFF/mute; counting those spawns phantom hits
                # that turn the noise's 18/9 shuffle into a triplet buzz. Envelope
                # mode (bit 4 clear) always keys on at level 15, so it's audible.
                vol = (self._noise_ctrl & 0x0F) if (self._noise_ctrl & 0x10) else 15
                if vol > 0:
                    self.noise_hits.append((self.frame, self._noise_period))

    # ---- 60 Hz frame boundary --------------------------------------------------
    def end_frame(self) -> None:
        for _ in range(4):                                 # ~240 Hz quarter-frames
            for ch in self.pulse:
                ch.clock_envelope()
            self.triangle.clock_linear()
        for _ in range(2):                                 # ~120 Hz half-frames
            for ch in self.pulse:
                ch.clock_length()
            self.triangle.clock_length()
        self.frame += 1
        self.writes = []

    def pitched_midis(self):
        """(pulse1, pulse2, triangle) current midi notes (None = silent)."""
        return (self.pulse[0].midi(), self.pulse[1].midi(), self.triangle.midi())

    def pitched_freqs(self):
        """(pulse1, pulse2, triangle) continuous Hz (0 = silent) — keeps the
        per-frame vibrato/slide that rounding to a MIDI note would erase."""
        return (self.pulse[0].freq(), self.pulse[1].freq(), self.triangle.freq())

    def pitched_timbres(self):
        """(p1_duty, p1_vol, p2_duty, p2_vol, tri_vol) this frame — the universal-
        tracker nuances: pulse duty index 0-3 ($4000/$4004 bits 6-7) and current
        envelope/constant volume 0-15 (triangle has no volume; 15 while sounding)."""
        p1, p2 = self.pulse
        return (p1.duty_reg >> 6, p1.volume(), p2.duty_reg >> 6, p2.volume(),
                15 if self.triangle.sounding() else 0)
