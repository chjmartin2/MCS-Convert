#!/usr/bin/env python
"""RCPlay launcher (PyInstaller entry point). Run:  python rcplay.py [songs]"""
import sys

from mcs_convert.gui.rcplay_win import main

if __name__ == "__main__":
    sys.exit(main())
