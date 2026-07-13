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

from collections import defaultdict
from typing import Dict, List, Tuple

from ..audio import _note_events
from ..model import Song
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


def _v_and_acc(midi: int, treble: bool, v_base: int = None) -> Tuple[int, int]:
    """Staff position v and accidental (-1/0/+1) for a MIDI note, sharp-spelled.

    `treble` picks the pitch WINDOW (the clef); `v_base` picks the screen
    POSITION (1 = top staff, 21 = bottom). These are independent — a bass clef
    can sit on the TOP staff (v_base 1), which is exactly how two same-clef staves
    stack as two separate staves instead of overlapping in one position."""
    if v_base is None:
        v_base = 1 if treble else 21             # default: treble on top, bass below
    letter, acc = _SPELL[midi % 12]
    octave = (midi - acc) // 12 - 1
    di = octave * 7 + _LETTERS.index(letter)
    top_di = _E7_DI if treble else _G5_DI        # top of the chosen clef's window
    return v_base + top_di - di, acc


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


# The real per-measure limit is HORIZONTAL POSITIONS, not entries. MCS plays 96+
# entries/measure fine — chords stack vertically on one x-slot for free (proven
# by test files) — so there is NO 32-entry buffer cap. What's finite is the
# x-slot: a 5-bit field, x 0..31. The editor renders ~24 positions across a bar
# cleanly (the 80-song corpus never exceeds 23), so 24 is the default range
# (x 2..25, keeping clear of the barline/clef at x 0..1). "force 32" opens the
# full field (x 0..31) — all 32 play, though MCS draws them cramped and the
# barline-region slots x 0..1 render loosely.
# (x_base, x_placement_end, x_cap): where the first note sits, how wide onsets
# spread proportionally, and the position ceiling when capping an import.
_POS_DEFAULT = (2, 25, 25)           # 24 clean positions
_POS_FORCE32 = (0, 31, 31)           # the full 5-bit field, 32 positions
_X_FIELD_MAX = 31                    # the x-slot is 5 bits; this is the hard wall


def _tick_to_x(tick: int, bar_ticks: int, x_base: int, x_end: int) -> int:
    """Within-measure tick (0..bar_ticks) -> an x-slot in [x_base, x_end]."""
    x = x_base + round(tick * (x_end - x_base) / max(1, bar_ticks))
    return max(x_base, min(x_end, x))


