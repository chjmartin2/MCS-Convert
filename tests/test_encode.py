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


def test_percussion_never_reaches_the_pitched_staff(tmp_path):
    # A dense percussion track (noise/DPCM drum hits) must NOT be written onto the
    # grand staff. It used to land as low bass notes and flood the 32-entry
    # measure buffer, dropping real melody/bass (Dr. Wily lost ~40% of its notes).
    song = Song(title="drums")
    mel = Track(name="Treble")
    for i in range(8):
        mel.add(NoteEvent(start_tick=i * 4, duration_ticks=4, midi_note=72 + i % 4))
    song.add_track(mel)
    drums = Track(name="Noise")
    for i in range(200):                              # a hit on every tick
        drums.add(NoteEvent(start_tick=i, duration_ticks=1, midi_note=55,
                            percussive=True))
    song.add_track(drums)
    got = _roundtrip(song, tmp_path, cap=True)
    # every melody note survives; no drum pitch (55 = G3) contaminates the staff
    assert _sounding(got) == sorted(_note_events(mel.notes))


def test_fit_meter_keeps_more_notes_on_dense_input(tmp_path):
    # A bar too dense for 4/4's 32-entry buffer must lose fewer notes when
    # fit_meter drops to a shorter meter (more measures = more total buffer),
    # and the result must still validate clean.
    from mcs_convert.mcs.validate import validate
    song = Song(title="dense")
    tr = Track(name="T")
    for i in range(60):                               # 60 sixteenths over 4 bars
        tr.add(NoteEvent(start_tick=i * 2, duration_ticks=2, midi_note=60 + i % 12))
    song.add_track(tr)
    fixed = _roundtrip_bytes(song, bar_ticks=32, cap=True)
    auto = _roundtrip_bytes(song, cap=True, fit_meter=True)
    assert _count(auto) >= _count(fixed)
    assert not [i for i in validate(encode_song(song, cap=True, fit_meter=True))
                if i.severity == "corrupt"]


def test_fit_meter_keeps_natural_meter_when_it_fits(tmp_path):
    # A sparse song fits 4/4 with zero drops, so fit_meter must NOT needlessly
    # shorten it: 15 measures of 4/4, not 30 of 2/4.
    from mcs_convert.mcs.reader import parse_records, split_staves
    song = _mk([(i * 8, 8, 72 + i % 5) for i in range(60)])   # one note per beat
    data = encode_song(song, cap=True, fit_meter=True)
    auto_meas = max(len(st) for st in split_staves(parse_records(data)))
    half_meas = max(len(st) for st in
                    split_staves(parse_records(encode_song(song, bar_ticks=16,
                                                            cap=True))))
    assert data[0x05] | (data[0x06] << 8) == 0x3AF9 + 3 * 1     # 4/4, not shortened
    assert auto_meas < half_meas                               # didn't drop to 2/4


def test_balance_keeps_more_of_a_low_dense_song(tmp_path):
    # A dense song crammed into one register overflows a pitch-split (everything
    # on the bass staff) but fits when balance spreads it across both staves.
    # Still a 2-staff file, still valid.
    from mcs_convert.mcs.reader import parse_records, split_staves
    from mcs_convert.mcs.validate import validate
    song = Song(title="low")
    for base in (55, 52, 48):                         # all low: bass register
        tr = Track(name=f"v{base}")
        for i in range(64):
            tr.add(NoteEvent(start_tick=i, duration_ticks=1, midi_note=base + i % 5))
        song.add_track(tr)
    data = encode_song(song, cap=True, balance=True)
    assert len(split_staves(parse_records(data))) == 2          # still a grand staff
    assert not [i for i in validate(data) if i.severity == "corrupt"]
    assert _count(_roundtrip_bytes(song, cap=True, balance=True)) >= \
        _count(_roundtrip_bytes(song, cap=True))                # beats pitch-split


def test_balance_uses_two_bass_staves_for_a_low_song():
    # A song entirely in the bass window must become two BASS-clef staves (so
    # notes can balance with no octave-folding), not a treble-over-bass split.
    from mcs_convert.mcs.reader import parse_records, split_staves, _read_staff, CLEF_BASS
    song = _mk([(i * 4, 4, 48 + i % 12) for i in range(32)])    # B2..bass only
    data = encode_song(song, cap=True, balance=True)
    staves = split_staves(parse_records(data))
    assert len(staves) == 2
    assert all(_read_staff(st).clef == CLEF_BASS for st in staves)


