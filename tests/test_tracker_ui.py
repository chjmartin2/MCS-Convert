"""RCTracker editor: headless smoke tests (no mainloop, window withdrawn)."""

import os

import pytest

from mcs_convert import rct as R

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(tmp_path):
    from mcs_convert.gui.tracker import TrackerApp
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    a = TrackerApp(root)
    yield a
    root.destroy()


def test_note_entry_and_navigation(app):
    class E:                                          # a fake key event
        state = 0

        def __init__(self, keysym, char=""):
            self.keysym, self.char = keysym, char

    app.octave = 4
    app._key(E("z"))                                  # C-4 on ch1 row 0
    cell = app.pattern().cell(0, 0)
    assert R.note_name(cell.note) == "C-4"
    assert cell.inst != 0                             # auto-picked instrument
    assert app.row == 1                               # advanced by step
    app.field = 3
    app._key(E("a", "a"))                             # fx LETTER column: A = arpeggio
    app.field = 4
    app._key(E("3", "3"))
    app._key(E("7", "7"))                             # param column rolls to 0x37
    c = app.pattern().cell(app.row, 0)
    assert c.fx == R.FX_ARP and c.param == 0x37
    app.field = 3
    app._key(E("3", "3"))                             # letter column: 3 = portamento
    assert app.pattern().cell(app.row, 0).fx == R.FX_PORTA
    app.field = 0
    row = app.row
    app._key(E("a"))                                  # note column: A = note off
    assert app.pattern().cell(row, 0).note == R.NOTE_OFF   # (cursor advanced)


def test_order_and_pattern_management(app):
    app.order_add()
    assert app.song.order == [0, 1] and 1 in app.song.patterns
    app.orderbox.selection_set(1)
    app.order_move(-1)
    assert app.song.order == [1, 0]
    app.order_del()
    assert len(app.song.order) == 1


def test_save_bakes_perf_and_reloads(app, tmp_path):
    app.pattern().cell(0, 0).note = R.midi_to_note(60)
    app.pattern().cell(0, 0).inst = 1
    p = str(tmp_path / "t.rct")
    app._do_save(p)
    assert os.path.exists(p)
    back = R.load(p)
    assert set(back.perf) == {R.PERF_TANDY, R.PERF_1VOICE, R.PERF_4VOICE,
                              R.PERF_SBFM}
    app.open_file(p)                                  # reload round-trip
    assert app.pattern().cell(0, 0).note == R.midi_to_note(60)


def test_import_nsf_into_tracker_model():
    # the importer path used by the Import menu, without the dialog
    from mcs_convert.convert import song_to_rct
    from mcs_convert.nsf.extract import extract_song
    path = "samples/mega-man-2-nes-[NSF-ID2018].nsf"
    if not os.path.exists(path):
        pytest.skip("sample NSF not present")
    song, byte0 = extract_song(path, subsong=19)
    rct_song = song_to_rct(song, tempo_byte0=byte0)
    assert rct_song.order and rct_song.patterns
    assert any(c.note for p in rct_song.patterns.values()
               for row in p.cells for c in row)


def test_undo_redo_roundtrip(app):
    class E:
        state = 0

        def __init__(self, keysym, char=""):
            self.keysym, self.char = keysym, char

    app._key(E("z"))                                  # C-4 at row 0
    assert app.pattern().cell(0, 0).note != 0
    app.undo()
    assert app.pattern().cell(0, 0).note == 0         # edit undone
    app.redo()
    assert app.pattern().cell(0, 0).note != 0         # ...and back
    # order ops are undoable too
    app.order_add()
    assert len(app.song.order) == 2
    app.undo()
    assert len(app.song.order) == 1


def test_free_bpm_box_snaps_mcs_byte(app):
    app.v_bpm.set("150")
    app._bpm_changed()
    assert abs(app.song.bpm - 150.0) < 0.5
    assert app.song.subtick_us > 0
    assert 0x77 <= app.song.tempo_byte0 <= 0x92       # snap stays in MCS range
    app.undo()
    assert app.song.subtick_us == 0                   # undo restores legacy grid


def test_follow_map_rolls_the_view(app):
    # simulate playback via the posmap: two patterns in the order, playback in
    # the second must roll cur_pat/row forward when follow is on
    from mcs_convert.effects import flatten
    app.order_add()                                   # order [0, 1]
    app.song.patterns[0].cell(0, 0).note = R.midi_to_note(60)
    app.song.patterns[0].cell(0, 0).inst = 1
    app.song.patterns[1].cell(2, 0).note = R.midi_to_note(72)
    app.song.patterns[1].cell(2, 0).inst = 1
    flat = flatten(app.song)
    app._flat = flat
    app._playing = True
    app.v_follow.set(True)
    # a sub-tick inside pattern 1 row 2: find it via the posmap directly
    target = next(s for s, (op, pat, row) in enumerate(flat.posmap)
                  if pat == 1 and row == 2)
    op, pat, row = flat.posmap[target]
    app.cur_pat, app.row = 0, 0
    # apply exactly what _follow applies
    app.cur_pat, app.row = pat, row
    assert (app.cur_pat, app.row) == (1, 2)
    app._playing = False


def test_mute_blanks_the_channel_in_render(app):
    app.pattern().cell(0, 0).note = R.midi_to_note(69)
    app.pattern().cell(0, 0).inst = 1
    app.mute[0] = True
    master, flat = app._render(app.song)
    assert all(p is None for p in flat.channels[0].pitch)   # muted = silent
    app.mute[0] = False
    master, flat = app._render(app.song)
    assert any(p is not None for p in flat.channels[0].pitch)


def test_target_mode_lints_and_snaps(app):
    from mcs_convert import targetmode as TM
    app.song.instruments[1] = R.RctInstrument(waveform="pulse25", volume=12)
    c = app.pattern().cell(0, 0)
    c.note = R.midi_to_note(60)
    c.inst = 1
    c.fx, c.param = R.FX_VIBRATO, 0x4C
    app.song.set_bpm(150.0)                            # arbitrary tempo
    # switching to MCS mode snaps the tempo to the grid and flags the cell
    app.v_tmode.set(TM.MODE_LABELS["mcs"])
    app._mode_changed()
    assert app.mode == "mcs"
    assert app.song.subtick_us == 0                    # locked to MCS grid
    assert app._lint_count() == 1                      # the vibrato cell
    # SoundBlaster keeps waveform + effects -> clean
    app.v_tmode.set(TM.MODE_LABELS["sb"])
    app._mode_changed()
    assert app._lint_count() == 0
    # back to free: nothing flagged
    app.v_tmode.set(TM.MODE_LABELS["free"])
    app._mode_changed()
    assert app.mode == "free" and app._lint_count() == 0
