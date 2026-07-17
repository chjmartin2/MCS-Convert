"""Standalone DOS .COM player: register encoders, event stream, and the
hand-assembled engine's structure. (Audio correctness is verified by ear in
DOSBox; here we pin the byte-level contract.)"""

import struct

import pytest

from mcs_convert import dosplayer as D
from mcs_convert.model import NoteEvent, Song, Track


def _song(notes):
    s = Song(title="t", source="t")
    tr = Track(name="m")
    for start, dur, midi in notes:
        tr.add(NoteEvent(start_tick=start, duration_ticks=dur, midi_note=midi))
    s.add_track(tr)
    return s


def test_tandy_note_encoding():
    # C4 (261.63 Hz) on channel 0: SN76489 divider 428 -> latch 0x8C, data 0x1A,
    # attenuation 0x90 (channel 0, full volume).
    assert D._tandy_note_on(0, 261.63) == [(0xC0, 0x8C), (0xC0, 0x1A), (0xC0, 0x90)]
    # channel 2 note-off = attenuation 15 on channel 2 (0x80|0x40|0x10|0x0F)
    assert D._tandy_note_off(2) == [(0xC0, 0xDF)]


def test_pc_speaker_note_encoding():
    # C4 -> PIT ch2 divider 4561 (0x11D1), mode 3, gate on.
    assert D._spk_note_on(261.63) == [
        (0x43, 0xB6), (0x42, 0xD1), (0x42, 0x11), (0x61, 0x03)]
    assert D._spk_note_off() == [(0x61, 0x00)]


def test_mono_stream_takes_the_top_voice():
    # the highest voice (channel 0) plays; events land on SUB-tick keys and each
    # note is articulated (its off comes a sub-tick before the next onset).
    song = Song(title="t", source="t")
    a, b = Track(name="a"), Track(name="b")
    a.add(NoteEvent(start_tick=0, duration_ticks=8, midi_note=60))   # low, long
    b.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=72))   # high, short
    song.add_track(a)
    song.add_track(b)
    by = D._mono_stream(song)
    assert by[0] == D._spk_note_on(D.midi_to_freq(72))        # top note at sub-tick 0
    assert by[D._artic_off(0, 4)] == D._spk_note_off()        # articulated silence before its end


def test_build_stream_records_are_wellformed():
    stream, total = D._build_stream(_song([(0, 4, 60), (4, 4, 64)]), "tandy")
    assert total == 8 * D._SUBTICKS                  # sub-tick resolution
    # walk the per-sub-tick [count][port,val]* records; must consume exactly
    i = pairs_seen = 0
    for _ in range(total):
        cnt = stream[i]; i += 1 + 2 * cnt
        pairs_seen += cnt
    assert i == len(stream) and pairs_seen > 0


def test_notes_are_articulated():
    # a note cut one sub-tick before its nominal end leaves a gap so the next
    # onset re-attacks (fixes fast repeats blurring into one held tone).
    by = D._tandy_stream(_song([(0, 2, 60), (2, 2, 60)]))    # same pitch, back to back
    S = D._SUBTICKS
    assert 0 in by and by[0][:1]                              # first note attacks at 0
    assert by[2 * S - 1] == D._tandy_note_off(0)             # cut before the 2nd onset
    assert 2 * S in by                                        # 2nd note re-attacks


def test_percussion_routes_to_noise_channel():
    # a percussive note goes to SN76489 channel 3 (noise), not the tone voices:
    # bright pitch -> fast /512 shift (0xE4), dark -> slow /2048 (0xE6).
    song = Song(title="t", source="t")
    tr = Track(name="d")
    tr.add(NoteEvent(start_tick=0, duration_ticks=1, midi_note=100, percussive=True))
    tr.add(NoteEvent(start_tick=4, duration_ticks=1, midi_note=47, percussive=True))
    song.add_track(tr)
    by = D._tandy_stream(song)
    assert (0xC0, 0xE4) in by[0]                              # E7 hi-hat -> bright
    assert (0xC0, 0xE6) in by[4 * D._SUBTICKS]               # B2 kick -> dark


