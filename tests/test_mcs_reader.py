"""Ground-truth tests for the MCS reader.

The decode matches MCSDISK.EXE's own player (found by disassembly): the vertical
position is 6 bits — byte1's low 3 bits over byte0's top 3 — indexing fixed per-clef
pitch windows (treble E7..G4 at v 1..20, bass G5..B2 at v 21..41), with the key
signature coming from accidental glyphs in the clef record. All byte values below are
verbatim from MINUETG.MCS / SCALE.MCS.
"""

import pytest

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
    # One tick = a 32nd. 0x00 is the 32nd note (1 tick); the ladder doubles up from there.
    assert decode_duration(0x00) == (False, 1)    # thirty-second
    assert decode_duration(0x01) == (False, 2)    # sixteenth
    assert decode_duration(0x02) == (False, 4)    # eighth
    assert decode_duration(0x03) == (False, 8)    # quarter
    assert decode_duration(0x04) == (False, 16)   # half
    assert decode_duration(0x05) == (False, 32)   # whole
    # vertical bits must not change the decoded duration
    assert decode_duration(0x82) == (False, 4)


def test_decode_duration_beamed_notes():
    # 0x14..0x18 are the note values 32nd..half, beamed (value = sym - 0x14). These carry
    # the bulk of fast runs (BUMBLE.MCD) and were previously dropped. 0x19 is NOT a beamed
    # whole — the engine dispatches it to a tie handler (it once inserted phantom whole
    # notes into CANON, ELSEWERE, BABYFACE, ...).
    assert decode_duration(0x14) == (False, 1)    # beamed 32nd
    assert decode_duration(0x15) == (False, 2)    # beamed 16th
    assert decode_duration(0x16) == (False, 4)    # beamed 8th
    assert decode_duration(0x17) == (False, 8)    # beamed quarter
    assert decode_duration(0x18) == (False, 16)   # beamed half
    with pytest.raises(ValueError):
        decode_duration(0x19)                     # below-the-notes tie glyph, not a note
    assert decode_duration(0xF5) == (False, 2)    # vertical bits don't affect duration


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
    assert all(not n.is_rest and n.duration_ticks == 2 for n in notes)   # 16th = 2 ticks
    assert [n.start_tick for n in notes] == list(range(0, 16, 2))


def test_decode_duration_rest_ladder():
    # rests are note symbol + 7 (MIN2 ground truth: 8th note 0x82 -> 8th rest 0x89);
    # 0x07 is the 32nd rest. One tick = a 32nd.
    assert decode_duration(0x07) == (True, 1)     # 32nd rest
    assert decode_duration(0x08) == (True, 2)     # 16th rest
    assert decode_duration(0x89) == (True, 4)     # 8th rest
    assert decode_duration(0x0A) == (True, 8)     # quarter rest
    assert decode_duration(0x0C) == (True, 32)    # whole rest


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
    # MINUETG's opening two bars, byte-for-byte. Pitches are the notation MCS draws on
    # screen (D5 G4 A4 B4 C5 | D5 G4 G4 in G major) — `MIDI = value/2 + 44`.
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
    assert [n.duration_ticks for n in notes] == [8, 4, 4, 4, 4, 8, 8, 8]
    assert [n.start_tick for n in notes] == [0, 8, 12, 16, 20, 24, 32, 40]


def test_minuet_bar3_key_signature_and_bar4_leap(tmp_path):
    # Bar 3 is E5 C5 D5 E5 F#5 — the F# comes from the key-sig glyph (sharp at v=7,
    # degree 6, the staff's F positions); bar 4 opens with the G5->G4 downward leap.
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
    assert notes[4].midi_note - notes[3].midi_note == 2       # key-sig sharp: +2 from the E


def test_minuet_bass_chord_and_dot(tmp_path):
    # Bass bar 1: {B3, G3} half chord (same x slot) + A3 quarter = 24 ticks = 3/4.
    # Bar 2: a half note with an augmentation dot (sym 0x11) = 24 ticks (16 + 8).
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
        (59, 0, 16),    # chord note 1, half
        (55, 0, 16),    # chord note 2, half (same slot)
        (57, 16, 8),    # quarter
        (59, 24, 24),   # dotted half fills bar 2 (16 + 8)
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
    assert [n.start_tick for n in notes] == [0, 8, 12, 16, 20]
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
    # Each staff sweeps its fixed window bottom-to-top (each staff spans
    # a fixed 20-position range and the two overlap by an octave).
    assert bass[0] == 47 and bass[-1] == 79        # window bottom .. top, bass
    assert treble[0] == 67 and treble[-1] == 100    # window bottom .. top, treble
    for midis in (treble, bass):
        assert len(midis) == 20
        assert all(b - a in (1, 2) for a, b in zip(midis, midis[1:]))


