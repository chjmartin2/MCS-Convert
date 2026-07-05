"""Ground-truth tests for the MCS reader.

Pitch comes from byte0's top 3 bits: the vertical staff position mod 8, INVERTED
(one step up = class-1), in a global frame where C4 has class 0. The octave is not
stored; it is reconstructed by resolving each note to the class candidate nearest
its predecessor (ties break downward). All byte values below are lifted verbatim
from MINUETG.MCS / SCALE.MCS dumps and MartyPC edit-and-diff experiments.
"""

from mcs_convert.mcs.reader import (
    decode_duration,
    parse,
    parse_records,
    pitch_class,
    position_to_midi,
    resolve_position,
    split_staves,
)


def _song(body: bytes, tmp_path):
    p = tmp_path / "x.mcs"
    p.write_bytes(b"\x00" * 0x0F + body)
    return parse(str(p))


# ---- primitives -----------------------------------------------------------------

def test_pitch_class_and_position_frame():
    # class = byte0 >> 5; global frame: C4 -> 0, D4 -> 7, ... D5 -> 0 again.
    assert pitch_class(0x21) == 1
    assert pitch_class(0xE1) == 7
    assert position_to_midi(0) == 60     # C4
    assert position_to_midi(8) == 74     # D5
    assert position_to_midi(4) == 67     # G4
    assert position_to_midi(-1) == 59    # B3
    assert position_to_midi(-3) == 55    # G3


def test_resolve_nearest_and_tie_down():
    # From D5 (pos 8), class 4 is G4 and A5, both 4 steps away: ties break DOWN
    # (MINUETG bar 1: D5 quarter then G4 eighth — not A5).
    assert resolve_position(4, 8) == 4          # G4
    # Stepwise motion resolves to the adjacent candidate (SCALE's +1 ladder).
    assert resolve_position(7, 8) == 9          # D5 -> E5
    assert resolve_position(0, 9) == 8          # E5 -> D5
    # Exact hit stays put.
    assert resolve_position(0, 8) == 8


def test_decode_duration_note_ladder():
    # low nibble = note value: 1=16th 2=8th 3=quarter 4=half 5=whole -> 2**(v-1) ticks.
    assert decode_duration(0x01) == (False, 1)    # sixteenth
    assert decode_duration(0x02) == (False, 2)    # eighth
    assert decode_duration(0x03) == (False, 4)    # quarter
    assert decode_duration(0x04) == (False, 8)    # half
    assert decode_duration(0x05) == (False, 16)   # whole
    # class bits (7:5) must not change the decoded duration
    assert decode_duration(0x82) == (False, 2)


def test_decode_duration_rest_flag():
    # bit3 (0x08) set = rest; low value picks the value: 8..12 = 16th..whole rest.
    assert decode_duration(0x08) == (True, 1)     # sixteenth rest
    assert decode_duration(0x09) == (True, 2)     # eighth rest  (MIN2 ground truth: 0x82->0x89)
    assert decode_duration(0x0A) == (True, 4)     # quarter rest
    assert decode_duration(0x89) == (True, 2)     # eighth rest with class bits set


# ---- record / staff structure -----------------------------------------------------

def test_records_and_staves_with_empty_measures():
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,               # treble clef
        0xFF, 0xFF, 2, 1, 0x03, 0x22, 0x82, 0x32,   # measure of 2 notes
        0xFF, 0xFF, 0, 2,                            # EMPTY measure (prev != 0)
        0xFF, 0xFF, 0, 0,                            # staff terminator
        0xFF, 0xFF, 1, 0, 0x0D, 0x74,               # bass clef
        0xFF, 0xFF, 0, 0,
    ])
    recs = parse_records(b"\x00" * 0x0F + body)
    staves = split_staves(recs)
    assert len(staves) == 2
    # the empty measure is kept in place, not treated as a terminator
    assert [r.count for r in staves[0]] == [1, 2, 0]
    assert staves[0][0].entries[0] == (0x06, 0x72)
    assert staves[1][0].entries[0] == (0x0D, 0x74)


# ---- MINUETG ground truth ---------------------------------------------------------

