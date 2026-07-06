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

from ..audio import Player, synth_song, tempo_bpm, wav_bytes
from ..mcs.reader import parse
from ..tracker import tracker_rows, tracker_text

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
        self.export_btn = tk.Button(bar, text="⬇ WAV…", command=self.export_wav,
                                    state="disabled")
        self.export_btn.pack(side="left", padx=(12, 0))
        self.track_btn = tk.Button(bar, text="⬇ Tracker…", command=self.export_tracker,
                                   state="disabled")
        self.track_btn.pack(side="left", padx=(4, 0))

        # Voice: the clean synth waveforms, plus "PC Speaker" — MCS's own 4-voice 1-bit
        # rendering (see audio._render_pcspeaker), for comparing against real hardware.
        self._voices = {"PC Speaker": "pcspeaker", "Square": "square",
                        "Triangle": "triangle", "Sine": "sine"}
        self.voice = tk.StringVar(value="PC Speaker")
        ttk.Combobox(bar, textvariable=self.voice, width=11, state="readonly",
                     values=list(self._voices)).pack(side="right")

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
        cols = ("bar", "v1", "v2", "v3", "v4")       # 4 voices, highest -> lowest
        style = ttk.Style()
        # Explicit light grid: set foreground too, so a shaded row never hides its text
        # (the old bar tag set only a dark background -> black-on-black first note).
        style.configure("Tracker.Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground="#141414", rowheight=20)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=24,
                                 style="Tracker.Treeview")
        self.tree.tag_configure("stripe", background="#e8f6e8", foreground="#141414")  # zebra
        self.tree.tag_configure("bar", background="#bfe3bf", foreground="#0b3d0b",      # bar start
                                font=("TkDefaultFont", 9, "bold"))
        for c, w in (("bar", 46), ("v1", 88), ("v2", 88), ("v3", 88), ("v4", 88)):
            self.tree.heading(c, text=c.upper() if c != "bar" else "Bar")
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
        self.path = path
        self._populate(path)
        self.play_btn.configure(state="normal")
        self.export_btn.configure(state="normal")
        self.track_btn.configure(state="normal")

    def _populate(self, path: str) -> None:
        self.tree.delete(*self.tree.get_children())
        # 4-voice tracker: 32nd-note rows, sounding notes ranked highest -> lowest.
        rows = tracker_rows(self.song)
        for idx, (lbl, is_bar, cols) in enumerate(rows):
            tag = "bar" if is_bar else ("stripe" if idx % 2 else "")
            self.tree.insert("", "end", values=(lbl, *cols),
                             tags=(tag,) if tag else ())
        self.tree.yview_moveto(0.0)          # always show the first row after a load
        self.tree.update_idletasks()         # force the grid to repaint now

        # Stamp the decoded first note into the title so THIS window self-identifies —
        # if the title and the grid ever disagree, this window is running stale code.
        first = next((c for _, _, cols in rows for c in cols if c and c != "R"), "?")
        self.root.title(f"MCS-Convert — {os.path.basename(path)}  (first note {first})")

        self.meta_vars["Time"].set(self.song.time_signature or "—")
        self.meta_vars["Key"].set(self.song.key_signature or "—")
        self.meta_vars["Tempo"].set(f"≈{round(tempo_bpm(self.song.tempo_tick_seconds))} BPM")

        total = sum(len(t.notes) for t in self.song.tracks)
        rests = sum(1 for tr in self.song.tracks for n in tr.notes if n.is_rest)
        self.status.configure(
            text=f"{os.path.basename(path)} — {len(self.song.tracks)} staff/staves, "
                 f"{total} notes ({rests} rests).")

    def _render(self):
        """Synthesize the loaded song to WAV bytes at its own tempo. Returns bytes or None."""
        # Timing comes from the file's own tempo (header byte 0); voice from the dropdown.
        pcm, sr = synth_song(self.song, step_seconds=self.song.tempo_tick_seconds,
                             waveform=self._voices[self.voice.get()])
        return wav_bytes(pcm, sr) if pcm else None

    def play(self) -> None:
        if not self.song:
            return
        wav = self._render()
        if wav is None:
            self.status.configure(text="Nothing to play (no decoded notes).")
            return
        self.player.play(wav)
        self.stop_btn.configure(state="normal")

    def export_wav(self) -> None:
        if not self.song:
            return
        wav = self._render()
        if wav is None:
            self.status.configure(text="Nothing to export (no decoded notes).")
            return
        default = os.path.splitext(os.path.basename(getattr(self, "path", "song")))[0] + ".wav"
        out = filedialog.asksaveasfilename(
            title="Export decoded playback as WAV", defaultextension=".wav",
            initialfile=default, filetypes=[("WAV audio", "*.wav")])
        if not out:
            return
        try:
            with open(out, "wb") as fh:
                fh.write(wav)
        except OSError as exc:
            messagebox.showerror("Cannot write WAV", str(exc))
            return
        self.status.configure(text=f"Exported {len(wav)} bytes → {os.path.basename(out)}")

    def export_tracker(self) -> None:
        if not self.song:
            return
        default = os.path.splitext(os.path.basename(getattr(self, "path", "song")))[0] + ".txt"
        out = filedialog.asksaveasfilename(
            title="Export tracker grid as text", defaultextension=".txt",
            initialfile=default, filetypes=[("Text", "*.txt")])
        if not out:
            return
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(tracker_text(self.song))
        except OSError as exc:
            messagebox.showerror("Cannot write tracker", str(exc))
            return
        self.status.configure(text=f"Exported tracker → {os.path.basename(out)}")

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
