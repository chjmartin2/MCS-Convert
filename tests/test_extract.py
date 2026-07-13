from mcs_convert.nsf.extract import _CLICKS, _drum_pitches, segment_frames


def test_auto_drum_splits_by_period():
    # "auto" maps bright/high NES noise tones (low period) to hi-hat and
    # dark/low tones (high period) to low bass — SMB's kick/hat two-tone split.
    assert _drum_pitches("auto", 3) == _CLICKS["hi-hat"]     # bright tss
    assert _drum_pitches("auto", 4) == _CLICKS["hi-hat"]
    assert _drum_pitches("auto", 12) == _CLICKS["low bass"]  # dark boom
    # a fixed choice ignores the period entirely
    assert _drum_pitches("block", 12) == _CLICKS["block"]
    assert _drum_pitches("low bass", 3) == _CLICKS["low bass"]


def test_segments_a_single_run():
    events = segment_frames([60, 60, 60])
    assert len(events) == 1
    e = events[0]
    assert e.start_tick == 0 and e.duration_ticks == 3 and e.midi_note == 60


def test_rests_split_notes():
    # note, rest, same note -> two separate events
    events = segment_frames([60, 60, None, 60])
    assert [(e.start_tick, e.duration_ticks, e.midi_note) for e in events] == [
        (0, 2, 60),
        (3, 1, 60),
    ]


def test_pitch_change_splits():
    events = segment_frames([60, 62, 64])
    assert [e.midi_note for e in events] == [60, 62, 64]
    assert all(e.duration_ticks == 1 for e in events)


def test_all_rests_yields_nothing():
    assert segment_frames([None, None]) == []
    assert segment_frames([]) == []
