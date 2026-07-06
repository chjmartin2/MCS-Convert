"""Tests for the 4-voice tracker grid."""

from mcs_convert.model import NoteEvent, Song, Track
from mcs_convert.tracker import tracker_rows, tracker_text


def _song():
    s = Song(title="t", time_signature="4/4")
    tr = Track(name="T")
    # a C5 eighth (4 ticks) with an E5 + G5 chord under it (three simultaneous), then a rest
    tr.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=72))
    tr.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=76))
    tr.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=79))
    tr.add(NoteEvent(start_tick=4, duration_ticks=4, midi_note=0, is_rest=True))
    s.add_track(tr)
    return s


def test_columns_are_highest_to_lowest_and_named_once():
    rows = tracker_rows(_song(), subdiv=2)
    # row 0 (the chord onset): highest G5 in v1, E5 v2, C5 v3, each PITCH:DUR (4 ticks = 8th)
    _, is_bar, _evt, cols = rows[0]
    assert is_bar
    assert cols == ["G5:8", "E5:8", "C5:8", ""]
    # next 32nd rows while the chord sustains: names blank (shown once)
    assert rows[1][3] == ["", "", "", ""]


def test_rest_onset_shows_R():
    rows = tracker_rows(_song(), subdiv=2)
    # the eighth chord is 4 ticks = 8 rows; the rest begins at row 8
    _, _, _evt, cols = rows[8]
    assert cols[0] == "R:8"


def test_thirty_second_grid_resolution():
    # subdiv 2 => two rows per sixteenth-tick; a 4-tick note spans 8 rows.
    rows = tracker_rows(_song(), subdiv=2)
    assert len(rows) == 16          # 8 (chord) + 8 (rest)


def test_tracker_text_has_four_voice_columns():
    txt = tracker_text(_song())
    assert "v1" in txt and "v4" in txt
    assert "G5" in txt and "R" in txt


def test_duration_tie_and_event_labels():
    s = Song(title="t", time_signature="4/4")
    tr = Track(name="T")
    tr.add(NoteEvent(start_tick=0, duration_ticks=12, midi_note=72, tied=True))   # dotted qtr
    tr.add(NoteEvent(start_tick=12, duration_ticks=5, midi_note=74))              # irregular
    s.add_track(tr)
    s.events.append((0, "T", "8^"))                                              # 8va marker
    rows = tracker_rows(s)
    _, _, evt, cols = rows[0]
    assert cols[0] == "C5:4.~"      # 12 ticks = dotted quarter, tied
    assert evt == "8^"              # 8va event shown in its column
    assert rows[12][3][0] == "D5:!5"   # 5 ticks isn't a clean value -> flagged
