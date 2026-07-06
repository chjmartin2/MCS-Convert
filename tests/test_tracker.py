"""Tests for the 4-voice tracker grid."""

from mcs_convert.model import NoteEvent, Song, Track
from mcs_convert.tracker import tracker_rows, tracker_text


def _song():
    s = Song(title="t", time_signature="4/4")
    tr = Track(name="T")
    # a C5 quarter with an E5 + G5 chord under it (three simultaneous), then a rest
    tr.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=72))
    tr.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=76))
    tr.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=79))
    tr.add(NoteEvent(start_tick=4, duration_ticks=4, midi_note=0, is_rest=True))
    s.add_track(tr)
    return s


def test_columns_are_highest_to_lowest_and_named_once():
    rows = tracker_rows(_song(), subdiv=2)
    # row 0 (the chord onset): highest G5 in v1, E5 v2, C5 v3
    _, is_bar, cols = rows[0]
    assert is_bar
    assert cols == ["G5", "E5", "C5", ""]
    # next 32nd rows while the chord sustains: names blank (shown once)
    assert rows[1][2] == ["", "", "", ""]


def test_rest_onset_shows_R():
    rows = tracker_rows(_song(), subdiv=2)
    # the quarter chord is 4 sixteenths = 8 rows; the rest begins at row 8
    _, _, cols = rows[8]
    assert cols[0] == "R"


def test_thirty_second_grid_resolution():
    # subdiv 2 => two rows per sixteenth-tick; a 4-tick note spans 8 rows.
    rows = tracker_rows(_song(), subdiv=2)
    assert len(rows) == 16          # 8 (chord) + 8 (rest)


def test_tracker_text_has_four_voice_columns():
    txt = tracker_text(_song())
    assert "v1" in txt and "v4" in txt
    assert "G5" in txt and "R" in txt
