"""Command-line interface for MCS-Convert.

Subcommands:
  inspect FILE.nsf            print NSF header info (works today)
  convert FILE.nsf OUT.mcs    full pipeline (blocked: needs 6502 core + MCS writer)
"""

from __future__ import annotations

import argparse
import sys

from .nsf.header import NSFHeader


def _cmd_inspect(args) -> int:
    try:
        h = NSFHeader.from_file(args.file)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    region = "PAL" if h.is_pal else "NTSC"
    if h.is_dual_region:
        region += " (dual)"
    chips = ", ".join(h.expansion_chips) or "none (2A03 only)"

    print(f"File:            {args.file}")
    print(f"NSF version:     {h.version}")
    print(f"Title:           {h.song_name or '(untitled)'}")
    print(f"Artist:          {h.artist or '(unknown)'}")
    print(f"Copyright:       {h.copyright or '(unknown)'}")
    print(f"Subsongs:        {h.total_songs} (starts at {h.starting_song})")
    print(f"Region:          {region}  ~{h.play_rate_hz:.2f} Hz play rate")
    print(f"Load / Init / Play: ${h.load_addr:04X} / ${h.init_addr:04X} / ${h.play_addr:04X}")
    print(f"Bankswitching:   {'yes' if h.uses_bankswitching else 'no'}")
    print(f"Expansion chips: {chips}")
    return 0


def _cmd_play(args) -> int:
    from .gui.player import main as gui_main
    return gui_main([args.file] if args.file else [])


def _cmd_convert(args) -> int:
    from .nsf.extract import extract_song
    from .mcs.writer import write_mcs

    try:
        song = extract_song(args.input, subsong=args.subsong)
        write_mcs(song, args.output)
    except NotImplementedError as exc:
        print(f"not yet implemented: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcs-convert", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="print NSF header info")
    p_inspect.add_argument("file", help="path to a .nsf file")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_play = sub.add_parser("play", help="open the MCS/MCD viewer + player GUI")
    p_play.add_argument("file", nargs="?", help="optional .mcs/.mcd file to open")
    p_play.set_defaults(func=_cmd_play)

    p_conv = sub.add_parser("convert", help="convert NSF -> MCS (in progress)")
    p_conv.add_argument("input", help="path to a .nsf file")
    p_conv.add_argument("output", help="output .mcs path")
    p_conv.add_argument("--subsong", type=int, default=None, help="1-based subsong index")
    p_conv.set_defaults(func=_cmd_convert)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
