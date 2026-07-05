"""Ground-truth tests for the MCS reader.

The decode matches MCSDISK.EXE's own player (found by disassembly): the vertical
position is 6 bits — byte1's low 3 bits over byte0's top 3 — indexing fixed per-clef
pitch windows (treble E7..G4 at v 1..20, bass G5..B2 at v 21..41), with the key
signature coming from accidental glyphs in the clef record. All byte values below are
verbatim from MINUETG.MCS / SCALE.MCS.
"""

from mcs_convert.mcs.reader import (
    decode_duration,
    parse,
    parse_records,
    split_staves,
    symbol,
    vertical,
    x_slot,
)


def _song(body: bytes, tmp_path):
    p = tmp_path / "x.mcs"
    p.write_bytes(b"\x00" * 0x0F + body)
    return parse(str(p))


EMPTY_TREBLE = bytes([0xFF, 0xFF, 1, 0, 0x06, 0x72, 0xFF, 0xFF, 0, 0])


# ---- primitives -----------------------------------------------------------------

def test_entry_bitfields():
    # SCALE's F3: byte0 0x21, byte1 0x93 -> v = (3 << 3) | 1 = 25, x slot 0x93 >> 3.
    assert vertical(0x21, 0x93) == 25
    assert symbol(0x21) == 1                 # sixteenth note
    assert symbol(0xEF) == 0x0F              # sharp glyph
    assert x_slot(0x93) == 18
    # DIXIE's 1-px-apart chord pair shares an 8-px slot
    assert x_slot(27) == x_slot(28) == 3


def test_decode_duration_note_ladder():
    assert decode_duration(0x01) == (False, 1)    # sixteenth
    assert decode_duration(0x02) == (False, 2)    # eighth
    assert decode_duration(0x03) == (False, 4)    # quarter
    assert decode_duration(0x04) == (False, 8)    # half
    assert decode_duration(0x05) == (False, 16)   # whole
    # vertical bits must not change the decoded duration
    assert decode_duration(0x82) == (False, 2)


def test_decode_duration_beamed_notes():
    # 0x15..0x19 are the same five note values as 0x01..0x05, beamed (value = sym - 0x14).
    # These carry the bulk of fast runs (BUMBLE.MCD) and were previously dropped.
    assert decode_duration(0x15) == (False, 1)    # beamed 16th
    assert decode_duration(0x16) == (False, 2)    # beamed 8th
    assert decode_duration(0x17) == (False, 4)    # beamed quarter
    assert decode_duration(0x18) == (False, 8)    # beamed half
    assert decode_duration(0x19) == (False, 16)   # beamed whole
    assert decode_duration(0xF5) == (False, 1)    # vertical bits don't affect duration


def test_beamed_run_is_sounded(tmp_path):
    # Eight beamed 16ths (symbol 0x15) fill a 2/4 measure; every one must sound. This is
    # the shape that made BUMBLE.MCD lose most of its melody before beamed notes decoded.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,
        0xFF, 0xFF, 8, 1,
        0x15, 8, 0x15, 16, 0x15, 24, 0x15, 32,
        0x15, 40, 0x15, 48, 0x15, 56, 0x15, 64,
        0xFF, 0xFF, 0, 8,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert len(notes) == 8
    assert all(not n.is_rest and n.duration_ticks == 1 for n in notes)
    assert [n.start_tick for n in notes] == list(range(8))


def test_decode_duration_rest_ladder():
    # rests are note symbol + 7 (MIN2 ground truth: 8th note 0x82 -> 8th rest 0x89)
    assert decode_duration(0x08) == (True, 1)
    assert decode_duration(0x89) == (True, 2)
    assert decode_duration(0x0A) == (True, 4)
    assert decode_duration(0x0C) == (True, 16)


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
    staves = split_staves(parse_records(b"\x00" * 0x0F + body))
    assert len(staves) == 2
    assert [r.count for r in staves[0]] == [1, 2, 0]
    assert staves[0][0].entries[0] == (0x06, 0x72)
    assert staves[1][0].entries[0] == (0x0D, 0x74)


