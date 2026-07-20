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
