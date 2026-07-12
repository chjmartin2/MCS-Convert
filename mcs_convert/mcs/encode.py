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
from .validate import MAX_STAVES, MAX_X_SLOT as _MAX_X_SLOT
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


def _emit_staff(bars, treble: bool, cap: bool = False):
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
        for tick, advance, midis, ties in bar:
            slot = []
            for base, dotted in _decompose(advance, allow_dot=bool(midis)):
                # x-slots max out at 30 in the corpus; x=31 pushes byte1 toward
                # 0xFF, where an entry can collide with the FF FF record marker.
                x = min(_MAX_X_SLOT, tick)
                if not midis:
                    slot.append(make_entry(_REST_SYM[base], rest_v, x))
                else:
                    for midi in sorted(midis, reverse=True):
                        v, acc = _v_and_acc(midi, treble)
                        if acc_state.get(v, key_state_default) != acc:
                            sym = _SYM_SHARP if acc > 0 else _SYM_NAT
                            slot.append(make_entry(sym, v, x))
                            acc_state[v] = acc
                        slot.append(make_entry(_NOTE_SYM[base], v, x))
                        if dotted:
                            slot.append(make_entry(_SYM_DOT, v,
                                                   min(_MAX_X_SLOT, x + 1)))
                    piece_end = tick + base + (base // 2 if dotted else 0)
                    piece_ties = midis if piece_end < tick + advance else ties
                    for m in piece_ties:            # one tie glyph per tied member,
                        tv, _ = _v_and_acc(m, treble)   # at that note's own v
                        slot.append(make_entry(_SYM_TIE, tv, min(_MAX_X_SLOT, x + 1)))
                tick += base + (base // 2 if dotted else 0)
            # add the whole slot only if it fits the measure buffer intact
            if len(entries) + len(slot) <= limit:
                entries.extend(slot)
            else:
                dropped += 1
        measures.append(entries)
    _emit_staff.last_dropped = dropped
    return measures


def _track_events(track, total_box):
    """(events fitted to a clef window, is_treble) for one track. total_box is a
    one-element list used to accumulate the song's total length."""
    raw, midis = [], []
    for start, dur, midi in _note_events(track.notes):
        if dur <= 0:
            continue
        total_box[0] = max(total_box[0], start + dur)
        raw.append((start, dur, midi))
        midis.append(midi)
    treble = (sorted(midis)[len(midis) // 2] >= 67) if midis else True
    lo, hi = (TREBLE_LO, TREBLE_HI) if treble else (BASS_LO, BASS_HI)
    return [(s, d, _fit(m, lo, hi)) for s, d, m in raw], treble


def encode_song(song: Song, *, bar_ticks: int = 32, tempo_byte0: int = 0x80,
                split: int = 67, by_track: bool = False) -> bytes:
    """Song (32nd-note ticks) -> .MCS bytes.

    Default: fold everything onto a treble+bass grand staff (split at `split`).
    `by_track=True`: give each source track its OWN staff (up to MCS's 4), clef
    chosen by its median pitch — this keeps a busy multi-channel import (NSF, PT3)
    from piling every voice into one measure and overflowing MCS's 32-entry
    per-measure buffer. Extra tracks beyond 4 fold onto the nearest-range staff."""
    total_box = [0]
    if by_track:
        live = [t for t in song.tracks if any(not n.is_rest for n in t.notes)]
        staff_evs = [_track_events(t, total_box) for t in live[:MAX_STAVES]]
        for extra in live[MAX_STAVES:]:                  # fold overflow tracks in
            evs, treble = _track_events(extra, total_box)
            match = next((i for i, (_, tr) in enumerate(staff_evs) if tr == treble),
                         0)
            staff_evs[match] = (staff_evs[match][0] + evs, staff_evs[match][1])
        if not staff_evs:
            staff_evs = [([], True)]
    else:
        treble_ev, bass_ev = [], []
        for tr in song.tracks:
            for start, dur, midi in _note_events(tr.notes):
                if dur <= 0:
                    continue
                total_box[0] = max(total_box[0], start + dur)
                if midi >= split:
                    treble_ev.append((start, dur, _fit(midi, TREBLE_LO, TREBLE_HI)))
                else:
                    bass_ev.append((start, dur, _fit(midi, BASS_LO, BASS_HI)))
        staff_evs = [(treble_ev, True), (bass_ev, False)]

    total = ((total_box[0] + bar_ticks - 1) // bar_ticks) * bar_ticks
    blocks = []
    for evs, treble in staff_evs:
        measures = _emit_staff(_staff_slots(evs, total, bar_ticks), treble=treble,
                               cap=by_track)
        clef = [make_entry(_G_CLEF if treble else _F_CLEF,
                           16 if treble else 32, 14)]
        blocks.append([clef] + measures)
    scroll = bytes([tempo_byte0, 0x86, 0x86, 0x77, 0x77])
    return build_file(blocks, tempo_level=2, scroll=scroll)
