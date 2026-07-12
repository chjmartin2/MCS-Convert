"""Encode a Song (the neutral note-event model) into a playable .MCS file.

This is the general writer the converter pipeline needs: where writer.py assembles
hand-authored entries, this module takes *any* Song whose ticks are 32nds — from the
MIDI importer, the (future) NSF extractor, or code — and makes the arrangement
decisions MCS's format forces:

  * staff split: notes at/above G4 go on the treble staff (window G4..E7), the rest
    on the bass staff (window B2..G5), octave-shifted into range when necessary;
  * voice cap: at most 3 simultaneous notes per staff (MCS runs six voices total);
  * slot timing: MCS advances a staff by note durations, so silence becomes explicit
    rests, and each onset group becomes one chord slot lasting until the next onset —
    notes that sustain past it continue as TIED chord members in following slots
    (the player's synth merges tied same-pitch chains back into seamless notes);
  * barlines: slots never cross them; sustains tie across instead;
  * durations: decomposed into MCS note values (32nd..whole, dotted) with ties;
  * spelling: sharps, with measure-scoped accidental glyphs exactly like a typist
    in the real program would place them (state per staff position per measure).
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from ..audio import _note_events
from ..model import Song
from .validate import MAX_X_SLOT as _MAX_X_SLOT
from .writer import build_file, make_entry

# Symbols (see reader.py / docs/mcs-format.md).
_G_CLEF, _F_CLEF = 0x06, 0x0D
_SYM_NAT, _SYM_SHARP, _SYM_DOT, _SYM_TIE = 0x0E, 0x0F, 0x11, 0x13
_NOTE_SYM = {1: 0x00, 2: 0x01, 4: 0x02, 8: 0x03, 16: 0x04, 32: 0x05}
_REST_SYM = {1: 0x07, 2: 0x08, 4: 0x09, 8: 0x0A, 16: 0x0B, 32: 0x0C}

_LETTERS = "cdefgab"
# MIDI pitch-class -> (letter, accidental) with sharp spelling.
_SPELL = {0: ("c", 0), 1: ("c", 1), 2: ("d", 0), 3: ("d", 1), 4: ("e", 0),
          5: ("f", 0), 6: ("f", 1), 7: ("g", 0), 8: ("g", 1), 9: ("a", 0),
          10: ("a", 1), 11: ("b", 0)}
_E7_DI, _G5_DI = 7 * 7 + 2, 5 * 7 + 4          # diatonic indices of the window tops

TREBLE_LO, TREBLE_HI = 67, 100                 # G4..E7
BASS_LO, BASS_HI = 47, 79                      # B2..G5

# The real program's song buffer: the two largest corpus songs (SOCKHOP, BRIDGE)
# are EXACTLY this many bytes with different staff splits — the editor's hard cap.
# (The manual's "~800 notes" is this same budget counted in noteheads; entries
# also include rests, ties, dots, and accidentals.) Our own player has no limit;
# this matters when a converted song should load in MCS 1984 itself.
MCS_MAX_BYTES = 4246


def _fit(midi: int, lo: int, hi: int) -> int:
    """Octave-shift a pitch into [lo, hi]."""
    while midi < lo:
        midi += 12
    while midi > hi:
        midi -= 12
    return midi


def _v_and_acc(midi: int, treble: bool) -> Tuple[int, int]:
    """Staff position v and accidental (-1/0/+1) for a MIDI note, sharp-spelled."""
    letter, acc = _SPELL[midi % 12]
    octave = (midi - acc) // 12 - 1
    di = octave * 7 + _LETTERS.index(letter)
    v = (1 + _E7_DI - di) if treble else (21 + _G5_DI - di)
    return v, acc


def _decompose(ticks: int, allow_dot: bool = True) -> List[Tuple[int, bool]]:
    """Split a duration into MCS-representable pieces [(base_ticks, dotted)...],
    longest first; the caller ties the pieces together. Rests can't be dotted
    (the engine's dot glyph extends a sounding note), so they use plain pieces."""
    out = []
    for base in (32, 16, 8, 4, 2, 1):
        while ticks >= base:
            if allow_dot and ticks == base + base // 2 and base >= 2:
                out.append((base, True))
                ticks -= base + base // 2
            else:
                out.append((base, False))
                ticks -= base
    return out


def _staff_slots(events: List[Tuple[int, int, int]], total: int, bar: int,
                 max_voices: int = 4):
    """(start, dur, midi) events -> per-bar slot lists for ONE staff.

    Each distinct onset becomes a chord slot lasting until the next onset (or bar
    end); sustains continue as tied members of following slots. Returns
    bars[bar_index] = [(tick_in_bar, advance, [midi...], tie_out)], with rests as
    empty midi lists."""
    n_bars = (total + bar - 1) // bar
    onsets: Dict[int, List[Tuple[int, int]]] = {}
    ends = set()
    for start, dur, midi in events:
        onsets.setdefault(start, []).append((midi, start + dur))
        ends.add(start + dur)                    # endings cut too, so rests appear
    cuts = sorted(set(onsets) | ends | {b * bar for b in range(n_bars + 1)})
    bars: List[list] = [[] for _ in range(n_bars)]
    active: List[Tuple[int, int]] = []           # (midi, end_tick)
    prev_keep: set = set()
    for i, t in enumerate(cuts):
        if t >= total:
            break
        nxt = cuts[i + 1] if i + 1 < len(cuts) else total
        active = [(m, e) for m, e in active if e > t]
        for m, e in onsets.get(t, []):
            active = [a for a in active if a[0] != m]    # same pitch: re-attack
            active.append((m, e))
        # Voice cap: keep the extremes (the staff's melody and bass), and among the
        # middle prefer notes we already kept — a sustained note must never drop out
        # and reappear, so anything dropped is truncated for good.
        sounding = sorted(active, key=lambda a: -a[0])
        if len(sounding) > max_voices:
            mid = sounding[1:-1]
            mid.sort(key=lambda a: (a not in prev_keep, -a[0]))
            sounding = [sounding[0]] + mid[:max_voices - 2] + [sounding[-1]]
            sounding.sort(key=lambda a: -a[0])
            active = list(sounding)
        prev_keep = set(sounding)
        advance = nxt - t
        midis = [m for m, _ in sounding]
        ties = [m for m, e in sounding if e > nxt]
        bars[t // bar].append((t % bar, advance, midis, ties))
    return bars


# Real MCS reads each measure into a fixed 32-entry buffer (see mcs/validate.py);
# more than this overflows it and corrupts playback. We stop a measure a hair
# under, so notes + their accidentals/ties always land as a complete unit.
_MAX_ENTRIES_PER_MEASURE = 32

# The horizontal note area of a measure. Real corpus notes NEVER use x-slot 0 or 1
# (reserved for the barline/clef); the first note sits at x 2-5 and a full bar
# spreads to about x 25. A note at x 0 is mishandled by real MCS (mis-drawn and
# mis-timed). So we map the within-measure tick position into this range, never
# the raw tick (which put every downbeat at x 0).
_X_BASE, _X_END = 2, 25


def _tick_to_x(tick: int, bar_ticks: int) -> int:
    """Within-measure tick (0..bar_ticks) -> a valid corpus-range x-slot."""
    x = _X_BASE + round(tick * (_X_END - _X_BASE) / max(1, bar_ticks))
    return max(_X_BASE, min(_MAX_X_SLOT, x))


def _emit_staff(bars, treble: bool, cap: bool = False, bar_ticks: int = 32):
    """Slot lists -> MCS measure entry lists (notes, rests, accidentals, dots, ties).
    With `cap`, a measure is limited to the real-MCS buffer size; once full, further
    onsets in that bar are dropped (whole slot at a time) rather than overflow and
    corrupt playback. Automated imports cap; hand-authoring/round-trip do not (the
    validator flags any overflow there instead of silently dropping)."""
    rest_v = 13 if treble else 33
    key_state_default = 0                        # C major: naturals by default
    measures = []
    dropped = 0
    limit = _MAX_ENTRIES_PER_MEASURE if cap else 10 ** 9
    for bar in bars:
        entries = []
        acc_state: Dict[int, int] = {}
        # x-slots must strictly increase across distinct time positions, or the
        # reader (and real MCS) group different notes into one chord. `last_x`
        # keeps them apart: dense measures pack tight left-to-right, sparse ones
        # keep their proportional spacing.
        last_x = _X_BASE - 1
        for tick, advance, midis, ties in bar:
            slot = []
            for base, dotted in _decompose(advance, allow_dot=bool(midis)):
                x = min(_MAX_X_SLOT, max(_tick_to_x(tick, bar_ticks), last_x + 1))
                aux_x = min(_MAX_X_SLOT, x + 1)     # dot / tie sit just after
                if not midis:
                    slot.append(make_entry(_REST_SYM[base], rest_v, x))
                    last_x = x
                else:
                    for midi in sorted(midis, reverse=True):
                        v, acc = _v_and_acc(midi, treble)
                        if acc_state.get(v, key_state_default) != acc:
                            sym = _SYM_SHARP if acc > 0 else _SYM_NAT
                            slot.append(make_entry(sym, v, x))
                            acc_state[v] = acc
                        slot.append(make_entry(_NOTE_SYM[base], v, x))
                        if dotted:
                            slot.append(make_entry(_SYM_DOT, v, aux_x))
                    piece_end = tick + base + (base // 2 if dotted else 0)
                    piece_ties = midis if piece_end < tick + advance else ties
                    for m in piece_ties:            # one tie glyph per tied member,
                        tv, _ = _v_and_acc(m, treble)   # at that note's own v
                        slot.append(make_entry(_SYM_TIE, tv, aux_x))
                    last_x = aux_x if (dotted or piece_ties) else x
                tick += base + (base // 2 if dotted else 0)
            # add the whole slot only if it fits the measure buffer intact
            if len(entries) + len(slot) <= limit:
                entries.extend(slot)
            else:
                dropped += 1
        measures.append(entries)
    _emit_staff.last_dropped = dropped
    return measures


def encode_song(song: Song, *, bar_ticks: int = 32, tempo_byte0: int = 0x80,
                split: int = 67, cap: bool = False) -> bytes:
    """Song (32nd-note ticks) -> .MCS bytes on a treble+bass grand staff (the
    2-staff layout every corpus song uses, so real MCS always loads it).

    `cap=True` (used by automated imports) limits each measure to real MCS's
    32-entry buffer, dropping the densest overflow rather than corrupting
    playback. The default path stays lossless for hand-authoring/round-trips;
    the validator flags any overflow there instead of silently dropping."""
    treble_ev, bass_ev = [], []
    total = 0
    for tr in song.tracks:
        # merge tied chains first: the encoder re-splits sustains itself, so a
        # pre-tied input (e.g. a re-encoded MCS file) must arrive as whole notes
        for start, dur, midi in _note_events(tr.notes):
            if dur <= 0:
                continue
            total = max(total, start + dur)
            if midi >= split:
                treble_ev.append((start, dur, _fit(midi, TREBLE_LO, TREBLE_HI)))
            else:
                bass_ev.append((start, dur, _fit(midi, BASS_LO, BASS_HI)))
    total = ((total + bar_ticks - 1) // bar_ticks) * bar_ticks
    tre = _emit_staff(_staff_slots(treble_ev, total, bar_ticks), True, cap=cap,
                      bar_ticks=bar_ticks)
    bas = _emit_staff(_staff_slots(bass_ev, total, bar_ticks), False, cap=cap,
                      bar_ticks=bar_ticks)
    clef_t = [make_entry(_G_CLEF, 16, 14)]
    clef_b = [make_entry(_F_CLEF, 32, 14)]
    scroll = bytes([tempo_byte0, 0x86, 0x86, 0x77, 0x77])
    # meter code from the bar length: 16=2/4, 24=3/4, 32=4/4 (48=6/8). Default 4/4.
    time_sig = {16: 0, 24: 3, 32: 1, 48: 2}.get(bar_ticks, 1)
    return build_file([[clef_t] + tre, [clef_b] + bas],
                      time_sig=time_sig, scroll=scroll)
