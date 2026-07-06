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
  bits [4:0]   = symbol:
                 0x01..0x05  note: 16th, 8th, quarter, half, whole (2**(n-1) ticks)
                 0x15..0x19  the same five notes, *beamed* (value = symbol - 0x14). The
                             engine dispatches these to the identical duration handlers;
                             fast beamed runs (BUMBLE.MCD) are stored entirely this way.
                 0x06 / 0x0D treble / bass clef glyph
                 0x08..0x0C  rest of the same ladder (n-7)
                 0x0E / 0x0F / 0x10  natural / sharp / flat glyph. In the clef record
                             these build the KEY SIGNATURE (applied at the glyph's
                             staff degree in every octave); inside a measure they set
                             a measure-scoped accidental at that exact position.
                 0x11        augmentation dot: the engine adds half the previous
                             note's duration to it (handler at MCSDISK 0x245c).
                 0x12        in the clef record: 8va for the staff (+12 semitones).
                 0x1F        the FF FF record marker seen as an entry; skipped.

  Pitch: the engine xlats v-1 through a fixed 41-byte grand-staff ladder chosen by
  the two staves' clefs (tables at MCSDISK image 0x5c88..): value = 2 x semitone.
  Accidentals add +/-2, the 8va adds +24, and the result indexes a 68-entry chromatic
  period table whose entry for G4 is PIT divisor 3044 = 392.00 Hz exactly, anchoring
  MIDI = value/2 + 44. Treble staff window = E7..G4, bass = G5..B2 (they overlap by
  an octave; the windows are FIXED — the header scroll bytes are pure view state).
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Tuple

from ..model import NoteEvent, Song, Track

CLEF_TREBLE = 0x06
CLEF_BASS = 0x0D
SYM_NATURAL, SYM_SHARP, SYM_FLAT = 0x0E, 0x0F, 0x10
SYM_DOT = 0x11
SYM_OCTAVA = 0x12         # 8va/8vb: shifts a whole measure ±1 octave (dir = glyph above/below notes)
SYM_TIE = 0x13           # tie/slur mark: carries the preceding note into the next
SYM_MARKER = 0x1F

# The per-clef vertical windows, lifted verbatim from MCSDISK.EXE (image 0x5cb1..):
# 2 x semitone per byte, one byte per staff position top-down, 0 = unusable.
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

# A note can be stored "beamed" — symbols 0x14..0x19 are the same six note values as
# 0x00..0x05 with 0x14 added (the engine's dispatch routes them to the identical
# duration handlers; confirmed by BUMBLE.MCD, whose beamed-16th runs otherwise vanish).
_BEAM_OFFSET = 0x14


def _note_value(sym: int) -> tuple[str, int]:
    """Classify a symbol: ('note'|'rest'|'', value) where value indexes _NOTE_TICKS.

    Notes are 0x00..0x05 (0x00 = 32nd), beamed notes 0x14..0x19, rests 0x07..0x0c
    (0x07 = 32nd rest). The 32nd note/rest were originally dropped, which shortened every
    measure that used them (ALLEGRO, DIE, ...); MCS's palette proves the 32nd is real.
    """
    if _BEAM_OFFSET <= sym <= _BEAM_OFFSET + 5:          # 0x14..0x19 = beamed note
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
    v_base: int                    # treble notes live at v 1..20, bass at v 21..41
    keysig: List[int]              # semitone*2 offset per staff degree (0..6)
    octava: int                    # +24 if the clef record carries an 8va glyph
    records: List[Record]

    def window(self) -> bytes:
        return _TREBLE_WINDOW if self.v_base == 1 else _BASS_WINDOW

    def midi(self, v: int, acc2: int) -> int:
        """MIDI note for vertical position v with accidental offset (2x semitones)."""
        idx = v - self.v_base
        win = self.window()
        if not (0 <= idx < len(win)) or win[idx] == 0:
            return 0
        return (win[idx] + acc2 + self.octava) // 2 + MIDI_ANCHOR


def _read_staff(recs: List[Record]) -> _Staff:
    """Decode a staff's clef record and pick its pitch window. The window follows where
    the notes actually sit (v 1..20 = treble, v 21..41 = bass) rather than staff order,
    so multi-staff songs and a bass line printed first both land in the right octave."""
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
    """One rhythmic position in a measure: a note, rest, or chord (same x slot)."""
    slot_x: int
    duration: int
    is_rest: bool
    midis: List[int]
    tied: bool = False        # a 0x13 tie/slur mark follows this slot
    octave: int = 0           # 8va/8vb applied to this slot: +1 up, -1 down


