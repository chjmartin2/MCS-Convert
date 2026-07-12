"""The validator must pass every real MCS song and catch the overflow that
corrupts playback."""

import glob
import os

from mcs_convert.mcs.encode import encode_song
from mcs_convert.mcs.validate import (
    MAX_ENTRIES_PER_MEASURE, summary, validate,
)
from mcs_convert.mcs.writer import build_file, make_entry
from mcs_convert.model import NoteEvent, Song, Track

CORPUS = glob.glob(os.path.join(os.path.dirname(__file__), "..", "samples",
                                "ia_1984", "extracted", "whmcs", "*.MC[SD]"))


def test_every_corpus_song_validates():
    # ground truth: real MCS songs play, so none may report a corrupting issue
    assert CORPUS, "corpus not present"
    for path in CORPUS:
        with open(path, "rb") as fh:
            data = fh.read()
        corrupt = [i for i in validate(data) if i.severity == "corrupt"]
        assert not corrupt, f"{os.path.basename(path)}: {summary(data)}"


def test_overflowing_measure_is_flagged():
    # a measure crammed past the 32-entry buffer must be caught
    entries = [make_entry(0x06, 16, 14)]                  # clef
    fat = [make_entry(0x01, 20, x % 30) for x in range(MAX_ENTRIES_PER_MEASURE + 5)]
    data = build_file([[entries, fat]], tempo_level=1)
    issues = validate(data)
    assert any(i.severity == "corrupt" and "buffer" in i.detail for i in issues)


def test_capped_import_stays_within_limits_and_2_staves():
    # a deliberately dense multi-track song (four channels of steady 16ths, all
    # overlapping) must encode to a valid, loadable file with cap=True
    song = Song(title="dense")
    for ch, base in enumerate((72, 76, 55, 48)):
        tr = Track(name=f"ch{ch}")
        for i in range(64):
            tr.add(NoteEvent(start_tick=i, duration_ticks=1, midi_note=base + i % 5))
        song.add_track(tr)
    data = encode_song(song, cap=True)
    assert not [i for i in validate(data) if i.severity == "corrupt"]
    from mcs_convert.mcs.reader import parse_records, split_staves
    # exactly 2 staves — the grand-staff layout real MCS loads (not the 4-staff
    # structure it rejects as "Not an MCS song")
    assert len(split_staves(parse_records(data))) == 2


def test_dense_measure_notes_get_distinct_x_slots():
    from mcs_convert.mcs.reader import (parse_records, split_staves, symbol,
                                        x_slot, _note_value)
    # 16 sixteenth notes filling a bar: each distinct onset must get its own
    # x-slot (a shared x is read as a chord, so collisions corrupt the rhythm)
    song = Song(title="run")
    tr = Track(name="m")
    for i in range(16):
        tr.add(NoteEvent(start_tick=i * 2, duration_ticks=2, midi_note=72 + i % 6))
    song.add_track(tr)
    data = encode_song(song, cap=True)
    for st in split_staves(parse_records(data)):
        for rec in st[1:]:
            xs = [x_slot(b1) for b0, b1 in rec.entries if _note_value(symbol(b0))[0]]
            assert len(xs) == len(set(xs)), f"x-slot collision: {xs}"
            assert all(x >= 2 for x in xs)         # and none in the barline region
