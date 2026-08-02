"""RCTracker — the RetroComputerist Tracker editor.

A classic vertical tracker (FastTracker lineage): patterns as rows scrolling
down, four channel columns (note / instrument / volume / effect+param per
cell), keyboard-first entry, an order list, instruments and ornaments, live
playback rendered through the SAME effects flattener every export uses — what
you hear here is what DOS plays.

Keys
----
  piano        Z S X D C V G B H N J M , (low octave)  Q 2 W 3 E R 5 T 6 Y 7 U (high)
  A / `        note-off (===)          Del      clear cell
  0-9 A-F      hex entry in the inst / vol / fx columns
  arrows/Tab   move   PgUp/PgDn page   Home/End top/bottom
  F1/F2        octave down/up          step +/- with - / =
  F5 / F6      play song / pattern     F8  stop
  Space        toggle edit cursor between note and fx columns

Everything else is menus/panels: order list, instruments, ornaments, song
settings, import (NSF/PT3/MCS/RCT), export (.RCT/.MCS/.COM/WAV/RCPLAY.COM).
"""

from __future__ import annotations

import copy
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import rct as R
from .. import targetmode as TM
from ..audio import WaveOutPlayer, pcm16
from ..effects import flatten, render_flat
from . import viz

# ---- theme ------------------------------------------------------------------
BG = "#101014"
BG2 = "#16161c"
FG = "#c8c8d0"
DIM = "#5a5a68"
ACC = "#7fd17f"                  # RC green
CUR = "#2a4a2a"                  # cursor cell
ROWHL = "#1c2433"                # playing row
BEAT = "#171d28"                 # every 4th row shade
CHCOL = ("#e8d44d", "#e07070", "#6f9fe8", "#70d0a0")   # per-channel accents
LINT = "#d88a2a"                 # amber: a cell the current target drops
FONT = ("Consolas", 11)
FONTB = ("Consolas", 11, "bold")

#: piano keys -> semitone offsets from the current octave's C
_PIANO = {"z": 0, "s": 1, "x": 2, "d": 3, "c": 4, "v": 5, "g": 6, "b": 7,
          "h": 8, "n": 9, "j": 10, "m": 11, "comma": 12,
          "q": 12, "2": 13, "w": 14, "3": 15, "e": 16, "r": 17, "5": 18,
          "t": 19, "6": 20, "y": 21, "7": 22, "u": 23}

_FIELDS = 5                      # note / inst / vol / fx letter / param
_FX_KEYS = {v: i for i, v in enumerate(R.FX_LETTERS)}