def test_metadata_tempo_time_key(tmp_path):
    # A 3/4 measure (quarter + 4 eighths = 24 ticks) in G major (one sharp glyph in the
    # clef record), with the tempo word 0x3B02 = level 3 at header offset 0x05.
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
    assert song.time_signature == "3/4"          # from the 0x05 meter code (0x3B02)
    assert song.key_signature == "G major"
    assert song.timesig_code == 3


def test_metadata_four_four_c_major(tmp_path):
    # No accidental glyphs -> C major; a 32-tick measure -> 4/4; default tempo word.
    header = bytearray(0x0F)
    header[0x05], header[0x06] = 0xFC, 0x3A       # 0x3AFC -> level 1
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,
        0xFF, 0xFF, 4, 1, 0x03, 34, 0x03, 50, 0x03, 66, 0x03, 82,   # 4 quarters = 32
        0xFF, 0xFF, 0, 4,
        0xFF, 0xFF, 0, 0,
    ])
    p = tmp_path / "c44.mcs"
    p.write_bytes(bytes(header) + body)
    song = parse(str(p))
    assert song.time_signature == "4/4"
    assert song.key_signature == "C major"
    assert song.timesig_code == 1


def test_whole_rest_fills_the_measure(tmp_path):
    # In a 2/4 song (16-tick measures), a measure holding a lone whole rest means
    # "rest this measure" (16 ticks), not a literal 32 — BUMBLE's bass opens this way.
    body = (EMPTY_TREBLE + bytes([
        0xFF, 0xFF, 1, 0, 0x0D, 0x74,
        0xFF, 0xFF, 1, 1, 0x0C, 90,                      # lone whole rest
        0xFF, 0xFF, 4, 1, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,   # 4 eighths = 2/4
        0xFF, 0xFF, 4, 4, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,
        0xFF, 0xFF, 0, 4,
        0xFF, 0xFF, 0, 0,
    ]))
    notes = _song(body, tmp_path).tracks[1].notes
    assert notes[0].is_rest and notes[0].duration_ticks == 16
    assert notes[1].start_tick == 16                     # music starts at measure 2


def test_grid_is_modal_not_max(tmp_path):
    # One long final measure (a 32-tick held note) must not stretch every 16-tick
    # measure of a 2/4 song — the old max() grid put a whole note of silence in each bar.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,
        0xFF, 0xFF, 4, 1, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,
        0xFF, 0xFF, 4, 4, 0x22, 30, 0x22, 60, 0x22, 90, 0x22, 120,
        0xFF, 0xFF, 1, 4, 0x25, 30,                      # finale: a whole note
        0xFF, 0xFF, 0, 1,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [n.start_tick for n in notes] == [0, 4, 8, 12, 16, 20, 24, 28, 32]
    assert notes[-1].duration_ticks == 32                 # the long bar keeps its length


def test_8va_glyph_raises_from_its_position(tmp_path):
    # An 8va glyph (0x12) raises notes AFTER it an octave; the engine restores the
    # baseline at the barline. Here the glyph leads the measure, so C6 @v10 sounds C7.
    # This is the fix that put ENTERTAN's main theme back in the intro's register.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,               # treble clef
        0xFF, 0xFF, 2, 1, 0x92, 0x10, 0x43, 0x51,   # 8va glyph @v4, then C6 quarter @v10
        0xFF, 0xFF, 0, 2,
        0xFF, 0xFF, 0, 0,
    ])
    note = _song(body, tmp_path).tracks[0].notes[0]
    assert note.midi_note == 96 and note.octave == 1     # C6 (84) + 12 = C7


def test_notes_before_a_mid_measure_8va_stay_put(tmp_path):
    # The engine SETS the shift when its walker passes the glyph, so earlier notes in the
    # measure are untouched. ENTERTAN bars 5/9/13/... put the glyph after the first two
    # notes; shifting them too is exactly the once-a-phrase wrong jump the fix removes.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,               # treble clef
        0xFF, 0xFF, 3, 1,
        0x43, 0x21,                                 # C6 quarter @v10 x4 — before the glyph
        0x92, 0x40,                                 # 8va glyph @v4 x8
        0x43, 0x61,                                 # C6 quarter @v10 x12 — after it
        0xFF, 0xFF, 0, 3,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert notes[0].midi_note == 84 and notes[0].octave == 0   # untouched
    assert notes[1].midi_note == 96 and notes[1].octave == 1   # raised


def test_8va_below_the_notes_still_raises(tmp_path):
    # The glyph's vertical placement is cosmetic: MCSDISK's handler is a bare
    # "working shift = +0x18" whatever v is. There is no 8vb in the engine.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,               # treble clef
        0xFF, 0xFF, 2, 1, 0x92, 0x12, 0x43, 0x51,   # 8va glyph @v20 (below), C6 @v10
        0xFF, 0xFF, 0, 2,
        0xFF, 0xFF, 0, 0,
    ])
    note = _song(body, tmp_path).tracks[0].notes[0]
    assert note.midi_note == 96 and note.octave == 1     # up, not down