def _octave_shift_at(oct_marks: List[Tuple[int, int]], x: int) -> int:
    """Octave shift for a note at x-slot `x`, given the measure's 8va/8vb markers.
    The first marker covers the whole measure; a later one switches from its x onward."""
    if not oct_marks:
        return 0
    shift = oct_marks[0][1]                                # first marker = whole measure
    for mx, direction in oct_marks:
        if mx <= x:
            shift = direction
    return shift


def _decode_measures(staff: _Staff) -> Tuple[List[List[_Slot]], List[Tuple[int, str]]]:
    """Decode a staff into per-measure slot lists, plus (measure_index, label) event marks
    for the tracker: '8^'/'8v' where a measure is under 8va/8vb, and 'G'/'F' for a mid-staff
    clef change (a clef change is diagnostic only; pitch re-windowing is not applied)."""
    measures: List[List[_Slot]] = []
    marks: List[Tuple[int, str]] = []
    for mi, rec in enumerate(staff.records):
        # 8va/8vb: a glyph sitting ABOVE the measure's notes shifts it up an octave, BELOW
        # shifts it down; it governs the whole measure (a second glyph switches from its x).
        note_vs = [vertical(b0, b1) for b0, b1 in rec.entries
                   if _note_value(symbol(b0))[0] == "note"]
        med = median(note_vs) if note_vs else 0
        oct_marks = sorted((x_slot(b1), 1 if vertical(b0, b1) < med else -1)
                           for b0, b1 in rec.entries if symbol(b0) == SYM_OCTAVA)
        if oct_marks:
            marks.append((mi, "8^" if oct_marks[0][1] > 0 else "8v"))
        slots: List[_Slot] = []
        measure_acc: Dict[int, int] = {}       # explicit accidentals, per position
        for b0, b1 in rec.entries:
            sym = symbol(b0)
            v = vertical(b0, b1)
            if sym in (CLEF_TREBLE, CLEF_BASS):
                marks.append((mi, "G" if sym == CLEF_TREBLE else "F"))
                continue
            if sym == SYM_OCTAVA:
                continue                                  # applied per note below
            if sym == SYM_TIE:
                if slots:                                 # tie/slur carries the last slot on
                    slots[-1].tied = True
                continue
            if sym in (SYM_NATURAL, SYM_SHARP, SYM_FLAT):
                # Mid-measure accidental at this position (0x0C = forced natural). The
                # Entertainer's main theme is the chromatic D-D#-E, so 0x0f (sharp) raises.
                measure_acc[v] = {SYM_NATURAL: 0x0C, SYM_SHARP: 2, SYM_FLAT: -2}[sym]
                continue
            if sym == SYM_DOT:
                # augmentation dot: engine adds half the note's own duration
                for s in reversed(slots):
                    if not s.is_rest:
                        s.duration += s.duration // 2
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
                oc = _octave_shift_at(oct_marks, x_slot(b1))
                midi = staff.midi(v, acc)
                if midi:
                    midi += 12 * oc                       # 8va/8vb: ±1 octave
                if slots and not slots[-1].is_rest and x_slot(b1) == slots[-1].slot_x:
                    slots[-1].midis.append(midi)          # chord: same 8-px slot
                    slots[-1].duration = max(slots[-1].duration, dur)
                else:
                    slots.append(_Slot(x_slot(b1), dur, False, [midi], octave=oc))
            # markers, unknown symbols: no time, no pitch
        measures.append(slots)
    return measures, marks


def _span(slots: List[_Slot]) -> int:
    return sum(s.duration for s in slots)


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
        return len(slots) == 1 and slots[0].is_rest and slots[0].duration == 32

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
                m[0].duration = measure_len

    # Per-measure durations: the grid, stretched only by genuinely longer measures.
    n_meas = max((len(ms) for _, ms in decoded), default=0)
    m_dur = [max([measure_len] + [_span(ms[mi]) for _, ms in decoded if mi < len(ms)])
             for mi in range(n_meas)]
    m_start = [0]
    for dur in m_dur:
        m_start.append(m_start[-1] + dur)

    # 8va/8vb spans and mid-staff clef changes -> tracker event markers (at measure start).
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
                    track.add(NoteEvent(start_tick=tick, duration_ticks=s.duration,
                                        midi_note=0, is_rest=True, tied=s.tied))
                else:
                    for m in s.midis:
                        track.add(NoteEvent(start_tick=tick, duration_ticks=s.duration,
                                            midi_note=m, tied=s.tied, octave=s.octave))
                tick += s.duration
        song.add_track(track)
    return song