def test_multi_staff_header_sizes_are_consistent():
    # 0x09 = staff-1 size, 0x0B = everything after it up to the final separator;
    # the two must bracket the body so real MCS locates staves 3+ (writing only
    # staff-2's size is what made an earlier multi-staff file fail to load).
    from mcs_convert.mcs.writer import build_file, make_entry
    staff = [[make_entry(0x06, 16, 14)], [make_entry(0x03, 16, 4)]]   # clef + a note
    data = build_file([staff, staff, staff])          # three staves
    s1 = data[0x09] | (data[0x0A] << 8)
    s2 = data[0x0B] | (data[0x0C] << 8)
    total = data[0x0D] | (data[0x0E] << 8)
    body = total - 0x10                                # header (0x0F) + 1 pad byte
    assert s2 == body - s1 - 8                         # the two 4-byte end separators


def _count(song):
    return sum(len([n for n in t.notes if not n.is_rest]) for t in song.tracks)


def _roundtrip_bytes(song, **kw):
    from mcs_convert.mcs.reader import parse_bytes
    return parse_bytes(encode_song(song, **kw))


def test_parse_bytes_matches_file_parse(tmp_path):
    # parse_bytes decodes an in-memory file identically to parse() off disk — the
    # dialog preview auditions these exact bytes so it matches the exported file
    # (previewing the raw source instead sustained/sounded notes the cap drops).
    from mcs_convert.mcs.reader import parse_bytes
    src = [(0, 8, 72), (8, 8, 74), (16, 16, 79), (0, 32, 48)]
    data = encode_song(_mk(src), cap=True)
    (tmp_path / "b.mcs").write_bytes(data)
    from_file = parse_mcs(str(tmp_path / "b.mcs"))
    from_bytes = parse_bytes(data)
    assert ([_note_events(t.notes) for t in from_bytes.tracks]
            == [_note_events(t.notes) for t in from_file.tracks])


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
    # the wood-block alternative: one humble D4 tick per hit
    block, _ = parse_pt3(_build_pt3(drum_sample=True), drum_sound="block")
    cb = {t.name: t for t in block.tracks}["AY C"]
    assert [(n.duration_ticks, n.midi_note) for n in cb.notes] == [(1, 62), (1, 62)]
    assert all(n.percussive for n in cb.notes)
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


def test_decay_shaping_truncates_to_audible_length():
    # A pluck sample (volume f/c/8 then silence, looping on a silent frame) is
    # audibly 3 frames long: with shaping on, the held C-3 becomes a short
    # note; off (the default) it sustains to the next event as before.
    from mcs_convert.pt3 import _sample_audible_ticks
    mod = bytearray(_build_pt3())                # B holds C-3 for 8 rows
    sam = len(mod)
    struct.pack_into("<H", mod, 0x69 + 3 * 2, sam)          # sample 3
    mod += bytes([3, 4,                                     # loop 3, len 4
                  0x00, 0x8F, 0, 0, 0x00, 0x8C, 0, 0,       # tone-only (noise
                  0x00, 0x88, 0, 0, 0x00, 0x80, 0, 0])      # off): f, c, 8, 0
    # channel B selects sample 3 before its note ($D3)
    b_stream = bytes([0xB1, 8, 0xD3, 0x68, 0x00])
    old = bytes([0xB1, 8, 0x68, 0x00])
    idx = bytes(mod).index(old)
    mod = bytes(mod[:idx]) + b_stream + bytes(mod[idx + len(old):])
    # stream grew by 1 byte: shift the C-channel pattern pointer
    pat_table = struct.unpack_from("<H", mod, 0x67)[0]
    mod = bytearray(mod)
    for k in (2,):                                          # channel C pointer
        addr = struct.unpack_from("<H", mod, pat_table + k * 2)[0]
        struct.pack_into("<H", mod, pat_table + k * 2, addr + 1)
    struct.pack_into("<H", mod, 0x69 + 3 * 2, sam + 1)      # sample moved too
    mod = bytes(mod)

    ticks = row_ticks_and_tempo(3)[0]
    assert _sample_audible_ticks(mod, 3, ticks, 3) == 1     # ceil(3 * 1/3)
    plain, _ = parse_pt3(mod)
    shaped, _ = parse_pt3(mod, shape_durations=True)
    b_plain = {t.name: t for t in plain.tracks}["AY B"].notes[0]
    b_shaped = {t.name: t for t in shaped.tracks}["AY B"].notes[0]
    assert b_plain.duration_ticks == 8 * ticks              # sustains (default)
    assert b_shaped.duration_ticks == 1                     # pluck recovered


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
