"""Render a decoded Song as a 4-voice tracker grid — a diagnostic view.

MCS plays up to four simultaneous voices through the PC speaker. This lays the decoded
notes on a fixed time grid (32nd-note rows — MCS's finest note value; one row per tick)
with four columns holding the sounding notes ranked **highest to lowest** at each row.
A note's
name is printed once, at its onset; while it sustains the cell is blank; a rest onset shows
`R`. Measure boundaries are marked so it lines up with a written score.

It's a plain grid so it diffs cleanly against a transcription of the real hardware output.
"""

from __future__ import annotations

from typing import List, Tuple

from .model import Song
from .pitch import midi_to_name

# time_signature "N/D" -> thirty-second-ticks per measure (32 thirty-seconds = a whole note).
_TS_DEN = {"1": 32, "2": 16, "4": 8, "8": 4, "16": 2, "32": 1}


def _measure_ticks(time_signature: str) -> int:
    try:
        num, den = time_signature.split("/")
        return int(num) * _TS_DEN[den]
    except (ValueError, KeyError):
        return 16                                    # sensible 4/4 default


def tracker_rows(song: Song, subdiv: int = 1) -> List[Tuple[str, bool, List[str]]]:
    """Grid rows for `song`. subdiv = rows per 32nd-tick (1 → one row per 32nd).

    Each row is (label, is_measure_start, [col0..col3]) with columns highest→lowest.
    A cell holds a note name only where that note begins; blank while it sustains; 'R'
    marks a rest onset when nothing else sounds in that slot.
    """
    events = []                                      # (start_row, end_row, midi, is_rest)
    for tr in song.tracks:
        for n in tr.notes:
            s = n.start_tick * subdiv
            e = (n.start_tick + n.duration_ticks) * subdiv
            if e > s:
                events.append((s, e, n.midi_note, n.is_rest))
    if not events:
        return []
    end = max(e for _, e, _, _ in events)
    rows_per_measure = _measure_ticks(song.time_signature) * subdiv

    rows: List[Tuple[str, bool, List[str]]] = []
    for r in range(end):
        sounding = sorted(((m, s == r) for s, e, m, rest in events
                           if not rest and s <= r < e), key=lambda x: -x[0])
        resting = [s == r for s, e, m, rest in events if rest and s <= r < e]
        cols = ["", "", "", ""]
        col = 0
        for midi, onset in sounding:                 # sounding notes fill the top columns
            if col >= 4:
                break
            cols[col] = midi_to_name(midi) if onset else ""   # name at onset, blank while held
            col += 1
        for onset in resting:                        # then resting voices, each as R
            if col >= 4:
                break
            cols[col] = "R" if onset else ""          # R at the rest's onset, blank while held
            col += 1
        measure, step = divmod(r, rows_per_measure) if rows_per_measure else (0, r)
        is_bar = step == 0
        label = f"{measure + 1:>3}" if is_bar else ("" if step % subdiv else f".{step}")
        rows.append((label, is_bar, cols))
    return rows


def tracker_text(song: Song, subdiv: int = 1, max_rows: int | None = None) -> str:
    """The tracker as monospaced text (blank line between measures)."""
    rows = tracker_rows(song, subdiv)
    if max_rows:
        rows = rows[:max_rows]
    out = ["  bar |  v1  |  v2  |  v3  |  v4  |", "-----+------+------+------+------+"]
    for label, is_bar, cols in rows:
        if is_bar and out and not out[-1].startswith("-"):
            out.append("-----+------+------+------+------+")
        cells = "|".join(f"{c:^6}" for c in cols)
        out.append(f"{label:>4} |{cells}|")
    return "\n".join(out)