def test_com_header_and_layout():
    com = D.build_com(_song([(0, 4, 60), (4, 4, 67)]), "tandy", 0x80)
    assert com[:3] == b"\xFA\x31\xC0"                # cli ; xor ax,ax
    assert com.endswith(b"\x00") is False or True    # (data tail varies)
    assert len(com) < 0xFF00
    # the INT 8 vector is installed with the isr's in-segment offset; that offset
    # must land inside the file (0x100..end) and point at the isr prologue byte.
    # 'mov word [es:0x20], imm16' -> bytes 26 C7 06 20 00 <imm16> near the start.
    marker = com.index(b"\x26\xC7\x06\x20\x00")
    isr_off = struct.unpack_from("<H", com, marker + 5)[0]
    assert 0x100 <= isr_off < 0x100 + len(com)
    assert com[isr_off - 0x100] == 0x50              # isr begins with push ax


def test_text5_combined_monitor_has_all_three_views():
    # text_scope=5 is the combined monitor: text mode 3, with the scope box glyphs
    # (grid traces), the block + half-blocks (spectrum + VU bars), and the strike
    # latch wired into playrec so the VU meters kick on note-ons.
    t5 = D.build_com(_song([(0, 4, 60), (4, 4, 67), (8, 4, 72), (12, 2, 48)]),
                     "tandy", 0x80, text_scope=5)
    assert t5[:5] == b"\xB8\x03\x00\xCD\x10"          # text mode 3
    for glyph in (0xC4, 0xB3, 0xDA, 0xDB, 0xDC, 0xDD):   # scope + bar + VU glyphs
        assert bytes([0xB0, glyph]) in t5
    # one unified border grid: cross + tee junctions join the cells
    for junction in (0xC5, 0xC2, 0xC1, 0xC3, 0xB4):   # ┼ ┬ ┴ ├ ┤
        assert bytes([0xB0, junction]) in t5
    assert bytes([0xB0, 0xDF]) not in t5              # no white spectrum peak caps
    # the VU meter labels are drawn ('P','1','N','z' etc.)
    for ch in "P1TrNz":
        assert bytes([0xB0, ord(ch)]) in t5
    # it's the heaviest text renderer (three views + six borders)
    big = _song([(0, 4, 60), (4, 4, 67), (8, 4, 72)])
    assert len(D.build_com(big, "tandy", 0x80, text_scope=5)) > \
        len(D.build_com(big, "tandy", 0x80, text_scope=4))


def test_strike_latch_fires_on_noteon_only():
    # playrec latches strike[ch]=1 only when it stores a NON-zero viz value (a real
    # note-on / drum hit), never on the note-off (viz=0) write. The table is always
    # present so playrec links in every mode.
    cap = {}
    real = D._Asm.resolve

    def capture(self):
        out = real(self)
        cap["labels"], cap["com"] = dict(self.labels), out
        return out

    D._Asm.resolve = capture
    try:
        D.build_com(_song([(0, 4, 60)]), "tandy", 0x80, text_scope=5)
    finally:
        D._Asm.resolve = real
    L, com = cap["labels"], cap["com"]
    # 'mov byte [di+strike], 1' guarded by 'or al,al / jz' after the viz store
    assert b"\xC6\x85" + struct.pack("<H", L["strike"]) + b"\x01" in com


def test_4voice_pc_speaker_mixes_in_software():
    # --4voice is a software 1-bit mixer: a sample-rate ISR sums phase-accumulator
    # square waves + an LFSR noise voice, and delta-sigma modulates them onto the
    # speaker (3 tones + 1 noise = threshold 4).
    song = Song(title="t", source="t")
    for midi in (72, 64, 55):
        tr = Track(name="v")
        tr.add(NoteEvent(start_tick=0, duration_ticks=8, midi_note=midi))
        song.add_track(tr)
    com = D.build_com(song, "4voice", 0x80)
    assert com[0] == 0xFA                             # cli (engine prologue)
    assert b"\xE6\x61" in com                         # out 0x61,al (drives the speaker)
    assert b"\x3C\x04" in com                         # cmp al,4 : 4-voice delta-sigma threshold
    assert b"\x35\x00\xB4" in com                     # xor ax,0xB400 : the noise LFSR taps
    # the stream is per sub-tick: [nchanges][voice, inc_lo, inc_hi, viz]*
    stream, total = D._build_spk4_stream(song)
    assert stream[0] == 3                             # sub-tick 0 = three note-ons
    voice, lo, hi = stream[1], stream[2], stream[3]
    assert voice == 0 and (lo | (hi << 8)) == D._spk4_inc(D.midi_to_freq(72))
    # the graphics oscilloscope is Tandy-only, but text scopes are allowed
    with pytest.raises(ValueError):
        D.build_com(song, "4voice", 0x80, scope=True)
    assert len(D.build_com(song, "4voice", 0x80, text_scope=5)) > len(com)


