"""The 1.0 universal-tracker architecture: model nuances, the universal synth,
retrack reduction, and the DOS wavetable engines."""

import numpy as np
import pytest

from mcs_convert import audio as A
from mcs_convert import dosplayer as D
from mcs_convert.model import NoteEvent, Song, Track
from mcs_convert.retrack import TARGET_WAVEFORMS, retrack


def _universal_song() -> Song:
    """3 tone tracks with NES nuances + a noise track + a drum track."""
    s = Song(title="u", source="test")
    lead = Track(name="Pulse 1", chip="nes-pulse", waveform="pulse12")
    lead.add(NoteEvent(0, 4, 72, velocity=90, waveform="pulse12",
                       effects={"duty": 0}))
    lead.add(NoteEvent(4, 4, 76, velocity=60, waveform="pulse25",
                       effects={"duty": 1}))
    s.add_track(lead)
    p2 = Track(name="Pulse 2", chip="nes-pulse", waveform="pulse50")
    p2.add(NoteEvent(0, 8, 64))
    s.add_track(p2)
    tri = Track(name="Triangle", chip="nes-triangle", waveform="nestri")
    tri.add(NoteEvent(0, 8, 48))
    s.add_track(tri)
    nz = Track(name="Noise", kind="noise", chip="nes-noise", waveform="noise")
    nz.add(NoteEvent(0, 1, 84, effects={"nesperiod": 3}))    # bright
    nz.add(NoteEvent(4, 1, 57, effects={"nesperiod": 12}))   # dark
    s.add_track(nz)
    drum = Track(name="DPCM", kind="drum", chip="nes-dpcm")
    drum.add(NoteEvent(6, 1, 48, percussive=True))
    s.add_track(drum)
    return s


def test_model_carries_every_nuance():
    s = _universal_song()
    assert len(s.tracks) == 5                        # more than 4 tracks: fine
    n = s.tracks[0].notes[0]
    assert n.waveform == "pulse12" and n.velocity == 90 and n.effects["duty"] == 0
    assert s.tracks[3].kind == "noise"


def test_universal_render_is_per_track():
    s = _universal_song()
    master, voices, sr = A.render_song(s, waveform="auto")
    assert len(voices) == 5                          # one buffer PER TRACK
    assert np.any(master)
    assert np.any(voices[3])                         # the noise voice sounds


def test_retrack_mcs_reduces_to_four_square_voices_plus_clicks():
    out = retrack(_universal_song(), "mcs")
    tone = [t for t in out.tracks if t.kind == "tone"]
    assert len(tone) <= 4 and all(t.waveform == "square" for t in tone)
    drums = [t for t in out.tracks if t.kind == "drum"]
    assert drums and all(n.percussive for n in drums[0].notes)
    # bright noise (midi 84) -> hi-hat E7 100; dark (57) -> low bass B2 47;
    # the DPCM hit (percussive midi 48, dark) joins as low bass too
    pitches = sorted(n.midi_note for n in drums[0].notes)
    assert pitches == [47, 47, 100]


def test_retrack_sb_keeps_waveforms_but_speaker_targets_do_not():
    sb = retrack(_universal_song(), "sb")
    assert any(t.waveform.startswith("pulse") or t.waveform == "nestri"
               for t in sb.tracks if t.kind == "tone")
    spk = retrack(_universal_song(), "1voice")
    assert all(t.waveform == "square" for t in spk.tracks)
    assert len(spk.tracks) == 1                      # top line only
    with pytest.raises(ValueError):
        retrack(_universal_song(), "gameboy")


def test_retrack_percussion_is_an_output_decision():
    s = _universal_song()
    # clicks (default): drums voiced on the percussion path
    clicks = retrack(s, "mcs", percussion="clicks")
    assert any(t.kind == "drum" for t in clicks.tracks)
    # drop: no percussion anywhere
    dropped = retrack(s, "mcs", percussion="drop")
    assert not any(t.kind == "drum" for t in dropped.tracks)
    tandy_dropped = retrack(s, "tandy", percussion="drop")
    assert not any(t.notes for t in tandy_dropped.tracks if t.kind == "noise")
    # pitched: the noise/drum notes play at their written pitches as tones
    pitched = retrack(s, "mcs", percussion="pitched")
    assert not any(t.kind == "drum" for t in pitched.tracks)
    tone_midis = {n.midi_note for t in pitched.tracks for n in t.notes}
    assert 84 in tone_midis                          # the bright noise note's pitch
    with pytest.raises(ValueError):
        retrack(s, "mcs", percussion="bogus")


