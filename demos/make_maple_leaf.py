"""Generate MAPLERAG.MCS — Maple Leaf Rag (Scott Joplin, 1899), A strain, arranged
for Music Construction Set's four voices.

The notes come verbatim from the Mutopia Project's public-domain LilyPond edition
(mutopiaproject.org/ftp/JoplinS/maple/maple.ly, "Reproduction of original edition
(1899)"); a small parser below resolves LilyPond's \\relative octaves. The pickup,
the 15-bar strain, both volta endings, and the full repeat are rendered — with the
final four bars of the second pass under an 8va, so the low echo phrase returns as
a high finale. That is also a deliberate workout for every decoded MCS feature:
key signature (A-flat: 4 flats), mid-measure accidentals with same-position
cancellation (C-flat then C-natural inside one bar), naturals against the key
signature (the D-diminished chords), ties across barlines, chords up to four
notes, both clef windows, and the 8va.

MCS's pitch windows (treble G4..E7, bass B2..G5) are narrower than a piano, so
like the original disk's arrangers we take three liberties, applied mechanically:
  * the lower note of exact-octave doubles is dropped (the 1-bit speaker gains
    nothing from doubling, and it frees voices);
  * the crossover bars 7-8 (which dive to A-flat 1) are lifted two octaves;
  * the handful of remaining right-hand harmony tones that fall below the treble
    window (E-flat/F-flat 4 in the low echo bars) are dropped — their pitches are
    already sounding in the left-hand chords.

Run:  python demos/make_maple_leaf.py     (writes demos/MAPLERAG.MCS and verifies it)
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcs_convert.mcs.writer import build_file, make_entry           # noqa: E402
from mcs_convert.mcs.reader import parse as mcs_parse               # noqa: E402

# --- the A strain, verbatim from Mutopia's maple.ly (Public Domain) ---------------
RH_PICKUP = "r8"
RH_BODY = r"""
  r16 as' <es' es,> as, c <es es,>8 g,16 |
  <es' es,>16 g, bes <es es,> ~ <es es,>4 |
  r16 as, <es' es,> as, c <es es,>8 g,16 |
  <es' es,>16 g, bes <es es,> ~ <es es,>8 r16 <es es,> |
  r16 as, ces <fes fes,> r16 <es es,> r16 <es es,> |
  r16 as, ces <fes fes,> r16 <es es,> r8 |
  r16 as,,, ces as' r as ces as' |
  r16 as ces as' r as ces as' |
  <as as,>8 <as as,> <as as,> <as as,>16 <as as,> ~ |
  <as as,> es f c es <f as,>8 <as, fes>16 ~ |
  <as fes> bes <ces fes,> as bes <c es,>8 as16 |
  <c es,> as <bes es,>8 <as es> r16 <as as,> ~ |
  <as as,>8 <as as,> <as as,> <as as,>16 <as as,> ~ |
  <as as,> es <f as,> c es <f as,>8 <as, fes>16 ~ |
  <as fes> bes <ces fes,> as bes <c es,>8 as16 |
"""
RH_ALT1 = "<c es,> as <bes es,>8 <as es> r8"
RH_ALT2 = "<c es,>16 as <bes es,>8 <as es> r8"

LH_PICKUP = "<es es,>8"
LH_BODY = r"""
  <as as,> <c as es> <c as es> <a a,> |
  <bes bes,> <des g, es> <des g, es> <es, es,> |
  <as as,> <c as es> <c as es> <a a,> |
  <bes bes,> <des g, es> <des g, es> <es, es,> |
  <fes fes,>4 <es es,>8 <es es,> |
  <fes fes,>4 <es es,>8 r |
  as,, r as' r |
  as' r as' r |
  <b as f d> <b as f d> <b as f d> <b as f d> |
  <c as es> <c as es> <c as es> <c as es> |
  <ces as fes> <ces as fes> <c as es> <c as es> |
  <c as es> <des g, es> <c as> r |
  <b, as f d> <b as f d> <b as f d> <b as f d> |
  <c as es> <c as es> <c as es> <c as es> |
  <ces as fes> <ces as fes> <c as es> <c as es> |