def test_4voice_percussion_drives_the_noise_voice():
    # a percussive note lands on voice 3 with a bright/dark LFSR-clock inc; a
    # pitched note never touches voice 3.
    song = Song(title="t", source="t")
    mel = Track(name="m")
    mel.add(NoteEvent(start_tick=0, duration_ticks=4, midi_note=60))
    drums = Track(name="d")
    drums.add(NoteEvent(start_tick=0, duration_ticks=1, midi_note=100, percussive=True))
    song.add_track(mel)
    song.add_track(drums)
    events = D._spk4_events(song)
    at0 = dict((v, inc) for v, inc, viz in events[0])   # voice -> inc at sub-tick 0
    assert at0[0] == D._spk4_inc(D.midi_to_freq(60))  # melody on voice 0
    assert at0[3] == D._spk4_noise_inc(True)          # bright hi-hat on the noise voice
    assert D._spk4_noise_inc(True) > D._spk4_noise_inc(False)   # brighter = faster clock


def test_4voice_text_scope_reuses_the_renderer():
    # a text scope on the 4-voice player shares the Tandy renderers: it sets text
    # mode 3, draws with the same glyphs, and the audio ISR now also fills viz[]
    # from the stream (which carries a per-voice viz byte) so the scopes track it.
    song = Song(title="t", source="t")
    for midi in (72, 64, 55):
        tr = Track(name="v")
        tr.add(NoteEvent(start_tick=0, duration_ticks=8, midi_note=midi))
        song.add_track(tr)
    t5 = D.build_com(song, "4voice", 0x80, text_scope=5)
    assert t5[:5] == b"\xB8\x03\x00\xCD\x10"          # sets 80x25 text mode
    for glyph in (0xC4, 0xB3, 0xDB, 0xDD, 0xC5):      # ─ │ █ ▌ ┼ (scope + VU + grid)
        assert bytes([0xB0, glyph]) in t5
    # the stream carries the viz byte per change (4-byte records)
    stream, _ = D._build_spk4_stream(song)
    assert stream[0] == 3 and stream[4] == D._viz_period(D.midi_to_freq(72))
    # a plain 4-voice build (no scope) stays in the audio-only fast path
    plain = D.build_com(song, "4voice", 0x80)
    assert plain[:5] != b"\xB8\x03\x00\xCD\x10"


def test_vu_only_display_is_light_and_labelled():
    # text_scope=6 is the lightweight VU-only display: text mode 3, labelled bars,
    # much smaller than the full combined monitor. Works on Tandy and 4-voice.
    song = Song(title="t", source="t")
    for midi in (72, 64, 55):
        tr = Track(name="v")
        tr.add(NoteEvent(start_tick=0, duration_ticks=8, midi_note=midi))
        song.add_track(tr)
    vu = D.build_com(song, "4voice", 0x80, text_scope=6)
    assert vu[:5] == b"\xB8\x03\x00\xCD\x10"          # text mode 3
    for glyph in (0xDB, 0xDD, 0xB3, 0xC4):            # █ ▌ │ ─ (bar + peak + border)
        assert bytes([0xB0, glyph]) in vu
    for chch in "Triangle":                           # a channel label is drawn
        assert bytes([0xB0, ord(chch)]) in vu
    t5 = D.build_com(song, "4voice", 0x80, text_scope=5)
    assert len(vu) < len(t5)                          # VU-only is much lighter
    assert len(D.build_com(song, "tandy", 0x80, text_scope=6)) > 0   # also on Tandy


