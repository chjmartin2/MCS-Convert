"""Tiny software synth: render a Song to PCM and play it (Windows, winsound).

Deliberately simple — a chiptune-flavoured square/triangle synth good enough to *hear*
whether the decoded notes are right. Not a faithful PC-speaker/Tandy emulation.

Playback writes a short-lived temp WAV and plays it async via winsound (so Stop works — see
Player). Non-Windows hosts can still synthesize and get WAV bytes; only live playback is
Windows-only for now.
"""

from __future__ import annotations

import io
import os
import tempfile
import wave
from typing import List, Optional, Tuple

import numpy as np

from .model import NoteEvent, Song


def midi_to_freq(midi: int) -> float:
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def _note_events(notes: List[NoteEvent]) -> List[Tuple[int, int, int]]:
    """(start_tick, duration_ticks, midi) for sounding notes, with tied same-pitch chains
    merged into ONE event. A tie means "don't re-attack": rendered as two notes, the
    second restarts phase and re-fades, which pops audibly (AXEL.MCD bar 3.28's D5~ into
    a half-note D5). A tie to a *different* pitch is a slur and keeps its own onset."""
    sounding = [n for n in notes if not n.is_rest]
    starts = {}
    for i, n in enumerate(sounding):
        starts.setdefault((n.start_tick, n.midi_note), i)
    consumed: set = set()
    events: List[Tuple[int, int, int]] = []
    for i, n in enumerate(sounding):
        if i in consumed:
            continue
        dur, cur = n.duration_ticks, n
        while cur.tied:
            j = starts.get((n.start_tick + dur, n.midi_note))
            if j is None or j in consumed:
                break
            consumed.add(j)
            cur = sounding[j]
            dur += cur.duration_ticks
        events.append((n.start_tick, dur, n.midi_note))
    return events


def _allocate_voices(per_track: List[List[Tuple[int, int, int]]], n: int = 4
                     ) -> List[List[Tuple[int, int, int]]]:
    """Deal events onto n voice channels: at each onset the highest pitch takes the
    first free channel, so channel 0 is the tracker's v1. Overflow (shouldn't happen —
    MCS itself has four voices) doubles up on the channel that frees soonest."""
    evs = sorted((e for track in per_track for e in track), key=lambda e: (e[0], -e[2]))
    ends = [0] * n
    chans: List[List[Tuple[int, int, int]]] = [[] for _ in range(n)]
    for e in evs:
        free = [i for i in range(n) if ends[i] <= e[0]]
        i = free[0] if free else min(range(n), key=ends.__getitem__)
        chans[i].append(e)
        ends[i] = max(ends[i], e[0] + e[1])
    return chans


def tempo_bpm(tick_seconds: float) -> float:
    """Quarter-note BPM for a per-tick duration. A tick is a 32nd, so 8 ticks per quarter.

    The real tempo is set by the file's header byte 0 (see reader.tick_seconds_for),
    measured from DOSBox-X captures — NOT the 0x05 "level" word."""
    return 60.0 / (8.0 * tick_seconds)


def _wave(phase: np.ndarray, waveform: str) -> np.ndarray:
    if waveform == "square":
        return np.sign(np.sin(2 * np.pi * phase)).astype(np.float32)
    if waveform == "triangle":
        return (2.0 * np.abs(2.0 * (phase - np.floor(phase + 0.5))) - 1.0).astype(np.float32)
    return np.sin(2 * np.pi * phase).astype(np.float32)  # sine


