"""Playback visualization windows: VU meters, spectrum analyzer, and the
DOS-replica preview.

All windows share one data source: the app's rendered buffers (master, per-voice
arrays, sample rate) plus the elapsed playback position. Each exposes
`draw(elapsed)` — called from the app's playhead loop ~30x/sec — and accepts
None to flatline. The DOS replica window imitates the standalone `.COM`
players' on-screen visualizations (the text-mode combined monitor, spectrum
analyzer, VU meters...) so the export dialog can preview what a chosen scope
will look like before building the .COM.
"""

from __future__ import annotations

import tkinter as tk
from typing import List, Optional

import numpy as np

_BG = "#14161c"
_ACCENT = "#7fd17f"

# DOS text-mode palette (the CGA/VGA colors the .COM scopes use)
_DOS = {"yellow": "#ffff55", "red": "#aa0000", "brightred": "#ff5555",
        "blue": "#0000aa", "green": "#55ff55", "white": "#ffffff",
        "grey": "#aaaaaa", "cyan": "#55ffff", "black": "#000000"}
#: per-channel DOS colors (matches the .COM _TATTR: yellow/red/blue/green + white)
_DOS_CH = ("#ffff55", "#ff5555", "#5555ff", "#55ff55", "#ffffff")


def _window(parent, title: str, minsize=(360, 240)):
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=_BG)
    win.minsize(*minsize)
    return win


def _rms_levels(voices, idx: int, span: int) -> List[float]:
    """Per-voice RMS of the ~30 ms window at idx, scaled to 0..1."""
    out = []
    for v in voices:
        seg = v[idx:idx + span] if v is not None and idx < len(v) else None
        if seg is None or len(seg) == 0:
            out.append(0.0)
        else:
            out.append(min(1.0, float(np.sqrt(np.mean(seg ** 2))) * 2.2))
    return out


def _spectrum(master, idx: int, sr: int, bars: int = 20) -> List[float]:
    """Log-spaced spectrum magnitudes 0..1 for the window at idx."""
    n = 2048
    seg = master[idx:idx + n] if master is not None and idx < len(master) else None
    if seg is None or len(seg) < 256:
        return [0.0] * bars
    if len(seg) < n:
        seg = np.pad(seg, (0, n - len(seg)))
    mag = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
    edges = np.geomspace(55.0, min(8000.0, sr / 2 - 1), bars + 1)
    out = []
    for i in range(bars):
        band = mag[(freqs >= edges[i]) & (freqs < edges[i + 1])]
        out.append(min(1.0, float(band.max()) / 60.0) if len(band) else 0.0)
    return out


class VUWindow:
    """Per-track VU meters: horizontal bars with green/yellow/red zones and a
    falling white peak tick — the standalone player's meters, native-styled."""

    def __init__(self, parent, names: List[str]) -> None:
        self.win = _window(parent, "MCS-Convert — VU Meters",
                           (420, 40 * max(1, len(names)) + 40))
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.names = names
        self.levels = [0.0] * len(names)
        self.peaks = [0.0] * len(names)

    def alive(self) -> bool:
        return bool(self.win.winfo_exists())

    def draw(self, levels: Optional[List[float]]) -> None:
        if not self.alive():
            return
        c = self.canvas
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 60)
        n = max(1, len(self.names))
        row_h = h / n
        c.delete("all")
        for i, name in enumerate(self.names):
            lv = 0.0 if levels is None else levels[i] if i < len(levels) else 0.0
            # attack fast, release slow; peak falls slower still
            self.levels[i] = max(lv, self.levels[i] * 0.82)
            self.peaks[i] = max(self.levels[i], self.peaks[i] - 0.012)
            y0 = i * row_h + row_h * 0.25
            y1 = (i + 1) * row_h - row_h * 0.25
            x0, x1 = 90, w - 16
            c.create_text(8, (y0 + y1) / 2, text=name, anchor="w",
                          fill=_DOS_CH[i % len(_DOS_CH)],
                          font=("Consolas", 10, "bold"))
            c.create_rectangle(x0, y0, x1, y1, outline="#333333")
            span = x1 - x0
            fill_to = x0 + span * self.levels[i]
            for frac, color in ((0.65, "#22cc22"), (0.85, "#cccc22"), (1.0, "#cc2222")):
                seg_end = min(fill_to, x0 + span * frac)
                seg_start = x0 + span * (0 if frac == 0.65 else (0.65 if frac == 0.85 else 0.85))
                if seg_end > seg_start:
                    c.create_rectangle(seg_start, y0 + 1, seg_end, y1 - 1,
                                       fill=color, outline="")
            px = x0 + span * self.peaks[i]
            c.create_line(px, y0, px, y1, fill="#ffffff", width=2)


