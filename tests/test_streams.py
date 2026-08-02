"""FlatSong -> DOS streams / PERF chunks / .COM builds, and Song<->RCT
conversion. The streams must be byte-compatible with the proven engines."""

import pytest

from mcs_convert import dosplayer as D
from mcs_convert import rct as R
from mcs_convert.convert import rct_to_universal, song_to_rct
from mcs_convert.effects import flatten
from mcs_convert.model import NoteEvent, Song, Track
from mcs_convert.streams import (build_com, mono_stream, perf_chunks,
                                 spk4_stream, tandy_stream)


def _rct(**cells) -> R.RctSong:
    s = R.RctSong(speed=4, tempo_byte0=0x80)
    pat = R.RctPattern(rows=4)
    for key, fields in cells.items():
        row, ch = key.split("c")
        cell = pat.cell(int(row[1:]), int(ch))
        for k, v in fields.items():
            setattr(cell, k, v)
    s.patterns = {0: pat}
    s.order = [0]
    return s


def _note(midi, **kw):
    return dict(note=R.midi_to_note(midi), inst=1, **kw)


def test_spk4_stream_walks_like_the_engine_expects():
    s = _rct(r0c0=_note(69), r0c3=dict(note=R.midi_to_note(84)))   # A4 + noise
    stream, total = spk4_stream(flatten(s), fs=11931.82)
    assert total == 17                                # 16 sub-ticks + all-off tail
    # walk the records exactly as the ISR does: [n][voice|lvl<<4, lo, hi, viz]*
    i = subs = 0
    while i < len(stream):
        n = stream[i]; i += 1
        for _ in range(n):
            voice = stream[i] & 0x0F
            assert 0 <= voice <= 3
            i += 4
        subs += 1
    assert i == len(stream) and subs == total
    # sub-tick 0 carries the A4 note-on with the right increment + a noise-on
    n0 = stream[0]
    assert n0 == 2
    inc = stream[2] | (stream[3] << 8)
    assert inc == D._spk4_inc(440.0, 11931.82)


def test_spk4_stream_slides_emit_one_small_record_per_subtick():
    s = _rct(r0c0=_note(60, fx=R.FX_SLIDE_UP, param=0x19))
    stream, _ = spk4_stream(flatten(s), fs=11931.82)
    # 4 sub-ticks of slide = 4 records for voice 0 (one retune each), then off
    recs = 0
    i = 0
    while i < len(stream):
        n = stream[i]; i += 1
        recs += n
        i += 4 * n
    assert recs >= 4                                  # a retune every sub-tick


def test_mono_stream_arps_and_retunes_cheaply():
    s = _rct(r0c0=_note(72), r0c1=_note(64), r0c2=_note(55))
    stream, total = mono_stream(flatten(s), arp=True)
    assert total == 16
    # first sub-tick: full note-on (mode 0x43 write); later steps retune only
    assert stream[1] == 0x43                          # PIT command port first
    body = bytes(stream)
    assert body.count(b"\x43\xb6") == 1               # ONE mode latch total


def test_tandy_stream_uses_real_attenuation_for_volume():
    s = _rct(r0c0=_note(69, vol=16), r1c0=dict(fx=R.FX_VOLSLIDE, param=0x04))
    stream, _ = tandy_stream(flatten(s))
    # volume 15 -> attenuation 0 at the start, then the slide raises attenuation
    att_writes = [b for b in stream if (b & 0x90) == 0x90 and (b & 0x60) == 0]
    assert att_writes                                  # tone-0 attenuation bytes
    assert (att_writes[0] & 0x0F) == 0                # full volume first


def test_perf_chunks_compile_for_all_targets():
    s = _rct(r0c0=_note(60), r0c3=dict(note=R.midi_to_note(84)))
    chunks = perf_chunks(s)
    assert set(chunks) == {R.PERF_TANDY, R.PERF_1VOICE, R.PERF_4VOICE,
                           R.PERF_SBFM}
    for target, payload in chunks.items():
        p = R.parse_perf(payload)
        assert p["target"] == target and p["total_subs"] >= 16
        assert len(p["stream"]) > 0


