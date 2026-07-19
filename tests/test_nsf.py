"""End-to-end NSF importer tests on a synthetic module: a real 6502 play routine,
executed by our core, driving the APU model, segmented into a Song."""

import os
import struct

import pytest

from mcs_convert.nsf.extract import detect_base_unit, extract_song, fit_grid


def _build_nsf() -> bytes:
    """One-track NSF: INIT is a bare RTS; PLAY (on its first call) enables the
    channels, keys an A440 pulse with a finite length counter, and fires one
    noise hit. The length counter runs out, silence follows, the song ends."""
    hdr = bytearray(0x80)
    hdr[0:5] = b"NESM\x1a"
    hdr[5] = 1                                   # version
    hdr[6] = 1                                   # total songs
    hdr[7] = 1                                   # starting song
    struct.pack_into("<H", hdr, 0x08, 0x8000)    # load
    struct.pack_into("<H", hdr, 0x0A, 0x8000)    # init (RTS)
    struct.pack_into("<H", hdr, 0x0C, 0x8001)    # play
    hdr[0x0E:0x0E + 9] = b"TEST TUNE"
    struct.pack_into("<H", hdr, 0x6E, 16666)     # NTSC speed (us per frame)

    def sta(addr):                               # LDA #imm; STA abs pairs below
        return bytes([0x8D, addr & 0xFF, addr >> 8])

    body = bytes([0xA5, 0x00,                    # LDA $00   (already played?)
                  0xD0, 0x20,                    # BNE done  (+32)
                  0xE6, 0x00,                    # INC $00
                  0xA9, 0x0F]) + sta(0x4015) + \
        bytes([0xA9, 0x1F]) + sta(0x4000) + \
        bytes([0xA9, 0xFD]) + sta(0x4002) + \
        bytes([0xA9, 0x08]) + sta(0x4003) + \
        bytes([0xA9, 0x05]) + sta(0x400E) + \
        bytes([0xA9, 0x08]) + sta(0x400F) + \
        bytes([0x60])                            # done: RTS
    program = bytes([0x60]) + body               # $8000: RTS (init), $8001: play
    return bytes(hdr) + program


@pytest.fixture()
def nsf_path(tmp_path):
    p = tmp_path / "test.nsf"
    p.write_bytes(_build_nsf())
    return str(p)


def test_extracts_pitch_length_and_drums(nsf_path):
    song, byte0 = extract_song(nsf_path)
    by = {t.name: t for t in song.tracks}
    assert set(by) == {"Pulse 1", "Pulse 2", "Triangle", "Noise", "DPCM"}
    p1 = by["Pulse 1"].notes
    assert len(p1) == 1
    assert p1[0].midi_note == 69                 # period $FD -> ~440.4 Hz -> A4
    # universal-tracker nuances: duty index 1 ($4000=$1F -> bits 6-7 = 0) ...
    assert by["Pulse 1"].chip == "nes-pulse"
    assert p1[0].waveform in ("pulse12", "pulse25", "pulse50", "pulse75")
    assert "duty" in p1[0].effects
    assert by["Triangle"].waveform == "nestri"
    # length index 1 -> 254 half-frame clocks -> ~127 frames of sound
    ticks_per_frame = p1[0].duration_ticks / 127
    assert 0.15 <= ticks_per_frame <= 0.55       # fpt between 2 and 6
    # the noise channel is a first-class noise TRACK now (not clicks): period 5
    # (bright) -> midi 93 - 3*5 = 78, kind "noise", not percussive
    noise_t = by["Noise"]
    assert noise_t.kind == "noise" and noise_t.chip == "nes-noise"
    assert noise_t.notes and noise_t.notes[0].midi_note == 78
    assert noise_t.notes[0].effects.get("nesperiod") == 5
    assert not by["Pulse 2"].notes and not by["Triangle"].notes
    assert 0x77 <= byte0 <= 0x92


def test_percussion_drop_and_retrack_clicks(nsf_path):
    song, _ = extract_song(nsf_path, percussion="drop")
    assert not {t.name: t for t in song.tracks}["Noise"].notes
    # drum clicks are now an EXPORT-side reduction: retrack(song, "mcs") turns
    # the noise track into register-extreme click notes (the classic converter).
    from mcs_convert.retrack import retrack
    song, _ = extract_song(nsf_path)
    mcs = retrack(song, "mcs", drum_sound="block")
    drums_t = {t.name: t for t in mcs.tracks}["Drums"]
    assert [n.midi_note for n in drums_t.notes] == [62]   # single wood-block tick
    assert all(n.percussive for n in drums_t.notes)
    # "auto" splits bright/dark: period 5 is bright -> hi-hat E7 (100)
    auto = retrack(song, "mcs", drum_sound="auto")
    assert [n.midi_note for n in {t.name: t for t in auto.tracks}["Drums"].notes] == [100]
    # tandy keeps a native noise voice instead of clicks
    tandy = retrack(song, "tandy")
    nz = [t for t in tandy.tracks if t.kind == "noise"][0]
    assert nz.notes and nz.notes[0].midi_note == 100      # bright hit