def test_mixed_duration_chord_advances_by_the_shortest(tmp_path):
    # THATSALL bar 3: a 16th stacked on an 8th at one x, then a 16th rest. The engine
    # feeds each voice separately — the next slot starts when the SHORTEST chord member
    # ends, the 8th keeps ringing under it, and the measure does NOT stretch (the old
    # max() rule made it 18/16 ticks, front-padding the other staff out of alignment).
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,               # treble clef
        0xFF, 0xFF, 3, 1,
        0x41, 0x21,                                 # 16th @v10 x4  (C6, 2 ticks)
        0x82, 0x21,                                 # 8th  @v12 x4  (A5, 4 ticks) chord
        0xA8, 0x41,                                 # 16th rest @x8
        0xFF, 0xFF, 0, 3,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    got = [(n.start_tick, n.duration_ticks, n.midi_note, n.is_rest) for n in notes]
    assert got == [
        (0, 2, 84, False),      # the 16th
        (0, 4, 81, False),      # the 8th, ringing past the 16th
        (2, 2, 0, True),        # the rest starts when the 16th ends, not the 8th
    ]


def test_chord_dots_target_their_own_note(tmp_path):
    # THATSALL m5: a chord of two 8ths followed by TWO dot glyphs, one at each member's
    # v. The engine dots the note sitting exactly at the glyph's v (handler 0x245c), so
    # each member is dotted once (6 ticks) — not every dot on every note, which used to
    # compound into a 9-tick "!9".
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,               # treble clef
        0xFF, 0xFF, 4, 1,
        0x42, 0x21,                                 # 8th @v10 x4 (C6)
        0x82, 0x21,                                 # 8th @v12 x4 (A5) — chord
        0x51, 0x29,                                 # dot @v10 x5
        0x91, 0x29,                                 # dot @v12 x5
        0xFF, 0xFF, 0, 4,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [(n.midi_note, n.duration_ticks) for n in notes] == [(84, 6), (81, 6)]


def test_bass_clef_on_top_staff_reads_bass_window(tmp_path):
    # THATSALL puts a BASS clef on the TOP staff. The engine's four ladder tables
    # (ds:0x5c47 + 2*clef1 + clef2) show the window follows the CLEF, not the staff
    # position — v10 under a bass clef is E4, not the treble window's C6.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x0D, 0x72,               # bass clef record, top staff
        0xFF, 0xFF, 1, 1, 0x43, 0x51,               # quarter @v10
        0xFF, 0xFF, 0, 1,
        0xFF, 0xFF, 0, 0,
    ])
    note = _song(body, tmp_path).tracks[0].notes[0]
    assert note.midi_note == 64                          # E4 via the bass window


def test_mid_staff_clef_change_rewindows(tmp_path):
    # A clef glyph inside a measure swaps the pitch window from that point on, and it
    # persists into later measures (unlike the 8va, the barline does not reset it).
    # CANON/SOCKHOP and 14 other songs depend on this.
    body = bytes([
        0xFF, 0xFF, 1, 0, 0x06, 0x72,               # treble clef record
        0xFF, 0xFF, 3, 1,
        0x43, 0x21,                                 # quarter @v10 x4  -> treble C6
        0x0D, 0x40,                                 # bass clef glyph @x8
        0x43, 0x61,                                 # same v10 @x12   -> now E4
        0xFF, 0xFF, 1, 3, 0x43, 0x21,               # next measure, still bass -> E4
        0xFF, 0xFF, 0, 1,
        0xFF, 0xFF, 0, 0,
    ])
    notes = _song(body, tmp_path).tracks[0].notes
    assert [n.midi_note for n in notes] == [84, 64, 64]


def test_tie_mark_flags_the_preceding_note(tmp_path):
    # A tie/slur glyph after a note flags it as carried into the next. 0x13 is the
    # above-the-notes form, 0x19 the below form — same effect, and 0x19 must not
    # decode as a note (it used to become a phantom beamed whole).
    for tie_b0 in (0x13, 0x99):                     # 0x99 = sym 0x19 with v bits set
        body = bytes([
            0xFF, 0xFF, 1, 0, 0x06, 0x72,
            0xFF, 0xFF, 2, 1, 0x43, 0x51, tie_b0, 0x60,   # C6 quarter, then a tie mark
            0xFF, 0xFF, 0, 2,
            0xFF, 0xFF, 0, 0,
        ])
        notes = _song(body, tmp_path).tracks[0].notes
        assert len(notes) == 1                            # the tie adds no note
        assert notes[0].tied and notes[0].midi_note == 84  # C6, tied forward


def test_scale_measure_alignment(tmp_path):
    song = _song(_scale_body(), tmp_path)
    by_name = {t.name: t for t in song.tracks}
    assert by_name["Bass"].notes[0].start_tick == 0
    assert by_name["Bass"].notes[16].start_tick == 32     # bass measure 2, tick 0
    assert by_name["Treble"].notes[0].start_tick == 40    # measure 2, front-padded by 8
    assert by_name["Treble"].notes[12].start_tick == 64   # measure 3 from its top