def test_sbfm_stream_carries_opl_words_and_rhythm_strikes():
    from mcs_convert.streams import sbfm_stream
    from mcs_convert.effects import flatten
    from mcs_convert import dosplayer as D
    s = _rct(r0c0=_note(69), r0c3=dict(note=R.midi_to_note(84)),
             r2c0=dict(note=R.NOTE_OFF))
    stream, total = sbfm_stream(flatten(s))
    assert total == 17                                # 16 subs + all-off tail
    # walk the records: [n][voice|lvl<<4, word_lo, word_hi, viz]*
    i = subs = 0
    events = []
    while i < len(stream):
        n = stream[i]; i += 1
        for _ in range(n):
            events.append((stream[i] & 0x0F,
                           stream[i + 1] | (stream[i + 2] << 8)))
            i += 4
        subs += 1
    assert i == len(stream) and subs == total
    # sub-tick 0: an A4 note word on voice 0 + rhythm clear-then-strike on 3
    a4 = D._opl_note_word(440.0)
    assert (0, a4) in events
    assert (3, D._OPL_RHYTHM) in events               # re-arm (clear)
    assert (3, D._OPL_RHYTHM | D._OPL_HH) in events   # bright hit -> hi-hat
    # the note-off at row 2 emits a key-off (word 0)
    assert (0, 0) in events
    # held notes emit NOTHING between changes (the OPL holds them): far fewer
    # events than sub-ticks x voices
    assert len(events) < total * 2


def test_rct_build_com_sb_fm():
    from mcs_convert.streams import build_com as rct_build_com
    s = _rct(r0c0=_note(60), r0c3=dict(note=R.midi_to_note(84)))
    com = rct_build_com(s, "4voice", sb_fm=True, text_scope=5)
    assert com[:2] == b"\xB8\x03" or com[0] == 0xFA  # a real player
    assert b"\xBA\x88\x03" in com                    # mov dx,0x388 (OPL port)


def test_build_com_from_rct_all_modes():
    s = _rct(r0c0=_note(60), r1c0=_note(64), r0c3=dict(note=R.midi_to_note(84)))
    for kw in (dict(mode="4voice"), dict(mode="4voice", foreground=True),
               dict(mode="4voice", sb=True), dict(mode="tandy"),
               dict(mode="1voice")):
        com = build_com(s, **kw)
        assert com[0] in (0xFA, 0xB8) and len(com) > 200   # a real player
    with pytest.raises(ValueError):
        build_com(s, mode="gameboy")


def test_song_to_rct_patternizes_and_dedupes():
    song = Song(title="t", source="t")
    lead = Track(name="lead")
    for bar in range(4):                              # 4 identical bars
        lead.add(NoteEvent(start_tick=bar * 32, duration_ticks=8, midi_note=60))
        lead.add(NoteEvent(start_tick=bar * 32 + 16, duration_ticks=8, midi_note=67))
    song.add_track(lead)
    nz = Track(name="n", kind="noise")
    for bar in range(4):
        nz.add(NoteEvent(start_tick=bar * 32, duration_ticks=1, midi_note=84))
    song.add_track(nz)
    rct = song_to_rct(song)
    assert len(rct.order) == 4                        # 4 bars
    assert len(rct.patterns) == 1                     # ...all the same pattern
    pat = rct.patterns[rct.order[0]]
    assert pat.cell(0, 0).note == R.midi_to_note(60)
    assert pat.cell(0, 3).note == R.midi_to_note(84)  # noise hit on ch4
    assert pat.cell(8, 0).note == R.NOTE_OFF          # articulated off


def test_rct_roundtrips_through_universal_for_mcs_export():
    s = _rct(r0c0=_note(60), r2c0=dict(note=R.NOTE_OFF),
             r0c3=dict(note=R.midi_to_note(84)))
    song = rct_to_universal(s)
    kinds = sorted(t.kind for t in song.tracks)
    assert kinds == ["noise", "tone"]
    tone = [t for t in song.tracks if t.kind == "tone"][0]
    assert tone.notes[0].midi_note == 60
    assert tone.notes[0].duration_ticks == 2          # cut at row 2
    # and the universal song feeds the existing .MCS encoder unchanged
    from mcs_convert.mcs.encode import encode_song
    from mcs_convert.retrack import retrack
    data = encode_song(retrack(song, "mcs"), tempo_byte0=0x80, cap=True)
    assert len(data) > 32


def test_effects_actually_reach_the_com():
    # a vibrato'd note must produce a .COM that differs from the plain note's
    plain = build_com(_rct(r0c0=_note(69)), mode="4voice")
    vib = build_com(_rct(r0c0=_note(69, fx=R.FX_VIBRATO, param=0x4F)),
                    mode="4voice")
    assert plain != vib
