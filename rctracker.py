#!/usr/bin/env python
"""RCTracker launcher (PyInstaller entry point). Run:  python rctracker.py"""
import sys

from mcs_convert.gui.tracker import main

if __name__ == "__main__":
    sys.exit(main())