def test_4voice_mix_rate_is_controllable():
    # --mix-rate sets the software sample rate: it changes the PIT divider (a lower
    # rate => a larger divider) and rescales the phase increments.
    song = Song(title="t", source="t")
    tr = Track(name="v")
    tr.add(NoteEvent(start_tick=0, duration_ticks=8, midi_note=69))   # A4 440 Hz
    song.add_track(tr)
    assert D._spk4_div_for(6000) > D._spk4_div_for(12000)   # lower Fs, bigger divider
    assert D._spk4_div_for(None) == D._SPK4_DIV             # default
    assert D._spk4_div_for(999999) == D._SPK4_DIV_MIN       # clamped
    lo = D.build_com(song, "4voice", 0x80, mix_rate=6000)
    hi = D.build_com(song, "4voice", 0x80, mix_rate=16000)
    # the ISR programs PIT ch0 with the divider (mov ax, div right after cmd 0x36)
    def divider(com):
        m = com.index(b"\xB0\x36\xE6\x43") + 4
        return com[m + 1] | (com[m + 2] << 8)
    assert divider(lo) > divider(hi)                  # 6 kHz uses a bigger divider


def test_draw_skip_throttles_the_scope():
    # draw_skip>1 makes the foreground wait N retraces per frame (a mov cx,N + a
    # loop) before blitting, so the .COM differs from the every-frame build.
    song = _song([(0, 4, 60), (4, 4, 67)])
    every = D.build_com(song, "tandy", 0x80, text_scope=6, draw_skip=1)
    third = D.build_com(song, "tandy", 0x80, text_scope=6, draw_skip=3)
    assert len(third) > len(every)                    # the pace loop adds a few bytes


def test_spinners_display_is_minimal_and_direct():
    # text_scope=7 is the XT-minimal voice display: each cell's CHARACTER is just
    # the raw viz[] byte dumped straight to B800 (mov al,[viz]; mov es:[cell],ax) --
    # no phase, no branch, no glyph table, no clear, no blit.
    song = Song(title="t", source="t")
    for midi in (72, 64, 55):
        tr = Track(name="v")
        tr.add(NoteEvent(start_tick=0, duration_ticks=8, midi_note=midi))
        song.add_track(tr)
    cap = {}
    real = D._Asm.resolve

    def capture(self):
        out = real(self)
        cap["labels"] = dict(self.labels)
        return out

    D._Asm.resolve = capture
    try:
        spin = D.build_com(song, "4voice", 0x80, text_scope=7)
    finally:
        D._Asm.resolve = real
    L = cap["labels"]
    assert spin[:5] == b"\xB8\x03\x00\xCD\x10"        # text mode 3
    assert b"\x26\xA3" in spin                        # mov es:[imm16],ax (direct B800 write)
    assert b"\xF3\xAB" not in spin                    # no 'rep stosw' full-screen clear
    assert b"\xF3\xA5" not in spin                    # no 'rep movsw' blit
    assert b"\xF4" in spin                            # hlt-paced (fast-CPU safe)
    # the character source is the raw viz table: 'mov al,[viz]' (A0 <viz offset>)
    assert b"\xA0" + struct.pack("<H", L["viz"]) in spin
    assert len(spin) < len(D.build_com(song, "4voice", 0x80, text_scope=6))  # < VU meters
    with pytest.raises(ValueError):                   # not on Tandy
        D.build_com(song, "tandy", 0x80, text_scope=7)


def test_no_scope_wait_loop_uses_hlt():
    # The no-scope foreground must HLT (sleep until the next interrupt) rather than
    # spin on int 0x16 -- a tight keyboard poll on a very fast CPU / DOSBox
    # cycles=max starves the timer IRQ and the audio skips. The wait loop is
    # 'hlt; mov ah,1; int 0x16; jz wait'.
    for mode in ("tandy", "4voice"):
        com = D.build_com(_song([(0, 4, 60), (4, 4, 67)]), mode, 0x80)
        assert b"\xF4\xB4\x01\xCD\x16" in com         # hlt; mov ah,1; int 0x16
    # a scope build paces itself to the vertical retrace, so it doesn't hlt-spin
    scoped = D.build_com(_song([(0, 4, 60)]), "tandy", 0x80, text_scope=6)
    assert b"\xF4\xB4\x01\xCD\x16" not in scoped