def test_explicit_length_disables_detection(nsf_path):
    song, _ = extract_song(nsf_path, max_seconds=5.0, detect_end=False)
    total = max(n.end_tick for t in song.tracks for n in t.notes)
    assert total >= 1                            # ran the full 5s without ending early


def test_detect_base_unit_ignores_arpeggio_jitter():
    # most onsets sit on a 6-frame grid; a few carry a 1-frame arpeggio grace
    # note. The dominant gap is 6, and the sub-3-frame jitter is ignored.
    onsets = list(range(0, 600, 6))
    for beat in (12, 30, 48):                     # sparse grace notes
        onsets.append(beat + 1)
    assert detect_base_unit(sorted(onsets)) == 6


def test_base_unit_measures_within_channels_not_across():
    # Three voices all sit on a clean 10-frame grid but start on different frames.
    # MERGING them and diffing the sorted union manufactures phantom sub-grid gaps
    # (a voice at ...,20,30 next to one at ...,25,35 reads as a run of 5s) that can
    # out-vote the real 10 — exactly what put Zelda's overworld on a 4.5-frame tick,
    # a quarter of its onsets off the beat. Measured per channel, the unit is 10.
    chans = [[10 * k for k in range(30)],            # phase 0
             [5 + 10 * k for k in range(30)],        # phase 5 (half the unit)
             [20 + 10 * k for k in range(28)]]        # phase 0, enters later
    assert detect_base_unit(chans) == 10             # per-channel: the true grid
    merged = sorted(o for ch in chans for o in ch)
    assert detect_base_unit(merged) == 5             # merged fakes a 5-frame unit
    # and the fitted grid lands every onset on a tick (fpt = 10/2 = a 16th)
    fpt, _ = fit_grid(chans, 60.0)
    pts = sorted({o for ch in chans for o in ch})
    off = [abs((o - pts[0]) / fpt - round((o - pts[0]) / fpt)) for o in pts]
    assert abs(fpt - 5.0) < 0.01 and max(off) < 0.01


def test_grid_maps_base_unit_to_whole_ticks():
    # a 6-frame base unit must land on a whole number of ticks so 1:2:4:8 note
    # ratios survive; and the tick must sit in MCS's tempo range.
    onsets = list(range(0, 600, 6))
    fpt, byte0 = fit_grid(onsets, 60.0)
    assert 2.0 <= fpt <= 6.3 and 0x77 <= byte0 <= 0x92
    assert abs(6 / fpt - round(6 / fpt)) < 0.1    # 6 frames -> whole ticks


def test_grid_is_beat_aligned_and_optimizer_finds_it():
    # A perfectly on-grid melody (onsets every 9 frames from an offset) must
    # quantize so every onset lands on an exact tick — the base unit maps to a
    # whole tick count. The old bug quantized at the rounded tempo's frame-rate,
    # drifting onsets a tick off the beat.
    from mcs_convert.nsf.extract import fit_grid, optimize_grid
    onsets = [3 + 9 * k for k in range(40)]          # clean 9-frame beat, phase 3
    fpt, byte0 = fit_grid(onsets, 60.0)
    off = [abs((o - onsets[0]) / fpt - round((o - onsets[0]) / fpt)) for o in onsets]
    assert max(off) < 0.01                           # every beat on an exact tick
    assert abs(fpt - 4.5) < 0.01                     # 9-frame unit -> a 16th (2 ticks)
    # the exhaustive optimizer agrees, at a small speed nudge to a real MCS tempo
    ofpt, obyte0, err, speed = optimize_grid(onsets, 60.0)
    assert err < 0.01 and 0x77 <= obyte0 <= 0x92 and speed < 0.1


def test_silent_noise_keyon_is_not_a_drum_hit():
    """A $400F key-on with volume 0 (constant-volume mode, low nibble 0) is a
    note-OFF/mute, not an audible drum — counting it spawns phantom hits that
    turn SMB's 18/9 shuffle into a triplet buzz. Envelope mode (bit 4 clear)
    keys on at level 15, so it IS a hit."""
    from mcs_convert.nsf.apu import APUState
    apu = APUState()
    apu.write(0x4015, 0x08)                          # enable noise
    apu.write(0x400E, 0x03)                          # period index 3
    apu.write(0x400C, 0x1C)                          # constant volume, level 12
    apu.write(0x400F, 0x18)                          # audible key-on
    apu.write(0x400C, 0x10)                          # constant volume, level 0
    apu.write(0x400F, 0x18)                          # SILENT key-on -> ignored
    apu.write(0x400C, 0x03)                          # envelope mode (starts at 15)
    apu.write(0x400F, 0x18)                          # audible key-on
    assert [p for _, p in apu.noise_hits] == [3, 3]  # two hits, the mute skipped