def test_minuet_opening_two_bars(tmp_path):
    # MINUETG.MCS treble bars 1-2, byte-for-byte from the file:
    #   bar 1: quarter c0 + eighths c4 c3 c2 c1  = D5  G4 A4 B4 C5
    #   bar 2: quarter c0 + quarters c4 c4       = D5  G4 G4
    body = bytes([
        0xFF, 0xFF, 2, 0, 0x06, 0x72, 0xEF, 0x80,                      # clef + 3/4 glyph
        0xFF, 0xFF, 5, 2,
        0x03, 34, 0x82, 50, 0x62, 66, 0x42, 82, 0x22, 98,
        0xFF, 0xFF, 3, 5,
        0x03, 50, 0x83, 66, 0x83, 82,
        0xFF, 0xFF, 0, 3,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [n.midi_note for n in notes] == [74, 67, 69, 71, 72, 74, 67, 67]
    assert [n.duration_ticks for n in notes] == [4, 2, 2, 2, 2, 4, 4, 4]
    # 3/4 time: measure length 12; bar 2 starts on tick 12
    assert [n.start_tick for n in notes] == [0, 4, 6, 8, 10, 12, 16, 20]


def test_minuet_bass_chord(tmp_path):
    # MINUETG.MCS bass bar 1: two HALF notes at the same x (a chord) + a quarter.
    # Classes 1 & 3 around the bass anchor = B3 + G3, then class 2 = A3.
    # The chord counts once toward the measure: 8 + 4 = 12 ticks = 3/4. Exact fit.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x0D, 0x74,
        0xFF, 0xFF, 3, 1,
        0x24, 36, 0x64, 36, 0x43, 100,
        0xFF, 0xFF, 0, 3,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [(n.midi_note, n.start_tick, n.duration_ticks) for n in notes] == [
        (59, 0, 8),    # B3 half  (chord)
        (55, 0, 8),    # G3 half  (chord, same start)
        (57, 8, 4),    # A3 quarter
    ]


def test_rest_replaces_note_like_min2(tmp_path):
    # Bar 1 with the first eighth replaced by an eighth rest (the MIN2 experiment:
    # byte0 0x82 -> 0x89). Rests keep time but carry no pitch and don't move the
    # octave-resolution reference.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,
        0xFF, 0xFF, 5, 1,
        0x03, 34, 0x89, 57, 0x62, 66, 0x42, 82, 0x22, 98,
        0xFF, 0xFF, 0, 5,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [n.is_rest for n in notes] == [False, True, False, False, False]
    assert [n.start_tick for n in notes] == [0, 4, 6, 8, 10]
    assert notes[1].midi_note == 0
    assert [n.midi_note for n in notes if not n.is_rest] == [74, 69, 71, 72]


# ---- SCALE ground truth -----------------------------------------------------------

def _scale_body() -> bytes:
    """SCALE.MCS body, verbatim: a 40-note ascending white-key scale. Treble staff:
    clef, EMPTY measure, 12 notes, 8 notes. Bass staff: clef, 16 notes, 4 notes."""
    def notes(*pairs):
        out = []
        for b0, b1 in pairs:
            out += [b0, b1]
        return bytes(out)

    treble = (bytes([0xFF, 0xFF, 1, 0, 0x06, 0x72]) +
              bytes([0xFF, 0xFF, 0, 1]) +
              bytes([0xFF, 0xFF, 12, 0]) +
              notes((0x81, 50), (0x61, 58), (0x41, 66), (0x21, 74), (0x01, 82),
                    (0xE1, 89), (0xC1, 97), (0xA1, 105), (0x81, 113), (0x61, 121),
                    (0x41, 129), (0x21, 137)) +
              bytes([0xFF, 0xFF, 8, 12]) +
              notes((0x01, 17), (0xE1, 40), (0xC1, 64), (0xA1, 88),
                    (0x81, 104), (0x61, 120), (0x41, 136), (0x21, 152)) +
              bytes([0xFF, 0xFF, 0, 8]) + bytes([0xFF, 0xFF, 0, 0]))
    bass = (bytes([0xFF, 0xFF, 1, 0, 0x0D, 0x74]) +
            bytes([0xFF, 0xFF, 16, 1]) +
            notes((0x01, 29), (0xE1, 36), (0xC1, 44), (0xA1, 52), (0x81, 60),
                  (0x61, 68), (0x41, 76), (0x21, 84), (0x01, 92), (0xE1, 99),
                  (0xC1, 107), (0xA1, 115), (0x81, 123), (0x61, 131),
                  (0x41, 139), (0x21, 147)) +
            bytes([0xFF, 0xFF, 4, 16]) +
            notes((0x01, 19), (0xE1, 26), (0xC1, 34), (0xA1, 42)) +
            bytes([0xFF, 0xFF, 0, 4]) + bytes([0xFF, 0xFF, 0, 0]))
    return treble + bass


def test_scale_ascends_within_each_staff(tmp_path):
    song = _song(_scale_body(), tmp_path)
    by_name = {t.name: t for t in song.tracks}
    for name in ("Treble", "Bass"):
        midis = [n.midi_note for n in by_name[name].notes]
        assert len(midis) == 20
        # strictly ascending white keys: every step is +1 or +2 semitones
        deltas = [b - a for a, b in zip(midis, midis[1:])]
        assert all(d in (1, 2) for d in deltas), (name, midis)


def test_scale_measure_alignment(tmp_path):
    # 16 sixteenths in the fullest measure -> 4/4. Treble sits out measure 1 (empty
    # record) and enters measure 2 four ticks late (its 12 notes start to the right
    # of the bass's 4). The bass fills measure 1 from tick 0.
    song = _song(_scale_body(), tmp_path)
    by_name = {t.name: t for t in song.tracks}
    assert by_name["Bass"].notes[0].start_tick == 0
    assert by_name["Bass"].notes[16].start_tick == 16     # bass measure 2, tick 0
    assert by_name["Treble"].notes[0].start_tick == 20    # measure 2, front-padded by 4
    assert by_name["Treble"].notes[12].start_tick == 32   # measure 3 from its top
