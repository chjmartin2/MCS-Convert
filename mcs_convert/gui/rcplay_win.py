"""RCPlay — the standalone Windows .RCT player.

A small dark-themed player for RetroComputerist Tracker songs: open one or
more .RCT files (a playlist), play/pause/stop with a seek bar, live
oscilloscopes (four channels + master rendered from the same effects
flattener the exports use), and the song's metadata. Drag the window small
or run it minimized — it's a music player, not an editor.

    python -m mcs_convert rcplaywin [song.rct ...]
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np

from .. import rct as R
from ..audio import WaveOutPlayer, pcm16
from ..effects import flatten, render_flat

BG = "#101014"
BG2 = "#16161c"
FG = "#c8c8d0"
DIM = "#5a5a68"
ACC = "#7fd17f"
CHCOL = ("#e8d44d", "#e07070", "#6f9fe8", "#70d0a0")
FONT = ("Consolas", 10)
FONTB = ("Consolas", 12, "bold")

_SR = 44100
_SCOPE_MS = 30                   # window each scope frame shows


class RCPlayApp:
    """The RCPlay main window."""

    def __init__(self, root: tk.Tk, paths=()):
        self.root = root
        root.title("RCPlay — RetroComputerist Tracker player")
        root.configure(bg=BG)
        root.geometry("560x420")
        self.player = WaveOutPlayer()
        self.master = None           # rendered PCM (float32)
        self.voices = None
        self.total_s = 0.0
        self.playing = False
        self.paused = False
        self._tick = None
        self._seek_drag = False
        self._build_ui()
        for p in paths:
            self._add(p)
        if self.playlist.size():
            self.playlist.selection_set(0)
            self.play_selected()

    def _build_ui(self):
        r = self.root
        self.v_title = tk.StringVar(value="drop a song in the playlist")
        self.v_meta = tk.StringVar(value="")
        self.v_time = tk.StringVar(value="0:00 / 0:00")
        tk.Label(r, textvariable=self.v_title, bg=BG, fg=ACC, font=FONTB,
                 anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(r, textvariable=self.v_meta, bg=BG, fg=DIM, font=FONT,
                 anchor="w").pack(fill="x", padx=10)

        self.scope = tk.Canvas(r, height=170, bg="#000000", highlightthickness=0)
        self.scope.pack(fill="x", padx=10, pady=6)

        bar = tk.Frame(r, bg=BG)
        bar.pack(fill="x", padx=10)
        self.seek = ttk.Scale(bar, from_=0.0, to=1.0, value=0.0,
                              command=self._seek_move)
        self.seek.pack(fill="x", side="top")
        self.seek.bind("<ButtonPress-1>", lambda e: self._set_drag(True))
        self.seek.bind("<ButtonRelease-1>", self._seek_release)
        tk.Label(bar, textvariable=self.v_time, bg=BG, fg=DIM, font=FONT
                 ).pack(side="right")
        btns = tk.Frame(bar, bg=BG)
        btns.pack(side="left", pady=4)
        for txt, cmd in (("▶", self.play_selected), ("⏸", self.pause),
                         ("■", self.stop), ("⏭", self.next_song)):
            tk.Button(btns, text=txt, command=cmd, width=4, bg=BG2, fg=FG,
                      relief="flat", activebackground="#2a4a2a",
                      font=FONTB).pack(side="left", padx=2)

        pf = tk.Frame(r, bg=BG)
        pf.pack(fill="both", expand=True, padx=10, pady=(2, 8))
        tk.Label(pf, text="PLAYLIST", bg=BG, fg=ACC, font=FONT).pack(anchor="w")
        self.playlist = tk.Listbox(pf, bg=BG2, fg=FG, font=FONT,
                                   selectbackground="#2a4a2a",
                                   exportselection=False)
        self.playlist.pack(fill="both", expand=True, side="left")
        self.playlist.bind("<Double-Button-1>", lambda e: self.play_selected())
        pb = tk.Frame(pf, bg=BG)
        pb.pack(side="left", fill="y", padx=(4, 0))
        tk.Button(pb, text="+", command=self.add_dialog, bg=BG2, fg=FG,
                  relief="flat", width=3).pack()
        tk.Button(pb, text="−", command=self.remove_selected, bg=BG2, fg=FG,
                  relief="flat", width=3).pack()
        self.paths: list = []

    # ---- playlist ------------------------------------------------------------

    def add_dialog(self):
        for p in filedialog.askopenfilenames(
                filetypes=[("RCT tracker songs", "*.rct")]):
            self._add(p)

    def _add(self, path: str):
        self.paths.append(path)
        self.playlist.insert("end", os.path.basename(path))

    def remove_selected(self):
        sel = self.playlist.curselection()
        if sel:
            self.playlist.delete(sel[0])
            self.paths.pop(sel[0])

    def next_song(self):
        n = self.playlist.size()
        if not n:
            return
        sel = self.playlist.curselection()
        i = ((sel[0] + 1) if sel else 0) % n
        self.playlist.selection_clear(0, "end")
        self.playlist.selection_set(i)
        self.play_selected()

    # ---- playback ------------------------------------------------------------

    def play_selected(self):
        sel = self.playlist.curselection()
        if not sel:
            return
        path = self.paths[sel[0]]
        try:
            song = R.load(path)
            flat = flatten(song)
            self.master, self.voices = render_flat(flat, sr=_SR)
        except (OSError, ValueError) as exc:
            self.v_title.set(f"error: {exc}")
            return
        self.total_s = len(self.master) / _SR
        mode = ("3 tone + noise"
                if song.channel_mode == R.MODE_3TONE_NOISE else "4 tone")
        self.v_title.set(song.title or os.path.basename(path))
        self.v_meta.set(f"{song.author or 'unknown'} — {mode}, "
                        f"{len(song.order)} positions, speed {song.speed}"
                        + (f" — {song.comment}" if song.comment else ""))
        self.player.stop()
        self.player.play(pcm16(self.master), _SR)
        self.playing, self.paused = True, False
        if not self._tick:
            self._frame()

    def pause(self):
        if self.playing:
            self.player.pause()
            self.paused = not self.paused

    def stop(self):
        self.playing = False
        try:
            self.player.stop()
        except Exception:
            pass
        self.seek.set(0.0)
        self.v_time.set("0:00 / " + self._fmt(self.total_s))

    def _set_drag(self, on: bool):
        self._seek_drag = on

    def _seek_move(self, _v):
        pass                                          # position label only on release

    def _seek_release(self, _e):
        self._seek_drag = False
        if self.master is None:
            return
        frac = float(self.seek.get())
        start = int(frac * len(self.master))
        # winmm has no seek: restart playback from the cut buffer
        self.player.stop()
        self.player.play(pcm16(self.master[start:]), _SR)
        self._seek_base = start / _SR
        self.playing, self.paused = True, False

    _seek_base = 0.0

    @staticmethod
    def _fmt(s: float) -> str:
        return f"{int(s) // 60}:{int(s) % 60:02d}"

    # ---- the scope frame -----------------------------------------------------

    def _frame(self):
        self._tick = self.root.after(33, self._frame)
        if not self.playing or self.master is None or self.paused:
            return
        pos = self.player.position_seconds() + self._seek_base
        if pos >= self.total_s - 0.05:
            self._seek_base = 0.0
            self.next_song()
            return
        if not self._seek_drag:
            self.seek.set(pos / self.total_s if self.total_s else 0)
        self.v_time.set(f"{self._fmt(pos)} / {self._fmt(self.total_s)}")
        cv = self.scope
        cv.delete("all")
        w = cv.winfo_width() or 540
        h = int(cv.winfo_height() or 170)
        n = int(_SR * _SCOPE_MS / 1000)
        i0 = max(0, int(pos * _SR) - n // 2)
        strip = h // 5
        for c in range(4):
            seg = self.voices[c][i0:i0 + n]
            self._trace(cv, seg, w, c * strip, strip, CHCOL[c])
        seg = self.master[i0:i0 + n]
        self._trace(cv, seg, w, 4 * strip, strip, "#ffffff")

    @staticmethod
    def _trace(cv, seg, w, y0, hh, colour):
        if seg is None or not len(seg):
            return
        mid = y0 + hh // 2
        n = len(seg)
        step = max(1, n // max(1, w // 2))
        pts = []
        for x in range(0, w, 2):
            i = min(n - 1, x * n // max(1, w))
            pts += [x, mid - int(seg[i] * (hh * 0.48) * 4)]
        if len(pts) >= 4:
            cv.create_line(*pts, fill=colour, width=1)


def main(argv=None) -> int:
    import sys
    args = list(argv if argv is not None else sys.argv[1:])
    root = tk.Tk()
    RCPlayApp(root, paths=[a for a in args if a.lower().endswith(".rct")])
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