class SpectrumWindow:
    """A live spectrum analyzer: log-spaced bars colored by height (green ->
    yellow -> red) with white falling peak caps — the .COM text-4 display's
    big sibling."""

    def __init__(self, parent, bars: int = 20) -> None:
        self.win = _window(parent, "MCS-Convert — Spectrum Analyzer")
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.nbars = bars
        self.levels = [0.0] * bars
        self.peaks = [0.0] * bars

    def alive(self) -> bool:
        return bool(self.win.winfo_exists())

    def draw(self, spectrum: Optional[List[float]]) -> None:
        if not self.alive():
            return
        c = self.canvas
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 60)
        c.delete("all")
        bw = w / self.nbars
        for i in range(self.nbars):
            lv = 0.0 if spectrum is None else spectrum[i]
            self.levels[i] = max(lv, self.levels[i] - 0.06)      # slow release
            self.peaks[i] = max(self.levels[i], self.peaks[i] - 0.008)
            x0 = i * bw + 2
            x1 = (i + 1) * bw - 2
            top = h - 8 - (h - 24) * self.levels[i]
            # colored by height: green base, yellow middle, red top
            for frac, color in ((0.5, "#22cc22"), (0.8, "#cccc22"), (1.0, "#cc2222")):
                zone_top = h - 8 - (h - 24) * min(self.levels[i], frac)
                zone_bot = h - 8 - (h - 24) * (0 if frac == 0.5 else (0.5 if frac == 0.8 else 0.8))
                if zone_top < zone_bot:
                    c.create_rectangle(x0, zone_top, x1, zone_bot, fill=color, outline="")
            py = h - 8 - (h - 24) * self.peaks[i]
            c.create_rectangle(x0, py - 2, x1, py, fill="#ffffff", outline="")


#: DOS-replica styles the preview window can imitate (.COM scope names).
DOS_STYLES = ("combined monitor (text 5)", "spectrum analyzer (text 4)",
              "VU meters", "line scopes (text 3)", "block scopes (text 1)")


