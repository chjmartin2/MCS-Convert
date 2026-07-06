"""Parse a Music Construction Set (IBM-PC 1984) song into our neutral Song model.

The decoding below is no longer inferred — it matches MCSDISK.EXE's own player,
located by disassembly (see docs/mcs-format.md for the full write-up):

  0x00..0x08  header: view/scroll bytes (display only — pitch does NOT depend on
              them) and a tempo word at 0x05 stored as 0x3AF9 + setting.
  0x09 uint16 byte size of the staff-1 section (CONFIRMED)
  0x0B uint16 byte size of the staff-2 section (CONFIRMED)
  0x0D uint16 total file length (CONFIRMED)
  0x0F..      body: FF FF (count, prev_count) records = MEASURES; (0, prev!=0) is an
              empty measure, (0, 0) terminates a staff. Staff 1 then staff 2.

Note entry = 16-bit little-endian word (byte0, byte1):

  bits [15:11] (byte1 top 5) = HORIZONTAL slot, 8-px units
  bits [10:5]  = v, the 6-bit VERTICAL staff position (1-based, smaller = higher;
                 staff 1 uses v 1..20, staff 2 uses v 21..41 — the engine splits at
                 v*4 >= 0x54). The old models missed that byte1's low 3 bits are the
                 high half of the vertical.
  bits [4:0]   = symbol (dispatch table at MCSDISK image 0x22b1, entry (sym+1)&0x1f,
                 handler = table word + 0x40):
                 0x00..0x05  note: 32nd, 16th, 8th, quarter, half, whole (2**n ticks)
                 0x14..0x18  the same values 32nd..half, *beamed* (value = symbol - 0x14);
                             the engine dispatches these to the identical duration
                             handlers; fast beamed runs (BUMBLE.MCD) are stored this way.
                 0x06 / 0x0D treble / bass clef glyph
                 0x07..0x0C  rest of the same ladder (n-7)
                 0x0E / 0x0F / 0x10  natural / sharp / flat glyph. In the clef record
                             these build the KEY SIGNATURE (applied at the glyph's
                             staff degree in every octave); inside a measure they set
                             a measure-scoped accidental at that exact position.
                 0x11        augmentation dot: the engine adds half the previous
                             note's duration to it (handler at MCSDISK 0x245c).
                 0x12        8va. In the clef record: whole-staff baseline +12 semitones
                             (0x1629/0x16b3: [0x5bbe/f] = 0x18). Mid-measure: sets the
                             staff's working shift to +12 (handler 0x24d7) from that
                             entry onward; the FF FF boundary handler (0x22fd) restores
                             the baseline, so it lasts to the END OF THE MEASURE only.
                             The shift is an absolute SET, always UP - the glyph's
                             position is cosmetic and the engine has NO 8vb at all.
                 0x13 / 0x19 tie/slur mark drawn above / below its notes (handlers
                             0x24e5/0x24df search down/up from the glyph for the
                             sounding voice). No duration, no pitch.
                 0x1F        the FF FF record marker seen as an entry: the engine's
                             measure-boundary handler (octave reset, accidental clear).

  Pitch: the engine xlats v through one of FOUR fixed 41-byte grand-staff ladders,
  picked by the two staves' clefs (ds:0x5c47 + 2*clef1 + clef2, clef 0=G / 0x29=F;
  image bases 0x5c87/0x5cb0/0x5cd9/0x5d02): value = 2 x semitone. The tables show the
  window follows the CLEF alone — G anywhere reads E7..G4, F anywhere reads G5..B2 —
  so a bass clef on the top staff (THATSALL) and mid-staff clef changes (CANON,
  SOCKHOP, 16 songs) just swap windows. Accidentals add +/-2, the 8va adds +24, and
  the result indexes a 68-entry chromatic period table whose entry for G4 is PIT
  divisor 3044 = 392.00 Hz exactly, anchoring MIDI = value/2 + 44. (The windows are
  FIXED — the header scroll bytes are pure view state.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from ..model import NoteEvent, Song, Track

CLEF_TREBLE = 0x06
CLEF_BASS = 0x0D
SYM_NATURAL, SYM_SHARP, SYM_FLAT = 0x0E, 0x0F, 0x10
SYM_DOT = 0x11
SYM_OCTAVA = 0x12         # 8va: +1 octave from the glyph to the end of the measure (never down)
SYM_TIE = 0x13            # tie/slur drawn above its notes
SYM_TIE_BELOW = 0x19      # tie/slur drawn below its notes (same playback effect)
_TIE_SYMS = (SYM_TIE, SYM_TIE_BELOW)
SYM_MARKER = 0x1F

# The per-clef vertical windows, lifted verbatim from MCSDISK.EXE. The engine keeps FOUR
# 41-entry ladders (ds:0x5c47 + 2*clef1 + clef2, clef value 0=G / 0x29=F; image +0x40 —
# bases 0x5c87/0x5cb0/0x5cd9/0x5d02 for G/G, G/F, F/G, F/F), one per clef combination.
# Their contents prove the window depends ONLY on the staff's clef, never on whether it
# is the top or bottom staff: 2 x semitone per byte, per staff position top-down, 0 =
# unusable. So a bass clef on the TOP staff (THATSALL) reads the bass window, and a
# mid-staff clef glyph simply swaps windows from that point on.
_TREBLE_WINDOW = bytes.fromhex("706c686662 5e5a5854504e4a4642403c 3836322e")  # E7 down to G4
_BASS_WINDOW = bytes.fromhex("4642403c38 3632 2e2a2824201e1a16 12100c080600")  # G5 down to B2
# MIDI = value/2 + 44, from the engine's PIT-divisor table (G4's divisor 3044 = 392.00 Hz).
# This matches the NOTATION MCS shows on screen: ENTERTAN reads D..E..C.. in C major, exactly
# as the program draws it. (An earlier "anchor 28 / -16" change was WRONG -- it chased pitches
# mis-read off the 1-bit polyphonic audio, whose octave/voice detection is unreliable.)
MIDI_ANCHOR = 44

# One tick = one THIRTY-SECOND note (MCS's smallest note value; symbol 0x00 is the 32nd,
# shown as the first note in the program's palette). value n -> 2**n thirty-second-ticks:
# 0=32nd, 1=16th, 2=8th, 3=quarter, 4=half, 5=whole.
_NOTE_TICKS = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16, 5: 32}

# A note can be stored "beamed" — symbols 0x14..0x18 are the note values 32nd..half with
# 0x14 added (the engine's dispatch routes them to the identical duration handlers;
# confirmed by BUMBLE.MCD, whose beamed-16th runs otherwise vanish). 0x19 is NOT a beamed
# whole — it's the below-the-notes tie glyph (222 occurrences corpus-wide; decoding it as
# a note inserted phantom whole notes into CANON, ELSEWERE, BABYFACE, ...).
_BEAM_OFFSET = 0x14


def _note_value(sym: int) -> tuple[str, int]:
    """Classify a symbol: ('note'|'rest'|'', value) where value indexes _NOTE_TICKS.

    Notes are 0x00..0x05 (0x00 = 32nd), beamed notes 0x14..0x18, rests 0x07..0x0c
    (0x07 = 32nd rest). The 32nd note/rest were originally dropped, which shortened every
    measure that used them (ALLEGRO, DIE, ...); MCS's palette proves the 32nd is real.
    """
    if _BEAM_OFFSET <= sym <= _BEAM_OFFSET + 4:          # 0x14..0x18 = beamed note
        return "note", sym - _BEAM_OFFSET
    if 0 <= sym <= 5:                                    # 0x00..0x05 = note
        return "note", sym
    if 7 <= sym <= 12:                                   # 0x07..0x0c = rest
        return "rest", sym - 7
    return "", 0

# Tempo: the 0x05 header word is 0x3AF9 + 3*level; the engine feeds `level` into its
# note-timing multiply (image 0x1535). The corpus uses levels 0..3 only.
TEMPO_BASE = 0x3AF9
TEMPO_STEP = 3

# The REAL playback tempo is set by header byte 0 (0x77..0x92, in steps of 3), NOT the
# 0x05 word. Measured from DOSBox-X captures: seconds per SIXTEENTH =
# 0.067 + 0.016 * step, where step = (byte0 - 0x77) // 3. Fits ENTERTAN (0x7a -> 83ms),
# AXEL/YANKEE (0x80 -> 115ms), MINUETG (0x83 -> 131ms), DIXIE (0x89 -> 163ms).
_TEMPO_BASE_BYTE = 0x77


def tick_seconds_for(byte0: int) -> float:
    """Seconds per tick. A tick is a 32nd, so it's half the measured per-sixteenth rate."""
    step = max(0, (byte0 - _TEMPO_BASE_BYTE) // 3)
    return (0.067 + 0.016 * step) / 2.0

# Key names by number of sharps / flats in the clef-record signature.
_SHARP_KEYS = ["C", "G", "D", "A", "E", "B", "F#", "C#"]
_FLAT_KEYS = ["C", "F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"]

# Common measure lengths (in thirty-second-ticks) -> time signature label.
_TIME_SIG = {4: "2/8", 8: "1/4", 12: "3/8", 16: "2/4", 24: "3/4",
             32: "4/4", 40: "5/4", 48: "6/8", 64: "4/2"}


def _time_signature(ticks: int) -> str:
    if ticks in _TIME_SIG:
        return _TIME_SIG[ticks]
    return f"{ticks}/32" if ticks else ""


def _key_name(keysig: List[int]) -> str:
    sharps = sum(1 for v in keysig if v > 0)
    flats = sum(1 for v in keysig if v < 0)
    if flats:
        return f"{_FLAT_KEYS[min(flats, 7)]} major"
    return f"{_SHARP_KEYS[min(sharps, 7)]} major"


def vertical(byte0: int, byte1: int) -> int:
    """The 6-bit staff position: byte1's low 3 bits are the high half."""
    return ((byte1 & 7) << 3) | (byte0 >> 5)


def symbol(byte0: int) -> int:
    return byte0 & 0x1F


def x_slot(byte1: int) -> int:
    """Horizontal position in 8-px slots (byte1's top 5 bits)."""
    return byte1 >> 3


def decode_duration(byte0: int) -> tuple[bool, int]:
    """(is_rest, ticks) for a note/rest symbol. One tick = one thirty-second."""
    kind, value = _note_value(symbol(byte0))
    if not kind:
        raise ValueError(f"symbol 0x{symbol(byte0):02x} is not a note or rest")
    return kind == "rest", _NOTE_TICKS[value]


def _u16(d: bytes, off: int) -> int:
    return d[off] | (d[off + 1] << 8)


@dataclass
class Record:
    count: int
    prev: int
    entries: List[Tuple[int, int]]   # (byte0, byte1)


def parse_records(d: bytes) -> List[Record]:
    recs: List[Record] = []
    i = 0x0F
    while i < len(d) - 1:
        if d[i] == 0xFF and d[i + 1] == 0xFF:
            count = d[i + 2] if i + 2 < len(d) else 0
            prev = d[i + 3] if i + 3 < len(d) else 0
            j = i + 4
            entries: List[Tuple[int, int]] = []
            while j < len(d) - 1 and not (d[j] == 0xFF and d[j + 1] == 0xFF):
                entries.append((d[j], d[j + 1]))
                j += 2
            recs.append(Record(count, prev, entries))
            i = j
        else:
            i += 1
    return recs


def split_staves(recs: List[Record]) -> List[List[Record]]:
    """Group records into staves. Only a (0, 0) record terminates a staff; (0, prev)
    with prev != 0 is an EMPTY MEASURE and is kept in place."""
    staves: List[List[Record]] = []
    cur: List[Record] = []
    for r in recs:
        if r.count == 0 and r.prev == 0:
            if cur:
                staves.append(cur)
                cur = []
            continue
        cur.append(r)
    if cur:
        staves.append(cur)
    return staves


@dataclass
class _Staff:
    clef: int
    v_base: int                    # staff POSITION: top staff v 1..20, bottom v 21..41
    keysig: List[int]              # semitone*2 offset per staff degree (0..6)
    octava: int                    # +24 if the clef record carries an 8va glyph
    records: List[Record]

    def midi(self, v: int, acc2: int, clef: int | None = None) -> int:
        """MIDI note for vertical position v with accidental offset (2x semitones).

        The window is chosen by the CLEF alone (the engine's four ladder tables carry
        the same values wherever the clef sits); v_base only rebases the position.
        `clef` overrides the staff's opening clef after a mid-staff clef change."""
        win = _TREBLE_WINDOW if (clef or self.clef) == CLEF_TREBLE else _BASS_WINDOW
        idx = v - self.v_base
        if not (0 <= idx < len(win)) or win[idx] == 0:
            return 0
        return (win[idx] + acc2 + self.octava) // 2 + MIDI_ANCHOR


def _read_staff(recs: List[Record]) -> _Staff:
    """Decode a staff's clef record. v_base is POSITIONAL — which screen staff the notes
    sit on (v 1..20 = top, v 21..41 = bottom) — while the pitch window itself follows
    the CLEF (see _Staff.midi), so a bass clef on the top staff reads correctly."""
    clef = CLEF_TREBLE
    keysig = [0] * 7
    octava = 0
    if recs and recs[0].entries:
        for b0, b1 in recs[0].entries:
            sym = symbol(b0)
            if sym in (CLEF_TREBLE, CLEF_BASS):
                clef = sym
            elif sym in (SYM_NATURAL, SYM_SHARP, SYM_FLAT):
                acc = {SYM_NATURAL: 0, SYM_SHARP: 2, SYM_FLAT: -2}[sym]
                keysig[(vertical(b0, b1) - 1) % 7] = acc
            elif sym == SYM_OCTAVA:
                octava = 24
    note_vs = [vertical(b0, b1) for rec in recs[1:] for b0, b1 in rec.entries
               if _note_value(symbol(b0))[0] == "note"]
    if note_vs:
        v_base = 21 if sum(v >= 21 for v in note_vs) * 2 >= len(note_vs) else 1
    else:
        v_base = 21 if clef == CLEF_BASS else 1
    return _Staff(clef, v_base, keysig, octava, recs[1:])


@dataclass
class _Slot:
    """One rhythmic position in a measure: a note, rest, or chord (same x slot).

    The engine feeds each of a staff's voices separately, so a mixed-duration chord
    (THATSALL bar 3: a 16th stacked on an 8th) advances to the next slot when its
    SHORTEST member ends while the longer ones keep ringing — `advance` is that gap,
    and each note in `notes` carries its own full duration."""
    slot_x: int
    advance: int              # ticks until the staff's next slot (min of the notes)
    is_rest: bool
    notes: List[List[int]]    # [midi, duration_ticks, v, tied] per chord member
    octave: int = 0           # 1 if the slot is under an 8va (the engine never shifts down)


def _decode_measures(staff: _Staff) -> Tuple[List[List[_Slot]], List[Tuple[int, str]]]:
    """Decode a staff into per-measure slot lists, plus (measure_index, label) event marks
    for the tracker: '8^' where a measure carries an 8va, and 'G'/'F' for a mid-staff
    clef change (which swaps the pitch window from that glyph onward — the engine's clef
    handler rewrites its ladder offset mid-walk, and unlike the 8va it is NOT reset at
    the barline; it lasts until the next clef glyph)."""
    measures: List[List[_Slot]] = []
    marks: List[Tuple[int, str]] = []
    cur_clef = staff.clef                      # persists across measures
    for mi, rec in enumerate(staff.records):
        slots: List[_Slot] = []
        measure_acc: Dict[int, int] = {}       # explicit accidentals, per position
        # 8va: the engine SETS the staff's shift to +1 octave when it walks past the glyph
        # and restores the baseline at the measure boundary — so it runs from the glyph's
        # stream position to the end of the measure, and only ever shifts UP (the glyph's
        # placement above or below the notes is purely cosmetic; there is no 8vb).
        oc = 0
        for b0, b1 in rec.entries:
            sym = symbol(b0)
            v = vertical(b0, b1)
            if sym in (CLEF_TREBLE, CLEF_BASS):
                marks.append((mi, "G" if sym == CLEF_TREBLE else "F"))
                cur_clef = sym                 # re-window from this glyph on
                continue
            if sym == SYM_OCTAVA:
                if oc == 0:
                    marks.append((mi, "8^"))
                oc = 1
                continue
            if sym in _TIE_SYMS:
                # The engine's tie handler finds ONE note by searching from the
                # glyph's v (handlers 0x24e5/0x24df); a tied chord carries one
                # glyph per member. Flag the nearest note of the last slot.
                for s in reversed(slots):
                    if not s.is_rest:
                        min(s.notes, key=lambda n: abs(n[2] - v))[3] = True
                        break
                continue
            if sym in (SYM_NATURAL, SYM_SHARP, SYM_FLAT):
                # Mid-measure accidental at this position (0x0C = forced natural). The
                # Entertainer's main theme is the chromatic D-D#-E, so 0x0f (sharp) raises.
                measure_acc[v] = {SYM_NATURAL: 0x0C, SYM_SHARP: 2, SYM_FLAT: -2}[sym]
                continue
            if sym == SYM_DOT:
                # Augmentation dot: the engine dots ONE note — the voice sitting exactly
                # at the glyph's v (handler 0x245c compares slot v verbatim; a chord gets
                # one dot glyph per member, THATSALL m4). It also refuses to dot a 32nd.
                for s in reversed(slots):
                    if not s.is_rest:
                        for note in s.notes:
                            if note[2] == v and note[1] >= 2:
                                note[1] += note[1] // 2
                        s.advance = min(n[1] for n in s.notes)
                        break
                continue
            if _note_value(sym)[0]:                       # note (incl. beamed) or rest
                is_rest, dur = decode_duration(b0)
                if is_rest:
                    slots.append(_Slot(x_slot(b1), dur, True, []))
                    continue
                acc = measure_acc.get(v, staff.keysig[(v - 1) % 7])
                if acc == 0x0C:
                    acc = 0
                midi = staff.midi(v, acc, cur_clef)
                if midi:
                    midi += 12 * oc                       # under an 8va: +1 octave
                if slots and not slots[-1].is_rest and x_slot(b1) == slots[-1].slot_x:
                    slots[-1].notes.append([midi, dur, v, False])   # chord member
                    slots[-1].advance = min(slots[-1].advance, dur)
                else:
                    slots.append(_Slot(x_slot(b1), dur, False,
                                       [[midi, dur, v, False]], octave=oc))
            # markers, unknown symbols: no time, no pitch
        measures.append(slots)
    return measures, marks


def _span(slots: List[_Slot]) -> int:
    """Ticks until every voice of the measure has finished — the engine's barline gate.
    Slots advance by their shortest member; a longer chord member rings past it."""
    t = end = 0
    for s in slots:
        longest = max((n[1] for n in s.notes), default=s.advance)
        end = max(end, t + longest)
        t += s.advance
    return max(end, t)


def parse(path: str) -> Song:
    """Parse an .MCS/.MCD file into a Song. Staves are time-aligned by measure;
    a staff entering a shared measure late (first x slot to the right of the other
    staff's) is front-padded by the measure's deficit."""
    with open(path, "rb") as fh:
        d = fh.read()
    song = Song(title="", source=f"mcs:{path}")

    staff_recs = split_staves(parse_records(d))
    decoded: List[Tuple[str, List[List[_Slot]]]] = []
    staff_marks: List[Tuple[str, List[Tuple[int, str]]]] = []
    staves: List[_Staff] = []
    name_counts: Dict[str, int] = {}
    for si, recs in enumerate(staff_recs):           # all staves (a few songs have 3-4)
        staff = _read_staff(recs)
        staves.append(staff)
        base = {CLEF_TREBLE: "Treble", CLEF_BASS: "Bass"}.get(staff.clef, f"Staff {si}")
        name_counts[base] = name_counts.get(base, 0) + 1
        name = base if name_counts[base] == 1 else f"{base} {name_counts[base]}"
        measures, marks = _decode_measures(staff)
        decoded.append((name, measures))
        staff_marks.append((name, marks))

    def _fills_measure(slots: List[_Slot]) -> bool:
        # Notation convention: a lone whole rest means "rest the whole measure",
        # whatever the meter (BUMBLE's 2/4 bass opens with four of them).
        return len(slots) == 1 and slots[0].is_rest and slots[0].advance == 32

    # The measure grid is the MODAL span (the meter), not the maximum — one long
    # final measure must not stretch every bar of the song (ties break upward so a
    # 4/4 song with many short pickup bars still reads 16).
    spans = [_span(m) for _, ms in decoded for m in ms if m and not _fills_measure(m)]
    if spans:
        measure_len = max(set(spans), key=lambda v: (spans.count(v), v))
    else:
        measure_len = 32
    for _, ms in decoded:
        for m in ms:
            if _fills_measure(m):
                m[0].advance = measure_len

    # Per-measure durations: the grid, stretched only by genuinely longer measures.
    n_meas = max((len(ms) for _, ms in decoded), default=0)
    m_dur = [max([measure_len] + [_span(ms[mi]) for _, ms in decoded if mi < len(ms)])
             for mi in range(n_meas)]
    m_start = [0]
    for dur in m_dur:
        m_start.append(m_start[-1] + dur)

    # 8va spans and mid-staff clef changes -> tracker event markers (at measure start).
    for name, marks in staff_marks:
        for mi, label in marks:
            if mi < len(m_start):
                song.events.append((m_start[mi], name, label))
    song.events.sort()

    # --- display metadata --------------------------------------------------
    if len(d) >= 0x07:
        song.tempo_raw = _u16(d, 0x05)
        song.tempo_level = max(0, (song.tempo_raw - TEMPO_BASE) // TEMPO_STEP)
    if d:
        song.tempo_tick_seconds = tick_seconds_for(d[0])      # real tempo from byte 0
    song.time_signature = _time_signature(measure_len)
    if staves:
        treble = next((s for s in staves if s.clef == CLEF_TREBLE), staves[0])
        song.key_signature = _key_name(treble.keysig)

    for idx, (name, measures) in enumerate(decoded):
        track = Track(name=name)
        for mi, slots in enumerate(measures):
            tick = m_start[mi]
            deficit = m_dur[mi] - _span(slots)
            if slots and deficit > 0:
                other_first = min(
                    (ms[mi][0].slot_x for j, (_, ms) in enumerate(decoded)
                     if j != idx and mi < len(ms) and ms[mi]),
                    default=None)
                if other_first is not None and slots[0].slot_x > other_first:
                    tick += deficit
            for s in slots:
                if s.is_rest:
                    track.add(NoteEvent(start_tick=tick, duration_ticks=s.advance,
                                        midi_note=0, is_rest=True))
                else:
                    for m, dur, _v, tied in s.notes:
                        track.add(NoteEvent(start_tick=tick, duration_ticks=dur,
                                            midi_note=m, tied=tied, octave=s.octave))
                tick += s.advance
        song.add_track(track)
    return song
