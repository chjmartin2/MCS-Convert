"""Convenience launcher: `python play.py [SONG.MCS]` opens the MCS/MCD player GUI.

Equivalent to `python -m mcs_convert play`. Safe to Run directly from an IDE.
"""

from mcs_convert.gui.player import main

if __name__ == "__main__":
    raise SystemExit(main())