class TrackerApp:
    """The RCTracker main window."""

    def __init__(self, root: tk.Tk, path: str = None):
        self.root = root
        root.title("RCTracker — RetroComputerist Tracker")
        root.configure(bg=BG)
        self.song = R.RctSong()
        self.path = None
        self.cur_pat = 0             # pattern being edited
        self.row = 0
        self.chan = 0
        self.field = 0               # 0 note 1 inst 2 vol 3 fx
        self.octave = 4
        self.step = 1
        self.mode = "free"           # target lint mode (TM.MODES)
        self.player = WaveOutPlayer()
        self._playing = False
        self._play_subs = 0
        self._play_follow = None
        self._flat = None            # FlatSong of the playing render (posmap)
        self._seek_base = 0.0        # seconds skipped when playing from a row
        self._undo: list = []        # (deepcopy(song), cur_pat, row) snapshots
        self._redo: list = []
        self.mute = [False] * 4
        self._voices = None
        self._master = None
        self.vizpack = viz.VizPack(root, ["CH1", "CH2", "CH3", "NSE"])
        self._dos_win = None
        self._periods = None
        self._build_ui()
        self._bind_keys()
        if path:
            self.open_file(path)
        self.refresh()
        self._viz_tick()

    # ---- UI construction ----------------------------------------------------

    def _build_ui(self):
        root = self.root
        # menu
        m = tk.Menu(root, bg=BG2, fg=FG, activebackground=CUR)
        fm = tk.Menu(m, tearoff=0, bg=BG2, fg=FG, activebackground=CUR)
        fm.add_command(label="New", command=self.new_song, accelerator="Ctrl+N")
        fm.add_command(label="Open… (.rct)", command=self.open_dialog,
                       accelerator="Ctrl+O")
        fm.add_command(label="Import… (.nsf/.pt3/.mcs)", command=self.import_dialog)
        fm.add_separator()
        fm.add_command(label="Save .RCT", command=self.save, accelerator="Ctrl+S")
        fm.add_command(label="Save .RCT As…", command=self.save_as)
        fm.add_separator()
        xm = tk.Menu(fm, tearoff=0, bg=BG2, fg=FG, activebackground=CUR)
        xm.add_command(label="4-voice PC speaker .COM",
                       command=lambda: self.export_com("4voice"))
        xm.add_command(label="4-voice foreground (XT) .COM",
                       command=lambda: self.export_com("4voice", foreground=True))
        xm.add_command(label="Tandy / PCjr .COM",
                       command=lambda: self.export_com("tandy"))
        xm.add_command(label="1-voice PC speaker .COM",
                       command=lambda: self.export_com("1voice"))
        xm.add_command(label=".MCS song", command=self.export_mcs)
        xm.add_command(label="WAV render", command=self.export_wav)
        xm.add_separator()
        xm.add_command(label="RCPLAY.COM (DOS player)", command=self.export_rcplay)
        fm.add_cascade(label="Export", menu=xm)
        fm.add_separator()
        fm.add_command(label="Export Center… (targets, previews, retrack)",
                       command=self.open_export_center, accelerator="Ctrl+E")
        fm.add_separator()
        fm.add_command(label="Quit", command=root.destroy)
        m.add_cascade(label="File", menu=fm)
        pm = tk.Menu(m, tearoff=0, bg=BG2, fg=FG, activebackground=CUR)
        pm.add_command(label="Play song", command=self.play_song, accelerator="F5")
        pm.add_command(label="Play pattern", command=self.play_pattern,
                       accelerator="F6")
        pm.add_command(label="Stop", command=self.stop, accelerator="F8")
        m.add_cascade(label="Play", menu=pm)
        root.config(menu=m)

        # top bar: song settings
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=6, pady=(6, 2))
        self.v_title = tk.StringVar(value=self.song.title)
        self.v_speed = tk.IntVar(value=self.song.speed)
        self.v_mode = tk.StringVar(value="3 tone + noise")
        self.v_status = tk.StringVar(value="ready")

        def lab(text):
            return tk.Label(top, text=text, bg=BG, fg=DIM, font=FONT)

        lab("Title").pack(side="left")
        tk.Entry(top, textvariable=self.v_title, width=18, bg=BG2, fg=FG,
                 insertbackground=FG, font=FONT).pack(side="left", padx=(2, 10))
        lab("BPM").pack(side="left")
        self.v_bpm = tk.StringVar(value=f"{self.song.bpm:.1f}")
        bpm_box = tk.Entry(top, textvariable=self.v_bpm, width=6, bg=BG2,
                           fg=FG, insertbackground=FG, font=FONT)
        bpm_box.pack(side="left", padx=(2, 2))
        bpm_box.bind("<Return>", lambda e: self._bpm_changed())
        bpm_box.bind("<FocusOut>", lambda e: self._bpm_changed())
        self.l_snap = tk.Label(top, text="", bg=BG, fg=DIM, font=FONT)
        self.l_snap.pack(side="left", padx=(0, 10))
        lab("Speed").pack(side="left")
        tk.Spinbox(top, from_=1, to=32, textvariable=self.v_speed, width=3,
                   bg=BG2, fg=FG, font=FONT, command=self._settings_changed
                   ).pack(side="left", padx=(2, 10))
        lab("Rows").pack(side="left")
        self.v_rows = tk.IntVar(value=32)
        tk.Spinbox(top, from_=1, to=64, textvariable=self.v_rows, width=3,
                   bg=BG2, fg=FG, font=FONT, command=self._rows_changed
                   ).pack(side="left", padx=(2, 10))
        lab("Channels").pack(side="left")
        mode = ttk.Combobox(top, textvariable=self.v_mode, width=14,
                            state="readonly",
                            values=["3 tone + noise", "4 tone"])
        mode.pack(side="left", padx=(2, 10))
        mode.bind("<<ComboboxSelected>>", lambda e: self._settings_changed())
        lab("Mode").pack(side="left")
        self.v_tmode = tk.StringVar(value=TM.MODE_LABELS["free"])
        tmode = ttk.Combobox(top, textvariable=self.v_tmode, width=16,
                             state="readonly",
                             values=[TM.MODE_LABELS[m] for m in TM.MODES])
        tmode.pack(side="left", padx=(2, 10))
        tmode.bind("<<ComboboxSelected>>", lambda e: self._mode_changed())
        self.l_oct = tk.Label(top, text="oct 4  step 1", bg=BG, fg=ACC, font=FONTB)
        self.l_oct.pack(side="right")
        self.v_follow = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="follow", variable=self.v_follow, bg=BG,
                       fg=FG, selectcolor=BG2, activebackground=BG,
                       font=FONT).pack(side="right", padx=(0, 8))
        self.v_vol = tk.DoubleVar(value=80.0)
        tk.Scale(top, from_=0, to=100, orient="horizontal",
                 variable=self.v_vol, showvalue=False, length=90, bg=BG,
                 troughcolor=BG2, highlightthickness=0,
                 command=lambda v: self.player.set_volume(float(v) / 100.0)
                 ).pack(side="right", padx=(0, 8))
        tk.Label(top, text="vol", bg=BG, fg=DIM, font=FONT).pack(side="right")

        vbar = tk.Frame(root, bg=BG)
        vbar.pack(fill="x", padx=6)
        for txt, cmd in (("\u3030 Scope", lambda: self.vizpack.open_scope()),
                         ("\u25ae VU", lambda: self.vizpack.open_vu()),
                         ("\u2581\u2583\u2585 Spectrum",
                          lambda: self.vizpack.open_spectrum()),
                         ("\u25a6 DOS view", self.open_dos_view)):
            tk.Button(vbar, text=txt, command=cmd, bg=BG2, fg=FG,
                      relief="flat", activebackground=CUR, font=FONT
                      ).pack(side="left", padx=(0, 4), pady=(0, 2))
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=6, pady=2)

        # left rail: order list + pattern picker
        rail = tk.Frame(body, bg=BG)
        rail.pack(side="left", fill="y", padx=(0, 6))
        tk.Label(rail, text="ORDER", bg=BG, fg=ACC, font=FONTB).pack(anchor="w")
        of = tk.Frame(rail, bg=BG)
        of.pack(fill="y", expand=False)
        self.orderbox = tk.Listbox(of, width=9, height=14, bg=BG2, fg=FG,
                                   font=FONT, selectbackground=CUR,
                                   exportselection=False)
        self.orderbox.pack(side="left", fill="y")
        self.orderbox.bind("<<ListboxSelect>>", self._order_pick)
        ob = tk.Frame(rail, bg=BG)
        ob.pack(fill="x")
        for txt, cmd in (("+", self.order_add), ("−", self.order_del),
                         ("↑", lambda: self.order_move(-1)),
                         ("↓", lambda: self.order_move(1)),
                         ("pat#", self.order_set)):
            tk.Button(ob, text=txt, command=cmd, bg=BG2, fg=FG, width=3,
                      relief="flat", activebackground=CUR).pack(side="left")
        tk.Label(rail, text="INSTRUMENTS", bg=BG, fg=ACC, font=FONTB
                 ).pack(anchor="w", pady=(8, 0))
        self.instbox = tk.Listbox(rail, width=16, height=8, bg=BG2, fg=FG,
                                  font=FONT, selectbackground=CUR,
                                  exportselection=False)
        self.instbox.pack()
        self.instbox.bind("<Double-Button-1>", lambda e: self.edit_instrument())
        ib = tk.Frame(rail, bg=BG)
        ib.pack(fill="x")
        tk.Button(ib, text="edit", command=self.edit_instrument, bg=BG2, fg=FG,
                  relief="flat", activebackground=CUR).pack(side="left")
        tk.Button(ib, text="+", command=self.add_instrument, bg=BG2, fg=FG,
                  relief="flat", activebackground=CUR).pack(side="left")
        tk.Button(ib, text="ornaments", command=self.edit_ornaments, bg=BG2,
                  fg=FG, relief="flat", activebackground=CUR).pack(side="left")

        # the pattern grid
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<MouseWheel>",
                         lambda e: self.move(-3 if e.delta > 0 else 3, 0))

        status = tk.Label(root, textvariable=self.v_status, bg=BG2, fg=DIM,
                          anchor="w", font=FONT)
        status.pack(fill="x", side="bottom")

    def _bind_keys(self):
        r = self.root
        r.bind("<Up>", lambda e: self.move(-1, 0))
        r.bind("<Down>", lambda e: self.move(1, 0))
        r.bind("<Left>", lambda e: self.move(0, -1))
        r.bind("<Right>", lambda e: self.move(0, 1))
        r.bind("<Tab>", self._tab)
        r.bind("<Prior>", lambda e: self.move(-8, 0))
        r.bind("<Next>", lambda e: self.move(8, 0))
        r.bind("<Home>", lambda e: self.jump(0))
        r.bind("<End>", lambda e: self.jump(self.pattern().rows - 1))
        r.bind("<Delete>", lambda e: self.clear_cell())
        r.bind("<F1>", lambda e: self.set_octave(self.octave - 1))
        r.bind("<F2>", lambda e: self.set_octave(self.octave + 1))
        r.bind("<F5>", lambda e: self.play_song())
        r.bind("<F6>", lambda e: self.play_pattern())
        r.bind("<F8>", lambda e: self.stop())
        r.bind("<minus>", lambda e: self.set_step(self.step - 1))
        r.bind("<equal>", lambda e: self.set_step(self.step + 1))
        r.bind("<space>", self._space)
        r.bind("<Control-z>", lambda e: self.undo())
        r.bind("<Control-y>", lambda e: self.redo())
        r.bind("<F7>", lambda e: self.v_follow.set(not self.v_follow.get()))
        r.bind("<F9>", lambda e: self.play_from_row())
        r.bind("<Control-n>", lambda e: self.new_song())
        r.bind("<Control-o>", lambda e: self.open_dialog())
        r.bind("<Control-s>", lambda e: self.save())
        r.bind("<Control-e>", lambda e: self.open_export_center())
        r.bind("<Key>", self._key)

    # ---- undo ----------------------------------------------------------------

    def _checkpoint(self):
        """Push an undo snapshot BEFORE a mutation (multi-level, capped)."""
        self._undo.append((copy.deepcopy(self.song), self.cur_pat, self.row))
        if len(self._undo) > 100:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return
        self._redo.append((copy.deepcopy(self.song), self.cur_pat, self.row))
        self.song, self.cur_pat, self.row = self._undo.pop()
        self._after_history_restore()

    def redo(self):
        if not self._redo:
            return
        self._undo.append((copy.deepcopy(self.song), self.cur_pat, self.row))
        self.song, self.cur_pat, self.row = self._redo.pop()
        self._after_history_restore()

    def _after_history_restore(self):
        if self.cur_pat not in self.song.patterns:
            self.cur_pat = self.song.order[0] if self.song.order else 0
        self.row = min(self.row, self.pattern().rows - 1)
        self.chan = min(self.chan, 3)
        self._sync_settings()
        self.refresh()

    # ---- model helpers -------------------------------------------------------

    def pattern(self) -> R.RctPattern:
        return self.song.patterns.setdefault(self.cur_pat, R.RctPattern())

    def cell(self) -> R.RctCell:
        return self.pattern().cell(self.row, self.chan)

    def _rows_changed(self):
        self._checkpoint()
        """Resize the CURRENT pattern (cells beyond the new length are kept in
        memory only until save, like most trackers)."""
        pat = self.pattern()
        n = max(1, min(64, int(self.v_rows.get())))
        while len(pat.cells) < n:
            pat.cells.append([R.RctCell() for _ in range(4)])
        pat.rows = n
        self.row = min(self.row, n - 1)
        self.refresh()

    def _settings_changed(self):
        self._checkpoint()
        self.song.title = self.v_title.get()[:32]
        self.song.speed = max(1, min(32, int(self.v_speed.get())))
        self.song.channel_mode = (R.MODE_4TONE if self.v_mode.get() == "4 tone"
                                  else R.MODE_3TONE_NOISE)
        self.refresh()

    def _mode_changed(self):
        label = self.v_tmode.get()
        self.mode = next((m for m in TM.MODES if TM.MODE_LABELS[m] == label),
                         "free")
        if TM.tempo_is_snapped(self.mode) and self.song.subtick_us:
            self._checkpoint()
            self.song.tempo_byte0 = self.song.mcs_tempo_byte()
            self.song.subtick_us = 0              # MCS-mode: lock to the grid
            self._sync_settings()
        self.refresh()

    def _cell_wave(self, cell) -> str:
        ins = self.song.instruments.get(cell.inst)
        return ins.waveform if ins else "square"

    def _lint_count(self) -> int:
        if self.mode == "free":
            return 0
        n = 0
        for pat in self.song.patterns.values():
            for row in pat.cells:
                for cell in row:
                    if not cell.empty and TM.lint_cell(
                            cell, self.mode, self._cell_wave(cell)):
                        n += 1
        return n

    def _bpm_changed(self):
        try:
            bpm = float(self.v_bpm.get())
        except ValueError:
            self.v_bpm.set(f"{self.song.bpm:.1f}")
            return
        self._checkpoint()
        self.song.set_bpm(bpm)                       # arbitrary; MCS byte snaps
        if TM.tempo_is_snapped(self.mode):           # MCS-mode: lock to grid
            self.song.subtick_us = 0
        self.v_bpm.set(f"{self.song.bpm:.1f}")
        self.refresh()

    # ---- navigation ----------------------------------------------------------

    def move(self, drow: int, dfield: int):
        if self._typing_elsewhere():
            return None
        pat = self.pattern()
        if drow:
            self.row = (self.row + drow) % pat.rows
        if dfield:
            f = self.chan * _FIELDS + self.field + dfield
            f %= 4 * _FIELDS
            self.chan, self.field = divmod(f, _FIELDS)
        self.refresh()
        return "break"

    def _tab(self, _e):
        self.chan = (self.chan + 1) % 4
        self.field = 0
        self.refresh()
        return "break"

    def jump(self, row: int):
        self.row = max(0, min(self.pattern().rows - 1, row))
        self.refresh()

    def set_octave(self, o: int):
        self.octave = max(0, min(7, o))
        self.refresh()

    def set_step(self, s: int):
        self.step = max(0, min(16, s))
        self.refresh()

    def _space(self, _e):
        self.field = 3 if self.field == 0 else 0     # hop note <-> fx
        self.refresh()
        return "break"

    def _click(self, e):
        self.canvas.focus_set()
        self.root.focus_set()                        # leave any Entry widget
        col = (e.x - 40) // self.chan_w
        if e.y < 24:                                 # header: mute / solo
            if 0 <= col < 4:
                if e.state & 0x4:                    # Ctrl+click = solo
                    solo = [c != col for c in range(4)]
                    self.mute = ([False] * 4 if self.mute == solo else solo)
                else:
                    self.mute[col] = not self.mute[col]
                self.refresh()
            return
        # canvas cell geometry mirrors _draw
        row = (e.y - 24) // 18 + self.top_row
        if 0 <= col < 4 and 0 <= row < self.pattern().rows:
            self.row, self.chan = row, col
            self.refresh()

    # ---- editing -------------------------------------------------------------

    def _typing_elsewhere(self) -> bool:
        """True while an Entry/Spinbox/Combobox has keyboard focus -- grid keys
        must not fire while the user types in the Title box."""
        w = self.root.focus_get()
        return isinstance(w, (tk.Entry, tk.Spinbox, ttk.Combobox))

    def _key(self, e):
        if e.state & 0x4:                            # Ctrl combos handled above
            return None
        if self._typing_elsewhere():
            return None
        ks = e.keysym.lower()
        if self.field == 0:                          # note column
            if ks in _PIANO or ks in ("a", "grave"):
                self._checkpoint()
            if ks in ("a", "grave"):
                self.cell().note = R.NOTE_OFF
                return self._advance()
            if ks in _PIANO:
                note = 12 * self.octave + _PIANO[ks] + 1
                c = self.cell()
                c.note = max(1, min(R.NOTE_MAX, note))
                if c.inst == 0:
                    c.inst = self._sel_inst()
                return self._advance()
            return None
        ch = e.char.lower()
        if not ch:
            return None
        self._checkpoint()
        c = self.cell()
        if self.field == 3:                          # effect LETTER column
            up = ch.upper()
            if up in _FX_KEYS and up != "-":         # A 1 2 3 4 V C D O F B
                c.fx = _FX_KEYS[up]
            elif ch in ("-", "."):
                c.fx = c.param = 0
            else:
                return None
        elif ch in "0123456789abcdef-.":
            if self.field == 1:                      # instrument (hex nibble)
                c.inst = int(ch, 16) if ch in "0123456789abcdef" else 0
            elif self.field == 2:                    # volume 0-F
                c.vol = (int(ch, 16) + 1) if ch in "0123456789abcdef" else 0
            else:                                    # param: hex digits roll in
                c.param = (0 if ch in "-." else
                           ((c.param << 4) & 0xFF) | int(ch, 16))
        else:
            return None
        self.refresh()
        return "break"

    def _advance(self):
        self.row = (self.row + self.step) % self.pattern().rows
        self.refresh()
        return "break"

    def _sel_inst(self) -> int:
        sel = self.instbox.curselection()
        keys = sorted(self.song.instruments)
        return keys[sel[0]] if sel and sel[0] < len(keys) else (keys[0] if keys else 1)

    def clear_cell(self):
        if self._typing_elsewhere():
            return None
        self._checkpoint()
        pat = self.pattern()
        pat.cells[self.row][self.chan] = R.RctCell()
        self._advance()

    # ---- order list ----------------------------------------------------------

    def _order_pick(self, _e):
        sel = self.orderbox.curselection()
        if sel:
            self.cur_pat = self.song.order[sel[0]]
            self.row = 0
            self.v_rows.set(self.pattern().rows)
            self.refresh()

    def order_add(self):
        self._checkpoint()
        new = max(self.song.patterns) + 1 if len(self.song.patterns) < 256 else None
        if new is None:
            return
        self.song.patterns[new] = R.RctPattern(rows=self.pattern().rows)
        self.song.order.append(new)
        self.cur_pat = new
        self.refresh()

    def order_del(self):
        self._checkpoint()
        if len(self.song.order) > 1:
            sel = self.orderbox.curselection()
            i = sel[0] if sel else len(self.song.order) - 1
            self.song.order.pop(i)
            self.refresh()

    def order_move(self, d: int):
        sel = self.orderbox.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + d
        if 0 <= j < len(self.song.order):
            o = self.song.order
            o[i], o[j] = o[j], o[i]
            self.refresh()
            self.orderbox.selection_set(j)

    def order_set(self):
        sel = self.orderbox.curselection()
        if not sel:
            return
        n = simpledialog.askinteger("Pattern", "pattern # (0-255):",
                                    parent=self.root, minvalue=0, maxvalue=255)
        if n is None:
            return
        self.song.patterns.setdefault(n, R.RctPattern(rows=self.pattern().rows))
        self.song.order[sel[0]] = n
        self.cur_pat = n
        self.refresh()

    # ---- instruments / ornaments --------------------------------------------

    def add_instrument(self):
        self._checkpoint()
        free = [i for i in range(1, 16) if i not in self.song.instruments]
        if free:
            self.song.instruments[free[0]] = R.RctInstrument()
            self.refresh()

    def edit_instrument(self):
        keys = sorted(self.song.instruments)
        sel = self.instbox.curselection()
        if not (keys and sel):
            return
        idx = keys[sel[0]]
        ins = self.song.instruments[idx]
        d = tk.Toplevel(self.root)
        d.title(f"Instrument {idx:X}")
        d.configure(bg=BG)
        tk.Label(d, text="Name", bg=BG, fg=FG, font=FONT).grid(row=0, column=0)
        vn = tk.StringVar(value=ins.name)
        tk.Entry(d, textvariable=vn, bg=BG2, fg=FG, insertbackground=FG,
                 font=FONT).grid(row=0, column=1)
        tk.Label(d, text="Waveform", bg=BG, fg=FG, font=FONT).grid(row=1, column=0)
        vw = tk.StringVar(value=ins.waveform)
        ttk.Combobox(d, textvariable=vw, values=list(R.WAVEFORM_IDS),
                     state="readonly").grid(row=1, column=1)
        tk.Label(d, text="Volume 0-15", bg=BG, fg=FG, font=FONT).grid(row=2, column=0)
        vv = tk.IntVar(value=ins.volume)
        tk.Spinbox(d, from_=0, to=15, textvariable=vv, width=4, bg=BG2,
                   fg=FG, font=FONT).grid(row=2, column=1)
        tk.Label(d, text="Ornament (0=none)", bg=BG, fg=FG, font=FONT
                 ).grid(row=3, column=0)
        vo = tk.IntVar(value=ins.ornament)
        tk.Spinbox(d, from_=0, to=15, textvariable=vo, width=4, bg=BG2,
                   fg=FG, font=FONT).grid(row=3, column=1)

        def ok():
            ins.name = vn.get()[:16]
            ins.waveform = vw.get()
            ins.volume = int(vv.get()) & 0x0F
            ins.ornament = int(vo.get())
            d.destroy()
            self.refresh()
        tk.Button(d, text="OK", command=ok, bg=BG2, fg=FG).grid(row=4, column=1)

    def edit_ornaments(self):
        cur = "; ".join(f"{i}: {','.join(map(str, o.steps))} loop {o.loop}"
                        for i, o in sorted(self.song.ornaments.items())) or "(none)"
        s = simpledialog.askstring(
            "Ornaments", "index: steps loop N (semicolon-separated)\n"
            "e.g.  1: 0,12 loop 0; 2: 0,4,7 loop 0\ncurrent: " + cur,
            parent=self.root)
        if s is None:
            return
        try:
            orns = {}
            for part in filter(None, (p.strip() for p in s.split(";"))):
                head, _, tail = part.partition(":")
                steps_s, _, loop_s = tail.partition("loop")
                steps = [int(x) for x in steps_s.replace(" ", "").split(",") if x]
                orns[int(head)] = R.RctOrnament(
                    steps=steps or [0], loop=int(loop_s or 0))
            self.song.ornaments = orns
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Ornaments", f"could not parse: {exc}")
        self.refresh()

    # ---- Export Center (MCS-Player's ExportDialog, adapted) ------------------

    def open_export_center(self):
        """Open the full export dialog -- every target with its own preview
        (audio + DOS viz + MCS NOTATION), Retrack-into-editor, and the tempo
        optimizers -- driven by an adapter that speaks the universal Song the
        dialog expects and folds any retrack result back into the tracker."""
        from ..convert import rct_to_universal
        from .export import ExportDialog
        self.stop()
        adapter = _ExportHost(self)
        adapter.song = rct_to_universal(self.song)
        adapter.song.tempo_tick_seconds = self.song.subtick_seconds * 4.0
        ExportDialog(adapter)

    def _load_universal(self, song, label: str = ""):
        """The Export Center's Retrack reloads the tracker with a reduced
        universal Song -- convert it back to RctSong (an automatic undo
        snapshot first, so Ctrl+Z restores the pre-retrack tracker)."""
        from ..convert import song_to_rct
        self._checkpoint()
        keep_bpm = self.song.subtick_us
        self.song = song_to_rct(song, tempo_byte0=self.song.tempo_byte0,
                                title=self.song.title)
        if keep_bpm:                                 # retrack preserves the BPM
            self.song.subtick_us = keep_bpm
            self.song.tempo_byte0 = self.song.mcs_tempo_byte()
        self.cur_pat = self.song.order[0]
        self.row = self.chan = 0
        self._sync_settings()
        self.refresh()
        self.v_status.set(f"tracker reloaded — {label}")

    # ---- DOS-replica preview -------------------------------------------------

    def open_dos_view(self):
        """The DOS visualization replica: pick a .COM display style, see the
        exact scope/VU/spectrum the standalone player will draw."""
        m = tk.Menu(self.root, tearoff=0, bg=BG2, fg=FG, activebackground=CUR)
        for style in viz.DOS_STYLES:
            m.add_command(label=style,
                          command=lambda s=style: self._open_dos(s))
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def _open_dos(self, style: str):
        if self._dos_win is not None and self._dos_win.alive():
            self._dos_win.win.destroy()
        self._dos_win = viz.DosVizWindow(self.root, style,
                                         ["CH1", "CH2", "CH3", "NSE"])
        waves = [i.waveform for i in self.song.instruments.values()]
        self._dos_win.set_wave(max(set(waves), key=waves.count)
                               if waves else "square")

    # ---- the always-on viz tick ----------------------------------------------

    def _viz_tick(self):
        if not self.root.winfo_exists():
            return
        self.root.after(33, self._viz_tick)
        pos = None
        if self._playing and self._master is not None:
            pos = self.player.position_seconds() + self._seek_base
        self.vizpack.tick(self._master, self._voices, 44100, pos)
        if self._dos_win is not None and self._dos_win.alive():
            if pos is not None and self._voices is not None:
                idx = int(pos * 44100)
                span = int(0.030 * 44100)
                self._dos_win.draw(viz._rms_levels(self._voices, idx, span),
                                   viz._spectrum(self._master, idx, 44100, 18),
                                   self._periods, elapsed=pos)
            else:
                self._dos_win.draw(None, None, None, elapsed=0.0)

    # ---- playback ------------------------------------------------------------

    def _render(self, song: R.RctSong):
        flat = flatten(song)
        for c, m in enumerate(self.mute):            # muted channels go silent
            if m:
                n = flat.total_subs
                flat.channels[c].pitch = [None] * n
        master, voices = render_flat(flat, sr=44100)
        self._voices = voices                        # for the viz windows
        return master, flat

    def play_song(self):
        self.stop()
        try:
            master, flat = self._render(self.song)
        except Exception as exc:
            messagebox.showerror("Play", str(exc))
            return
        self._play_total = flat.total_subs
        self._sub_s = flat.subtick_seconds
        self._flat = flat
        self._seek_base = 0.0
        self._master = master
        try:
            from ..convert import rct_to_universal
            pr = viz.voice_periods(rct_to_universal(self.song))
            self._periods = (list(pr) + [0.3] * 4)[:4]   # always 4 entries
        except Exception:
            self._periods = [0.3] * 4
        self.player.play(pcm16(master), 44100)
        self._playing = True
        self._follow()

    def play_pattern(self):
        self.stop()
        solo = R.RctSong(channel_mode=self.song.channel_mode,
                         tempo_byte0=self.song.tempo_byte0,
                         speed=self.song.speed)
        solo.patterns = {0: self.pattern()}
        solo.order = [0]
        solo.instruments = self.song.instruments
        solo.ornaments = self.song.ornaments
        try:
            master, flat = self._render(solo)
        except Exception as exc:
            messagebox.showerror("Play", str(exc))
            return
        self._play_total = flat.total_subs
        self._sub_s = flat.subtick_seconds
        self._flat = None                            # pattern solo: no follow map
        self._seek_base = 0.0
        self._master = master
        self.player.play(pcm16(master), 44100)
        self._playing = True
        self._follow()

    def play_from_row(self):
        """F9: start song playback at the cursor's row (first occurrence of
        the current pattern+row in the order walk)."""
        self.stop()
        try:
            master, flat = self._render(self.song)
        except Exception as exc:
            messagebox.showerror("Play", str(exc))
            return
        start_sub = 0
        for s, (_op, pat, row) in enumerate(flat.posmap):
            if pat == self.cur_pat and row == self.row:
                start_sub = s
                break
        self._play_total = flat.total_subs
        self._sub_s = flat.subtick_seconds
        self._flat = flat
        self._master = master
        self._seek_base = start_sub * flat.subtick_seconds
        off = int(self._seek_base * 44100)
        self.player.play(pcm16(master[off:]), 44100)
        self._playing = True
        self._follow()

    def _follow(self):
        if not self._playing:
            return
        pos = self.player.position_seconds() + self._seek_base
        sub = int(pos / self._sub_s) if self._sub_s else 0
        self._play_subs = sub
        if sub >= self._play_total:
            self.stop()
            return
        # FOLLOW: the pattern view rolls forward with playback (the posmap
        # already accounts for speed changes and pattern breaks)
        if (self.v_follow.get() and self._flat is not None
                and sub < len(self._flat.posmap)):
            op, pat, row = self._flat.posmap[sub]
            if pat != self.cur_pat or row != self.row:
                self.cur_pat, self.row = pat, row
                self.orderbox.selection_clear(0, "end")
                if op < len(self.song.order):
                    self.orderbox.selection_set(op)
                self.v_rows.set(self.pattern().rows)
        self.refresh()
        self._play_follow = self.root.after(50, self._follow)

    def stop(self):
        self._playing = False
        if self._play_follow:
            self.root.after_cancel(self._play_follow)
            self._play_follow = None
        try:
            self.player.stop()
        except Exception:
            pass
        self.refresh()

    # ---- file ops ------------------------------------------------------------

    def new_song(self):
        self.stop()
        self.song = R.RctSong()
        self.path = None
        self.cur_pat = self.row = self.chan = self.field = 0
        self._sync_settings()
        self.refresh()

    def open_dialog(self):
        p = filedialog.askopenfilename(filetypes=[("RCT tracker", "*.rct")])
        if p:
            self.open_file(p)

    def open_file(self, path: str):
        self.stop()
        try:
            self.song = R.load(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open", str(exc))
            return
        self.path = path
        self.cur_pat = self.song.order[0] if self.song.order else 0
        self.row = self.chan = self.field = 0
        self._sync_settings()
        self.refresh()

    def import_dialog(self):
        p = filedialog.askopenfilename(filetypes=[
            ("chiptunes", "*.nsf;*.pt3;*.mcs;*.mcd"), ("all", "*.*")])
        if p:
            self.stop()
            _ImportDialog(self, p)

    def _load_import(self, rct_song, source: str):
        """Called by the import dialog with a finished RctSong."""
        self._checkpoint()
        self.song = rct_song
        self.path = None
        self.cur_pat = self.song.order[0]
        self.row = self.chan = 0
        self.mute = [False] * 4
        self._sync_settings()
        self.refresh()
        self.v_status.set(f"imported {source} — {self.song.bpm:.1f} BPM "
                          f"(exact tempo preserved)")

    def save(self):
        if not self.path:
            return self.save_as()
        self._do_save(self.path)

    def save_as(self):
        p = filedialog.asksaveasfilename(defaultextension=".rct",
                                         filetypes=[("RCT tracker", "*.rct")])
        if p:
            self._do_save(p)

    def _do_save(self, path: str):
        self._settings_changed()
        from ..streams import perf_chunks
        try:
            self.song.perf = perf_chunks(self.song)   # bake DOS streams on save
            R.save(path, self.song)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Save", str(exc))
            return
        self.path = path
        self.v_status.set(f"saved {path} (with DOS performance streams)")

    def export_com(self, mode: str, foreground: bool = False):
        from ..streams import build_com
        p = filedialog.asksaveasfilename(defaultextension=".com",
                                         filetypes=[("DOS player", "*.com")])
        if not p:
            return
        try:
            ts = 5 if (mode == "4voice" and not foreground) else 0
            data = build_com(self.song, mode, foreground=foreground,
                             text_scope=ts)
            with open(p, "wb") as fh:
                fh.write(data)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export", str(exc))
            return
        self.v_status.set(f"wrote {p} ({len(data)} bytes)")

    def export_mcs(self):
        p = filedialog.asksaveasfilename(defaultextension=".mcs",
                                         filetypes=[("MCS song", "*.mcs")])
        if not p:
            return
        from ..convert import rct_to_universal
        from ..mcs.encode import encode_song
        from ..retrack import retrack
        try:
            data = encode_song(retrack(rct_to_universal(self.song), "mcs"),
                               tempo_byte0=self.song.tempo_byte0, cap=True)
            with open(p, "wb") as fh:
                fh.write(data)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export", str(exc))
            return
        self.v_status.set(f"wrote {p} ({len(data)} bytes)")

    def export_wav(self):
        p = filedialog.asksaveasfilename(defaultextension=".wav",
                                         filetypes=[("WAV", "*.wav")])
        if not p:
            return
        from ..audio import wav_bytes
        try:
            master, _ = self._render(self.song)
            with open(p, "wb") as fh:
                fh.write(wav_bytes(pcm16(master), 44100))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export", str(exc))
            return
        self.v_status.set(f"wrote {p}")

    def export_rcplay(self):
        p = filedialog.asksaveasfilename(defaultextension=".com",
                                         initialfile="RCPLAY.COM",
                                         filetypes=[("DOS player", "*.com")])
        if not p:
            return
        from ..rcplay_dos import save_rcplay
        try:
            n = save_rcplay(p)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export", str(exc))
            return
        self.v_status.set(f"wrote {p} ({n} bytes) — put it next to your .RCT "
                          f"files in DOS")

    def _sync_settings(self):
        self.v_title.set(self.song.title)
        self.v_rows.set(self.pattern().rows)
        self.v_bpm.set(f"{self.song.bpm:.1f}")
        self.v_speed.set(self.song.speed)
        self.v_mode.set("4 tone" if self.song.channel_mode == R.MODE_4TONE
                        else "3 tone + noise")

    # ---- drawing -------------------------------------------------------------

    top_row = 0
    chan_w = 150

    def refresh(self):
        self.l_oct.config(text=f"oct {self.octave}  step {self.step}"
                          + ("  ▶" if self._playing else ""))
        self.l_snap.config(
            text=f"→ MCS {self.song.tempo_byte0:#04x}"
                 if self.song.subtick_us else "(MCS grid)")
        if self.mode != "free":
            n = self._lint_count()
            self.v_status.set(
                f"{TM.MODE_LABELS[self.mode]} mode: "
                + (f"{n} cell(s) won't fully export (amber)" if n
                   else "every cell exports cleanly"))
        # order list
        self.orderbox.delete(0, "end")
        for i, pnum in enumerate(self.song.order):
            mark = "▶" if pnum == self.cur_pat else " "
            self.orderbox.insert("end", f"{mark}{i:02X}:{pnum:02X}")
        # instrument list
        self.instbox.delete(0, "end")
        for i in sorted(self.song.instruments):
            ins = self.song.instruments[i]
            self.instbox.insert("end", f"{i:X} {ins.name[:10]:<10}{ins.waveform[:4]}")
        if self.song.instruments and not self.instbox.curselection():
            self.instbox.selection_set(0)
        self._draw()

    def _draw(self):
        cv = self.canvas
        cv.delete("all")
        pat = self.pattern()
        h = max(1, cv.winfo_height() or 400)
        vis_rows = max(8, (h - 30) // 18)
        self.top_row = max(0, min(self.row - vis_rows // 2,
                                  pat.rows - vis_rows))
        # header
        cv.create_text(20, 12, text=f"P{self.cur_pat:02X}", fill=ACC, font=FONTB)
        names = (["CH1", "CH2", "CH3",
                  "NSE" if self.song.channel_mode == R.MODE_3TONE_NOISE else "CH4"])
        for c, name in enumerate(names):
            label = f"[{name}]" if self.mute[c] else name
            cv.create_text(40 + c * self.chan_w + self.chan_w // 2, 12,
                           text=label, fill=DIM if self.mute[c] else CHCOL[c],
                           font=FONTB)
        play_row = None
        if (self._playing and self._flat is not None
                and self._play_subs < len(self._flat.posmap)):
            _op, ppat, prow = self._flat.posmap[self._play_subs]
            if ppat == self.cur_pat:
                play_row = prow
        elif self._playing and self._flat is None:
            play_row = self._play_subs // max(1, self.song.speed)
            if play_row >= pat.rows:
                play_row = None
        for i in range(vis_rows):
            row = self.top_row + i
            if row >= pat.rows:
                break
            y = 24 + i * 18 + 8
            if row == play_row:
                cv.create_rectangle(0, y - 9, 40 + 4 * self.chan_w, y + 9,
                                    fill=ROWHL, width=0)
            elif row % 4 == 0:
                cv.create_rectangle(0, y - 9, 40 + 4 * self.chan_w, y + 9,
                                    fill=BEAT, width=0)
            cv.create_text(20, y, text=f"{row:02X}", fill=DIM, font=FONT)
            for c in range(4):
                cell = pat.cell(row, c)
                x0 = 40 + c * self.chan_w
                if row == self.row and c == self.chan:
                    fx0 = x0 + (0, 46, 64, 84, 100)[self.field]
                    fx1 = x0 + (44, 62, 82, 98, 128)[self.field]
                    cv.create_rectangle(fx0, y - 9, fx1, y + 9, fill=CUR, width=0)
                bad = self.mode != "free" and TM.lint_cell(
                    cell, self.mode, self._cell_wave(cell))
                note = R.note_name(cell.note)
                inst = f"{cell.inst:X}" if cell.inst else "."
                vol = f"{cell.vol - 1:X}" if cell.vol else "."
                if bad:
                    cv.create_text(x0 + 4, y, text="•",
                                   fill=LINT, font=FONT, anchor="w")
                fxl = R.FX_LETTERS[cell.fx] if cell.fx else "."
                fxp = f"{cell.param:02X}" if (cell.fx or cell.param) else ".."
                colour = CHCOL[c] if cell.note else FG if not cell.empty else DIM
                cv.create_text(x0 + 22, y, text=note, fill=colour, font=FONT)
                cv.create_text(x0 + 54, y, text=inst, fill=FG if cell.inst else DIM,
                               font=FONT)
                cv.create_text(x0 + 73, y, text=vol, fill=FG if cell.vol else DIM,
                               font=FONT)
                cv.create_text(x0 + 91, y, text=fxl,
                               fill=ACC if cell.fx else DIM, font=FONT)
                cv.create_text(x0 + 112, y, text=fxp,
                               fill=ACC if cell.fx else DIM, font=FONT)


class _ImportDialog(tk.Toplevel):
    """Import options for NSF / PT3 / MCS: subsong, percussion handling, drop
    noise, with per-channel note-count stats and an audition button. Preserves
    the source's exact tempo (song_to_rct sets subtick_us)."""

    _PERC = {"two-tone clicks": ("clicks", "auto"),
             "wood block": ("clicks", "block"),
             "as written (pitched)": ("pitched", "auto"),
             "drop": ("drop", "auto")}

    def __init__(self, app: "TrackerApp", path: str):
        super().__init__(app.root)
        self.app = app
        self.path = path
        self.ext = path.lower().rsplit(".", 1)[-1]
        self.title(f"Import — {path.rsplit('/', 1)[-1]}")
        self.configure(bg=BG)
        self._preview = None
        r = 0
        tk.Label(self, text=path, bg=BG, fg=ACC, font=FONT, wraplength=380,
                 anchor="w").grid(row=r, column=0, columnspan=2, sticky="w",
                                  padx=8, pady=(8, 4))
        r += 1
        self.v_sub = tk.IntVar(value=1)
        if self.ext == "nsf":
            tk.Label(self, text="Subsong #", bg=BG, fg=FG, font=FONT).grid(
                row=r, column=0, sticky="w", padx=8)
            tk.Spinbox(self, from_=1, to=255, textvariable=self.v_sub, width=5,
                       bg=BG2, fg=FG, font=FONT, command=self._restat).grid(
                row=r, column=1, sticky="w")
            r += 1
        tk.Label(self, text="Percussion", bg=BG, fg=FG, font=FONT).grid(
            row=r, column=0, sticky="w", padx=8)
        self.v_perc = tk.StringVar(value="two-tone clicks")
        ttk.Combobox(self, textvariable=self.v_perc, width=18, state="readonly",
                     values=list(self._PERC)).grid(row=r, column=1, sticky="w")
        r += 1
        self.v_dropn = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="drop the noise channel", variable=self.v_dropn,
                       bg=BG, fg=FG, selectcolor=BG2, activebackground=BG,
                       font=FONT).grid(row=r, column=0, columnspan=2, sticky="w",
                                       padx=6)
        r += 1
        self.stats = tk.Label(self, text="", bg=BG2, fg=DIM, font=FONT,
                              justify="left", anchor="w")
        self.stats.grid(row=r, column=0, columnspan=2, sticky="we", padx=8, pady=4)
        r += 1
        bar = tk.Frame(self, bg=BG)
        bar.grid(row=r, column=0, columnspan=2, sticky="e", padx=8, pady=8)
        for txt, cmd in (("Audition", self._audition), ("Stop", self.app.stop),
                         ("Import", self._do_import),
                         ("Cancel", self.destroy)):
            tk.Button(bar, text=txt, command=cmd, bg=BG2, fg=FG, relief="flat",
                      activebackground=CUR, font=FONT).pack(side="left", padx=3)
        self._restat()

    def _parse(self):
        perc, drum = self._PERC[self.v_perc.get()]
        if self.ext == "nsf":
            from ..nsf.extract import extract_song
            return extract_song(self.path, subsong=self.v_sub.get(),
                                percussion=perc, drum_sound=drum)
        if self.ext == "pt3":
            from ..pt3 import parse_pt3
            with open(self.path, "rb") as fh:
                return parse_pt3(fh.read(), percussion=perc, drum_sound=drum)
        from ..mcs.reader import parse
        from .export import nearest_tempo_byte0
        song = parse(self.path)
        return song, nearest_tempo_byte0(song.tempo_tick_seconds)

    def _build(self):
        from ..convert import song_to_rct
        song, byte0 = self._parse()
        rct = song_to_rct(song, tempo_byte0=byte0)
        if self.v_dropn.get():
            for pat in rct.patterns.values():
                for row in pat.cells:
                    if row[3].note and rct.channel_mode == R.MODE_3TONE_NOISE:
                        row[3] = R.RctCell()
        return song, rct

    def _restat(self):
        try:
            song, rct = self._build()
        except Exception as exc:
            self.stats.config(text=f"(cannot read: {exc})")
            return
        lines = [f"{song.title or 'untitled'} — {rct.bpm:.1f} BPM "
                 f"(MCS snap {rct.mcs_tempo_byte():#04x})"]
        for i, t in enumerate(song.tracks):
            n = sum(1 for x in t.notes if not x.is_rest)
            lines.append(f"  track {i + 1} [{getattr(t, 'kind', 'tone')}]: "
                         f"{n} notes, wave {t.waveform}")
        self.stats.config(text="\n".join(lines[:8]))

    def _audition(self):
        try:
            _song, rct = self._build()
        except Exception as exc:
            messagebox.showerror("Audition", str(exc))
            return
        self._preview = rct
        master, flat = self.app._render(rct)
        self.app._master, self.app._voices = master, self.app._voices
        self.app._flat = None
        self.app._seek_base = 0.0
        self.app.player.play(pcm16(master), 44100)
        self.app._playing = True

    def _do_import(self):
        try:
            _song, rct = self._build()
        except Exception as exc:
            messagebox.showerror("Import", str(exc))
            return
        self.app._load_import(rct, self.path.rsplit("/", 1)[-1])
        self.destroy()


class _ExportHost:
    """Adapts a TrackerApp to the surface ExportDialog expects of its host
    (`.song` universal, `.player`, `.root`, `.volume`, `.path`, `.load_song`).
    Lets the tracker reuse MCS-Player's export center verbatim -- including the
    MCS notation preview and Retrack-into-editor -- so nothing is lost."""

    class _Vol:
        def __init__(self, app):
            self.app = app

        def get(self):
            return self.app.v_vol.get()

    def __init__(self, app: "TrackerApp"):
        self.app = app
        self.root = app.root
        self.player = app.player
        self.volume = _ExportHost._Vol(app)
        self.path = app.path
        self.song = None                             # set by open_export_center

    def load_song(self, song, label: str = ""):
        self.app._load_universal(song, label)


def main(argv=None) -> int:
    import sys
    args = list(argv if argv is not None else sys.argv[1:])
    root = tk.Tk()
    TrackerApp(root, path=args[0] if args else None)
    root.geometry("980x640")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