"""
LH_ALT1 = "<c as es> <des g, es> <c as> <es, es,>"
LH_ALT2 = "<c' as es> <des g, es> <c as> <a a,>"

# --- a just-big-enough LilyPond \relative parser -----------------------------------
STEP = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
LETTERS = "cdefgab"
NOTE_RE = re.compile(r"([a-g])(isis|eses|is|es|s)?(!*)([',]*)")
TOK_RE = re.compile(
    r"<[^>]+>[0-9]*\.?|~|\||[a-g](?:isis|eses|is|es|s)?!*[',]*[0-9]*\.?|r[0-9]*\.?")
DUR_TICKS = {"16": 2, "8": 4, "4": 8, "2": 16, "1": 32, "8.": 6, "16.": 3, "4.": 12}
ACC = {"is": 1, "es": -1, "s": -1, "isis": 2, "eses": -2, "": 0}


def _resolve(tok: str, ref):
    """One note token in \\relative mode: nearest octave to ref, then '/, marks."""
    m = NOTE_RE.match(tok)
    letter, acc = m.group(1), ACC[m.group(2) or ""]
    shift = m.group(4).count("'") - m.group(4).count(",")
    diff = LETTERS.index(letter) - LETTERS.index(ref[0])
    diff += -7 if diff > 3 else (7 if diff < -3 else 0)
    abs_ix = ref[1] * 7 + LETTERS.index(ref[0]) + diff + shift * 7
    octave = abs_ix // 7
    midi = 12 * (octave + 1) + STEP[letter] + acc
    return (letter, octave, midi, acc)


def parse_lily(text: str, ref, last_dur="8"):
    """-> (bars, ref, last_dur). Each bar = [(ticks, notes|None, tied)], where notes
    are (letter, octave, midi, acc) and None is a rest."""
    bars = [[]]
    for tok in TOK_RE.findall(text):
        if tok == "|":
            bars.append([])
            continue
        if tok == "~":
            tgt = bars[-1] if bars[-1] else bars[-2]
            tgt[-1] = (tgt[-1][0], tgt[-1][1], True)
            continue
        mdur = re.search(r"([0-9]+\.?)$", tok)
        if mdur:
            last_dur = mdur.group(1)
        ticks = DUR_TICKS[last_dur]
        if tok.startswith("r"):
            bars[-1].append((ticks, None, False))
        elif tok.startswith("<"):
            notes, cref = [], ref
            for i, nt in enumerate(tok[1:tok.index(">")].split()):
                cref = _resolve(nt, cref)
                notes.append(cref)
                if i == 0:
                    ref = cref                    # post-chord ref = first chord note
            bars[-1].append((ticks, notes, False))
        else:
            ref = _resolve(tok, ref)
            bars[-1].append((ticks, [ref], False))
    return [b for b in bars if b], ref, last_dur


