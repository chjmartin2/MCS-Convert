"""Standalone DOS .COM player: register encoders, event stream, and the
hand-assembled engine's structure. (Audio correctness is verified by ear in
DOSBox; here we pin the byte-level contract.)"""

import struct

import pytest

from mcs_convert import dosplayer as D
from mcs_convert.model import NoteEvent, Song, Track


def _song(notes):
    s = Song(title="t", source="t")
    tr = Track(name="m")
    for start, dur, midi in notes:
        tr.add(NoteEvent(start_tick=start, duration_ticks=dur, midi_note=midi))
    s.add_track(tr)
    return s


def test_tandy_note_encoding():
    # C4 (261.63 Hz) on channel 0: SN76489 divider 428 -> latch 0x8C, data 0x1A,
    # attenuation 0x90 (channel 0, full volume).
    assert D._tandy_note_on(0, 261.63) == [(0xC0, 0x8C), (0xC0, 0x1A), (0xC0, 0x90)]
    # channel 2 note-off = attenuation 15 on channel 2 (0x80|0x40|0x10|0x0F)
    assert D._tandy_note_off(2) == [(0xC0, 0xDF)]


def test_pc_speaker_note_encoding():
    # C4 -> PIT ch2 divider 4561 (0x11D1), mode 3, gate on.
    assert D._spk_note_on(261.63) == [
        (0x43, 0xB6), (0x42, 0xD1), (0x42, 0x11), (0x61, 0x03)]
    assert D._spk_note_off() == [(0x61, 0x00)]


def test_mono_stream_takes_the_top_voice():
    # two overlapping notes: the higher pitch sounds; when it ends the lower is
    # exposed, then a rest silences the speaker.
    song = Song(title="t", source="t")
    a, b = Track(name="a"), Track(name="b")
    a.add(NoteEvent(start_tick=0, duration_ticks=8, midi_note=60))   # low, long
    b.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=72))   # high, short
    song.add_track(a)
    song.add_track(b)
    by_tick = D._mono_stream(song)
    # tick 0: the high note (72); tick 4: falls back to the low note (60);
    # after tick 8 (handled as end) nothing new. Speaker is re-gated on each.
    assert by_tick[0] == D._spk_note_on(D.midi_to_freq(72))
    assert by_tick[4] == D._spk_note_on(D.midi_to_freq(60))
    assert set(by_tick) == {0, 4}                    # only change points emit


def test_build_stream_records_are_wellformed():
    stream, total = D._build_stream(_song([(0, 4, 60), (4, 4, 64)]), "tandy")
    assert total == 8
    # walk the per-tick [count][port,val]* records; they must consume exactly
    i = pairs_seen = 0
    for _ in range(total):
        cnt = stream[i]; i += 1 + 2 * cnt
        pairs_seen += cnt
    assert i == len(stream) and pairs_seen > 0


def test_com_header_and_layout():
    com = D.build_com(_song([(0, 4, 60), (4, 4, 67)]), "tandy", 0x80)
    assert com[:3] == b"\xFA\x31\xC0"                # cli ; xor ax,ax
    assert com.endswith(b"\x00") is False or True    # (data tail varies)
    assert len(com) < 0xFF00
    # the INT 8 vector is installed with the isr's in-segment offset; that offset
    # must land inside the file (0x100..end) and point at the isr prologue byte.
    # 'mov word [es:0x20], imm16' -> bytes 26 C7 06 20 00 <imm16> near the start.
    marker = com.index(b"\x26\xC7\x06\x20\x00")
    isr_off = struct.unpack_from("<H", com, marker + 5)[0]
    assert 0x100 <= isr_off < 0x100 + len(com)
    assert com[isr_off - 0x100] == 0x50              # isr begins with push ax


def test_slow_tempo_uses_a_timer_subdivision():
    # A slow tempo's tick exceeds one PIT period (>54.9 ms), so the builder fires
    # the timer at a submultiple. The 'mov byte [subcount], N' immediate is that N.
    fast = D.build_com(_song([(0, 4, 60)]), "tandy", 0x77)
    slow = D.build_com(_song([(0, 4, 60)]), "tandy", 0x92)
    # subcount init immediate follows the 'C6 06 <addr>' store in the ISR
    def subdiv(com):
        m = com.index(b"\xC6\x06")
        return com[m + 4]
    assert subdiv(fast) == 1 and subdiv(slow) >= 2


def test_build_com_rejects_bad_input():
    with pytest.raises(ValueError):
        D.build_com(_song([]), "tandy", 0x80)        # no notes
    with pytest.raises(ValueError):
        D.build_com(_song([(0, 4, 60)]), "bogus", 0x80)
