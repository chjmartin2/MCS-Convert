"""The .RCT native format: byte-exact round-trips, validation, forward compat."""

import struct

import pytest

from mcs_convert import rct as R


def _demo_song() -> R.RctSong:
    s = R.RctSong(title="Demo", author="RC", comment="hello",
                  channel_mode=R.MODE_3TONE_NOISE, target_hint="4voice",
                  tempo_byte0=0x7D, speed=4)
    pat = R.RctPattern(rows=8)
    pat.cell(0, 0).note = R.midi_to_note(60)         # C-4 on ch1
    pat.cell(0, 0).inst = 1
    pat.cell(0, 1).note = R.midi_to_note(64)
    pat.cell(0, 1).fx, pat.cell(0, 1).param = R.FX_ARP, 0x37   # arp +3,+7
    pat.cell(2, 3).note = R.midi_to_note(84)         # noise hit (bright)
    pat.cell(4, 0).note = R.NOTE_OFF
    pat.cell(6, 2).note = R.midi_to_note(48)
    pat.cell(6, 2).vol = 9                           # volume 8
    s.patterns = {0: pat, 3: R.RctPattern(rows=16)}
    s.order = [0, 3, 0]
    s.instruments = {1: R.RctInstrument(name="lead", waveform="pulse25", volume=12),
                     2: R.RctInstrument(name="bass", waveform="triangle",
                                        volume=15, ornament=1)}
    s.ornaments = {1: R.RctOrnament(steps=[0, 12, 7, -5], loop=1)}
    s.perf[R.PERF_4VOICE] = R.make_perf(R.PERF_4VOICE, divider=100,
                                        samps_per_sub=160, total_subs=96,
                                        stream=b"\x00" * 97)
    return s


def test_roundtrip_is_byte_exact():
    a = R.write_rct(_demo_song())
    b = R.write_rct(R.read_rct(a))
    assert a == b                                    # decode(encode) re-encodes identically
    assert a[:4] == b"RCT!" and a[4] == 1


def test_all_fields_survive():
    s2 = R.read_rct(R.write_rct(_demo_song()))
    assert (s2.title, s2.author, s2.comment) == ("Demo", "RC", "hello")
    assert s2.channel_mode == R.MODE_3TONE_NOISE
    assert s2.target_hint == "4voice" and s2.tempo_byte0 == 0x7D and s2.speed == 4
    assert s2.order == [0, 3, 0] and set(s2.patterns) == {0, 3}
    c = s2.patterns[0].cell(0, 1)
    assert (c.note, c.fx, c.param) == (R.midi_to_note(64), R.FX_ARP, 0x37)
    assert s2.patterns[0].cell(4, 0).note == R.NOTE_OFF
    assert s2.instruments[1].waveform == "pulse25"
    assert s2.instruments[2].ornament == 1
    assert s2.ornaments[1].steps == [0, 12, 7, -5] and s2.ornaments[1].loop == 1
    p = R.parse_perf(s2.perf[R.PERF_4VOICE])
    assert (p["divider"], p["samps_per_sub"], p["total_subs"]) == (100, 160, 96)
    assert len(p["stream"]) == 97


def test_note_helpers():
    assert R.note_name(0) == "..." and R.note_name(R.NOTE_OFF) == "==="
    assert R.note_name(R.midi_to_note(60)) == "C-4"   # middle C
    assert R.note_name(R.midi_to_note(61)) == "C#4"
    assert R.note_to_midi(R.midi_to_note(69)) == 69   # A-4 round-trips


def test_unknown_chunks_are_skipped():
    data = R.write_rct(_demo_song())
    extra = b"XTRA" + struct.pack("<I", 5) + b"hello"
    assert R.read_rct(data + extra).title == "Demo"   # unknown chunk ignored


def test_validation_rejects_garbage():
    with pytest.raises(ValueError):
        R.read_rct(b"NOPE" + b"\x00" * 20)            # bad magic
    s = _demo_song()
    s.order = [7]                                     # missing pattern
    with pytest.raises(ValueError):
        R.write_rct(s)
    s = _demo_song()
    s.speed = 0
    with pytest.raises(ValueError):
        R.write_rct(s)
    with pytest.raises(ValueError):                   # PERF over the DOS cap
        R.make_perf(R.PERF_4VOICE, 100, 160, 60000, b"\x00" * (51 * 1024))


def test_channel_kinds_follow_mode():
    s = _demo_song()
    assert [s.channel_kind(c) for c in range(4)] == ["tone"] * 3 + ["noise"]
    s.channel_mode = R.MODE_4TONE
    assert s.channel_kind(3) == "tone"


def test_arbitrary_bpm_roundtrips_and_snaps_to_mcs():
    # free BPM: the exact sub-tick period lives in the v1 header's reserved
    # bytes; tempo_byte0 tracks the nearest MCS speed (the MCS-mode snap)
    s = _demo_song()
    s.set_bpm(144.0)
    assert abs(s.bpm - 144.0) < 0.5                   # us resolution is plenty
    assert s.subtick_us > 0
    assert 0x77 <= s.tempo_byte0 <= 0x92              # snapped, still valid MCS
    back = R.read_rct(R.write_rct(s))
    assert back.subtick_us == s.subtick_us            # survives the file
    assert abs(back.bpm - s.bpm) < 0.01
    # legacy files (subtick_us == 0) still derive tempo from the MCS byte
    legacy = _demo_song()
    legacy.subtick_us = 0
    from mcs_convert.mcs.reader import tick_seconds_for
    assert abs(legacy.subtick_seconds - tick_seconds_for(legacy.tempo_byte0) / 4) < 1e-9
    b2 = R.read_rct(R.write_rct(legacy))
    assert b2.subtick_us == 0                         # legacy stays legacy


def test_free_bpm_reaches_the_engines_exactly():
    # the flattener and the PERF dividers use the EXACT period, not the snap
    from mcs_convert.effects import flatten
    from mcs_convert.streams import perf_chunks
    s = _demo_song()
    s.set_bpm(150.0)
    flat = flatten(s)
    assert abs(flat.subtick_seconds - s.subtick_seconds) < 1e-9
    p = R.parse_perf(perf_chunks(s)[R.PERF_TANDY])
    import math
    want_div = round(1193182.0 * s.subtick_seconds)
    assert abs(p["divider"] - want_div) <= 1          # exact PIT divider