# --- arrangement: fit Joplin's piano range into MCS's windows ----------------------
def arrange(bars, floor, bar_lifts):
    """Drop exact-octave lower doubles; lift whole bars (bar_lifts, semitones);
    drop what still falls below `floor`. Notes keep (letter, octave, midi, acc)."""
    out = []
    for bi, bar in enumerate(bars):
        lift = bar_lifts.get(bi, 0)
        nb = []
        for ticks, notes, tied in bar:
            if notes is None:
                nb.append((ticks, None, tied))
                continue
            kept = []
            for n in notes:
                if any(k[2] == n[2] + 12 and k[0] == n[0] for k in notes):
                    continue                      # lower note of an octave double
                n2 = (n[0], n[1] + lift // 12, n[2] + lift, n[3])
                if n2[2] < floor:
                    continue                      # below the window: covered by LH
                if not any(k[2] == n2[2] for k in kept):
                    kept.append(n2)
            nb.append((ticks, kept or None, tied))
        out.append(nb)
    return out


# --- MCS emission ------------------------------------------------------------------
G_CLEF, F_CLEF = 0x06, 0x0D
SYM_NAT, SYM_SHARP, SYM_FLAT, SYM_OCTAVA, SYM_TIE = 0x0E, 0x0F, 0x10, 0x12, 0x13
NOTE_SYM = {2: 0x01, 4: 0x02, 6: 0x02, 8: 0x03, 16: 0x04}   # ticks -> note symbol
REST_SYM = {2: 0x08, 4: 0x09, 8: 0x0A, 16: 0x0B}
KEY_FLAT_DEGREES = set("bead")                               # A-flat major

E7_DI, G5_DI = 7 * 7 + 2, 5 * 7 + 4                          # diatonic indices


def v_of(letter: str, octave: int, treble: bool) -> int:
    di = octave * 7 + LETTERS.index(letter)
    return (1 + E7_DI - di) if treble else (21 + G5_DI - di)


def emit_staff(bars, treble: bool, octava_bars=(), untie_bars=()):
    """Bars -> MCS measure entry lists: notes/rests with x from their tick position,
    accidental glyphs when a pitch departs from the measure's running state, tie
    marks after tied slots, and an 8va glyph opening each `octava_bars` measure."""
    rest_v = 13 if treble else 33
    measures = []
    for bi, bar in enumerate(bars):
        entries = []
        acc_state = {}                              # v -> accidental in force
        if bi in octava_bars:
            entries.append(make_entry(SYM_OCTAVA, 3, 0))
        tick = 0
        for ticks, notes, tied in bar:
            x = 1 + (tick * 22) // 16
            if notes is None:
                entries.append(make_entry(REST_SYM[ticks], rest_v, x))
            else:
                glyphs, heads = [], []
                for letter, octave, midi, acc in sorted(notes, key=lambda n: -n[2]):
                    v = v_of(letter, octave, treble)
                    default = -1 if letter in KEY_FLAT_DEGREES else 0
                    if acc_state.get(v, default) != acc:
                        sym = {1: SYM_SHARP, -1: SYM_FLAT, 0: SYM_NAT}[acc]
                        glyphs.append(make_entry(sym, v, max(0, x - 1)))
                        acc_state[v] = acc
                    heads.append(make_entry(NOTE_SYM[ticks], v, x))
                entries.extend(glyphs)
                entries.extend(heads)
                if tied and bi not in untie_bars:
                    for n in notes:                 # one tie glyph per chord member,
                        tv = v_of(n[0], n[1], treble)   # at that note's own v
                        entries.append(make_entry(SYM_TIE, tv, x + 1))
            tick += ticks
        assert tick == 16, f"bar {bi} spans {tick} ticks"
        measures.append(entries)
    return measures


def build_song():
    # Parse the strain (both volta endings continue from the body's reference).
    rh_ref, lh_ref = ("c", 4, 60, 0), ("c", 3, 48, 0)       # \relative c' / c
    rpk, rref, rd = parse_lily(RH_PICKUP, rh_ref)
    rbody, rref, rd = parse_lily(RH_BODY, rref, rd)
    ra1, _, _ = parse_lily(RH_ALT1, rref, rd)
    ra2, _, _ = parse_lily(RH_ALT2, rref, rd)
    lpk, lref, ld = parse_lily(LH_PICKUP, lh_ref)
    lbody, lref, ld = parse_lily(LH_BODY, lref, ld)
    la1, _, _ = parse_lily(LH_ALT1, lref, ld)
    la2, _, _ = parse_lily(LH_ALT2, lref, ld)

    # Pickup bar: leading rests, then the strain's upbeat eighth.
    rh_bar0 = [(8, None, False), (4, None, False)] + rpk[0]
    lh_bar0 = [(8, None, False), (4, None, False)] + lpk[0]

    # pickup + pass 1 + first ending + pass 2 + second ending  (33 bars)
    rh = [rh_bar0] + rbody + ra1 + rbody + ra2
    lh = [lh_bar0] + lbody + la1 + lbody + la2

    # Bars 7-8 dive to A-flat 1: lift the crossover two octaves into the windows.
    rh = arrange(rh, floor=67, bar_lifts={7: 24, 7 + 16: 24})
    lh = arrange(lh, floor=47, bar_lifts={7: 24, 7 + 16: 24})

    # The finale: the second pass's last four bars (the low echo + cadence) return
    # an octave UP — MCS's 8va is per-measure, so each bar carries its own glyph.
    finale = {29, 30, 31, 32}
    untie = {28}            # bar 28's tie would cross into the 8va jump: re-attack

    rh_meas = emit_staff(rh, treble=True, octava_bars=finale, untie_bars=untie)
    lh_meas = emit_staff(lh, treble=False)

    # Clef records: clef glyph + the four flats of A-flat major (B, E, A, D degrees).
    rh_clef = [make_entry(G_CLEF, 16, 14)] + \
        [make_entry(SYM_FLAT, v, 15 + i) for i, v in enumerate((18, 15, 19, 16))]
    lh_clef = [make_entry(F_CLEF, 32, 14)] + \
        [make_entry(SYM_FLAT, v, 15 + i) for i, v in enumerate((33, 37, 34, 38))]

    # Header byte 0 = 0x89 -> ~92 BPM: "It is never right to play Ragtime fast."
    # Maple Leaf Rag is in 2/4 (meter code 0); the tempo is scroll[0] = 0x89.
    return build_file([[rh_clef] + rh_meas, [lh_clef] + lh_meas],
                      time_sig=0, scroll=bytes([0x89, 0x86, 0x86, 0x77, 0x77]))


def main() -> int:
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MAPLERAG.MCS")
    data = build_song()
    with open(out, "wb") as fh:
        fh.write(data)
    song = mcs_parse(out)                        # verify with our own reader
    notes = sum(1 for t in song.tracks for n in t.notes if not n.is_rest)
    print(f"wrote {out} ({len(data)} bytes)")
    print(f"  {song.time_signature}, {song.key_signature}, "
          f"{len(song.tracks)} staves, {notes} notes, "
          f"{sum(1 for t, s, l in song.events if l == '8^')} 8va bars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
