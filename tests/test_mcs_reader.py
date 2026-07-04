"""Ground-truth tests for the MCS reader.

The pitch mapping is pinned to a known melody: MINUET in G opens with a rising scale
G4 A4 B4 C5 D5. These are the actual byte1 values from MINUETG.MCS (clef anchor 114).
"""

from mcs_convert.mcs.reader import (
    treble_pitch,
    note_duration,
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


def test_note_duration_low_two_bits():
    # bits[1:0] = note value, longest->shortest; higher render bits are ignored.
    assert note_duration(0x00) == 8   # half
    assert note_duration(0x01) == 4   # quarter
    assert note_duration(0x02) == 2   # eighth
    assert note_duration(0x03) == 1   # sixteenth
    # stem bits (7:5) and flag bits (4:2) must not change the decoded duration
    assert note_duration(0x82) == 2   # 0x82 = eighth with stem bits set
    assert note_duration(0xE1) == 4   # quarter, high bits set


def test_parse_emits_durations_and_advances_start_ticks(tmp_path):
    # DAISY-style "Dai-sy": a half note (code 0) then a quarter (code 1), back to back.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,                # treble clef
        0xFF, 0xFF, 2, 1, 0x00, 0x22, 0x01, 0x32,    # half @G4, quarter @A4
        0xFF, 0xFF, 0, 2,
        0xFF, 0xFF, 0, 0,
    ])
    p = tmp_path / "x.mcs"
    p.write_bytes(b"\x00" * 0x0F + body)
    song = parse(str(p))
    notes = song.tracks[0].notes
    assert [n.duration_ticks for n in notes] == [8, 4]     # long then short
    assert [n.start_tick for n in notes] == [0, 8]         # second note starts after the first


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
