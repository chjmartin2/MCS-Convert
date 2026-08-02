"""Universal Song <-> RctSong conversion.

`song_to_rct` patternizes any imported Song (NSF, PT3, MCS...) into the native
tracker format: tone tracks dealt onto the 3 tone channels, noise/drums onto
channel 4, one row per 32nd-note tick (speed 4), 32-row patterns with identical
patterns deduplicated, instruments derived from the waveforms actually used.

`rct_to_universal` goes the other way for the .MCS/notation path: the flattened
performance is quantized back to tick-resolution NoteEvents (nearest semitone
per tick), so effect motion becomes what MCS can express -- and everything the
retrack/encode pipeline already does keeps working untouched.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .audio import _allocate_voices, _note_events
from .effects import flatten
from .model import NoteEvent, Song, Track
from .rct import (MODE_3TONE_NOISE, MODE_4TONE, NOTE_OFF, RctCell,
                  RctInstrument, RctPattern, RctSong, WAVEFORM_IDS,
                  midi_to_note)

_ROWS = 32                       # pattern length: one 4/4 bar of 32nd notes


def song_to_rct(song: Song, tempo_byte0: int = 0x80,
                title: Optional[str] = None) -> RctSong:
    """Patternize a universal Song into an RctSong (speed 4: row = 32nd)."""
    rct = RctSong(title=(title or song.title or "imported")[:32],
                  channel_mode=MODE_3TONE_NOISE, tempo_byte0=tempo_byte0,
                  speed=4)
    rct.patterns.clear()
    rct.instruments.clear()

    # -- instruments: one per waveform used (square first, max 15) ------------
    waves: List[str] = []
    for t in song.tracks:
        if getattr(t, "kind", "tone") == "tone":
            for n in t.notes:
                w = (n.waveform or t.waveform or "square")
                if w in WAVEFORM_IDS and w not in waves:
                    waves.append(w)
    if not waves:
        waves = ["square"]
    inst_of: Dict[str, int] = {}
    for i, w in enumerate(waves[:15], start=1):
        rct.instruments[i] = RctInstrument(name=w, waveform=w, volume=15)
        inst_of[w] = i

    # -- deal tone tracks onto 3 channels; collect noise/drum hits ------------
    tone_tracks, perc = [], []
    for t in song.tracks:
        kind = getattr(t, "kind", "tone")
        if kind in ("noise", "drum"):
            for n in t.notes:
                if not n.is_rest:
                    perc.append((n.start_tick, n.midi_note))
            continue
        tone_tracks.append([(s, d, m, n) for (s, d, m) in
                            _note_events([n for n in t.notes if not n.percussive])
                            for n in [_src(t, s, m)]])
        for n in t.notes:
            if n.percussive and not n.is_rest:
                perc.append((n.start_tick, n.midi_note))
    chans = _allocate_voices([[e[:3] for e in tt] for tt in tone_tracks], n=3)
    src_by = {}
    for tt in tone_tracks:
        for s, d, m, n in tt:
            src_by[(s, m)] = n

    total_ticks = max([e[0] + e[1] for ch in chans for e in ch] +
                      [t + 1 for t, _ in perc] + [1])
    n_pats = (total_ticks + _ROWS - 1) // _ROWS
    total_ticks = n_pats * _ROWS                     # pad to whole patterns, so
    #                                                  a final-bar note-off lands
    #                                                  like every other bar's and
    #                                                  identical bars dedupe

    # -- fill a working grid of patterns, then dedupe -------------------------
    grid = [RctPattern(rows=_ROWS) for _ in range(n_pats)]

    def cell(tick: int, ch: int) -> RctCell:
        return grid[tick // _ROWS].cell(tick % _ROWS, ch)

    for ch, events in enumerate(chans):
        for start, dur, midi in events:
            src = src_by.get((start, midi))
            c = cell(start, ch)
            c.note = midi_to_note(midi)
            wave = ((src.waveform if src is not None else "") or "square")
            c.inst = inst_of.get(wave, 1)
            if src is not None and src.velocity < 100:
                c.vol = max(1, min(16, round(src.velocity * 15 / 127) + 1))
            end = start + dur
            if end < total_ticks:
                off = cell(end, ch)
                if off.note == 0:                     # don't clobber a new note
                    off.note = NOTE_OFF
    seen = set()
    for tick, midi in sorted(perc):
        if tick in seen:                              # one noise hit per tick
            continue
        seen.add(tick)
        c = cell(tick, 3)
        c.note = midi_to_note(midi)
        c.inst = 1
        nxt = tick + 1
        if nxt < total_ticks and cell(nxt, 3).note == 0:
            cell(nxt, 3).note = NOTE_OFF

    # -- dedupe identical patterns via their packed bytes ---------------------
    by_bytes: Dict[bytes, int] = {}
    order: List[int] = []
    for pat in grid:
        key = b"".join(pat.cells[r][c].pack()
                       for r in range(pat.rows) for c in range(4))
        if key not in by_bytes:
            idx = len(by_bytes)
            by_bytes[key] = idx
            rct.patterns[idx] = pat
        order.append(by_bytes[key])
    rct.order = order or [0]
    if not rct.patterns:
        rct.patterns[0] = RctPattern()
    return rct


def _src(track: Track, start: int, midi: int) -> Optional[NoteEvent]:
    for n in track.notes:
        if n.start_tick == start and n.midi_note == midi and not n.is_rest:
            return n
    return None


def rct_to_universal(rct: RctSong) -> Song:
    """Flatten the RctSong and quantize back to tick-resolution NoteEvents for
    the .MCS/notation/retrack pipeline. Effects become their nearest-semitone
    tick contour; the noise channel becomes a kind='noise' track."""
    flat = flatten(rct)
    song = Song(title=rct.title, source="rct")
    ticks = flat.total_subs // 4
    for c, ch in enumerate(flat.channels):
        kind = ch.kind
        tr = Track(name=f"CH{c + 1}" if kind == "tone" else "Noise",
                   kind="tone" if kind == "tone" else "noise")
        # per-tick pitch: the value at the tick's first sub-tick
        cur: Optional[Tuple[int, int, int, str]] = None   # (start, midi, vel, wave)
        for t in range(ticks):
            s = t * 4
            p = ch.pitch[s]
            midi = round(p) if p is not None else None
            onset = any(ch.onset[s:s + 4])
            vel = round(ch.vol[s] * 127 / 15) if p is not None else 0
            wave = ch.wave[s]
            if cur is not None and (midi is None or midi != cur[1] or onset):
                tr.add(NoteEvent(start_tick=cur[0], duration_ticks=t - cur[0],
                                 midi_note=cur[1], velocity=cur[2],
                                 waveform=cur[3]))
                cur = None
            if midi is not None and cur is None:
                cur = (t, midi, vel, wave)
        if cur is not None:
            tr.add(NoteEvent(start_tick=cur[0], duration_ticks=ticks - cur[0],
                             midi_note=cur[1], velocity=cur[2], waveform=cur[3]))
        if kind == "noise":
            for n in tr.notes:
                n.duration_ticks = 1                  # noise = per-hit
        if tr.notes:
            majority = max(set(n.waveform for n in tr.notes if n.waveform) or
                           {"square"}, key=lambda w: sum(
                               1 for n in tr.notes if n.waveform == w))
            tr.waveform = majority or "square"
            song.add_track(tr)
    return song
