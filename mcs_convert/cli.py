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
    from .mcs.encode import encode_song

    ext = args.input.lower().rsplit(".", 1)[-1]
    try:
        if ext == "pt3":
            from .pt3 import parse_pt3
            with open(args.input, "rb") as fh:
                song, byte0 = parse_pt3(fh.read(), percussion=args.percussion,
                                        drum_sound=args.drum_sound,
                                        shape_durations=args.shape_durations)
            data = encode_song(song, tempo_byte0=byte0, cap=True)
        elif ext == "nsf":
            from .nsf.extract import extract_song
            byte0 = 0x77 + 3 * max(0, min(9, args.slow))
            song, byte0 = extract_song(args.input, subsong=args.subsong,
                                       percussion=args.percussion,
                                       drum_sound=args.drum_sound,
                                       tempo_byte0=byte0)
            data = encode_song(song, tempo_byte0=byte0, cap=True)
        else:
            print(f"error: no importer for .{ext} (supported: .pt3, .nsf)",
                  file=sys.stderr)
            return 1
    except NotImplementedError as exc:
        print(f"not yet implemented: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with open(args.output, "wb") as fh:
        fh.write(data)
    notes = sum(1 for t in song.tracks for n in t.notes if not n.is_rest)
    print(f"wrote {args.output} ({len(data)} bytes, {notes} notes, "
          f"'{song.title}')")
    from .mcs.validate import validate
    corrupt = [i for i in validate(data) if i.severity == "corrupt"]
    if corrupt:
        print(f"WARNING: {len(corrupt)} issue(s) may corrupt playback in real "
              f"MCS — run 'mcs-convert validate {args.output}'", file=sys.stderr)
    return 0


def _cmd_validate(args) -> int:
    from .mcs.validate import summary, validate
    with open(args.file, "rb") as fh:
        data = fh.read()
    print(f"{args.file}: {summary(data)}")
    return 1 if any(i.severity == "corrupt" for i in validate(data)) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcs-convert", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="print NSF header info")
    p_inspect.add_argument("file", help="path to a .nsf file")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_play = sub.add_parser("play", help="open the MCS/MCD viewer + player GUI")
    p_play.add_argument("file", nargs="?", help="optional .mcs/.mcd file to open")
    p_play.set_defaults(func=_cmd_play)

    p_val = sub.add_parser("validate",
                           help="check a .MCS against real-MCS playback limits")
    p_val.add_argument("file", help="path to a .mcs/.mcd file")
    p_val.set_defaults(func=_cmd_validate)

    p_conv = sub.add_parser("convert",
                            help="convert a chiptune module (.pt3, .nsf) to .MCS")
    p_conv.add_argument("input", help="path to a .pt3 (Vortex Tracker) or .nsf file")
    p_conv.add_argument("output", help="output .mcs path")
    p_conv.add_argument("--subsong", type=int, default=None, help="1-based subsong index")
    p_conv.add_argument("--percussion", choices=("clicks", "pitched", "drop"),
                        default="clicks",
                        help="drum-note handling: synthesize clicks (default), "
                             "play written pitches, or drop them")
    p_conv.add_argument("--drum-sound", choices=("cluster", "block"),
                        default="cluster",
                        help="click timbre: G3+Ab3 dissonant cluster (default) "
                             "or a single D4 wood-block tick")
    p_conv.add_argument("--shape-durations", action="store_true",
                        help="truncate notes to their sample's audible decay "
                             "(recovers plucks/staccato; MCS has no volume)")
    p_conv.add_argument("--slow", type=int, choices=range(0, 10), default=0,
                        metavar="N",
                        help="NSF: playback-speed step 0-9. 0 (default) plays at "
                             "the real NES speed with full timing detail; higher "
                             "values study the tune in slow motion. Note timing is "
                             "always kept at MCS's finest resolution either way.")
    p_conv.set_defaults(func=_cmd_convert)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
