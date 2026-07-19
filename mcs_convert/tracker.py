"""Render a decoded Song as a 4-voice tracker grid — a diagnostic view.

MCS plays up to four simultaneous voices through the PC speaker. This lays the decoded
notes on a fixed time grid (32nd-note rows — MCS's finest note value; one row per tick)
with four columns holding the sounding notes ranked **highest to lowest** at each row.

Each cell is `PITCH:DUR` at the note's onset (blank while it sustains); a rest is `R:DUR`.
DUR is the note-value denominator — `32 16 8 4 2 1` for 32nd…whole, a trailing `.` for
dotted, and `!N` (raw ticks) for anything that isn't a clean value, so a decode glitch
stands out. A trailing `~` marks a tie/slur. A leading **Evt** column carries per-measure
events: `8^` for an 8va span (MCS only shifts up) and `G`/`F` for a mid-staff clef change.

It's a plain grid so it diffs cleanly against a transcription of the real hardware output.
"""

from __future__ import annotations

from typing import List, Tuple

from .model import NoteEvent, Song
from .pitch import midi_to_name

# time_signature "N/D" -> thirty-second-ticks per measure (32 thirty-seconds = a whole note).
_TS_DEN = {"1": 32, "2": 16, "4": 8, "8": 4, "16": 2, "32": 1}

# duration in 32nd-ticks -> note-value denominator; dotted values carry a trailing dot.
_DUR = {1: "32", 2: "16", 4: "8", 8: "4", 16: "2", 32: "1"}
_DOT = {3: "16.", 6: "8.", 12: "4.", 24: "2.", 48: "1."}


def _dur_label(ticks: int) -> str:
    """Note-value name for a tick count: denominator, dotted, or `!N` when it's irregular."""
    return _DUR.get(ticks) or _DOT.get(ticks) or f"!{ticks}"


def _note_label(n: NoteEvent) -> str:
    tie = "~" if n.tied else ""
    head = "R" if n.is_rest else midi_to_name(n.midi_note)
    return f"{head}:{_dur_label(n.duration_ticks)}{tie}"


def _measure_ticks(time_signature: str) -> int:
    try:
        num, den = time_signature.split("/")
        return int(num) * _TS_DEN[den]
    except (ValueError, KeyError):
        return 32                                    # sensible 4/4 default (in 32nd-ticks)


def track_columns(song: Song) -> List[str]:
    """Column headers for the universal per-track view: the tracks' names, with
    kind markers (noise: ⌁, drum: ◆) so the non-tone voices self-identify."""
    marks = {"noise": " ⌁", "drum": " ◆"}
    return [t.name + marks.get(getattr(t, "kind", "tone"), "")
            for t in song.tracks]


def tracker_rows_universal(song: Song, subdiv: int = 1):
    """The UNIVERSAL tracker grid: one column PER TRACK (however many the song
    carries — noise and drum tracks included), each cell that track's own note.
    A cell is `PITCH:DUR` at onset with a trailing `*` when the note carries
    effects (ornaments, slides, envelopes... hover the export dialog for the
    full nomenclature); rows are (label, is_measure_start, evt, [cells...])."""
    ntracks = len(song.tracks)
    events = []                                      # (row, endrow, col, label)
    for col, tr in enumerate(song.tracks):
        for n in tr.notes:
            s = n.start_tick * subdiv
            e = (n.start_tick + n.duration_ticks) * subdiv
            if e <= s:
                continue
            label = _note_label(n)
            if n.effects:
                label += "*"
            events.append((s, e, col, label))
    if not events:
        return []
    end = max(e for _, e, _, _ in events)
    rows_per_measure = _measure_ticks(song.time_signature) * subdiv
    evt_by_row: dict = {}
    for tick, _staff, label in getattr(song, "events", []):
        evt_by_row.setdefault(tick * subdiv, [])
        if label not in evt_by_row[tick * subdiv]:
            evt_by_row[tick * subdiv].append(label)
    onset_by_row: dict = {}
    for s, e, col, label in events:
        onset_by_row.setdefault(s, []).append((col, label))
    rows = []
    for r in range(end):
        cols = [""] * ntracks
        for col, label in onset_by_row.get(r, ()):
            cols[col] = label
        measure, step = divmod(r, rows_per_measure) if rows_per_measure else (0, r)
        is_bar = step == 0
        label = f"{measure + 1:>3}" if is_bar else ("" if step % subdiv else f".{step}")
        rows.append((label, is_bar, " ".join(evt_by_row.get(r, [])), cols))
    return rows


def tracker_rows(song: Song, subdiv: int = 1) -> List[Tuple[str, bool, str, List[str]]]:
    """Grid rows for `song`. subdiv = rows per 32nd-tick (1 → one row per 32nd).

    Each row is (label, is_measure_start, evt, [col0..col3]); columns rank highest→lowest,
    each cell is `PITCH:DUR`/`R:DUR` at onset (blank while held), and `evt` holds the
    measure's 8va/clef marker (empty on non-event rows).
    """
    events = []                                      # (start_row, end_row, midi, is_rest, label)
    for tr in song.tracks:
        for n in tr.notes:
            s = n.start_tick * subdiv
            e = (n.start_tick + n.duration_ticks) * subdiv
            if e > s:
                events.append((s, e, n.midi_note, n.is_rest, _note_label(n)))
    if not events:
        return []
    end = max(e for _, e, _, _, _ in events)
    rows_per_measure = _measure_ticks(song.time_signature) * subdiv

    evt_by_row: dict = {}                            # 8va/clef markers keyed by grid row
    for tick, _staff, label in getattr(song, "events", []):
        evt_by_row.setdefault(tick * subdiv, [])
        if label not in evt_by_row[tick * subdiv]:
            evt_by_row[tick * subdiv].append(label)

    rows: List[Tuple[str, bool, str, List[str]]] = []
    for r in range(end):
        sounding = sorted(((m, lbl, s == r) for s, e, m, rest, lbl in events
                           if not rest and s <= r < e), key=lambda x: -x[0])
        resting = [(lbl, s == r) for s, e, m, rest, lbl in events if rest and s <= r < e]
        cols = ["", "", "", ""]
        col = 0
        for _midi, lbl, onset in sounding:           # sounding notes fill the top columns
            if col >= 4:
                break
            cols[col] = lbl if onset else ""          # label at onset, blank while held
            col += 1
        for lbl, onset in resting:                   # then resting voices
            if col >= 4:
                break
            cols[col] = lbl if onset else ""
            col += 1
        measure, step = divmod(r, rows_per_measure) if rows_per_measure else (0, r)
        is_bar = step == 0
        label = f"{measure + 1:>3}" if is_bar else ("" if step % subdiv else f".{step}")
        evt = " ".join(evt_by_row.get(r, []))
        rows.append((label, is_bar, evt, cols))
    return rows


def tracker_text(song: Song, subdiv: int = 1, max_rows: int | None = None) -> str:
    """The tracker as monospaced text (separator line between measures)."""
    rows = tracker_rows(song, subdiv)
    if max_rows:
        rows = rows[:max_rows]
    sep = "-----+-----+--------+--------+--------+--------+"
    out = ["  bar | evt |   v1   |   v2   |   v3   |   v4   |", sep]
    for label, is_bar, evt, cols in rows:
        if is_bar and out and not out[-1].startswith("-"):
            out.append(sep)
        cells = "|".join(f"{c:^8}" for c in cols)
        out.append(f"{label:>4} |{evt:^5}|{cells}|")
    return "\n".join(out)
