from mcs_convert.nsf.extract import segment_frames


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
