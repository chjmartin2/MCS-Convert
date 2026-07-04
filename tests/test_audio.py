import io
import sys
import wave

from mcs_convert.audio import Player, midi_to_freq, synth_song, wav_bytes
from mcs_convert.model import NoteEvent, Song, Track


def _demo_song():
    song = Song(title="demo")
    t = Track(name="Treble")
    for i, m in enumerate([67, 69, 71, 72, 74]):   # G4 A4 B4 C5 D5
        t.add(NoteEvent(start_tick=i, duration_ticks=1, midi_note=m))
    song.add_track(t)
    return song


def test_midi_to_freq_a4():
    assert abs(midi_to_freq(69) - 440.0) < 1e-6
    assert abs(midi_to_freq(81) - 880.0) < 1e-6   # one octave up


def test_synth_length_matches_notes():
    sr = 22050
    step = 0.1
    pcm, got_sr = synth_song(_demo_song(), sample_rate=sr, step_seconds=step)
    assert got_sr == sr
    # 5 notes * 1 tick * 0.1s * 22050 = 11025 samples * 2 bytes
    assert len(pcm) == 5 * int(step * sr) * 2


def test_empty_song_is_silent():
    pcm, _ = synth_song(Song(title="empty"))
    assert pcm == b""


def test_wav_bytes_roundtrip():
    pcm, sr = synth_song(_demo_song(), step_seconds=0.05)
    wav = wav_bytes(pcm, sr)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == sr
        assert w.getnframes() == len(pcm) // 2


def test_player_plays_async_from_file_and_stop_purges(monkeypatch):
    import os

    pcm, sr = synth_song(_demo_song(), step_seconds=0.05)
    wav = wav_bytes(pcm, sr)

    calls: list[tuple[object, int]] = []

    class DummyWinSound:
        SND_FILENAME = 0x20000
        SND_ASYNC = 0x1
        SND_PURGE = 0x40
        def PlaySound(self, data, flags):
            calls.append((data, flags))

    monkeypatch.setitem(sys.modules, "winsound", DummyWinSound())

    player = Player()
    player.play(wav)

    # Played asynchronously from a real file whose contents are our WAV bytes.
    path, flags = calls[-1]
    assert isinstance(path, str) and os.path.exists(path)
    assert flags == DummyWinSound.SND_FILENAME | DummyWinSound.SND_ASYNC
    with open(path, "rb") as fh:
        assert fh.read() == wav

    # Stop purges the async sound and removes the temp file.
    player.stop()
    assert calls[-1] == (None, DummyWinSound.SND_PURGE)
    assert not os.path.exists(path)
    assert player._path is None
