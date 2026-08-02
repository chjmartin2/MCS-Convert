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
    assert set(back.perf) == {R.PERF_TANDY, R.PERF_1VOICE, R.PERF_4VOICE}
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