# ---- MINUETG ground truth ---------------------------------------------------------

def test_minuet_opening_two_bars(tmp_path):
    # Treble bars 1-2, byte-for-byte: D5 G4 A4 B4 C5 | D5 G4 G4.
    body = bytes([
        0xFF, 0xFF, 2, 0, 0x06, 0x72, 0xEF, 0x80,   # clef + key-sig sharp on the F line
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
    assert [n.start_tick for n in notes] == [0, 4, 6, 8, 10, 12, 16, 20]


def test_minuet_bar3_key_signature_and_bar4_leap(tmp_path):
    # Bar 3: E5 C5 D5 E5 F#5 — the F# comes from the key-sig glyph (sharp at v=7,
    # degree 6, i.e. the staff's F positions). Bar 4 opens with the G5 -> G4 drop
    # that no proximity-based model could decode.
    body = bytes([
        0xFF, 0xFF, 2, 0, 0x06, 0x72, 0xEF, 0x80,
        0xFF, 0xFF, 5, 2,
        0xE3, 41, 0x22, 58, 0x02, 74, 0xE2, 81, 0xC2, 97,
        0xFF, 0xFF, 3, 5,
        0xA3, 41, 0x83, 58, 0x83, 74,
        0xFF, 0xFF, 0, 3,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [n.midi_note for n in notes] == [76, 72, 74, 76, 78, 79, 67, 67]
    #                                       E5  C5  D5  E5 F#5  G5  G4  G4


def test_minuet_bass_chord_and_dot(tmp_path):
    # Bass bar 1: {B3, G3} half chord (same x slot) + A3 quarter = 12 ticks = 3/4.
    # Bar 2: a half note with an augmentation dot (sym 0x11) = 12 ticks.
    body = (EMPTY_TREBLE + bytes([
        0xFF, 0xFF, 2, 0, 0x0D, 0x74, 0xBF, 0x83,   # bass clef + key-sig sharp
        0xFF, 0xFF, 3, 2,
        0x24, 36, 0x64, 36, 0x43, 100,
        0xFF, 0xFF, 2, 3,
        0x24, 60, 0x31, 76,                          # half B3 + dot
        0xFF, 0xFF, 0, 2,
        0xFF, 0xFF, 0, 0,
    ]))
    notes = _song(body, tmp_path).tracks[1].notes
    assert [(n.midi_note, n.start_tick, n.duration_ticks) for n in notes] == [
        (59, 0, 8),     # B3 half (chord)
        (55, 0, 8),     # G3 half (chord, same slot)
        (57, 8, 4),     # A3 quarter
        (59, 12, 12),   # dotted half B3 fills bar 2
    ]


def test_rest_replaces_note_like_min2(tmp_path):
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
    """SCALE.MCS body, verbatim: every staff position bottom-to-top as 16th notes.
    Treble staff: clef, EMPTY measure, 12 notes, 8 notes. Bass: clef, 16, 4."""
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


def test_scale_covers_the_fixed_windows(tmp_path):
    song = _song(_scale_body(), tmp_path)
    by_name = {t.name: t for t in song.tracks}
    treble = [n.midi_note for n in by_name["Treble"].notes]
    bass = [n.midi_note for n in by_name["Bass"].notes]
    # Each staff sweeps its fixed window bottom-to-top: bass B2..G5, treble G4..E7.
    assert bass[0] == 47 and bass[-1] == 79        # B2 .. G5
    assert treble[0] == 67 and treble[-1] == 100   # G4 .. E7
    for midis in (treble, bass):
        assert len(midis) == 20
        assert all(b - a in (1, 2) for a, b in zip(midis, midis[1:]))


def test_metadata_tempo_time_key(tmp_path):
    # A 3/4 measure (quarter + 4 eighths = 12) in G major (one sharp glyph in the clef
    # record), with the tempo word 0x3B02 = level 3 at header offset 0x05.
    header = bytearray(0x0F)
    header[0x05], header[0x06] = 0x02, 0x3B      # tempo word 0x3B02 -> level 3
    body = bytes([
        0xFF, 0xFF, 2, 0, 0x06, 0x72, 0xEF, 0x80,   # treble clef + key-sig sharp (F line)
        0xFF, 0xFF, 5, 2,
        0x03, 34, 0x82, 50, 0x62, 66, 0x42, 82, 0x22, 98,
        0xFF, 0xFF, 0, 5,
        0xFF, 0xFF, 0, 0,
    ])
    p = tmp_path / "meta.mcs"
    p.write_bytes(bytes(header) + body)
    song = parse(str(p))
    assert song.time_signature == "3/4"
    assert song.key_signature == "G major"
    assert song.tempo_level == 3
    assert song.tempo_raw == 0x3B02


def test_metadata_four_four_c_major(tmp_path):
    # No accidental glyphs -> C major; a 16-tick measure -> 4/4; default tempo word.
    header = bytearray(0x0F)
    header[0x05], header[0x06] = 0xFC, 0x3A       # 0x3AFC -> level 1
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,
        0xFF, 0xFF, 4, 1, 0x03, 34, 0x03, 50, 0x03, 66, 0x03, 82,   # 4 quarters = 16
        0xFF, 0xFF, 0, 4,
        0xFF, 0xFF, 0, 0,
    ])
    p = tmp_path / "c44.mcs"
    p.write_bytes(bytes(header) + body)
    song = parse(str(p))
    assert song.time_signature == "4/4"
    assert song.key_signature == "C major"
    assert song.tempo_level == 1


def test_whole_rest_fills_the_measure(tmp_path):
    # In a 2/4 song (8-tick measures), a measure holding a lone whole rest means
    # "rest this measure" (8 ticks), not a literal 16 — BUMBLE's bass opens this way.
    body = (EMPTY_TREBLE + bytes([
        0xFF, 0xFF, 1, 0, 0x0D, 0x74,
        0xFF, 0xFF, 1, 1, 0x0C, 90,                      # lone whole rest
        0xFF, 0xFF, 4, 1, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,   # 4 eighths = 2/4
        0xFF, 0xFF, 4, 4, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,
        0xFF, 0xFF, 0, 4,
        0xFF, 0xFF, 0, 0,
    ]))
    notes = _song(body, tmp_path).tracks[1].notes
    assert notes[0].is_rest and notes[0].duration_ticks == 8
    assert notes[1].start_tick == 8                       # music starts at measure 2


def test_grid_is_modal_not_max(tmp_path):
    # One long final measure (a 16-tick held note) must not stretch every 8-tick
    # measure of a 2/4 song — the old max() grid put 16 ticks of silence in each bar.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,
        0xFF, 0xFF, 4, 1, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,
        0xFF, 0xFF, 4, 4, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,
        0xFF, 0xFF, 1, 4, 0x25, 30,                      # finale: a whole note
        0xFF, 0xFF, 0, 1,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [n.start_tick for n in notes] == [0, 2, 4, 6, 8, 10, 12, 14, 16]
    assert notes[-1].duration_ticks == 16                 # the long bar keeps its length


def test_scale_measure_alignment(tmp_path):
    song = _song(_scale_body(), tmp_path)
    by_name = {t.name: t for t in song.tracks}
    assert by_name["Bass"].notes[0].start_tick == 0
    assert by_name["Bass"].notes[16].start_tick == 16     # bass measure 2, tick 0
    assert by_name["Treble"].notes[0].start_tick == 20    # measure 2, front-padded by 4
    assert by_name["Treble"].notes[12].start_tick == 32   # measure 3 from its top
