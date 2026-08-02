"""1-voice chord ARPEGGIATION: the shared core, the DOS beeper stream, and the
1-voice .MCS encode. Old-school 8-bit trick -- fake a chord on one voice by
cycling its notes fast; low notes octave up so each slice still reads as a pitch."""

from mcs_convert import audio as A
from mcs_convert import dosplayer as D
from mcs_convert.mcs.encode import encode_song
from mcs_convert.model import NoteEvent, Song, Track


def _chord_song(midis, dur=8):
    s = Song(title="t", source="t")
    for m in midis:
        tr = Track(name=f"v{m}")
        tr.add(NoteEvent(start_tick=0, duration_ticks=dur, midi_note=m))
        s.add_track(tr)
    return s


def test_arp_core_cycles_the_chord_with_a_continuous_index():
    # a C-E-G triad, all sounding for 4 slices, cycles low->mid->high->low
    picks = A.arp_pick_array([(0, 4, 60), (0, 4, 64), (0, 4, 67)])
    assert picks == [60, 64, 67, 60]                 # continuous, even round-robin
    # RLE merge keeps every step distinct (a real arp, not one held note)
    assert A.rle_events(picks) == [(0, 1, 60), (1, 1, 64), (2, 1, 67), (3, 1, 60)]


def test_arp_holds_a_lone_note_but_shimmers_a_chord():
    # a single sustained note must NOT machine-gun re-attack: one held event
    assert A.rle_events(A.arp_pick_array([(0, 6, 60)])) == [(0, 6, 60)]
    # two overlapping notes alternate every slice (a trill/dyad)
    assert A.arp_pick_array([(0, 4, 60), (0, 4, 67)]) == [60, 67, 60, 67]


def test_arp_octaves_up_the_bass_below_the_floor():
    # a low bass note (C2 = 36, ~65 Hz) is raised until >= ~110 Hz so its arp
    # slice gets enough cycles to read as a pitch; a mid note is left alone
    assert A.octave_up_to_floor(36) == 48            # C2 -> C3 (130.8 Hz)
    assert A.midi_to_freq(48) >= A.ARP_FLOOR_HZ
    assert A.octave_up_to_floor(72) == 72            # already high enough
    # the octave-up happens inside the arp: a triad with a sub-floor bass
    picks = A.arp_pick_array([(0, 3, 72), (0, 3, 64), (0, 3, 36)])
    assert 36 not in picks and 48 in picks           # bass shows up an octave up


def test_dos_1voice_arp_beats_the_plain_top_line():
    # plain mono keeps only the top voice (2 writes: on + articulated off);
    # the arp cycles all three voices across sub-ticks (many more events)
    song = _chord_song([72, 64, 40])
    plain = D._mono_stream(song, arp=False)
    arped = D._mono_stream(song, arp=True)
    assert len(arped) > 3 * len(plain)               # the single voice now moves
    # the arp uses the sub-tick grid; a plain build differs from an arp build
    assert D.build_com(song, "1voice", 0x80, arp=True) != \
        D.build_com(song, "1voice", 0x80, arp=False)
    # arp is a 1-voice-only feature
    import pytest
    for mode in ("tandy", "4voice"):
        with pytest.raises(ValueError):
            D.build_com(song, mode, 0x80, arp=True)


def test_mcs_1voice_arp_encodes_more_notes_than_top_line_collapse():
    # the 1-voice .MCS collapse normally keeps the highest note; the arp variant
    # cycles the chord onto the 32nd grid, so it encodes strictly more onsets
    song = _chord_song([72, 64, 55])
    plain = encode_song(song, voices=1, tempo_byte0=0x80)
    arped = encode_song(song, voices=1, arp=True, tempo_byte0=0x80)
    assert plain != arped and len(arped) > len(plain)
    # both are still valid single-voice files (they encode without error and are
    # non-trivial); the arp adds note events, not staff/clef structure
    assert len(arped) > 32
