"""Pitch math: NES timer period <-> frequency <-> MIDI note.

The NES APU sets a channel's pitch with an 11-bit "timer" period `t`. The audible
frequency depends on the channel family:

    pulse    f = CPU / (16 * (t + 1))
    triangle f = CPU / (32 * (t + 1))

where CPU is the 2A03 clock. We use the NTSC clock by default.
"""

from __future__ import annotations

import math

# 2A03 CPU clock, Hz.
CPU_CLOCK_NTSC = 1_789_773.0
CPU_CLOCK_PAL = 1_662_607.0

# MIDI reference: A4 = note 69 = 440 Hz.
A4_HZ = 440.0
A4_MIDI = 69


def pulse_period_to_freq(period: int, cpu_clock: float = CPU_CLOCK_NTSC) -> float:
    """Frequency (Hz) for a pulse channel timer period. 0/invalid -> 0.0 (silent)."""
    if period < 8:  # pulse channels are silenced for periods below 8
        return 0.0
    return cpu_clock / (16.0 * (period + 1))


def triangle_period_to_freq(period: int, cpu_clock: float = CPU_CLOCK_NTSC) -> float:
    """Frequency (Hz) for the triangle channel timer period."""
    if period < 2:
        return 0.0
    return cpu_clock / (32.0 * (period + 1))


def freq_to_midi(freq: float) -> float:
    """Continuous MIDI note number for a frequency. <=0 -> NaN."""
    if freq <= 0.0:
        return float("nan")
    return A4_MIDI + 12.0 * math.log2(freq / A4_HZ)


def freq_to_midi_int(freq: float) -> int | None:
    """Nearest integer MIDI note, or None if frequency is silent/invalid."""
    m = freq_to_midi(freq)
    if math.isnan(m):
        return None
    return int(round(m))


# Note names for pretty-printing (sharps).
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_name(note: int) -> str:
    """MIDI note number -> scientific pitch name, e.g. 69 -> 'A4'."""
    return f"{_NOTE_NAMES[note % 12]}{note // 12 - 1}"
