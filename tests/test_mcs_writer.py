"""Tests for the MCS writer and the round-trip it enables.

The writer is the format's strongest self-check: anything the reader decodes, the writer
re-encodes, and parsing the result must recover it. (Byte-identical rewrite of the real
corpus is verified separately by tools; the corpus is gitignored so it can't live here.)
"""

import os
import sys

from mcs_convert.mcs.reader import (
    CLEF_BASS, CLEF_TREBLE, SYM_FLAT, SYM_NATURAL, SYM_OCTAVA, SYM_SHARP, parse,
    parse_records,
)
from mcs_convert.mcs.writer import (
    build_file, make_entry, rewrite, serialize_records, v_for_midi,
)
from mcs_convert.pitch import midi_to_name


def test_serialize_is_inverse_of_parse(tmp_path):
    # A crafted file's record stream must survive parse -> serialize unchanged.
    body = bytes([
        0xFF, 0xFF, 2, 0, 0x06, 0x72, 0xEF, 0x80,
        0xFF, 0xFF, 3, 2, 0x03, 34, 0x82, 50, 0x22, 98,
        0xFF, 0xFF, 0, 3,
        0xFF, 0xFF, 0, 0,
    ])
    d = b"\x00" * 0x0F + b"\x00" + body        # header pad + pad byte + records
    p = tmp_path / "x.mcs"
    p.write_bytes(d)
    assert rewrite(str(p)) == d                 # byte-identical
    assert serialize_records(parse_records(d)) == body


def test_v_for_midi_roundtrips_through_reader():
    # Encoding a pitch then decoding it must return the same MIDI note.
    for midi in (67, 72, 74, 76, 77, 79, 84):   # naturals across the treble window
        v = v_for_midi(midi, 1)
        b0, b1 = make_entry(3, v, 5)             # a quarter note
        # rebuild a one-note song and read it back
        data = build_file([[[make_entry(CLEF_TREBLE, 16, 14)], [(b0, b1)]]])
        note = parse_bytes(data).tracks[0].notes[0]
        assert note.midi_note == midi, midi


def parse_bytes(data):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".mcs")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    try:
        return parse(path)
    finally:
        os.remove(path)


def test_build_file_headers_and_sizes():
    clef = make_entry(CLEF_TREBLE, 16, 14)
    note = make_entry(3, v_for_midi(72, 1), 5)
    data = build_file([[[clef], [note]]], tempo_level=2, word7=18)
    assert data[0x05] | (data[0x06] << 8) == 0x3AF9 + 3 * 2      # tempo word
    assert data[0x07] | (data[0x08] << 8) == 18                  # word7
    assert data[0x0D] | (data[0x0E] << 8) == len(data)          # total length
    assert data[0x0F] == 0x00                                    # pad byte
    assert data[0x10:0x12] == b"\xff\xff"                        # records start


def test_full_encode_decode_loop():
    # Build a staff exercising notes, a beamed note, a rest, an accidental, a chord and
    # a dot; parse it back and confirm every element survives the round-trip.
    clef = [make_entry(CLEF_TREBLE, 16, 14), make_entry(SYM_SHARP, 7, 16)]  # key: G major
    m_notes = [make_entry(3, v_for_midi(72, 1), 2),       # C5 quarter
               make_entry(0x17, v_for_midi(74, 1), 6)]    # D5 beamed quarter
    m_rest = [make_entry(10, 10, 2)]                       # quarter rest (fills nothing else)
    m_acc = [make_entry(SYM_FLAT, v_for_midi(76, 1), 2),  # flat glyph...
             make_entry(3, v_for_midi(76, 1), 4)]         # ...on E5 -> Eb5
    m_chord = [make_entry(3, v_for_midi(72, 1), 2),       # C5 + E5 + G5 same x = chord
               make_entry(3, v_for_midi(76, 1), 2),
               make_entry(3, v_for_midi(79, 1), 2)]
    m_key = [make_entry(3, v_for_midi(77, 1), 2)]         # F5 -> F#5 via key signature
    song = parse_bytes(build_file([[clef, m_notes, m_rest, m_acc, m_chord, m_key]]))
    notes = song.tracks[0].notes
    names = [("rest" if n.is_rest else midi_to_name(n.midi_note)) for n in notes]
    assert names[:2] == ["C5", "D5"]                       # note + beamed note
    assert notes[1].duration_ticks == 4                    # beamed quarter = 4 ticks
    assert names[2] == "rest"
    assert names[3] == "D#5"                               # E5 + flat = Eb5 (== D#5)
    assert names[4:7] == ["C5", "E5", "G5"]                # chord
    assert notes[4].start_tick == notes[5].start_tick == notes[6].start_tick
    assert names[7] == "F#5"                               # key signature sharped the F
    assert song.key_signature == "G major"


def test_8va_clef_glyph_raises_staff_an_octave():
    bass_clef = [make_entry(CLEF_BASS, 32, 14), make_entry(SYM_OCTAVA, 24, 22)]
    note = make_entry(3, v_for_midi(48, 21), 2)            # C3 written
    song = parse_bytes(build_file([[bass_clef, [note]]]))
    assert song.tracks[0].notes[0].midi_note == 60         # sounds C4 (+octave)


def test_reference_test_song_covers_every_element():
    # The generated MCSTEST.MCS must decode with each element present.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools"))
    import make_test_mcs
    song = parse_bytes(make_test_mcs.build())
    assert song.time_signature == "4/4"
    assert song.key_signature == "G major"
    treble = next(t for t in song.tracks if t.name == "Treble")
    bass = next(t for t in song.tracks if t.name == "Bass")
    durs = {n.duration_ticks for n in treble.notes if not n.is_rest}
    assert {1, 2, 4, 8, 16} <= durs                        # every note duration present
    rest_durs = {n.duration_ticks for n in treble.notes if n.is_rest}
    assert {1, 2, 4, 8, 16} <= rest_durs                   # every rest duration present
    # a chord: three notes sharing a start tick
    starts = [n.start_tick for n in treble.notes if not n.is_rest]
    assert any(starts.count(s) >= 3 for s in set(starts))
    # 8va: the bass C3 (48) sounds an octave up
    assert bass.notes[0].midi_note == 60