def _render_track(events: List[Tuple[int, int, int]], sr: int, step: float,
                  amp: float, waveform: str) -> np.ndarray:
    """Render (start_tick, duration_ticks, midi) events at their absolute positions.

    Placement is by start_tick (not cumulative), so chords overlap and a staff that
    sits out a measure stays silent for it. Rests need no events — silence is the
    default between placed notes.
    """
    total_samples = int(max(s + d for s, d, _ in events) * step * sr)
    out = np.zeros(max(total_samples, 1), dtype=np.float32)
    fade = max(1, int(0.006 * sr))
    for start, dur, midi in events:
        pos = int(start * step * sr)
        ns = int(dur * step * sr)
        if ns <= 0:
            continue
        t = np.arange(ns, dtype=np.float32) / sr
        seg = amp * _wave(midi_to_freq(midi) * t, waveform)
        # short linear fades to kill clicks between notes
        f = min(fade, ns // 2)
        if f > 0:
            seg[:f] *= np.linspace(0.0, 1.0, f, dtype=np.float32)
            seg[-f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
        out[pos:pos + ns] += seg
    return out


def _render_pcspeaker(events: List[Tuple[int, int, int]], sr: int, step: float) -> np.ndarray:
    """Reproduce MCS's 4-voice PC-speaker rendering.

    The real engine (MCSDISK.EXE loop at image 0x1929) runs four phase accumulators, one
    per voice, adds a per-voice increment each pass, and combines their overflows into the
    single 1-bit speaker — all four voices sound at once, quantized to 1 bit. This models
    that faithfully (not cycle-exact): sum the voices' 1-bit square waves, then render the
    sum back to 1 bit with first-order delta-sigma (PDM), which is what gives the speaker
    its gritty texture. Comparable directly against real captures. See docs/mcs-format.md.
    """
    if not events:
        return np.zeros(1, dtype=np.float32)
    total = int(max(s + d for s, d, _ in events) * step * sr) + 1
    level = np.zeros(total, dtype=np.float32)               # 0..N voices high per sample
    for start, dur, midi in events:
        pos = int(start * step * sr)
        ns = int(dur * step * sr)
        if ns <= 0:
            continue
        phase = midi_to_freq(midi) * np.arange(pos, pos + ns, dtype=np.float64) / sr
        level[pos:pos + ns] += (np.mod(phase, 1.0) < 0.5).astype(np.float32)  # 1-bit square
    peak = float(level.max()) or 1.0
    duty = level / peak                                     # target speaker density 0..1
    # First-order delta-sigma: the running count of output 1s tracks the integral of duty,
    # so each output bit is floor(cumsum) stepping up. Vectorized (no per-sample loop).
    cum = np.floor(np.cumsum(duty, dtype=np.float64))
    bits = np.diff(np.concatenate(([0.0], cum)))           # 0/1 speaker state per sample
    return (bits * 2.0 - 1.0).astype(np.float32) * 0.6


def _render_voice_bits(events: List[Tuple[int, int, int]], sr: int, step: float,
                       total: int) -> np.ndarray:
    """One voice's own 1-bit square (±0.6 while sounding, 0 when silent) — the scope's
    view of what that voice contributes to the PC-speaker mix."""
    out = np.zeros(total, dtype=np.float32)
    for start, dur, midi in events:
        pos = int(start * step * sr)
        ns = min(int(dur * step * sr), total - pos)
        if ns <= 0:
            continue
        phase = midi_to_freq(midi) * np.arange(pos, pos + ns, dtype=np.float64) / sr
        out[pos:pos + ns] = ((np.mod(phase, 1.0) < 0.5).astype(np.float32) * 2.0 - 1.0) * 0.6
    return out


def render_song(song: Song, sample_rate: int = 22050, step_seconds: float = 0.0625,
                amplitude: float = 0.35, waveform: str = "square"
                ) -> Tuple[np.ndarray, List[np.ndarray], int]:
    """Render (master, four voice channels, sample_rate) as float32 arrays in [-1, 1].

    The master is the playback mix (synth_song wraps it in PCM); the four channels are
    the same notes dealt onto MCS's voices (channel 0 = highest, like the tracker's v1)
    for the oscilloscope view. Tied same-pitch notes are merged before rendering."""
    per_track = [_note_events(tr.notes) for tr in song.tracks]
    chans = _allocate_voices(per_track)
    silent = np.zeros(1, dtype=np.float32)
    if not any(per_track):
        return silent, [silent] * 4, sample_rate
    if waveform == "pcspeaker":
        flat = [e for track in per_track for e in track]
        master = _render_pcspeaker(flat, sample_rate, step_seconds)
        voices = [_render_voice_bits(c, sample_rate, step_seconds, len(master))
                  for c in chans]
        return master, voices, sample_rate
    bufs = [_render_track(c, sample_rate, step_seconds, amplitude, waveform) if c else silent
            for c in chans]
    length = max(len(b) for b in bufs)
    mix = np.zeros(length, dtype=np.float32)
    for b in bufs:
        mix[:len(b)] += b
    peak = float(np.max(np.abs(mix))) or 1.0
    gain = 0.9 / peak
    voices = [np.pad(b, (0, length - len(b))) * gain for b in bufs]
    return mix * gain, voices, sample_rate


def pcm16(buf: np.ndarray) -> bytes:
    """Float [-1, 1] buffer -> mono 16-bit PCM bytes; b'' when silent (nothing to play)."""
    if not np.any(buf):
        return b""
    return (buf * 32767.0).astype("<i2").tobytes()


def synth_song(song: Song, sample_rate: int = 22050, step_seconds: float = 0.0625,
               amplitude: float = 0.35, waveform: str = "square") -> Tuple[bytes, int]:
    """Render every track (mixed) to mono 16-bit PCM. Returns (pcm_bytes, sample_rate)."""
    master, _, sr = render_song(song, sample_rate, step_seconds, amplitude, waveform)
    return pcm16(master), sr


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono 16-bit PCM in a WAV container (for playback or file save)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Player:
    """Async, stoppable WAV playback backed by winsound (Windows).

    Plays via ``SND_FILENAME | SND_ASYNC`` from a temp file rather than ``SND_MEMORY``:
    Windows rejects async-from-memory outright, and ``SND_PURGE`` can't reliably interrupt
    a *synchronous* in-memory sound running on a worker thread — so a memory-based player
    can't honour Stop. Async-from-file is the pattern winsound actually supports stopping.
    """

    def __init__(self) -> None:
        self._path: str | None = None   # temp WAV backing the current playback

    def play(self, wav: bytes) -> None:
        import winsound

        self.stop()  # halt anything playing and clean up its temp file
        fd, path = tempfile.mkstemp(prefix="mcs_", suffix=".wav")
        with os.fdopen(fd, "wb") as fh:
            fh.write(wav)
        self._path = path
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)

    def stop(self) -> None:
        import winsound

        winsound.PlaySound(None, winsound.SND_PURGE)  # stops the async sound, frees the file
        self._cleanup()

    def _cleanup(self) -> None:
        if self._path:
            try:
                os.remove(self._path)
            except OSError:
                pass  # file may still be held briefly, or already gone; harmless to skip
            self._path = None
