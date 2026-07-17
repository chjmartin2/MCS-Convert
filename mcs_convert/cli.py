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
    target = _com_target(args)                       # tandy/1voice/4voice or None
    try:
        if ext == "pt3":
            from .pt3 import parse_pt3
            with open(args.input, "rb") as fh:
                song, byte0 = parse_pt3(fh.read(), percussion=args.percussion,
                                        drum_sound=args.drum_sound,
                                        shape_durations=args.shape_durations)
        elif ext == "nsf":
            from .nsf.extract import extract_song
            song, byte0 = extract_song(args.input, subsong=args.subsong,
                                       percussion=args.percussion,
                                       drum_sound=args.drum_sound)
            if args.slow:                            # override the fitted tempo
                byte0 = 0x77 + 3 * min(9, args.slow)  # for slow-motion study
        else:
            print(f"error: no importer for .{ext} (supported: .pt3, .nsf)",
                  file=sys.stderr)
            return 1

        if target:                                   # standalone DOS .COM player
            text_scope = (7 if args.scope_spin else 6 if args.scope_vu else
                          5 if args.scope_text5 else 4 if args.scope_text4 else
                          3 if args.scope_text3 else 2 if args.scope_text2 else
                          1 if args.scope_text else 0)
            if args.scope and target != "tandy":
                raise ValueError("--scope (graphics oscilloscope) needs --tandy")
            if text_scope and target not in ("tandy", "4voice"):
                raise ValueError("--scope-text.. / --scope-vu needs --tandy or --4voice")
            if args.mix_rate and target != "4voice":
                raise ValueError("--mix-rate only applies to --4voice")
            from .dosplayer import build_com
            data = build_com(song, target, byte0, scope=args.scope,
                             text_scope=text_scope, mix_rate=args.mix_rate,
                             draw_skip=args.draw_skip)
        else:                                        # the default .MCS song file
            data = encode_song(song, tempo_byte0=byte0, cap=True)
    except NotImplementedError as exc:
        print(f"not yet implemented: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with open(args.output, "wb") as fh:
        fh.write(data)
    notes = sum(1 for t in song.tracks for n in t.notes if not n.is_rest)
    kind = f"{target} .COM player" if target else ".MCS song"
    print(f"wrote {args.output} ({len(data)} bytes, {notes} notes, "
          f"'{song.title}') - {kind}")
    if not target:
        from .mcs.validate import validate
        corrupt = [i for i in validate(data) if i.severity == "corrupt"]
        if corrupt:
            print(f"WARNING: {len(corrupt)} issue(s) may corrupt playback in real "
                  f"MCS — run 'mcs-convert validate {args.output}'", file=sys.stderr)
    return 0


def _com_target(args) -> "str | None":
    """Which standalone-player target the flags select, if any. An output ending
    in .com defaults to Tandy; --tandy/--1voice/--4voice force it explicitly."""
    for mode in ("tandy", "1voice", "4voice"):
        if getattr(args, mode, False):
            return mode
    if args.output.lower().endswith(".com"):
        return "tandy"
    return None


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

    p_conv = sub.add_parser(
        "convert",
        help="convert a chiptune (.pt3, .nsf) to a .MCS song or a .COM player")
    p_conv.add_argument("input", help="path to a .pt3 (Vortex Tracker) or .nsf file")
    p_conv.add_argument("output", help="output path (.mcs song, or .com player)")
    # Standalone-player target: emit a self-contained DOS .COM instead of a .MCS.
    target = p_conv.add_mutually_exclusive_group()
    target.add_argument("--tandy", action="store_true",
                        help="output a .COM that plays on Tandy/PCjr (SN76489, "
                             "3 square voices)")
    target.add_argument("--1voice", dest="1voice", action="store_true",
                        help="output a .COM that plays on the PC speaker "
                             "(monophonic, top line)")
    target.add_argument("--4voice", dest="4voice", action="store_true",
                        help="output a 4-voice PC-speaker .COM (software 1-bit "
                             "mixing; currently 3 tone voices, noise voice next)")
    p_conv.add_argument("--scope", action="store_true",
                        help="Tandy .COM only: draw a 320x200 graphics "
                             "oscilloscope (4 channels + master) while playing")
    p_conv.add_argument("--scope-text", dest="scope_text", action="store_true",
                        help="Tandy .COM only: draw lighter 80x25 TEXT-mode block "
                             "scopes (60 fps on real hardware) instead of graphics")
    p_conv.add_argument("--scope-text2", dest="scope_text2", action="store_true",
                        help="Tandy .COM only: 80x25 text-mode box-drawing LINE "
                             "oscilloscope trace")
    p_conv.add_argument("--scope-text3", dest="scope_text3", action="store_true",
                        help="Tandy .COM only: 80x25 text-mode box-line 2x2 grid "
                             "+ full-width master (the graphics layout in text)")
    p_conv.add_argument("--scope-text4", dest="scope_text4", action="store_true",
                        help="Tandy .COM only: 80x25 text-mode faux spectrum "
                             "analyzer (green/yellow/red bars + peak caps)")
    p_conv.add_argument("--scope-text5", dest="scope_text5", action="store_true",
                        help="Tandy .COM only: 80x25 text-mode combined monitor "
                             "(2x2 scopes + spectrum + VU meters)")
    p_conv.add_argument("--scope-vu", dest="scope_vu", action="store_true",
                        help="Tandy/4voice .COM: lightweight 80x25 VU-meter display")
    p_conv.add_argument("--scope-spin", dest="scope_spin", action="store_true",
                        help="4voice .COM: minimal 4 per-voice character cells (the "
                             "raw note byte dumped to screen; runs on a real 6 MHz XT)")
    p_conv.add_argument("--mix-rate", dest="mix_rate", type=int, default=None,
                        metavar="HZ",
                        help="4voice .COM only: software mixing sample rate in Hz "
                             "(~11900 default; lower to ~6000 for a real XT)")
    p_conv.add_argument("--draw-skip", dest="draw_skip", type=int, default=1,
                        metavar="N",
                        help="redraw the scope every Nth frame (default 1; higher "
                             "= lighter/slower visuals on slow machines)")
    p_conv.add_argument("--subsong", type=int, default=None, help="1-based subsong index")
    p_conv.add_argument("--percussion", choices=("clicks", "pitched", "drop"),
                        default="clicks",
                        help="drum-note handling: synthesize clicks (default), "
                             "play written pitches, or drop them")
    p_conv.add_argument("--drum-sound",
                        choices=("auto", "low bass", "hi-hat", "block", "cluster"),
                        default="auto",
                        help="click pitch: two-tone auto (default), low bass (B2), "
                             "hi-hat (E7), wood block (D4), or legacy cluster")
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