def _emit_staff(bars, treble: bool, cap: bool = False, bar_ticks: int = 32,
                v_base: int = None, x_range=_POS_DEFAULT):
    """Slot lists -> MCS measure entry lists (notes, rests, accidentals, dots, ties).
    `treble` is the clef (pitch window); `v_base` is the screen position (1 = top
    staff, 21 = bottom). `x_range` = (x_base, x_placement_end, x_cap) bounds the
    horizontal slots. With `cap`, onsets past `x_cap` are dropped (24 positions by
    default, 32 in force mode); WITHOUT cap (round-trip / hand-authoring) onsets
    bump right up to the 5-bit field max so nothing is lost. Entries themselves
    are NOT limited — chords stack on one slot freely."""
    if v_base is None:
        v_base = 1 if treble else 21
    x_base, x_place_end, x_cap = x_range
    ceiling = x_cap if cap else _X_FIELD_MAX      # drop at the cap, else fill the field
    rest_v = 13 if v_base == 1 else 33           # rest sits in the staff's position
    key_state_default = 0                        # C major: naturals by default
    measures = []
    dropped = 0
    for bar in bars:
        entries = []
        acc_state: Dict[int, int] = {}
        # x-slots must strictly increase across distinct time positions, or the
        # reader (and real MCS) group different notes into one chord. `last_x`
        # keeps them apart: dense measures pack tight left-to-right, sparse ones
        # keep their proportional spacing.
        last_x = x_base - 1
        for tick, advance, midis, ties in bar:
            if last_x + 1 > ceiling:             # no horizontal room left this bar
                if cap:
                    dropped += 1
                    continue                     # drop the whole onset
            slot = []
            for base, dotted in _decompose(advance, allow_dot=bool(midis)):
                x = min(ceiling, max(_tick_to_x(tick, bar_ticks, x_base, x_place_end),
                                     last_x + 1))
                aux_x = min(ceiling, x + 1)        # dot / tie sit just after
                if not midis:
                    slot.append(make_entry(_REST_SYM[base], rest_v, x))
                    last_x = x
                else:
                    for midi in sorted(midis, reverse=True):
                        v, acc = _v_and_acc(midi, treble, v_base)
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
                        tv, _ = _v_and_acc(m, treble, v_base)   # at that note's own v
                        slot.append(make_entry(_SYM_TIE, tv, aux_x))
                    last_x = aux_x if (dotted or piece_ties) else x
                tick += base + (base // 2 if dotted else 0)
            entries.extend(slot)                 # entries are unlimited (chords free)
        measures.append(entries)
    _emit_staff.last_dropped = dropped
    return measures


# Meters we can notate, shortest measure (most capacity) last. Real MCS reads a
# fixed 32-entry buffer PER MEASURE, so a shorter meter — more, smaller measures
# — gives more total buffer and drops far fewer notes on dense imports (Dr.
# Wily's busiest track keeps 544/611 in 2/4 vs 381 in 4/4). Same tempo, same
# timing; only the barline spacing changes. 6/8 is omitted: its 48-tick measure
# has the LEAST capacity, and we never want to auto-pick it.
_FIT_METERS = (32, 24, 16)                       # 4/4, 3/4, 2/4 (natural first)


def _pick_clefs(events) -> Tuple[bool, bool]:
    """Choose the clef (True=treble G, False=bass F) for the TWO staves of the
    grand staff to match the music's register. If one window already holds ~all
    the notes, use TWO of that clef — both staves then share the same pitch
    window, so notes can be balanced between them with NO octave-folding (Dr.
    Wily lives in B2..A5, so it becomes two bass staves). Only a genuinely
    wide-range song falls back to the classic treble-over-bass split."""
    if not events:
        return True, False
    ms = [m for _, _, m in events]
    n = len(ms)
    in_bass = sum(BASS_LO <= m <= BASS_HI for m in ms)
    in_treble = sum(TREBLE_LO <= m <= TREBLE_HI for m in ms)
    if in_bass >= 0.9 * n and in_bass >= in_treble:
        return False, False                          # two bass staves
    if in_treble >= 0.9 * n:
        return True, True                            # two treble staves
    return True, False                               # wide range: treble + bass


def _balanced_split(events, bar_ticks, a_treble, b_treble):
    """Deal (start,dur,midi) events onto two staves, keeping each measure's load
    even so neither overflows its own 32-entry buffer (two staves = 64 entries a
    measure, but only if they're filled evenly — a pitch-split dumps a low song
    entirely on the bass staff). When the staves share a clef any note can go on
    either, so the balance is exact and lossless; on a treble+bass split a note
    goes to the staff whose window holds it, and only the overlap is balanced."""
    a, b = [], []
    ca, cb = defaultdict(int), defaultdict(int)
    for s, d, m in sorted(events):
        meas = s // bar_ticks
        if a_treble == b_treble:                     # same window: free choice
            use_a = ca[meas] <= cb[meas]
        else:
            a_lo, a_hi = (TREBLE_LO, TREBLE_HI) if a_treble else (BASS_LO, BASS_HI)
            b_lo, b_hi = (TREBLE_LO, TREBLE_HI) if b_treble else (BASS_LO, BASS_HI)
            in_a, in_b = a_lo <= m <= a_hi, b_lo <= m <= b_hi
            if in_a and not in_b:
                use_a = True
            elif in_b and not in_a:
                use_a = False
            else:                                    # both fit (overlap): balance
                use_a = ca[meas] <= cb[meas]
        (a if use_a else b).append((s, d, m))
        (ca if use_a else cb)[meas] += 1
    return a, b


def _monophonic(events):
    """Collapse overlapping voices to a single melodic line: at every tick the
    highest sounding pitch wins (the melody usually rides on top), and lower
    notes are silenced while it sounds. For the PC-speaker 1-voice target, where
    MCS can only sound one note at a time. (A first cut — 'highest wins' loses an
    inner melody that dips below an accompaniment; smarter voice-leading later.)"""
    if not events:
        return []
    end = max(s + d for s, d, m in events)
    pitch = [None] * end
    for s, d, m in events:
        for t in range(s, min(end, s + d)):
            if pitch[t] is None or m > pitch[t]:
                pitch[t] = m
    out, i = [], 0
    while i < end:
        if pitch[i] is None:
            i += 1
            continue
        j = i
        while j < end and pitch[j] == pitch[i]:
            j += 1
        out.append((i, j - i, pitch[i]))
        i = j
    return out


def _staff_for(events, total, bar_ticks, treble, cap, v_base=None,
               x_range=_POS_DEFAULT):
    """(start,dur,midi) events -> a staff (clef record + measure lists), fitting
    pitches into the clef's window at screen position `v_base` (1 = top, 21 =
    bottom). The clef GLYPH follows the clef (G/F) but sits at the position's
    height (v 16 top, 32 bottom), so a bass-clef top staff draws correctly.
    `x_range` bounds the horizontal slots (see _emit_staff). Drops reported via
    _emit_staff.last_dropped."""
    if v_base is None:
        v_base = 1 if treble else 21
    lo, hi = (TREBLE_LO, TREBLE_HI) if treble else (BASS_LO, BASS_HI)
    fitted = [(s, d, _fit(m, lo, hi)) for s, d, m in events]
    measures = _emit_staff(_staff_slots(fitted, total, bar_ticks), treble,
                           cap=cap, bar_ticks=bar_ticks, v_base=v_base,
                           x_range=x_range)
    clef = make_entry(_G_CLEF if treble else _F_CLEF, 16 if v_base == 1 else 32, 14)
    return [[clef]] + measures                   # clef is its own opening measure


def encode_song(song: Song, *, bar_ticks: int = 32, tempo_byte0: int = 0x80,
                split: int = 67, cap: bool = False,
                fit_meter: bool = False, balance: bool = False,
                voices: int = 6, force32: bool = False) -> bytes:
    """Song (32nd-note ticks) -> .MCS bytes on a two-staff grand staff — the
    layout real MCS renders (a top and a bottom staff). Both staves' clefs are
    free, so they need not be treble-over-bass.

    Default: a fixed treble+bass pitch-split (clean notation, used for round-trip
    and hand-authoring). `balance=True` (automated imports) instead picks BOTH
    clefs to match the register (a low song becomes two bass staves) and deals
    notes so each measure fills evenly. Dr. Wily goes from ~62% kept to ~100%.

    `voices` targets an output chip's polyphony: 1 collapses to a single melodic
    line (PC-speaker 1-voice), 3 suits a Tandy/PCjr (3 tones), 4 the PC-speaker
    4-voice multiplex. Our chiptune sources are already <=3 voices.

    A measure holds a fixed number of horizontal POSITIONS (onsets), not entries —
    chords stack on one slot for free. `force32=False` uses the clean 24 the
    editor renders comfortably; `force32=True` opens the full 32 the x-slot field
    allows (plays, but MCS draws it cramped). `cap=True` drops onsets past that
    edge; `fit_meter=True` picks the meter that keeps the most notes."""
    x_range = _POS_FORCE32 if force32 else _POS_DEFAULT
    if fit_meter:
        # Phase 1: prefer a LOSSLESS natural pitch-split (each voice in its own
        # register, no cross-staff reshuffling) at the longest meter that fits.
        # A sparse song (a Zelda theme) lands here — 2/4 pitch-split, untouched.
        for bt in _FIT_METERS:
            data = encode_song(song, bar_ticks=bt, tempo_byte0=tempo_byte0,
                               split=split, cap=cap, balance=False, voices=voices,
                               force32=force32)
            if encode_song.last_dropped == 0:
                return data
        # Phase 2: nothing fits naturally (a dense song like Dr. Wily) — allow
        # rebalancing across two register-matched staves; take the fewest drops.
        best = None
        for bt in _FIT_METERS:
            data = encode_song(song, bar_ticks=bt, tempo_byte0=tempo_byte0,
                               split=split, cap=cap, balance=balance, voices=voices,
                               force32=force32)
            dropped = encode_song.last_dropped
            if best is None or dropped < best[1]:
                best = (data, dropped)
        return best[0]                           # fewest-drop (2/4 balanced)

    # All sounding events (percussion excluded — no home on a pitched staff).
    events = [(s, d, m) for tr in song.tracks
              for s, d, m in _note_events([n for n in tr.notes if not n.percussive])
              if d > 0]
    if voices <= 1:
        events = _monophonic(events)
    total = max((s + d for s, d, m in events), default=0)
    total = ((total + bar_ticks - 1) // bar_ticks) * bar_ticks
    scroll = bytes([tempo_byte0, 0x86, 0x86, 0x77, 0x77])
    time_sig = {16: 0, 24: 3, 32: 1, 48: 2}.get(bar_ticks, 1)   # else 4/4

    def emit(a_ev, b_ev, a_treble, b_treble):
        # Staff A is the TOP staff (v-positions 1..20), staff B the BOTTOM
        # (21..41) — independent of clef, so two bass staves stack as two staves,
        # not one heap. Returns (staves, dropped-slot count).
        top = _staff_for(a_ev, total, bar_ticks, a_treble, cap, v_base=1,
                         x_range=x_range)
        d = _emit_staff.last_dropped
        bottom = _staff_for(b_ev, total, bar_ticks, b_treble, cap, v_base=21,
                            x_range=x_range)
        return [top, bottom], d + _emit_staff.last_dropped

    if voices <= 1:                              # single melodic line -> one staff
        treble = _pick_clefs(events)[0]
        staves, dropped = emit(events, [], treble, treble)
    else:
        # The natural treble-over-bass pitch-split (clean, each note in its own
        # register). Only if it OVERFLOWS a measure do we rebalance — spreading a
        # low, dense song across two register-matched staves. A sparse song like
        # a Zelda theme keeps the pitch-split untouched (rebalancing would only
        # reshuffle voices across staves and muddle its tie chains for nothing).
        staves, dropped = emit([e for e in events if e[2] >= split],
                               [e for e in events if e[2] < split], True, False)
        if balance and dropped > 0:
            at, bt = _pick_clefs(events)
            ba, bb = _balanced_split(events, bar_ticks, at, bt)
            bstaves, bdropped = emit(ba, bb, at, bt)
            if bdropped < dropped:               # keep balance only if it helps
                staves, dropped = bstaves, bdropped

    encode_song.last_dropped = dropped
    return build_file(staves, time_sig=time_sig, scroll=scroll)
