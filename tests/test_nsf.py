"""End-to-end NSF importer tests on a synthetic module: a real 6502 play routine,
executed by our core, driving the APU model, segmented into a Song."""

import os
import struct

import pytest

from mcs_convert.nsf.extract import extract_song, fit_grid


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
    # length index 1 -> 254 half-frame clocks -> ~127 frames of sound
    ticks_per_frame = p1[0].duration_ticks / 127
    assert 0.15 <= ticks_per_frame <= 0.55       # fpt between 2 and 6
    noise = by["Noise"].notes
    assert noise and all(n.percussive for n in noise)
    assert not by["Pulse 2"].notes and not by["Triangle"].notes
    assert 0x77 <= byte0 <= 0x92


def test_percussion_drop_and_block(nsf_path):
    song, _ = extract_song(nsf_path, percussion="drop")
    assert not {t.name: t for t in song.tracks}["Noise"].notes
    song, _ = extract_song(nsf_path, drum_sound="block")
    hits = {t.name: t for t in song.tracks}["Noise"].notes
    assert [n.midi_note for n in hits] == [62]   # single wood-block tick


def test_explicit_length_disables_detection(nsf_path):
    song, _ = extract_song(nsf_path, max_seconds=5.0, detect_end=False)
    total = max(n.end_tick for t in song.tracks for n in t.notes)
    assert total >= 1                            # ran the full 5s without ending early


def test_fit_grid_prefers_clean_onsets():
    # onsets on multiples of 4 frames: fpt=4 should beat fpt=3
    onsets = list(range(0, 400, 4))
    fpt, byte0 = fit_grid(onsets, 60.1)
    assert fpt == 4 and 0x77 <= byte0 <= 0x92
