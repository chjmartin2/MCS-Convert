"""Tests for the Song -> MCS encoder and the PT3 importer."""

import os
import struct

from mcs_convert.audio import _note_events
from mcs_convert.mcs.encode import encode_song
from mcs_convert.mcs.reader import parse as parse_mcs
from mcs_convert.model import NoteEvent, Song, Track
from mcs_convert.pt3 import parse_pt3, row_ticks_and_tempo


def _roundtrip(song, tmp_path, **kw):
    p = tmp_path / "enc.mcs"
    p.write_bytes(encode_song(song, **kw))
    return parse_mcs(str(p))


def _sounding(song):
    """Merged (start, midi, duration) multiset across all tracks (ties collapsed)."""
    out = []
    for tr in song.tracks:
        out += _note_events(tr.notes)
    return sorted(out)


def _mk(notes):
    s = Song(title="t")
    tr = Track(name="T")
    for start, dur, midi in notes:
        tr.add(NoteEvent(start_tick=start, duration_ticks=dur, midi_note=midi))
    s.add_track(tr)
    return s


def test_simple_melody_roundtrips(tmp_path):
    src = [(0, 8, 72), (8, 4, 76), (12, 4, 79), (16, 16, 72)]
    got = _roundtrip(_mk(src), tmp_path)
    assert _sounding(got) == sorted(src)


def test_gap_becomes_a_rest(tmp_path):
    # a note, silence, a note: the gap must be explicit rest time, not stretch
    src = [(0, 4, 72), (12, 4, 72)]
    got = _roundtrip(_mk(src), tmp_path)
    assert _sounding(got) == sorted(src)


def test_odd_duration_splits_into_tied_pieces(tmp_path):
    # 20 ticks isn't an MCS value: 16 + 4 tied, merged back on parse
    src = [(0, 20, 72), (20, 12, 74)]                 # 12 = dotted quarter
    got = _roundtrip(_mk(src), tmp_path)
    assert _sounding(got) == sorted(src)


def test_sustain_ties_across_the_barline(tmp_path):
    # a whole note starting mid-bar crosses into bar 2 as a tied continuation
    src = [(24, 32, 72)]
    got = _roundtrip(_mk(src), tmp_path)
    assert _sounding(got) == [(24, 32, 72)]


def test_chord_with_sustain_keeps_both_voices(tmp_path):
    # C sustains while E changes above it (slot advance = shorter member)
    src = [(0, 16, 60), (0, 8, 76), (8, 8, 77)]
    got = _roundtrip(_mk(src), tmp_path)
    assert _sounding(got) == sorted(src)


def test_out_of_range_notes_are_octave_shifted(tmp_path):
    got = _roundtrip(_mk([(0, 8, 24), (8, 8, 110)]), tmp_path)   # C1, D8
    snd = _sounding(got)
    assert [m for _, _, m in snd] == [48, 98]          # C3 (bass floor), D7
    assert all(47 <= m <= 100 for _, _, m in snd)


def test_maplerag_reencodes_losslessly(tmp_path):
    demo = os.path.join(os.path.dirname(__file__), "..", "demos", "MAPLERAG.MCS")
    orig = parse_mcs(demo)
    # feed the parsed song straight back through the encoder; compare as sets —
    # the encoder legitimately merges exact unisons (two tracks striking the
    # same pitch at the same tick become one voice)
    reenc = _roundtrip(orig, tmp_path, tempo_byte0=0x89)
    assert set(_sounding(reenc)) == set(_sounding(orig))


# ---- PT3 importer -----------------------------------------------------------------