def test_pt3_mark_mode_and_drumbright_wins_over_pitch():
    # a marked drum carries the importer's own bright/dark verdict, which beats
    # the pitch heuristic: a LOW-pitched hit marked bright still maps to hi-hat
    s = Song(title="t", source="t")
    tr = Track(name="AY A", chip="ay-tone")
    tr.add(NoteEvent(0, 4, 60))                      # a melody note
    tr.add(NoteEvent(4, 1, 50, percussive=True,      # low pitch, but marked BRIGHT
                     effects={"drumbright": 1}))
    s.add_track(tr)
    out = retrack(s, "mcs", drum_sound="auto")
    drums_t = [t for t in out.tracks if t.kind == "drum"][0]
    assert [n.midi_note for n in drums_t.notes] == [100]   # hi-hat, not low bass
    # and the melody note stayed melodic
    assert any(n.midi_note == 60 for t in out.tracks for n in t.notes)


def test_retrack_mcs_voice_counts_and_drop_noise():
    s = _universal_song()
    # 3 voices (Tandy/PCjr): 2 tone voices + the drum voice
    tandy3 = retrack(s, "mcs", voices=3)
    assert sum(1 for t in tandy3.tracks if t.kind == "tone") == 2
    assert any(t.kind == "drum" for t in tandy3.tracks)
    # single voice: top line only, no drums
    solo = retrack(s, "mcs", voices=1)
    assert len(solo.tracks) == 1 and solo.tracks[0].kind == "tone"
    # drop_noise kills the noise CHANNEL but keeps other drums (the DPCM hit)
    nonoise = retrack(s, "mcs", drop_noise=True)
    drums_t = [t for t in nonoise.tracks if t.kind == "drum"]
    assert drums_t and len(drums_t[0].notes) == 1     # DPCM only, noise gone
    # ...and works for the noise-native targets too
    tandy = retrack(s, "tandy", drop_noise=True)
    nz = [t for t in tandy.tracks if t.kind == "noise"][0]
    assert len(nz.notes) == 1                         # DPCM only
    with pytest.raises(ValueError):
        retrack(s, "tandy", voices=3)                 # mcs-only option
    with pytest.raises(ValueError):
        retrack(s, "mcs", voices=2)                   # 1/3/4 only


def test_target_waveform_matrix():
    assert TARGET_WAVEFORMS["mcs"] == ("square",)
    assert "sine" in TARGET_WAVEFORMS["sb"] and "nestri" in TARGET_WAVEFORMS["sb"]
    assert "triangle" in TARGET_WAVEFORMS["4voice"]  # via high-rate PWM modeling


def test_wave_tables():
    sq = D._wave_table("square", 30, signed=True)
    assert len(sq) == 256 and sq[0] == 30 and sq[200] == (256 - 30)  # +30 / -30
    tri = D._wave_table("triangle", 20, signed=False)
    assert max(tri) == 40 and min(tri) == 0          # unsigned 0..2*amp
    p12 = D._wave_table("pulse12", 30, signed=True)
    assert sum(1 for b in p12 if b == 30) == 32      # 12.5% duty high


