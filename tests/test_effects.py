"""The RCT effects engine: every effect's flattened per-sub-tick semantics."""

import pytest

from mcs_convert import rct as R
from mcs_convert.effects import flatten, render_flat


def _song(rows=4, speed=4, **cells) -> R.RctSong:
    """Build a one-pattern song; cells keyed 'r{row}c{ch}' -> RctCell fields."""
    s = R.RctSong(speed=speed, tempo_byte0=0x80)
    pat = R.RctPattern(rows=rows)
    for key, fields in cells.items():
        row, ch = key.split("c")
        cell = pat.cell(int(row[1:]), int(ch))
        for k, v in fields.items():
            setattr(cell, k, v)
    s.patterns = {0: pat}
    s.order = [0]
    return s


def _note(midi, **kw):
    return dict(note=R.midi_to_note(midi), inst=1, **kw)


def test_plain_note_sustains_across_rows():
    f = flatten(_song(rows=2, r0c0=_note(60)))
    ch = f.channels[0]
    assert f.total_subs == 8                          # 2 rows x speed 4
    assert ch.pitch == [60.0] * 8                     # held through both rows
    assert ch.onset == [True] + [False] * 7           # one attack
    assert all(v == 15 for v in ch.vol)               # instrument default volume


def test_note_off_and_empty_channels():
    f = flatten(_song(rows=2, r0c0=_note(60), r1c0=dict(note=R.NOTE_OFF)))
    assert f.channels[0].pitch[:4] == [60.0] * 4
    assert f.channels[0].pitch[4:] == [None] * 4      # === silences
    assert f.channels[1].pitch == [None] * 8          # untouched channel


def test_arpeggio_cycles_three_offsets_per_subtick():
    f = flatten(_song(rows=1, r0c0=_note(60, fx=R.FX_ARP, param=0x37)))
    assert f.channels[0].pitch == [60.0, 63.0, 67.0, 60.0]   # +0,+3,+7,+0


def test_pitch_slides_accumulate_in_cents():
    up = flatten(_song(rows=1, r0c0=_note(60, fx=R.FX_SLIDE_UP, param=0x19)))
    # 0x19 = 25 -> +100 cents = +1 semitone per sub-tick
    assert up.channels[0].pitch == pytest.approx([61.0, 62.0, 63.0, 64.0])
    dn = flatten(_song(rows=1, r0c0=_note(60, fx=R.FX_SLIDE_DOWN, param=0x19)))
    assert dn.channels[0].pitch == pytest.approx([59.0, 58.0, 57.0, 56.0])


def test_portamento_glides_without_retrigger():
    f = flatten(_song(rows=2, r0c0=_note(60),
                      r1c0=_note(64, fx=R.FX_PORTA, param=0x19)))   # 1 semi/sub
    ch = f.channels[0]
    assert ch.pitch[4:] == pytest.approx([61.0, 62.0, 63.0, 64.0])  # glide to E
    assert ch.onset[4:] == [False] * 4                # no new attack
    assert ch.pitch[7] == 64.0                        # lands exactly on target


def test_vibrato_oscillates_around_the_note():
    f = flatten(_song(rows=4, r0c0=_note(60),
                      r1c0=dict(fx=R.FX_VIBRATO, param=0x8F),
                      r2c0=dict(fx=R.FX_VIBRATO, param=0x8F),
                      r3c0=dict(fx=R.FX_VIBRATO, param=0x8F)))
    wob = [p for p in f.channels[0].pitch[4:] if p is not None]
    assert max(wob) > 60.05 and min(wob) < 59.95      # excursions both ways
    assert abs(sum(wob) / len(wob) - 60.0) < 0.4      # centred on the note


def test_volume_slide_and_explicit_volume():
    f = flatten(_song(rows=2, r0c0=_note(60, vol=11),  # vol column: 11 -> 10
                      r1c0=dict(fx=R.FX_VOLSLIDE, param=0x03)))
    assert f.channels[0].vol[:4] == [10] * 4
    assert f.channels[0].vol[4:] == [7, 4, 1, 0]      # -3/sub-tick, clamped at 0


