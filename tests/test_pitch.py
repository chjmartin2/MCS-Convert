import math

from mcs_convert.pitch import (
    pulse_period_to_freq,
    triangle_period_to_freq,
    freq_to_midi,
    freq_to_midi_int,
    midi_to_name,
)


def test_a4_round_trips():
    assert freq_to_midi_int(440.0) == 69
    assert midi_to_name(69) == "A4"


def test_pulse_period_a4():
    # NTSC pulse period 253 lands very close to A4 (440 Hz).
    freq = pulse_period_to_freq(253)
    assert abs(freq - 440.0) < 3.0
    assert freq_to_midi_int(freq) == 69


def test_low_periods_are_silent():
    assert pulse_period_to_freq(7) == 0.0
    assert triangle_period_to_freq(1) == 0.0
    assert freq_to_midi_int(0.0) is None
    assert math.isnan(freq_to_midi(0.0))


def test_triangle_is_octave_below_pulse_for_same_period():
    # Triangle divides by 32 vs pulse's 16 -> one octave lower for the same period.
    p = 400
    assert abs(freq_to_midi(pulse_period_to_freq(p))
               - freq_to_midi(triangle_period_to_freq(p)) - 12.0) < 1e-6
