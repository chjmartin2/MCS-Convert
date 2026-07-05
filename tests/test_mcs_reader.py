"""Ground-truth tests for the MCS reader.

The pitch mapping is pinned to a known melody: MINUET in G opens with a rising scale
G4 A4 B4 C5 D5. These are the actual byte1 values from MINUETG.MCS (clef anchor 114).
"""

from mcs_convert.mcs.reader import (
    treble_pitch,
    decode_duration,
    _white_key,
    parse,
    parse_records,
    split_staves,
    Record,
)


def test_minuet_opening_scale():
    clef = 114
    byte1s = [34, 50, 66, 82, 98]           # G A B C D, ascending, from MINUETG.MCS
    got = [treble_pitch(b, clef) for b in byte1s]
    assert got == [67, 69, 71, 72, 74]      # G4 A4 B4 C5 D5


def test_decode_duration_note_ladder():
    # low nibble = note value: 1=16th 2=8th 3=quarter 4=half 5=whole -> 2**(v-1) ticks.
    assert decode_duration(0x01) == (False, 1)    # sixteenth
    assert decode_duration(0x02) == (False, 2)    # eighth
    assert decode_duration(0x03) == (False, 4)    # quarter
    assert decode_duration(0x04) == (False, 8)    # half
    assert decode_duration(0x05) == (False, 16)   # whole
    # stem bits (7:5) must not change the decoded duration
    assert decode_duration(0x82) == (False, 2)    # eighth with stem bits set (MINUETG's A)


def test_decode_duration_rest_flag():
    # bit3 (0x08) set = rest; low value picks the value: 8..12 = 16th..whole rest.
    assert decode_duration(0x08) == (True, 1)     # sixteenth rest
    assert decode_duration(0x09) == (True, 2)     # eighth rest  (MIN2 ground truth: 0x82->0x89)
    assert decode_duration(0x0A) == (True, 4)     # quarter rest
    assert decode_duration(0x89) == (True, 2)     # eighth rest with stem bits set


def test_parse_emits_durations_rests_and_start_ticks(tmp_path):
    # MINUETG's opening measure, but with the first eighth (A) turned into an eighth rest,
    # exactly like the emulator edit: quarter G, eighth-REST, eighth B, eighth C, eighth D.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,                            # treble clef
        0xFF, 0xFF, 5, 1,
        0x03, 0x22,   0x89, 0x39,   0x62, 0x42,   0x42, 0x52,   0x22, 0x62,
        0xFF, 0xFF, 0, 5,
        0xFF, 0xFF, 0, 0,
    ])
    p = tmp_path / "x.mcs"
    p.write_bytes(b"\x00" * 0x0F + body)
    notes = parse(str(p)).tracks[0].notes
    assert [n.duration_ticks for n in notes] == [4, 2, 2, 2, 2]      # q + 4 eighths = 12 (3/4)
    assert [n.is_rest for n in notes] == [False, True, False, False, False]
    assert [n.start_tick for n in notes] == [0, 4, 6, 8, 10]
    assert notes[1].midi_note == 0                                   # rest doesn't carry a pitch


def test_white_key_ladder():
    assert _white_key(0) == 67    # G4
    assert _white_key(-1) == 65   # F4
    assert _white_key(3) == 72    # C5 (crosses the B->C octave boundary)
    assert _white_key(7) == 79    # G5, one octave up


def test_records_and_staves():
    # clef record (count 1), a 2-note record, terminator, then a second staff.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,          # treble clef @ byte1 0x72=114
        0xFF, 0xFF, 2, 1, 0x03, 0x22, 0x82, 0x32,
        0xFF, 0xFF, 0, 2,                       # end of staff
        0xFF, 0xFF, 0, 0,                       # separator
        0xFF, 0xFF, 1, 0, 0x0D, 0x74,          # bass clef
        0xFF, 0xFF, 0, 1,
        0xFF, 0xFF, 0, 0,
    ])
    d = b"\x00" * 0x0F + body
    recs = parse_records(d)
    assert recs[0].count == 1 and recs[0].entries == [(0x06, 0x72)]
    assert recs[1].entries == [(0x03, 0x22), (0x82, 0x32)]
    staves = split_staves(recs)
    assert len(staves) == 2
    assert staves[0][0].entries[0] == (0x06, 0x72)   # treble clef leads staff 0
    assert staves[1][0].entries[0] == (0x0D, 0x74)   # bass clef leads staff 1