def test_sb_wavetable_and_spk_pwm_builds():
    s = _universal_song()
    # EVERY SoundBlaster build mixes through the volume-scaled wavetable bank --
    # a voice contributes a real amplitude at its note's volume, not a sign bit
    plain = D.build_com(s, "4voice", 0x80, sb=True, mix_rate=22000)
    assert len(plain) > 256 * D._SB_LEVELS           # the bank is baked in
    sine = D.build_com(s, "4voice", 0x80, sb=True, sb_wave="sine", mix_rate=22000)
    native = D.build_com(s, "4voice", 0x80, sb=True, sb_wave="native",
                         mix_rate=22000)
    assert len(sine) == len(plain) == len(native)    # same engine, different bank
    assert native == plain                           # the default IS the song's own
    assert sine != plain                             # ...and a forced bank differs
    # quieter notes really are quieter: level 3 is ~3/7 of full amplitude
    bank = D._sb_wave_bank("square")
    amp = lambda lv: max(b - 256 if b > 127 else b for b in bank[lv * 256:(lv + 1) * 256])
    assert amp(0) == 0 and amp(7) == D._SB_AMP and 0 < amp(3) < amp(7)
    pwm = D.build_com(s, "4voice", 0x80, spk_wave="sine", mix_rate=24000)
    assert len(pwm) > 700
    with pytest.raises(ValueError):                  # PWM modeling needs a high carrier
        D.build_com(s, "4voice", 0x80, spk_wave="sine", mix_rate=4000)
    with pytest.raises(ValueError):                  # speaker-only feature
        D.build_com(s, "4voice", 0x80, spk_wave="sine", sb=True, mix_rate=24000)


def test_dosplayer_understands_noise_tracks():
    s = _universal_song()
    per, perc = D._split_notes(s)
    assert len(per) == 3                             # the 3 tone tracks
    assert len(perc) == 3                            # 2 noise hits + 1 DPCM hit
    com = D.build_com(s, "tandy", 0x80)
    assert len(com) > 100                            # noise routed to the SN76489


def test_nes_waveform_oscillators():
    ph = np.linspace(0, 1, 1024, endpoint=False)
    for wf, frac in (("pulse12", 0.125), ("pulse25", 0.25), ("pulse75", 0.75)):
        w = A._wave(ph, wf)
        assert abs(float((w > 0).mean()) - frac) < 0.01
    assert len(np.unique(np.round(A._wave(ph, "nestri"), 4))) == 16


