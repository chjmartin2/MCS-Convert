"""Target-mode linting: which cells each output target can express."""

from mcs_convert import rct as R
from mcs_convert import targetmode as TM


def _cell(**kw):
    c = R.RctCell()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_free_mode_flags_nothing():
    c = _cell(note=R.midi_to_note(60), inst=1, vol=8, fx=R.FX_VIBRATO, param=0x4C)
    assert TM.lint_cell(c, "free", "pulse25") == []
    assert TM.cell_ok(c, "free", "pulse25")


def test_mcs_mode_drops_effects_volume_and_waveform():
    c = _cell(note=R.midi_to_note(60), inst=1, vol=8, fx=R.FX_VIBRATO, param=0x4C)
    issues = TM.lint_cell(c, "mcs", "pulse25")
    assert "effect dropped" in issues
    assert "volume ignored" in issues
    assert "waveform -> square" in issues
    # a plain square note with no effect is clean in MCS
    assert TM.cell_ok(_cell(note=R.midi_to_note(60), inst=1), "mcs", "square")
    # structural effects (speed / break) are always fine
    assert TM.cell_ok(_cell(fx=R.FX_SPEED, param=6), "mcs")
    assert TM.cell_ok(_cell(fx=R.FX_BREAK, param=0), "mcs")


def test_sb_keeps_waveform_and_volume():
    c = _cell(note=R.midi_to_note(60), inst=1, vol=8, fx=R.FX_VIBRATO, param=0x40)
    assert TM.lint_cell(c, "sb", "pulse25") == []      # DAC/FM keeps it all


def test_4voice_ignores_volume_but_keeps_effects():
    c = _cell(note=R.midi_to_note(60), inst=1, vol=8, fx=R.FX_ARP, param=0x37)
    issues = TM.lint_cell(c, "4voice", "square")
    assert issues == ["volume ignored"]               # 1-bit: no volume


def test_tandy_keeps_volume():
    c = _cell(note=R.midi_to_note(60), inst=1, vol=8)
    assert TM.lint_cell(c, "tandy", "square") == []    # SN has 4-bit attenuation


def test_tempo_snap_only_for_mcs():
    assert TM.tempo_is_snapped("mcs")
    for m in ("free", "tandy", "4voice", "1voice", "sb"):
        assert not TM.tempo_is_snapped(m)