def test_note_cut_and_delay():
    cut = flatten(_song(rows=1, r0c0=_note(60, fx=R.FX_CUT, param=2)))
    assert cut.channels[0].pitch == [60.0, 60.0, None, None]
    dly = flatten(_song(rows=1, r0c0=_note(60, fx=R.FX_DELAY, param=2)))
    assert dly.channels[0].pitch == [None, None, 60.0, 60.0]
    assert dly.channels[0].onset == [False, False, True, False]


def test_ornament_follows_table_and_loops():
    s = _song(rows=2, r0c0=_note(60, fx=R.FX_ORNAMENT, param=1))
    s.ornaments[1] = R.RctOrnament(steps=[0, 12], loop=0)   # octave trill, loops
    f = flatten(s)
    assert f.channels[0].pitch == [60.0, 72.0] * 4    # alternates forever
    s.ornaments[1] = R.RctOrnament(steps=[12, 0, 0], loop=-1)  # one-shot attack blip
    f = flatten(s)
    assert f.channels[0].pitch == [72.0] + [60.0] * 7  # holds the last step


def test_instrument_default_ornament_applies_on_trigger():
    s = _song(rows=1, r0c0=_note(60))
    s.instruments[1] = R.RctInstrument(volume=15, ornament=2)
    s.ornaments[2] = R.RctOrnament(steps=[0, 4, 7], loop=0)
    f = flatten(s)
    assert f.channels[0].pitch == [60.0, 64.0, 67.0, 60.0]


def test_speed_change_and_pattern_break():
    # Fxx from row 1 doubles the row length; Bxx jumps into the next pattern
    s = R.RctSong(speed=2, tempo_byte0=0x80)
    p0 = R.RctPattern(rows=3)
    p0.cell(0, 0).note = R.midi_to_note(60)
    p0.cell(0, 0).inst = 1
    p0.cell(1, 0).fx, p0.cell(1, 0).param = R.FX_SPEED, 4
    p0.cell(1, 1).fx, p0.cell(1, 1).param = R.FX_BREAK, 1
    p1 = R.RctPattern(rows=4)
    p1.cell(1, 0).note = R.midi_to_note(72)
    p1.cell(1, 0).inst = 1
    s.patterns = {0: p0, 1: p1}
    s.order = [0, 1]
    f = flatten(s)
    # pattern 0: row0 at speed 2 (2 subs) + row1 at speed 4 (4 subs), then break
    # -> pattern 1 starts at ROW 1 (3 rows x speed 4 = 12 subs)
    assert f.total_subs == 2 + 4 + 12
    assert f.channels[0].pitch[6] == 72.0             # C-5 lands right at the jump


def test_render_flat_produces_audio():
    f = flatten(_song(rows=2, r0c0=_note(60), r0c3=dict(note=R.midi_to_note(84))))
    master, voices = render_flat(f, sr=8000)
    assert master.dtype.name == "float32" and len(voices) == 4
    assert (abs(master) > 0.01).any()                 # actually sounds
    assert (abs(voices[3]) > 0.001).any()             # noise channel too


def test_render_balance_triangle_sits_under_pulse():
    # a full-volume triangle must NOT drown a pulse at the same volume in the
    # preview mix (that inverted balance buried imported NES melodies)
    import numpy as np
    from mcs_convert.effects import _WAVE_GAIN
    assert _WAVE_GAIN["nestri"] < _WAVE_GAIN["pulse25"]
    assert _WAVE_GAIN["triangle"] < _WAVE_GAIN["pulse12"]
    # render a pulse and a triangle at identical volume; the pulse is louder
    def rms_of(wave):
        s = R.RctSong(speed=4, tempo_byte0=0x80)
        s.instruments = {1: R.RctInstrument(waveform=wave, volume=15)}
        pat = R.RctPattern(rows=4)
        pat.cell(0, 0).note = R.midi_to_note(60)
        pat.cell(0, 0).inst = 1
        s.patterns = {0: pat}
        s.order = [0]
        m, v = render_flat(flatten(s), sr=8000)
        return float(np.sqrt(np.mean(v[0] ** 2)))
    assert rms_of("pulse25") > rms_of("nestri")