def _build_pt3(delay=3, drums=False, drum_sample=False):
    """A minimal 1-pattern module: A plays C-4/E-4/G-4/off, B a held C-3, C is
    silent — or, with drums=True, C hammers one low note with noise commands;
    with drum_sample=True, C selects a noise-only sample (tone muted) and hits
    C-4 then C-1 — the sample-table percussion recipe."""
    hdr = bytearray(0xC9)
    hdr[0:13] = b"ProTracker 3."
    hdr[0x0D] = ord("6")
    hdr[0x1E:0x1E + 8] = b"TESTMOD\x00"
    hdr[0x42:0x42 + 5] = b"MCSC\x00"
    hdr[0x63] = 2                                  # frequency table (unused here)
    hdr[0x64] = delay
    hdr[0x65] = 2                                  # patterns + 1
    hdr[0x66] = 0
    order = bytes([0, 0xFF])                       # play pattern 0 once

    # channel streams (addresses filled after layout)
    ch_a = bytes([
        0xB1, 2,                                   # skip = 2 rows per line
        0x74,                                      # C-4  (note 0x24)
        0x78,                                      # E-4
        0x7B,                                      # G-4
        0xC0,                                      # off
        0x00,
    ])
    ch_b = bytes([
        0xB1, 8,
        0x68,                                      # C-3, held 8 rows (one line)
        0x00,
    ])
    if drum_sample:                                # sample 2 + noise, C-4 then C-1
        ch_c = bytes([0xB1, 2, 0xD2, 0x25, 0x74, 0x50, 0xC0, 0xD0, 0x00])
    elif drums:                                    # noise-set + repeated C-1 hits
        ch_c = bytes([0xB1, 2,
                      0x25, 0x50, 0x25, 0x50, 0x25, 0x50, 0x25, 0x50, 0x00])
    else:
        ch_c = bytes([0xB1, 8, 0xD0, 0x00])

    base = 0xC9 + len(order)
    pat_table = base
    a_addr = pat_table + 6
    b_addr = a_addr + len(ch_a)
    c_addr = b_addr + len(ch_b)
    sam_addr = c_addr + len(ch_c)
    struct.pack_into("<H", hdr, 0x67, pat_table)
    body = struct.pack("<HHH", a_addr, b_addr, c_addr) + ch_a + ch_b + ch_c
    if drum_sample:
        # sample 2: loop 0, one frame, byte1 = 0x1F (tone OFF via bit4, noise
        # ON via clear bit7, full amplitude)
        struct.pack_into("<H", hdr, 0x69 + 2 * 2, sam_addr)
        body += bytes([0, 1, 0x00, 0x1F, 0x00, 0x00])
    return bytes(hdr) + order + body


def test_pt3_note_extraction():
    song, byte0 = parse_pt3(_build_pt3(delay=3))
    assert song.title == "TESTMOD"
    by_name = {t.name: t for t in song.tracks}
    ticks, want_byte0 = row_ticks_and_tempo(3)
    assert byte0 == want_byte0
    a = [(n.start_tick, n.duration_ticks, n.midi_note) for n in by_name["AY A"].notes]
    # C-4 (0x74-0x50=0x24=36 -> midi 60), E-4, G-4, each 2 rows, then off
    assert a == [(0, 2 * ticks, 60), (2 * ticks, 2 * ticks, 64),
                 (4 * ticks, 2 * ticks, 67)]
    b = [(n.start_tick, n.duration_ticks, n.midi_note) for n in by_name["AY B"].notes]
    assert b == [(0, 8 * ticks, 48)]               # held C-3 to the pattern end


def test_pt3_converts_to_playable_mcs(tmp_path):
    song, byte0 = parse_pt3(_build_pt3())
    p = tmp_path / "pt3.mcs"
    p.write_bytes(encode_song(song, tempo_byte0=byte0))
    got = parse_mcs(str(p))
    assert _sounding(got) == _sounding(song)


def test_noise_commands_are_counted_per_channel():
    song, _ = parse_pt3(_build_pt3(drums=True))
    by_name = {t.name: t for t in song.tracks}
    assert by_name["AY C"].meta["noise_cmds"] == 4
    assert by_name["AY A"].meta["noise_cmds"] == 0


def test_channel_stats_flag_percussion():
    from mcs_convert.gui.player import channel_stats
    song, _ = parse_pt3(_build_pt3(drums=True))
    by_name = {t.name: t for t in song.tracks}
    drums = channel_stats(by_name["AY C"])
    melody = channel_stats(by_name["AY A"])
    assert drums["verdict"] == "percussion"        # noisy + one repeated pitch
    assert melody["verdict"] in ("melody", "bass")  # clean wandering line
    assert drums["score"] > melody["score"]


