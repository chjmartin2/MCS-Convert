"""Tiny software synth: render a Song to PCM and play it (Windows, winsound).

Deliberately simple — a chiptune-flavoured square/triangle synth good enough to *hear*
whether the decoded notes are right. Not a faithful PC-speaker/Tandy emulation.

Playback uses winsound with an in-memory WAV image (no temp files). Non-Windows hosts can
still synthesize and get WAV bytes; only live playback is Windows-only for now.
"""

from __future__ import annotations

import io
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


def _render_track(notes: List[Tuple[int, int]], sr: int, step: float,
                  amp: float, waveform: str) -> np.ndarray:
    total_samples = int(sum(d for _, d in notes) * step * sr)
    out = np.zeros(max(total_samples, 1), dtype=np.float32)
    fade = max(1, int(0.006 * sr))
    pos = 0
    for midi, dur in notes:
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
        pos += ns
    return out


def synth_song(song: Song, sample_rate: int = 22050, step_seconds: float = 0.125,
               amplitude: float = 0.35, waveform: str = "square") -> Tuple[bytes, int]:
    """Render every track (mixed) to mono 16-bit PCM. Returns (pcm_bytes, sample_rate)."""
    tracks = []
    for tr in song.tracks:
        notes = [(n.midi_note, n.duration_ticks) for n in tr.notes]
        if notes:
            tracks.append(_render_track(notes, sample_rate, step_seconds, amplitude, waveform))
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
    """Wrap mono 16-bit PCM in a WAV container (for winsound SND_MEMORY / file save)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Player:
    """Async WAV playback with stop, backed by winsound (Windows)."""

    def __init__(self) -> None:
        self._wav = b""  # keep a reference alive during SND_ASYNC playback

    def play(self, wav: bytes) -> None:
        import winsound
        self._wav = wav
        winsound.PlaySound(self._wav, winsound.SND_MEMORY | winsound.SND_ASYNC)

    def stop(self) -> None:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
        self._wav = b""
