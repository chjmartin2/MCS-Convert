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


#: Targets whose hardware can actually express per-note nuance. A real DAC can
#: sound each note's own waveform at its own volume; the 1-bit speaker engines
#: and MCS's notation cannot, so for them the reduction drops that detail (a
#: preview that kept it would lie about what the target plays).
KEEPS_NOTE_DETAIL = frozenset({"sb"})


def _tone_events(song: Song, include_perc: bool = False):
    """Per-track (start, dur, midi, note) for the melodic content: tone tracks'
    non-percussive notes. Noise/drum tracks and percussive notes are excluded
    (they go through the percussion path) — unless `include_perc` (the "pitched"
    percussion mode), which plays marked drums at their written pitches too.
    The source note rides along so a rich target can keep its velocity/waveform."""
    out = []
    for t in song.tracks:
        if t.kind != KIND_TONE:
            if include_perc:                     # noise/drum notes as tones
                out.append([(n.start_tick, n.duration_ticks, n.midi_note, n)
                            for n in t.notes if not n.is_rest])
            continue
        out.append([(n.start_tick, n.duration_ticks, n.midi_note, n)
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
    """Deal (start, dur, midi, note) events onto n monophonic voices, highest
    first (same policy as audio._allocate_voices, kept here to avoid the
    import). The source note is carried through unchanged."""
    evs = sorted((e for track in events for e in track), key=lambda e: (e[0], -e[2]))
    ends = [0] * n
    chans: List[List[Tuple]] = [[] for _ in range(n)]
    for e in evs:
        free = [i for i in range(n) if ends[i] <= e[0]]
        i = free[0] if free else min(range(n), key=ends.__getitem__)
        # clip an overflow note so the voice stays monophonic
        start = max(e[0], ends[i]) if not free else e[0]
        dur = e[1] - (start - e[0])
        if dur <= 0:
            continue
        chans[i].append((start, dur, e[2], e[3]))
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
            percussion: str = "clicks", voices: Optional[int] = None,
            drop_noise: bool = False) -> Song:
    """Reduce `song` to what `target` can play; returns a NEW Song (the input is
    never modified). See the module docstring for the per-target shapes.

    `percussion` is the OUTPUT-side drum decision (the universal import only
    captures and marks drums): "clicks" voices them on the target's percussion
    path (MCS register-extreme clicks / the noise channel), with `drum_sound`
    picking the click palette; "pitched" plays them at their written pitches as
    ordinary tones; "drop" silences them. `voices` (mcs target only) caps the
    voice count for the destination player: 4 = the PC-speaker 4-voice engine
    (default), 3 = Tandy/PCjr's three tone channels, 1 = a single beeper line.
    `drop_noise` removes the noise CHANNEL specifically (kind="noise" tracks)
    while other percussion (DPCM, marked drums) still follows `percussion`."""
    if target not in TARGET_WAVEFORMS:
        raise ValueError(f"unknown retrack target {target!r} "
                         f"(one of {tuple(TARGET_WAVEFORMS)})")
    if percussion not in ("clicks", "pitched", "drop"):
        raise ValueError(f"percussion must be clicks/pitched/drop, "
                         f"not {percussion!r}")
    if voices is not None and (target != "mcs" or voices not in (1, 3, 4)):
        raise ValueError("voices applies to the mcs target only, as 1, 3 or 4")
    if drum_sound is None:
        drum_sound = getattr(song, "percussion_pref", (None, "auto"))[1] or "auto"
    if drop_noise:
        pruned = Song(title=song.title, tick_hz=song.tick_hz, source=song.source,
                      time_signature=song.time_signature,
                      key_signature=song.key_signature,
                      timesig_code=song.timesig_code,
                      tempo_tick_seconds=song.tempo_tick_seconds)
        pruned.tracks = [t for t in song.tracks if t.kind != KIND_NOISE]
        song = pruned
    allowed = TARGET_WAVEFORMS[target]
    tones = _tone_events(song, include_perc=(percussion == "pitched"))
    hits = _perc_hits(song) if percussion == "clicks" else []

    out = Song(title=song.title, tick_hz=song.tick_hz,
               source=f"{song.source} [retrack:{target}]" if song.source
               else f"retrack:{target}",
               time_signature=song.time_signature, key_signature=song.key_signature,
               timesig_code=song.timesig_code,
               tempo_tick_seconds=song.tempo_tick_seconds)

    keep_detail = target in KEEPS_NOTE_DETAIL

    def carry(track: Track, events, waveform: str) -> Track:
        for start, dur, midi, src in events:
            if keep_detail:
                # a real DAC sounds each note's own waveform at its own volume
                track.add(NoteEvent(start_tick=start, duration_ticks=dur,
                                    midi_note=midi, velocity=src.velocity,
                                    waveform=(src.waveform
                                              if src.waveform in allowed else ""),
                                    effects=dict(src.effects)))
            else:
                track.add(NoteEvent(start_tick=start, duration_ticks=dur,
                                    midi_note=midi))
        track.waveform = waveform
        return track

    if target == "mcs":
        # Up to `voices` notation voices (4 = the PC-speaker 4-voice engine,
        # 3 = Tandy/PCjr, 1 = single beeper line); percussion takes the last
        # voice as click notes, exactly like the classic converter. A single
        # voice keeps only the top line — no room for drums.
        nv = voices or 4
        use_drums = bool(hits) and nv > 1
        chans = _allocate(tones, nv - 1 if use_drums else nv)
        for i, ev in enumerate(chans):
            out.add_track(carry(Track(name=f"Voice {i + 1}", chip="mcs"), ev,
                                "square"))
        if use_drums:
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