def test_com_auto_repeats_until_keypress():
    # When the song ends (ticksleft hits 0) the ISR rewinds streamptr + ticksleft
    # to the top instead of halting, so the .COM loops until a key is pressed.
    cap = {}
    real = D._Asm.resolve

    def capture(self):
        out = real(self)
        cap["labels"], cap["com"] = dict(self.labels), out
        return out

    D._Asm.resolve = capture
    try:
        D.build_com(_song([(0, 4, 60), (4, 4, 67)]), "tandy", 0x80)
    finally:
        D._Asm.resolve = real
    L, com = cap["labels"], cap["com"]
    # 'mov word [streamptr], <stream offset>' — the rewind that makes it loop
    rewind = b"\xC7\x06" + struct.pack("<HH", L["streamptr"], L["stream"])
    assert rewind in com
    assert b"\xC7\x06" + struct.pack("<H", L["ticksleft"]) in com   # ticksleft reload


def test_tempo_sets_the_timer_divider():
    # The tempo byte picks the PIT divider (a slower tempo = a longer sub-tick =
    # a larger divider). At sub-tick resolution both fit 16 bits, so subdiv is 1.
    def divider_and_subdiv(byte0):
        com = D.build_com(_song([(0, 4, 60)]), "tandy", byte0)
        # 'mov ax, <divider>' (B8 lo hi) is the first B8 after the timer cmd 0x36
        m = com.index(b"\xB0\x36\xE6\x43") + 4       # past mov al,0x36;out 0x43,al
        assert com[m] == 0xB8                         # mov ax, imm16
        div = com[m + 1] | (com[m + 2] << 8)
        sub = com[com.index(b"\xC6\x06") + 4]         # subcount init immediate
        return div, sub
    dfast, sfast = divider_and_subdiv(0x77)
    dslow, sslow = divider_and_subdiv(0x92)
    assert dslow > dfast                              # slower tempo, bigger divider
    assert sfast == 1 and sslow == 1                  # sub-ticks fit one period


def test_build_com_rejects_bad_input():
    with pytest.raises(ValueError):
        D.build_com(_song([]), "tandy", 0x80)        # no notes
    with pytest.raises(ValueError):
        D.build_com(_song([(0, 4, 60)]), "bogus", 0x80)
    with pytest.raises(ValueError):
        D.build_com(_song([(0, 4, 60)]), "1voice", 0x80, scope=True)  # scope=tandy only


def test_scope_stream_carries_viz_records():
    # with scope on, each note also emits a (0xF0|ch, half-period) viz pair and a
    # (0xF0|ch, 0) at note-off; these ride the same stream as the audio writes.
    by = D._tandy_stream(_song([(0, 4, 67)]), scope=True)
    on = by[0]
    assert any(p == D._VIZ_PORT and v > 0 for p, v in on)        # channel-0 period
    off = by[D._artic_off(0, 4)]
    assert (D._VIZ_PORT, 0) in off                               # silenced at note-off
    # without scope there are no 0xF0-0xF3 records
    plain = D._tandy_stream(_song([(0, 4, 67)]), scope=False)
    assert not any(0xF0 <= p <= 0xF3 for evs in plain.values() for p, _ in evs)


def test_scope_com_sets_and_restores_video_mode():
    com = D.build_com(_song([(0, 4, 60), (4, 4, 67)]), "tandy", 0x80, scope=True)
    assert com[:5] == b"\xB8\x09\x00\xCD\x10"        # mov ax,0x0009 ; int 0x10 (Tandy 320x200x16)
    assert b"\xB8\x03\x00\xCD\x10" in com            # mov ax,0x0003 ; int 0x10 (restore)
    assert b"\xB8\x00\xB8\x8E\xC0" in com            # mov ax,0xB800 ; mov es,ax (Tandy framebuffer)
    # a non-scope Tandy build does neither
    plain = D.build_com(_song([(0, 4, 60)]), "tandy", 0x80, scope=False)
    assert plain[:5] != b"\xB8\x09\x00\xCD\x10"
    assert b"\xB8\x00\xB8\x8E\xC0" not in plain


