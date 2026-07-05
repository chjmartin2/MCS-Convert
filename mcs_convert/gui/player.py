"""MCS/MCD viewer + player — a tracker-style read-only window.

Open a Music Construction Set song and see its notes laid out per staff (tracker columns),
then play it back through the built-in synth. This is a validation harness for the format
reverse-engineering: when a song plays back recognizably, we've decoded it correctly.

Run:  python -m mcs_convert.gui.player [SONG.MCS]      (or:  mcs-convert play SONG.MCS)

The decoder matches MCSDISK.EXE's own playback engine (recovered by disassembly):
6-bit vertical positions into fixed per-clef pitch windows, key signatures and
accidentals, dots, chords, rests, and measure-aligned staves. See docs/mcs-format.md.
"""

from __future__ import annotations

# Allow "Run" in an IDE (which executes this file as a plain script, so relative imports
# would fail). Re-launch ourselves as a proper package module, then stop.
if __package__ in (None, ""):
    import os as _os
    import sys as _sys
    import runpy as _runpy

    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    _runpy.run_module("mcs_convert.gui.player", run_name="__main__", alter_sys=True)
    raise SystemExit(0)

import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from ..audio import Player, synth_song, tempo_bpm, tempo_step_seconds, wav_bytes
from ..mcs.reader import parse
from ..pitch import midi_to_name

_BG = "#12141a"
_FG = "#d7dae0"
_ACCENT = "#7fd1b9"


class PlayerApp:
    def __init__(self, root: tk.Tk, initial: str | None = None) -> None:
        self.root = root
        self.player = Player()
        self.song = None
        root.title("MCS-Convert — Player")
        root.configure(bg=_BG)
        root.geometry("560x640")

        self._build_toolbar()
        self._build_meta()
        self._build_tracker()
        self._build_statusbar()

        if initial:
            self.load(initial)

    # ---- layout ----------------------------------------------------------
    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root, bg=_BG)
        bar.pack(fill="x", padx=8, pady=6)

        tk.Button(bar, text="Open…", command=self.open_dialog).pack(side="left")
        self.play_btn = tk.Button(bar, text="▶ Play", command=self.play, state="disabled")
        self.play_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = tk.Button(bar, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(4, 0))

    def _build_meta(self) -> None:
        # Read-only song metadata extracted from the file. Playback follows these; there
        # are no manual overrides — the point is to reproduce what the .MCS actually stores.
        meta = tk.Frame(self.root, bg=_BG)
        meta.pack(fill="x", padx=8, pady=(0, 2))
        self.meta_vars = {k: tk.StringVar(value="—") for k in ("Time", "Key", "Tempo")}
        for label in ("Time", "Key", "Tempo"):
            cell = tk.Frame(meta, bg=_BG)
            cell.pack(side="left", padx=(0, 20))
            tk.Label(cell, text=label, bg=_BG, fg=_ACCENT,
                     font=("TkDefaultFont", 8)).pack(anchor="w")
            tk.Label(cell, textvariable=self.meta_vars[label], bg=_BG, fg=_FG,
                     font=("TkDefaultFont", 11, "bold")).pack(anchor="w")

    def _build_tracker(self) -> None:
        frame = tk.Frame(self.root, bg=_BG)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("step", "treble", "bass")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=24)
        for c, w in (("step", 60), ("treble", 200), ("bass", 200)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _build_statusbar(self) -> None:
        self.status = tk.Label(self.root, text="Open an .MCS / .MCD song to begin.",
                               bg="#0c0d11", fg=_ACCENT, anchor="w")
        self.status.pack(fill="x", side="bottom")

    # ---- actions ---------------------------------------------------------
    def open_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Music Construction Set song",
            filetypes=[("MCS/MCD songs", "*.mcs *.mcd *.MCS *.MCD"), ("All files", "*.*")])
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        try:
            self.song = parse(path)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
            messagebox.showerror("Cannot open file", f"{os.path.basename(path)}:\n{exc}")
            return
        self._populate(path)
        self.play_btn.configure(state="normal")

    def _populate(self, path: str) -> None:
        self.tree.delete(*self.tree.get_children())
        by_name = {t.name: t for t in self.song.tracks}
        treble = by_name.get("Treble", self.song.tracks[0] if self.song.tracks else None)
        bass = by_name.get("Bass")
        tnotes = treble.notes if treble else []
        bnotes = bass.notes if bass else []

        def label(notes, i):
            if i >= len(notes):
                return ""
            n = notes[i]
            return "— rest" if n.is_rest else midi_to_name(n.midi_note)

        for i in range(max(len(tnotes), len(bnotes))):
            self.tree.insert("", "end",
                             values=(i + 1, label(tnotes, i), label(bnotes, i)))

        self.meta_vars["Time"].set(self.song.time_signature or "—")
        self.meta_vars["Key"].set(self.song.key_signature or "—")
        if self.song.tempo_level is not None:
            self.meta_vars["Tempo"].set(
                f"{self.song.tempo_level}/3  (≈{round(tempo_bpm(self.song.tempo_level))} BPM)")
        else:
            self.meta_vars["Tempo"].set("—")

        total = sum(len(t.notes) for t in self.song.tracks)
        rests = sum(1 for tr in self.song.tracks for n in tr.notes if n.is_rest)
        self.status.configure(
            text=f"{os.path.basename(path)} — {len(self.song.tracks)} staff/staves, "
                 f"{total} notes ({rests} rests).")

    def play(self) -> None:
        if not self.song:
            return
        # Timing comes from the file's own stored tempo; timbre is the PC-speaker square.
        step = tempo_step_seconds(self.song.tempo_level)
        pcm, sr = synth_song(self.song, step_seconds=step, waveform="square")
        if not pcm:
            self.status.configure(text="Nothing to play (no decoded notes).")
            return
        self.player.play(wav_bytes(pcm, sr))
        self.stop_btn.configure(state="normal")

    def stop(self) -> None:
        self.player.stop()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    initial = argv[0] if argv else None
    root = tk.Tk()
    PlayerApp(root, initial)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
