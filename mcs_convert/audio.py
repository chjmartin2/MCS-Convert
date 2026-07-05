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
from typing import List, Tuple

import numpy as np

from .model import Song


def midi_to_freq(midi: int) -> float:
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


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


def synth_song(song: Song, sample_rate: int = 22050, step_seconds: float = 0.125,
               amplitude: float = 0.35, waveform: str = "square") -> Tuple[bytes, int]:
    """Render every track (mixed) to mono 16-bit PCM. Returns (pcm_bytes, sample_rate)."""
    tracks = []
    for tr in song.tracks:
        events = [(n.start_tick, n.duration_ticks, n.midi_note)
                  for n in tr.notes if not n.is_rest]
        if events:
            tracks.append(_render_track(events, sample_rate, step_seconds, amplitude, waveform))
    if not tracks:
        return b"", sample_rate
    length = max(len(t) for t in tracks)
    mix = np.zeros(length, dtype=np.float32)
    for t in tracks:
        mix[:len(t)] += t
    peak = float(np.max(np.abs(mix))) or 1.0
    mix = (mix / peak) * 0.9
    pcm = (mix * 32767.0).astype("<i2").tobytes()
    return pcm, sample_rate


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