def test_noise_only_samples_become_clicks():
    # A channel playing a noise-only sample (tone mixer muted in the sample
    # table) is AY percussion; MCS has no noise, so each hit becomes a 1-tick
    # DISSONANT CLUSTER (G3+Ab3): beating squares read as roughness, not pitch
    # (single notes failed by ear — E7 ticks dominated, B2 thuds hummed).
    song, _ = parse_pt3(_build_pt3(drum_sample=True))
    c = {t.name: t for t in song.tracks}["AY C"]
    assert c.meta["drum_notes"] == 2
    hits = [(n.start_tick, n.duration_ticks, n.midi_note) for n in c.notes]
    assert hits[0:2] == [(0, 1, 55), (0, 1, 56)]
    assert hits[2][2] == 55 and hits[3][2] == 56
    assert all(n.percussive for n in c.notes)
    # the melodic channels are untouched
    a = {t.name: t for t in song.tracks}["AY A"]
    assert a.meta["drum_notes"] == 0 and a.notes[0].midi_note == 60


def test_percussion_modes():
    # clicks (default): 1-tick C3/E7 hits; pitched: written pitches, full
    # durations; drop: drum notes silenced, melodic channels untouched.
    mod = _build_pt3(drum_sample=True)
    clicks, _ = parse_pt3(mod)
    pitched, _ = parse_pt3(mod, percussion="pitched")
    dropped, _ = parse_pt3(mod, percussion="drop")

    def c_notes(song):
        by = {t.name: t for t in song.tracks}
        tr = by.get("AY C")
        return [(n.start_tick, n.duration_ticks, n.midi_note)
                for n in tr.notes] if tr else []

    ticks = clicks.tracks[0].notes[1].start_tick // 2
    assert c_notes(clicks) == [(0, 1, 55), (0, 1, 56),
                               (2 * ticks, 1, 55), (2 * ticks, 1, 56)]
    assert c_notes(pitched) == [(0, 2 * ticks, 60), (2 * ticks, 2 * ticks, 24)]
    assert c_notes(dropped) == []                  # drum channel silenced
    # melodic channels identical across all three modes
    for song in (pitched, dropped):
        assert [n.midi_note for n in song.tracks[0].notes] == \
               [n.midi_note for n in clicks.tracks[0].notes]
    # drum stats survive in every mode (the preview verdict still works)
    assert {t.name: t.meta["drum_notes"] for t in pitched.tracks}["AY C"] == 2
    # clicks are flagged percussive (octave-shift exempt); pitched notes aren't
    c = {t.name: t for t in clicks.tracks}["AY C"]
    assert all(n.percussive for n in c.notes)
    p = {t.name: t for t in pitched.tracks}["AY C"]
    assert not any(n.percussive for n in p.notes)


def test_drum_detection_is_noise_duty_cycle():
    # ALF's two lessons: a snare keeps noise on EVERY frame even with tone+
    # envelope also on (must be a drum), while a slap-bass has one noise
    # ATTACK frame on a pure-tone body (must stay melodic).
    from mcs_convert.pt3 import _sample_is_drum
    mod = bytearray(_build_pt3(drum_sample=True))
    addr = struct.unpack_from("<H", mod, 0x69 + 2 * 2)[0]
    # rewrite sample 2 as tone+noise+env, full amp, 3 frames  -> drum
    mod[addr:addr + 2 + 12] = bytes([0, 3] + [0x00, 0x0F, 0, 0] * 3)
    assert _sample_is_drum(bytes(mod), 2)
    # rewrite as noise attack frame + 3 tone-only frames      -> melodic
    mod[addr:addr + 2 + 16] = bytes([0, 4, 0x00, 0x0F, 0, 0]
                                    + [0x00, 0x8F, 0, 0] * 3)
    assert not _sample_is_drum(bytes(mod), 2)


def test_vortex_tracker_magic_is_accepted():
    # Vortex Tracker II writes its own signature over the same header layout
    # ("Vortex Tracker II 1.0 module: ..." — seen in the wild); only the first
    # 0x1E bytes differ from ProTracker's, and they're all cosmetic text.
    mod = bytearray(_build_pt3())
    vt2 = b"Vortex Tracker II 1.0 module: "
    mod[:len(vt2)] = vt2
    song, _ = parse_pt3(bytes(mod))
    assert sum(len(t.notes) for t in song.tracks) == 4


def test_row_tempo_mapping_stays_in_mcs_range():
    for delay in range(1, 16):
        ticks, byte0 = row_ticks_and_tempo(delay)
        assert ticks in (1, 2, 4, 8) and 0x77 <= byte0 <= 0x92
