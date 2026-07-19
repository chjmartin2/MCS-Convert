"""Retrack: constrain a universal Song to a TARGET's capabilities.

The universal tracker (model.py) captures everything an import expressed —
any number of tracks, noise voices, waveforms, duties, velocities, effects.
Real outputs can't play all of that, so the reduction happens HERE, at export
time (never at import): retrack(song, target) returns a NEW Song shaped to
what the target hardware/format can actually do. The GUI's "Retrack" button
runs the same function and reloads the result into the tracker, so what you
see is exactly what the export will contain.

Targets:
    "mcs"     4 tone voices on the notation grid, squares only, noise/drums ->
              register-extreme drum clicks (the classic converter behaviour).
    "tandy"   3 square voices + the SN76489 noise channel (drums as percussive
              hits with the bright/dark split).
    "1voice"  the top melodic line only (PC speaker beeper).
    "4voice"  3 tone voices + the LFSR noise voice (the software-mixed speaker
              and the MCS drive) — waveforms collapse to square unless the mix
              rate is high enough to model them (the exporter decides).
    "sb"      3 tone voices + noise, but waveforms SURVIVE (a real DAC).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import drums
from .model import KIND_DRUM, KIND_NOISE, KIND_TONE, NoteEvent, Song, Track

#: noise-track midi at/above this = a bright hit (hi-hat); below = dark (kick).
#: Matches dosplayer._DRUM_BRIGHT_MIDI and the NSF noise mapping (93 - 3*7).
BRIGHT_MIDI = 72

#: Which universal waveforms each target can actually voice.
TARGET_WAVEFORMS = {
    "mcs": ("square",),
    "tandy": ("square",),
    "1voice": ("square",),
    "4voice": ("square", "triangle", "sine"),   # modeled only at high mix rates
    "sb": ("square", "triangle", "sine", "nestri",
           "pulse12", "pulse25", "pulse50", "pulse75"),
}


def _tone_events(song: Song, include_perc: bool = False):
    """Per-track (start, dur, midi) for the melodic content: tone tracks' non-
    percussive notes. Noise/drum tracks and percussive notes are excluded (they
    go through the percussion path) — unless `include_perc` (the "pitched"
    percussion mode), which plays marked drums at their written pitches too."""
    out = []
    for t in song.tracks:
        if t.kind != KIND_TONE:
            if include_perc:                     # noise/drum notes as tones
                out.append([(n.start_tick, n.duration_ticks, n.midi_note)
                            for n in t.notes if not n.is_rest])
            continue
        out.append([(n.start_tick, n.duration_ticks, n.midi_note)
                    for n in t.notes
                    if not n.is_rest and (include_perc or not n.percussive)])
    return out


def _bright(n: NoteEvent) -> bool:
    """A hit's two-tone verdict: the importer's own analysis when it marked one
    (PT3 effects["drumbright"], from the sample's character), else pitch — the
    NSF noise mapping puts bright periods at/above midi 72."""
    if "drumbright" in n.effects:
        return bool(n.effects["drumbright"])
    return n.midi_note >= BRIGHT_MIDI


def _perc_hits(song: Song) -> List[Tuple[int, bool]]:
    """(start_tick, bright) for every percussion event: noise/drum-track notes
    and percussive-marked notes on tone tracks."""
    hits = []
    for t in song.tracks:
        if t.kind in (KIND_NOISE, KIND_DRUM):
            hits.extend((n.start_tick, _bright(n))
                        for n in t.notes if not n.is_rest)
        else:
            hits.extend((n.start_tick, _bright(n))
                        for n in t.notes if n.percussive and not n.is_rest)
    hits.sort()
    dedup, last = [], None
    for tick, bright in hits:
        if tick == last:
            continue
        last = tick
        dedup.append((tick, bright))
    return dedup


def _allocate(events, n: int):
    """Deal (start, dur, midi) events onto n monophonic voices, highest first
    (same policy as audio._allocate_voices, kept here to avoid the import)."""
    evs = sorted((e for track in events for e in track), key=lambda e: (e[0], -e[2]))
    ends = [0] * n
    chans: List[List[Tuple[int, int, int]]] = [[] for _ in range(n)]
    for e in evs:
        free = [i for i in range(n) if ends[i] <= e[0]]
        i = free[0] if free else min(range(n), key=ends.__getitem__)
        # clip an overflow note so the voice stays monophonic
        start = max(e[0], ends[i]) if not free else e[0]
        dur = e[1] - (start - e[0])
        if dur <= 0:
            continue
        chans[i].append((start, dur, e[2]))
        ends[i] = start + dur
    return chans


def _drum_click_notes(hits, drum_sound: str) -> List[NoteEvent]:
    """Percussion hits -> MCS-style register-extreme click notes."""
    notes = []
    for tick, bright in hits:
        if drum_sound == "auto":
            pitches = drums.two_tone(bright)
        else:
            pitches = drums.CLICKS.get(drum_sound, drums.CLICKS["block"])
        for midi in pitches:
            notes.append(NoteEvent(start_tick=tick, duration_ticks=1,
                                   midi_note=midi, percussive=True))
    return notes


def retrack(song: Song, target: str, drum_sound: Optional[str] = None,
            percussion: str = "clicks") -> Song:
    """Reduce `song` to what `target` can play; returns a NEW Song (the input is
    never modified). See the module docstring for the per-target shapes.

    `percussion` is the OUTPUT-side drum decision (the universal import only
    captures and marks drums): "clicks" voices them on the target's percussion
    path (MCS register-extreme clicks / the noise channel), with `drum_sound`
    picking the click palette; "pitched" plays them at their written pitches as
    ordinary tones; "drop" silences them."""
    if target not in TARGET_WAVEFORMS:
        raise ValueError(f"unknown retrack target {target!r} "
                         f"(one of {tuple(TARGET_WAVEFORMS)})")
    if percussion not in ("clicks", "pitched", "drop"):
        raise ValueError(f"percussion must be clicks/pitched/drop, "
                         f"not {percussion!r}")
    if drum_sound is None:
        drum_sound = getattr(song, "percussion_pref", (None, "auto"))[1] or "auto"
    allowed = TARGET_WAVEFORMS[target]
    tones = _tone_events(song, include_perc=(percussion == "pitched"))
    hits = _perc_hits(song) if percussion == "clicks" else []

    out = Song(title=song.title, tick_hz=song.tick_hz,
               source=f"{song.source} [retrack:{target}]" if song.source
               else f"retrack:{target}",
               time_signature=song.time_signature, key_signature=song.key_signature,
               timesig_code=song.timesig_code,
               tempo_tick_seconds=song.tempo_tick_seconds)

    def carry(track: Track, events, waveform: str) -> Track:
        for start, dur, midi in events:
            track.add(NoteEvent(start_tick=start, duration_ticks=dur,
                                midi_note=midi))
        track.waveform = waveform
        return track

    if target == "mcs":
        # 4 notation voices; percussion becomes click notes sharing voice 4
        # (or its own voice when free) exactly like the classic converter.
        voices = _allocate(tones, 4 if not hits else 3)
        for i, ev in enumerate(voices):
            out.add_track(carry(Track(name=f"Voice {i + 1}", chip="mcs"), ev,
                                "square"))
        if hits:
            drum = Track(name="Drums", chip="mcs", kind=KIND_DRUM,
                         waveform="square")
            drum.notes = _drum_click_notes(hits, drum_sound)
            drum.meta["drum_notes"] = len(drum.notes)
            out.add_track(drum)
    elif target == "1voice":
        top = _allocate(tones, 1)[0] if tones else []
        out.add_track(carry(Track(name="Speaker", chip="pc-speaker"), top,
                            "square"))
    else:                                            # tandy / 4voice / sb
        voices = _allocate(tones, 3)
        for i, ev in enumerate(voices):
            src = song.tracks[i] if i < len(song.tracks) else None
            wf = "square"
            if src is not None and src.kind == KIND_TONE and src.waveform in allowed:
                wf = src.waveform                    # sb keeps NES duties etc.
            out.add_track(carry(Track(name=f"Tone {i + 1}",
                                      chip=target), ev, wf))
        noise = Track(name="Noise", kind=KIND_NOISE, chip=target,
                      waveform="noise")
        for tick, bright in hits:
            noise.add(NoteEvent(start_tick=tick, duration_ticks=1,
                                midi_note=100 if bright else 47,
                                percussive=True))
        noise.meta["drum_notes"] = len(noise.notes)
        out.add_track(noise)
    return out