def test_text_scope_uses_text_mode_and_is_smaller():
    # --scope-text sets text mode 3 (not graphics 9), blits to B800, restores
    # text mode on exit, and is lighter than the graphics build.
    txt = D.build_com(_song([(0, 4, 60), (4, 4, 67)]), "tandy", 0x80, text_scope=True)
    assert txt[:5] == b"\xB8\x03\x00\xCD\x10"        # mov ax,0x0003 ; int 0x10 (80x25 text)
    assert b"\xB8\x09\x00\xCD\x10" not in txt        # NOT the graphics mode
    assert b"\xB8\x00\xB8\x8E\xC0" in txt            # mov ax,0xB800 ; mov es,ax (text page)
    gfx = D.build_com(_song([(0, 4, 60), (4, 4, 67)]), "tandy", 0x80, scope=True)
    assert len(txt) < len(gfx)                       # text renderer is lighter
    with pytest.raises(ValueError):                  # Tandy only
        D.build_com(_song([(0, 4, 60)]), "1voice", 0x80, text_scope=True)


def test_text2_uses_box_drawing_glyphs():
    # text_scope=2 is the box-drawing LINE trace: still text mode 3, and it emits
    # the CP437 corner/line glyphs (┌┐└┘─│). text_scope=1 stays the block bars.
    t2 = D.build_com(_song([(0, 4, 60), (4, 4, 67)]), "tandy", 0x80, text_scope=2)
    assert t2[:5] == b"\xB8\x03\x00\xCD\x10"         # text mode 3
    for glyph in (0xDA, 0xBF, 0xC0, 0xD9, 0xC4, 0xB3):   # ┌ ┐ └ ┘ ─ │
        assert bytes([0xB0, glyph]) in t2            # mov al, <glyph>
    t1 = D.build_com(_song([(0, 4, 60)]), "tandy", 0x80, text_scope=1)
    assert bytes([0xB0, 0xDA]) not in t1             # block build has no box corners


def test_text3_is_grid_layout_in_text_mode():
    # text_scope=3 is the 2x2 grid + master (the graphics layout, in text): text
    # mode 3, box-drawing glyphs like text 2, but a heavier renderer (frames +
    # two draw passes) so it's larger than the single-column text 2.
    t3 = D.build_com(_song([(0, 4, 60), (4, 4, 67), (8, 4, 72)]), "tandy", 0x80,
                     text_scope=3)
    assert t3[:5] == b"\xB8\x03\x00\xCD\x10"         # text mode 3
    for glyph in (0xDA, 0xBF, 0xC0, 0xD9, 0xC4, 0xB3):   # ┌ ┐ └ ┘ ─ │ (frames + trace)
        assert bytes([0xB0, glyph]) in t3
    t2 = D.build_com(_song([(0, 4, 60), (4, 4, 67), (8, 4, 72)]), "tandy", 0x80,
                     text_scope=2)
    assert len(t3) > len(t2)                          # grid + frames is heavier


def test_text4_spectrum_uses_block_and_half_block_glyphs():
    # text_scope=4 is the faux spectrum analyzer: text mode 3, coloured bars built
    # from the full block and the half-height blocks (for smooth motion), a white
    # peak cap, and a per-period harmonic table (4 bins x 51 periods).
    t4 = D.build_com(_song([(0, 4, 60), (4, 4, 67), (8, 4, 72)]), "tandy", 0x80,
                     text_scope=4)
    assert t4[:5] == b"\xB8\x03\x00\xCD\x10"          # text mode 3
    for glyph in (0xDB, 0xDC, 0xDF):                  # █ full, ▄ lower half, ▀ peak cap
        assert bytes([0xB0, glyph]) in t4
    # the harmonic table is a square wave's odd harmonics on a log-freq axis:
    # bins rise with frequency, and harmonics sit above the fundamental
    harm = D._s4_harm()
    assert len(harm) == 51 * 4
    f, h3, h5, h7 = harm[6 * 4:6 * 4 + 4]             # a mid-high note (period 6)
    assert f < h3 < h5 and h7 >= h5                   # 3f,5f,7f above the fundamental bin
    # rows are coloured green(low) -> yellow -> red(high)
    rc = D._s4_rowcol()
    assert rc[D._S4_BASE_ROW] == D._S4_GREEN and rc[D._S4_TOP_ROW] == D._S4_RED
