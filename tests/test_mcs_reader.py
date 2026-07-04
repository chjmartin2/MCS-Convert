"""Ground-truth tests for the MCS reader.

The pitch mapping is pinned to a known melody: MINUET in G opens with a rising scale
G4 A4 B4 C5 D5. These are the actual byte1 values from MINUETG.MCS (clef anchor 114).
"""

from mcs_convert.mcs.reader import (
    treble_pitch,
    _white_key,
    parse_records,
    split_staves,
    Record,
)


def test_minuet_opening_scale():
    clef = 114
    byte1s = [34, 50, 66, 82, 98]           # G A B C D, ascending, from MINUETG.MCS
    got = [treble_pitch(b, clef) for b in byte1s]
    assert got == [67, 69, 71, 72, 74]      # G4 A4 B4 C5 D5


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