class DosVizWindow:
    """A replica of the standalone .COM players' DOS visualizations, drawn on a
    320x200-proportioned canvas with the DOS palette — so "what will this scope
    look like?" is answered before the .COM is ever built. The style mirrors
    the export dialog's scope selection."""

    def __init__(self, parent, style: str = DOS_STYLES[0],
                 names: Optional[List[str]] = None) -> None:
        self.win = _window(parent, f"DOS preview — {style}", (480, 320))
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0,
                                width=640, height=400)
        self.canvas.pack(fill="both", expand=True)
        self.style = style
        self.names = (names or ["P1", "P2", "Tr", "Nz"])[:4]
        self.vu = [0.0] * 4
        self.vupk = [0.0] * 4
        self.spec = [0.0] * 18
        self.phase = 0.0

    def alive(self) -> bool:
        return bool(self.win.winfo_exists())

    # -- shared painters ----------------------------------------------------
    def _grid_cell(self):
        c = self.canvas
        w = max(c.winfo_width(), 320)
        h = max(c.winfo_height(), 200)
        return c, w, h

    def _scope_trace(self, c, x0, y0, x1, y1, level: float, period: float,
                     color: str, line: bool) -> None:
        """One channel's square-wave trace: sounding -> a scrolling square of
        that period; silent -> the center line."""
        mid = (y0 + y1) / 2
        amp = (y1 - y0) * 0.32
        if level <= 0.001:
            c.create_line(x0, mid, x1, mid, fill=color)
            return
        pts = []
        px = max(6.0, period * 26.0)                 # on-screen half-period
        x = x0
        state = int((self.phase * 60) / px) & 1
        while x < x1:
            nxt = min(x1, x + px)
            y = mid - amp if state else mid + amp
            if line and pts:
                pts.extend([x, y])
            pts.extend([x, y, nxt, y])
            x = nxt
            state ^= 1
        c.create_line(*pts, fill=color, width=2)

    # -- draw ---------------------------------------------------------------
    def draw(self, levels, spectrum, periods) -> None:
        """levels: per-voice 0..1 (None = flat); spectrum: 18+ mags; periods:
        per-voice relative wave periods 0..1 (pitch -> tightness)."""
        if not self.alive():
            return
        c, w, h = self._grid_cell()
        c.delete("all")
        self.phase += 1.0
        lv = levels or [0.0] * 4
        sp = spectrum or [0.0] * 18
        pr = periods or [0.3] * 4
        style = self.style
        if style.startswith("combined"):
            self._draw_combined(c, w, h, lv, sp, pr)
        elif style.startswith("spectrum"):
            self._draw_spectrum(c, w, h, sp, full=True)
        elif style.startswith("VU"):
            self._draw_vu(c, w, h, lv, 0, 0, w, h, big=True)
        else:
            line = style.startswith("line")
            self._draw_scopegrid(c, w, h, lv, pr, 0, 0, w, h, line=line)

    def _draw_combined(self, c, w, h, lv, sp, pr) -> None:
        """The text-5 combined monitor: 2x2 scopes on top, spectrum bottom-left,
        VU meters bottom-right, one grey grid."""
        self._draw_scopegrid(c, w, h, lv, pr, 0, 0, w, h * 0.5, line=True)
        self._draw_spectrum(c, w, h, sp, region=(0, h * 0.5, w * 0.5, h))
        self._draw_vu(c, w, h, lv, w * 0.5, h * 0.5, w, h)
        grey = _DOS["grey"]
        c.create_rectangle(2, 2, w - 2, h - 2, outline=grey)
        c.create_line(2, h * 0.5, w - 2, h * 0.5, fill=grey)
        c.create_line(w * 0.5, 2, w * 0.5, h - 2, fill=grey)
        c.create_line(2, h * 0.25, w - 2, h * 0.25, fill=grey)

    def _draw_scopegrid(self, c, w, h, lv, pr, x0, y0, x1, y1, line=True) -> None:
        cw, chh = (x1 - x0) / 2, (y1 - y0) / 2
        for k in range(4):
            cx0 = x0 + (k % 2) * cw
            cy0 = y0 + (k // 2) * chh
            color = _DOS_CH[k]
            c.create_text(cx0 + 8, cy0 + 6, text=self.names[k], anchor="nw",
                          fill=color, font=("Consolas", 9))
            if k == 3 and lv[3] > 0.001:             # noise: jitter, not a square
                rng = np.random.default_rng(int(self.phase))
                ys = rng.uniform(-1, 1, 24)
                mid = cy0 + chh / 2
                pts = []
                for i, y in enumerate(ys):
                    pts.extend([cx0 + 8 + i * (cw - 16) / 23,
                                mid + y * chh * 0.3])
                c.create_line(*pts, fill=color)
            else:
                self._scope_trace(c, cx0 + 6, cy0 + 4, cx0 + cw - 6,
                                  cy0 + chh - 4, lv[k], pr[k], color, line)
            if not line:                             # block style: fill under trace
                pass
        grey = _DOS["grey"]
        c.create_line(x0 + 2, y0 + chh, x1 - 2, y0 + chh, fill=grey)

    def _draw_spectrum(self, c, w, h, sp, region=None, full=False) -> None:
        x0, y0, x1, y1 = region or (0, 0, w, h)
        n = len(sp)
        bw = (x1 - x0 - 16) / n
        for i in range(n):
            v = min(1.0, sp[i])
            self.spec[i % 18] = max(v, self.spec[i % 18] - 0.05)
            v = self.spec[i % 18]
            bx0 = x0 + 8 + i * bw + 1
            bx1 = x0 + 8 + (i + 1) * bw - 1
            top = y1 - 10 - (y1 - y0 - 24) * v
            color = (_DOS["green"] if v < 0.5 else
                     _DOS["yellow"] if v < 0.8 else _DOS["brightred"])
            c.create_rectangle(bx0, top, bx1, y1 - 10, fill=color, outline="")

    def _draw_vu(self, c, w, h, lv, x0, y0, x1, y1, big=False) -> None:
        n = 4
        row_h = (y1 - y0) / n
        for i in range(n):
            self.vu[i] = max(lv[i] if i < len(lv) else 0.0, self.vu[i] * 0.82)
            self.vupk[i] = max(self.vu[i], self.vupk[i] - 0.01)
            ry = y0 + i * row_h
            c.create_text(x0 + 10, ry + row_h / 2, text=self.names[i], anchor="w",
                          fill=_DOS_CH[i], font=("Consolas", 10 if big else 9))
            bx0 = x0 + 50
            bx1 = x1 - 16
            span = bx1 - bx0
            fill = bx0 + span * self.vu[i]
            for frac, color in ((0.65, _DOS["green"]), (0.85, _DOS["yellow"]),
                                (1.0, _DOS["brightred"])):
                s0 = bx0 + span * (0 if frac == 0.65 else (0.65 if frac == 0.85 else 0.85))
                s1 = min(fill, bx0 + span * frac)
                if s1 > s0:
                    c.create_rectangle(s0, ry + row_h * 0.3, s1, ry + row_h * 0.7,
                                       fill=color, outline="")
            px = bx0 + span * self.vupk[i]
            c.create_line(px, ry + row_h * 0.25, px, ry + row_h * 0.75,
                          fill="#ffffff", width=2)


def voice_periods(song) -> List[float]:
    """Relative scope periods per track (0..1, lower = higher pitch) from each
    track's median pitch — drives the DOS replica's wave tightness."""
    out = []
    for t in song.tracks:
        pitches = [n.midi_note for n in t.notes if not n.is_rest]
        if not pitches:
            out.append(0.3)
        else:
            med = sorted(pitches)[len(pitches) // 2]
            out.append(max(0.05, min(1.0, (96 - med) / 60.0)))
    return out
