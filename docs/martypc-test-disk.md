# Self-booting MCS test disk (for MartyPC / any PC emulator)

`samples/MCS_boot_360k.img` (also copied as `.dsk`) is a **self-booting 360K FAT12 floppy**
that boots FreeDOS and runs Music Construction Set, so you can load a song, edit it, and
**save it back to the floppy** — the thing the DOSBox rip won't let you do reliably.

The image lives under the gitignored `samples/` tree (it bundles FreeDOS + the copyrighted
MCS binaries). Rebuild it any time with [`tools/make_mcs_dsk.py`](../tools/make_mcs_dsk.py).

## Why a bootable 360K disk
The 1984 IBM MCS is a *self-booting* program — no DOS underneath. Demonlord's rip exposes two
DOS executables (see `samples/.../READ.TXT`):
- **MCSDISK.EXE** — reads/writes songs on a **360K floppy** (its low-level disk code only
  understands 360K geometry). This is the one on the disk.
- **MCS.EXE** — saves to a hard disk but *must* run from one and rejects floppies.

A bare emulator like MartyPC has no OS to start from, so the disk carries its own: a proven
FreeDOS FAT12 boot sector + 8086 `KERNEL.SYS` + FreeCOM `COMMAND.COM` (all GPL, redistributable).

## What's on it
- `KERNEL.SYS`, `COMMAND.COM`, `FDCONFIG.SYS`, `AUTOEXEC.BAT` — FreeDOS
- `MCSDISK.EXE` — Music Construction Set
- 40 songs (`MINUETG.MCS`, `DAISY.MCS`, `JINGLE.MCS`, …), ~112 KB left free for saving

## Verified
Boot-tested in QEMU (`qemu-system-i386 -fda MCS_boot_360k.img -boot a`): it boots FreeDOS to
an `A:\>` prompt, and `MCSDISK` launches into MCS's "Measuring computer speed…" init. On QEMU's
fast CPU that speed calibration runs long; on MartyPC's cycle-accurate 8088 it completes
quickly (that's what MartyPC is for).

## Load it in MartyPC
1. Point MartyPC at the image as floppy drive **A:** (in `martypc.toml`, set a `[[floppy]]`
   / drive-0 image path to `MCS_boot_360k.img`, or drag-drop via the GUI). Use the `.img`
   name — it's a raw sector image.
2. Boot. FreeDOS starts and prints the on-disk help, leaving you at `A:\>`.
3. Type **`MCSDISK`** and press Enter. Wait out "Measuring computer speed…".

## The rest-decoding experiment (why we built this) — SOLVED
This disk cracked rests. Editing MINUETG's first eighth note into an eighth rest and diffing
the saved file changed exactly two bytes: `byte0 0x82 → 0x89` (the rest flag, bit 3) and
`byte1 0x32 → 0x39` (pitch → rest-glyph position). That pinned the byte0 low-nibble note/rest
ladder in [mcs-format.md](mcs-format.md). To pin the remaining dotted nibbles (0,6,7), repeat
the same controlled-edit recipe:

1. In MCS: `LOAD MINUETG` (or any short song).
2. Note the exact spot, then **insert or extend one rest** — one deliberate, known change.
3. **Save under a new name**, e.g. `SAVE TEST` (writes `TEST.MCS` to A:).
4. Quit MCS (**Ctrl-X**) → back to `A:\>`. Shut down MartyPC.
5. Pull `TEST.MCS` out of the image and diff it against the original:
   ```
   python tools/mcs_dump.py TEST.MCS          # structural dump
   # compare bytes to the original MINUETG.MCS
   ```
   Extract a file from the image with the reader in `tools/make_mcs_dsk.py`
   (`extract_file_from_image`), or re-mount the image.

The bytes that change between "no rest" and "one rest" reveal exactly how rests are stored —
then we teach `mcs_convert/mcs/reader.py` to read them.

## Rebuild
```
python tools/make_mcs_dsk.py \
  --boot-template <freedos_boot_floppy.img> --kernel-from-template \
  --command <COMMAND.COM> --mcsdisk samples/ia_1984/extracted/whmcs/MCSDISK.EXE \
  --songs samples/ia_1984/extracted/whmcs --out samples/MCS_boot_360k.img
```
FreeDOS pieces: kernel/`SYS.COM` from the FDOS/kernel release, `COMMAND.COM` from FDOS/freecom,
boot sector lifted from the FreeDOS 1.3 FloppyEdition 720K `x86BOOT.img`.