def test_noise_track_renders_as_percussive_hits_not_a_wash():
    # A drum hit is an ATTACK. Rendering each note for its full WRITTEN length
    # with a flat envelope turned a dense hi-hat line into continuous static
    # (SMB: sounding 32% of the song against the NES reference's 8%), which is
    # why the drums were right in the true-hardware render but wrong everywhere
    # downstream. Hits are now decay-shaped and capped to a hit's length.
    step = 0.0735                                    # a 32nd at ~102 BPM: 73 ms
    hits = [(i * 4, 1, 84) for i in range(40)]       # bright hits, 1 tick each
    buf = A._render_noise_track(hits, 22050, step, 0.3)
    duty = float((np.abs(buf) > 0.02).mean())
    assert duty < 0.20, f"noise is a wash ({duty:.0%} sounding)"
    # the burst is far shorter than the note's written length...
    one = A._render_noise_track([(0, 1, 84)], 22050, step, 0.3)
    sounding = int((np.abs(one) > 0.02).sum())
    assert sounding < int(0.5 * step * 22050)
    # ...and decays rather than sitting flat
    head = float(np.abs(one[:len(one) // 4]).mean())
    tail = float(np.abs(one[-len(one) // 4:]).mean())
    assert head > tail * 2, "hit does not decay"
    # dark hits ring longer than bright ones (kick body vs hat tick)
    dark = A._render_noise_track([(0, 8, 40)], 22050, step, 0.3)
    bright = A._render_noise_track([(0, 8, 90)], 22050, step, 0.3)
    assert (np.abs(dark) > 0.02).sum() > (np.abs(bright) > 0.02).sum()


def test_scope_traces_follow_the_sounded_waveform():
    # The scopes drew a two-level square regardless of what the build sounded,
    # so a sine and a 12.5% pulse looked identical. Each channel now runs a
    # phase accumulator against a baked SHAPE table, so the trace has the real
    # waveform's contour.
    sq = D._wave_shape("square", D._TAMP)
    assert [b - 256 if b > 127 else b for b in sq] == [-2] * 8 + [2] * 8
    sine = [b - 256 if b > 127 else b for b in D._wave_shape("sine", D._TAMP)]
    assert sine != [-2] * 8 + [2] * 8 and min(sine) == -2 and max(sine) == 2
    assert 0 in sine                                 # ...and passes through centre
    # duty cycles are visible: pulse12 sits high for 2 of 16 columns, pulse25 for 4
    for wf, high in (("pulse12", 2), ("pulse25", 4), ("pulse50", 8)):
        shape = [b - 256 if b > 127 else b for b in D._wave_shape(wf, D._TAMP)]
        assert sum(1 for v in shape if v < 0) == high
    # the shape a build bakes is the one it actually sounds
    s = _universal_song()
    kw = dict(mix_rate=22000, text_scope=5)
    sb_sine = D.build_com(s, "4voice", 0x80, sb=True, sb_wave="sine", **kw)
    sb_sq = D.build_com(s, "4voice", 0x80, sb=True, sb_wave="square", **kw)
    assert bytes(D._wave_shape("sine", D._TAMP)) in sb_sine
    assert bytes(D._wave_shape("sine", D._TAMP)) not in sb_sq
    assert bytes(sq) in sb_sq
    # the speaker's PWM-modelled waveform reaches its scope too...
    pwm = D.build_com(s, "4voice", 0x80, spk_wave="sine", mix_rate=24000,
                      text_scope=5)
    assert bytes(D._wave_shape("sine", D._TAMP)) in pwm
    # ...while Tandy/PIT hardware really does sound squares
    tandy = D.build_com(s, "tandy", 0x80, text_scope=5)
    assert bytes(sq) in tandy
    # the graphics scope gets the same contour at scanline amplitude
    gfx = D.build_com(s, "tandy", 0x80, scope=True)
    assert bytes(D._wave_shape("square", D._GAMP)) in gfx


def test_text_scope_resolves_sub_row_heights_with_caps():
    # 80x25 text gives a 5-row band, so a two-level square wasted it. The block
    # scope now measures the wave in QUARTER rows and caps the column with a
    # partial glyph, resolving ~17 heights in the same 5 rows.
    q = [b - 256 if b > 127 else b for b in D._wave_shape("sine", D._TAMP * 4)]
    assert max(q) == D._TAMP * 4 and min(q) == -D._TAMP * 4
    heights = {(abs(v) >> 2, abs(v) & 3) for v in q}       # (full rows, leftover)
    assert len(heights) > 2                                # more than hi/lo
    assert any(rem for _rows, rem in heights)              # partial cells occur
    # a SQUARE is always a whole number of rows, so it draws no caps at all --
    # square builds look exactly as they always did
    sq = [b - 256 if b > 127 else b for b in D._wave_shape("square", D._TAMP * 4)]
    assert {abs(v) & 3 for v in sq} == {0}
    # the cap glyphs are the half blocks (oriented by travel) + shading
    s = _universal_song()
    com = D.build_com(s, "4voice", 0x80, sb=True, sb_wave="sine",
                      mix_rate=22000, text_scope=1)
    for glyph in (D._CAP_QUARTER, D._CAP_THREE, D._CAP_HALF_UP, D._CAP_HALF_DOWN):
        assert bytes([0xB0, glyph]) in com                 # mov al,<glyph>


def test_graphics_preview_is_a_native_framebuffer():
    # The graphics screens are rendered into a REAL 320x200 palette-indexed
    # framebuffer with the engine's own constants and blitted at 2x, so the
    # preview is pixel-for-pixel what DOS shows rather than a vector sketch.
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:                              # headless CI
        pytest.skip("no display")
    root.withdraw()
    from mcs_convert.gui import viz
    win = viz.DosVizWindow(root, "Tandy graphics", ["P1", "P2", "Tr", "Nz"])
    try:
        # the canvas IS the DOS screen: 320x200 at 2x
        assert (win.NATIVE_W, win.NATIVE_H, win.ZOOM) == (320, 200, 2)
        assert int(win.canvas["width"]) == 640 and int(win.canvas["height"]) == 400

        def pixels(style):
            win.set_style(style)
            win.draw([0.6, 0.5, 0.7, 0.4], [0.3] * 18, [0.25, 0.4, 0.6, 0.5], 2.0)
            # one blitted image, not a pile of vector items
            assert len(win.canvas.find_all()) == 1
            img = win._img
            assert (img.width(), img.height()) == (640, 400)
            return {img.get(x, y) if isinstance(img.get(x, y), tuple)
                    else tuple(int(v) for v in str(img.get(x, y)).split())
                    for x in range(0, 640, 7) for y in range(0, 400, 5)}

        win.set_wave("sine")
        tandy, vga = pixels("Tandy graphics"), pixels("VGA 256")
        # mode 9 packs the dark palette entries; mode 13h indexes the bright ones
        assert (170, 0, 0) in tandy and (0, 0, 170) in tandy       # dark red/blue
        assert (255, 85, 85) in vga and (85, 255, 255) in vga      # bright red/cyan
        assert (255, 255, 85) in tandy and (255, 255, 85) in vga   # ch0 yellow both
        assert (255, 255, 255) in tandy                            # the frames
        assert tandy != vga
        # every style renders at native size for every waveform
        for wf in ("square", "sine", "triangle", "nestri", "pulse12"):
            win.set_wave(wf)
            for st in viz.DOS_STYLES:
                win.set_style(st)
                win.draw([0.6] * 4, [0.3] * 18, [0.3] * 4, 1.0)
        # the text screens use the true VGA cell grid (80x25 of 8x16 = 640x400)
        assert win.CELL_W * 80 == 640 and win.CELL_H * 25 == 400
        win.set_wave("sine")
        win.set_style("block scopes (text 1)")
        win.draw([0.9, 0.9, 0.9, 0.0], [0] * 18, [0.5] * 4, 0.0)
        glyphs = {win.canvas.itemcget(i, "text") for i in win.canvas.find_all()
                  if win.canvas.type(i) == "text"}
        assert "█" in glyphs and glyphs & {"▄", "▀", "░", "▓"}
    finally:
        root.destroy()


def test_static_poster_preview_shows_the_baked_framebuffer():
    # The poster preview unpacks the ACTUAL CGA framebuffer the .COM carries,
    # so it is the very image DOS displays -- not a redrawing of the song.
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    from mcs_convert.gui import viz
    song = _universal_song()
    win = viz.DosVizWindow(root, "static poster")
    try:
        win.set_song(song, 0.075)
        win.draw([0.5] * 4, [0.3] * 18, [0.3] * 4, 1.0)
        assert len(win.canvas.find_all()) == 1        # one blit
        assert win._poster == D._render_static_poster(song)   # the real bytes
        assert len(win._poster) == 0x4000             # a full CGA framebuffer
        win.draw([0.5] * 4, [0.3] * 18, [0.3] * 4, 2.0)
        assert win._poster_for is song                # cached, not re-rendered
    finally:
        root.destroy()


def test_silent_channel_clears_the_cap_state():
    # The sub-row cap is drawn from wrem/wrow, which _emit_wave_pick only sets
    # on its SOUNDING path. A silent channel used to fall through with the
    # PREVIOUS channel's leftover still in wrem, so the cap painted a stray
    # glyph into that channel's band -- a character appearing and vanishing as
    # voices came in and out. The silent path must zero wrem.
    cap = {}
    real = D._Asm.resolve

    def capture(self):
        out = real(self)
        cap["labels"], cap["com"] = dict(self.labels), out
        return out

    song = Song(title="t", source="t")
    tr = Track(name="a")
    tr.add(NoteEvent(0, 8, 72))
    song.add_track(tr)
    song.add_track(Track(name="silent"))             # never sounds
    D._Asm.resolve = capture
    try:
        D.build_com(song, "4voice", 0x80, sb=True, sb_wave="sine",
                    mix_rate=22000, text_scope=1)
    finally:
        D._Asm.resolve = real
    L, com = cap["labels"], cap["com"]
    import struct
    clear = b"\xC6\x06" + struct.pack("<H", L["wrem"]) + b"\x00"
    # one clear per tone channel's silent path (plus the sounding-path stores)
    assert com.count(clear) >= 3
    # and the block scope only ever caps INSIDE its own band: a cap is drawn at
    # centre -/+ (full+1), and a full 2-row swing leaves no remainder at all
    q = [b - 256 if b > 127 else b for b in D._wave_shape("sine", D._TAMP * 4)]
    for v in q:
        full, rem = abs(v) >> 2, abs(v) & 3
        if rem:
            assert full + 1 <= D._TAMP                # stays within the band


def test_sb_fm_puts_tones_on_the_opl2_and_noise_on_the_dac():
    # --sb-fm uses BOTH SoundBlaster outputs: the tone voices go to the OPL2 FM
    # synth (nine hardware channels that hold a note once written, costing the
    # CPU nothing per sample) while the noise/percussion stays on the DAC. That
    # also frees the timer ISR, which is what starved the visualizations.
    s = _universal_song()
    dac = D.build_com(s, "4voice", 0x80, sb=True, mix_rate=22000)
    fm = D.build_com(s, "4voice", 0x80, sb=True, sb_fm=True, mix_rate=22000)
    assert bytes([0xBA, 0x88, 0x03]) in fm            # mov dx,0x388 (OPL2)
    assert bytes([0xBA, 0x88, 0x03]) not in dac       # the DAC build has no OPL
    assert b"\xB0\xD1" in fm                          # ...but still resets the DSP
    assert bytes([0xB0, 0xBD, 0xEE]) in fm            # rhythm register 0xBD
    # Tone operators must be SUSTAINING (register 0x20 bit 5). A percussive
    # envelope runs attack->decay->release even while the key is held, so with
    # sustain level 0 and a fast release every note died the instant it
    # started -- that is why the FM build had no audio until the noise channel.
    assert bytes([0xB0, 0x21, 0xEE]) in fm            # EG-TYP | multiple 1
    # The timer fires once per SUB-TICK rather than per sample, because the OPL
    # holds its own notes: orders of magnitude fewer interrupts, which is what
    # leaves the CPU free to redraw the visualization.
    def isr_hz(com):
        i = com.index(bytes([0xB0, 0x36, 0xE6, 0x43])) + 4
        return D._PIT_HZ / (com[i + 1] | (com[i + 2] << 8))
    assert isr_hz(fm) < 400                           # a few hundred Hz
    assert isr_hz(dac) > 10000                        # the DAC must run at Fs
    assert isr_hz(dac) / isr_hz(fm) > 50
    # the OPL pitch encoding is accurate across the range
    for freq in (65.41, 261.63, 440.0, 1046.5, 4186.0):
        block, fnum = D._opl_fnum(freq)
        assert 0 <= block <= 7 and 1 <= fnum < 1024
        back = fnum * D._OPL_CLOCK / (1 << (20 - block))
        assert abs(back / freq - 1.0) < 0.001         # within 0.1%
    # the stream carries the packed OPL word for tone voices in FM builds
    word = D._opl_note_word(440.0)
    block, fnum = D._opl_fnum(440.0)
    assert word & 0xFF == fnum & 0xFF                 # F-number low byte
    assert (word >> 8) & 0x20                         # key-on bit
    assert ((word >> 8) >> 2) & 7 == block            # block
    fm_stream, _ = D._build_spk4_stream(s, 22000, True)
    dac_stream, _ = D._build_spk4_stream(s, 22000, False)
    assert fm_stream != dac_stream                    # different note encoding
    # --sb-fm is meaningless without --sb
    with pytest.raises(ValueError):
        D.build_com(s, "4voice", 0x80, sb_fm=True, mix_rate=22000)
    # every visualization still builds on the FM target
    for ts in range(9):
        D.build_com(s, "4voice", 0x80, sb=True, sb_fm=True,
                    mix_rate=22000, text_scope=ts)


def test_opl2_follows_the_adlib_programming_rules():
    """The OPL2 register conventions, per the AdLib/Bochs hardware notes.

    Verified sources: writes need 6 status reads after the address and 35 after
    the data; key-on is bit 5 of 0xB0-0xB8, sharing that byte with block and the
    F-number high bits, so 0xA0 (F-number low) must be written FIRST; rhythm
    mode is 0xBD bit 5 and REQUIRES the key-on bits of channels 6/7/8 to stay
    clear; and playback is driven at a tempo-derived tick rate, not a fixed
    18.2 Hz.
    """
    s = _universal_song()
    com = D.build_com(s, "4voice", 0x80, sb=True, sb_fm=True)

    # -- the mandated settle delays ------------------------------------------
    assert bytes([0xB9, 0x06, 0x00]) in com          # mov cx,6  after an address
    assert bytes([0xB9, 0x23, 0x00]) in com          # mov cx,35 after data

    # -- rhythm mode needs channels 6/7/8 NOT keyed on -----------------------
    for hz in (D._OPL_BD_HZ, D._OPL_HH_HZ):
        block, fnum = D._opl_fnum(hz)
        assert not ((block << 2) | (fnum >> 8)) & 0x20      # key-on bit clear
    events = D._spk4_events(s, 22000, fm=True)
    melodic = {v for recs in events.values() for (v, _w, _z, _l) in recs if v < 3}
    assert melodic <= {0, 1, 2}                      # never channels 6/7/8

    # -- a drum only re-strikes on a CLEAR -> SET transition of its 0xBD bit --
    drum = [(st, w) for st, recs in sorted(events.items())
            for (v, w, _z, _l) in recs if v == 3]
    strikes = [(st, w) for st, w in drum if w & 0x1F]
    assert strikes, "no drum strikes emitted"
    for st, w in strikes:                            # each is preceded by a re-arm
        rearm = [x for (t, x) in drum if t == st and x == D._OPL_RHYTHM]
        assert rearm, f"strike at {st} has no clearing write"
        assert w & D._OPL_RHYTHM                     # rhythm stays enabled

    # -- a clean, loud voice: additive connection with no feedback ------------
    assert bytes([0xB0, 0x01, 0xEE]) in com          # 0xC0 = feedback 0 | additive

    # -- the tick rate is derived from the TEMPO, as ROL players do -----------
    def isr_hz(c):
        i = c.index(bytes([0xB0, 0x36, 0xE6, 0x43])) + 4
        return D._PIT_HZ / (c[i + 1] | (c[i + 2] << 8))
    slow = isr_hz(D.build_com(s, "4voice", 0x77, sb=True, sb_fm=True))
    fast = isr_hz(D.build_com(s, "4voice", 0x92, sb=True, sb_fm=True))
    assert slow != fast                              # follows the tempo
    assert abs(slow - 18.2) > 1                      # not a fixed 18.2 Hz tick


def test_graphics_scope_draws_smooth_full_resolution_waveforms():
    # The graphics scopes (mode 9 / VGA 13h) have 20 scanlines of amplitude, so
    # a 16-step phase table drew curves as a coarse staircase -- a triangle
    # jumped ~6 scanlines a step. They now sample a 256-entry table by the FULL
    # phase high byte, so the trace steps at most one scanline at a time.
    tri16 = [b - 256 if b > 127 else b for b in D._wave_shape("triangle", D._GAMP, 16)]
    tri256 = [b - 256 if b > 127 else b for b in D._wave_shape("triangle", D._GAMP, 256)]
    jump = lambda t: max(abs(t[i] - t[i - 1]) for i in range(1, len(t)))
    assert jump(tri16) >= 5                           # the old staircase
    assert jump(tri256) <= 1                          # a smooth curve now
    assert len(set(tri256)) > 3 * len(set(tri16))     # far more distinct heights
    sine = [b - 256 if b > 127 else b for b in D._wave_shape("sine", D._GAMP, 256)]
    assert jump(sine) <= 1 and min(sine) == -D._GAMP and max(sine) == D._GAMP

    # the graphics channel pick indexes the FULL byte (no 4-bit shift): the
    # 8-byte 'shr al,1' run that quantised the phase must be gone from the pick.
    song = _universal_song()
    gfx = D.build_com(song, "tandy", 0x80, text_scope=8)   # VGA mode 13h
    assert b"\xD0\xE8\xD0\xE8\xD0\xE8\xD0\xE8" not in gfx
    # ...while a TEXT scope, whose band is only a couple of rows, still quantises
    txt = D.build_com(song, "tandy", 0x80, text_scope=2)
    assert b"\xD0\xE8\xD0\xE8\xD0\xE8\xD0\xE8" in txt

    # the noise channel now carries its own previous-y slot (prevy has a 4th
    # word) so it draws as a connected jagged trace, not centre-anchored spikes
    cap = {}
    real = D._Asm.resolve

    def capture(self):
        out = real(self)
        cap["labels"] = dict(self.labels)
        return out

    D._Asm.resolve = capture
    try:
        D.build_com(song, "tandy", 0x80, scope=True)
    finally:
        D._Asm.resolve = real
    assert "prevy" in cap["labels"]                   # the noise connects prev->cur
