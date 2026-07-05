"""Build a self-booting 360K FreeDOS floppy image carrying Music Construction Set.

Produces a raw 360K (368,640-byte) FAT12 disk image that boots straight to a DOS
prompt and runs MCSDISK.EXE, so you can load a song, edit it, and SAVE it back to
the floppy inside an IBM-PC emulator (MartyPC, PCem, 86Box) or on real hardware.

Why this shape (see docs/mcs-format.md + samples READ.TXT):
  * The 1984 IBM MCS is a *self-booting* disk with no DOS. Demonlord's rip exposes two
    DOS executables; MCSDISK.EXE is the one that reads/writes songs on a **360K floppy**
    (its low-level disk code only understands 360K geometry). So the carrier must be a
    360K FAT12 disk, and it must boot an OS on its own for a bare emulator.
  * We supply that OS with FreeDOS (GPL, freely redistributable): a proven FreeDOS FAT12
    boot sector + KERNEL.SYS, plus the FreeCOM COMMAND.COM shell.

The FreeDOS boot sector is BPB-driven: we lift the 512-byte sector from a known-good
FreeDOS boot floppy and overlay a 360K BPB, so the same boot code drives a 360K disk.

None of the inputs are committed to the repo (FreeDOS + the copyrighted MCS binaries live
under the gitignored samples/ tree); this script just assembles them.

Usage:
    python tools/make_mcs_dsk.py \
        --boot-template samples/.../fdboot720.img \
        --kernel-from-template \
        --command samples/.../COMMAND.COM \
        --mcsdisk samples/ia_1984/extracted/whmcs/MCSDISK.EXE \
        --songs   samples/ia_1984/extracted/whmcs \
        --out     samples/MCS_boot_360k.img
"""

from __future__ import annotations

import argparse
import os
import struct
from dataclasses import dataclass
from typing import List, Tuple

# --- 360K 5.25" DSDD geometry (FAT12) ---------------------------------------
BPS = 512
SEC_PER_CLUS = 2
RESERVED = 1
NUM_FATS = 2
ROOT_ENTRIES = 112
TOTAL_SECTORS = 720          # 720 * 512 = 368,640 bytes
SEC_PER_FAT = 2
SEC_PER_TRACK = 9
HEADS = 2
MEDIA = 0xFD                 # 360K, double-sided, 9 sectors

ROOT_START = RESERVED + NUM_FATS * SEC_PER_FAT          # sector 5
ROOT_SECTORS = (ROOT_ENTRIES * 32 + BPS - 1) // BPS      # 7
DATA_START = ROOT_START + ROOT_SECTORS                    # sector 12
TOTAL_CLUSTERS = (TOTAL_SECTORS - DATA_START) // SEC_PER_CLUS   # 354
BYTES_PER_CLUS = BPS * SEC_PER_CLUS                       # 1024

# A fixed DOS timestamp so the image is byte-reproducible (1984-01-01 00:00).
_DOS_DATE = ((1984 - 1980) << 9) | (1 << 5) | 1
_DOS_TIME = 0


# --- FAT12 reader (only what we need: follow a chain in a FAT12 image) -------
def _read_fat12_entry(fat: bytes, idx: int) -> int:
    off = idx + (idx >> 1)               # idx * 3 // 2
    pair = fat[off] | (fat[off + 1] << 8)
    return (pair >> 4) if (idx & 1) else (pair & 0x0FFF)


def extract_file_from_image(img: bytes, name_8_3: str) -> bytes:
    """Read a root-directory file out of a FAT12 floppy image by 8.3 name."""
    spc = img[13]
    reserved = struct.unpack_from("<H", img, 14)[0]
    nfats = img[16]
    root_ent = struct.unpack_from("<H", img, 17)[0]
    spf = struct.unpack_from("<H", img, 22)[0]
    root_start = reserved + nfats * spf
    root_secs = (root_ent * 32 + BPS - 1) // BPS
    data_start = root_start + root_secs
    fat = img[reserved * BPS: (reserved + spf) * BPS]

    target = name_8_3.upper().ljust(8) if "." not in name_8_3 else None
    want = _pack_83(name_8_3)
    roff = root_start * BPS
    for i in range(root_ent):
        e = img[roff + i * 32: roff + i * 32 + 32]
        if e[0] in (0x00, 0xE5) or (e[11] & 0x08):
            continue
        if e[0:11] != want:
            continue
        size = struct.unpack_from("<I", e, 28)[0]
        clus = struct.unpack_from("<H", e, 26)[0]
        out = bytearray()
        while 2 <= clus < 0xFF0:
            sec = data_start + (clus - 2) * spc
            out += img[sec * BPS: (sec + spc) * BPS]
            clus = _read_fat12_entry(fat, clus)
        return bytes(out[:size])
    raise KeyError(f"{name_8_3} not found in boot template image")


def add_file_to_image(img: bytes, name_8_3: str, data: bytes) -> bytes:
    """Inject one file into an existing FAT12 image (allocates free clusters + a root
    entry, updates both FATs). Used to drop a generated test song onto the MCS disk so
    it can be loaded in MartyPC. Replaces any existing file of the same name."""
    out = bytearray(img)
    spc = out[13]
    reserved = struct.unpack_from("<H", out, 14)[0]
    nfats = out[16]
    root_ent = struct.unpack_from("<H", out, 17)[0]
    spf = struct.unpack_from("<H", out, 22)[0]
    total = struct.unpack_from("<H", out, 19)[0]
    root_start = reserved + nfats * spf
    root_secs = (root_ent * 32 + BPS - 1) // BPS
    data_start = root_start + root_secs
    nclusters = (total - data_start) // spc
    bpc = BPS * spc
    fat = bytearray(out[reserved * BPS:(reserved + spf) * BPS])
    want = _pack_83(name_8_3)

    # free any existing file of this name, and find a directory slot
    roff = root_start * BPS
    slot = None
    for i in range(root_ent):
        e = out[roff + i * 32: roff + i * 32 + 32]
        if e[0:11] == want and not (e[11] & 0x08):
            clus = struct.unpack_from("<H", e, 26)[0]
            while 2 <= clus < 0xFF0:
                nxt = _read_fat12_entry(fat, clus)
                _set_fat12(fat, clus, 0)
                clus = nxt
            out[roff + i * 32] = 0xE5
        if slot is None and e[0] in (0x00, 0xE5):
            slot = i
    if slot is None:
        raise ValueError("root directory full")

    need = max(1, (len(data) + bpc - 1) // bpc)
    free = [c for c in range(2, nclusters + 2) if _read_fat12_entry(fat, c) == 0]
    if len(free) < need:
        raise ValueError(f"not enough free space for {name_8_3} ({len(data)} bytes)")
    chain = free[:need]
    for k, c in enumerate(chain):
        _set_fat12(fat, c, 0xFFF if k == need - 1 else chain[k + 1])
        base = (data_start + (c - 2) * spc) * BPS
        chunk = data[k * bpc:(k + 1) * bpc]
        out[base:base + len(chunk)] = chunk

    e = bytearray(32)
    e[0:11] = want
    e[11] = 0x20                                    # archive
    struct.pack_into("<H", e, 22, _DOS_TIME)
    struct.pack_into("<H", e, 24, _DOS_DATE)
    struct.pack_into("<H", e, 26, chain[0])
    struct.pack_into("<I", e, 28, len(data))
    out[roff + slot * 32: roff + slot * 32 + 32] = e

    for f in range(nfats):                          # write both FAT copies
        off = (reserved + f * spf) * BPS
        out[off:off + len(fat)] = fat
    return bytes(out)


# --- FAT12 writer -----------------------------------------------------------
def _pack_83(name: str) -> bytes:
    base, _, ext = name.upper().partition(".")
    return base[:8].ljust(8).encode("ascii") + ext[:3].ljust(3).encode("ascii")


@dataclass
class FileSpec:
    name: str            # 8.3 DOS name
    data: bytes


def _set_fat12(fat: bytearray, idx: int, val: int) -> None:
    off = idx + (idx >> 1)
    if idx & 1:
        fat[off] = (fat[off] & 0x0F) | ((val << 4) & 0xF0)
        fat[off + 1] = (val >> 4) & 0xFF
    else:
        fat[off] = val & 0xFF
        fat[off + 1] = (fat[off + 1] & 0xF0) | ((val >> 8) & 0x0F)


def build_image(boot_sector: bytes, files: List[FileSpec], label: str = "MCS DISK") -> bytes:
    if len(files) > ROOT_ENTRIES:
        raise ValueError(f"{len(files)} files exceeds root dir capacity {ROOT_ENTRIES}")

    img = bytearray(TOTAL_SECTORS * BPS)

    # Boot sector: keep the FreeDOS jump+code, overwrite the BPB for 360K geometry.
    bs = bytearray(boot_sector[:BPS])
    struct.pack_into("<H", bs, 0x0B, BPS)
    bs[0x0D] = SEC_PER_CLUS
    struct.pack_into("<H", bs, 0x0E, RESERVED)
    bs[0x10] = NUM_FATS
    struct.pack_into("<H", bs, 0x11, ROOT_ENTRIES)
    struct.pack_into("<H", bs, 0x13, TOTAL_SECTORS)
    bs[0x15] = MEDIA
    struct.pack_into("<H", bs, 0x16, SEC_PER_FAT)
    struct.pack_into("<H", bs, 0x18, SEC_PER_TRACK)
    struct.pack_into("<H", bs, 0x1A, HEADS)
    struct.pack_into("<I", bs, 0x1C, 0)            # hidden sectors
    struct.pack_into("<I", bs, 0x20, 0)            # total sectors 32
    bs[0x24] = 0x00                                 # drive number = A:
    bs[0x25] = 0x00
    bs[0x26] = 0x29                                 # extended boot signature
    struct.pack_into("<I", bs, 0x27, 0x4D435331)   # volume id (fixed, "MCS1")
    bs[0x2B:0x36] = label[:11].ljust(11).encode("ascii")
    bs[0x36:0x3E] = b"FAT12   "
    bs[0x1FE] = 0x55
    bs[0x1FF] = 0xAA
    img[0:BPS] = bs

    fat = bytearray(SEC_PER_FAT * BPS)
    _set_fat12(fat, 0, 0xF00 | MEDIA)
    _set_fat12(fat, 1, 0xFFF)

    root = bytearray(ROOT_SECTORS * BPS)
    next_clus = 2

    for i, f in enumerate(files):
        n = len(f.data)
        nclus = max(1, (n + BYTES_PER_CLUS - 1) // BYTES_PER_CLUS)
        if next_clus + nclus - 2 > TOTAL_CLUSTERS + 1:
            raise ValueError(f"out of space adding {f.name} ({n} bytes)")
        start = next_clus
        for c in range(nclus):
            cur = start + c
            _set_fat12(fat, cur, 0xFFF if c == nclus - 1 else cur + 1)
        # write data into the cluster run
        base_sec = DATA_START + (start - 2) * SEC_PER_CLUS
        img[base_sec * BPS: base_sec * BPS + n] = f.data
        # directory entry
        e = bytearray(32)
        e[0:11] = _pack_83(f.name)
        e[11] = 0x20                                 # archive
        struct.pack_into("<H", e, 22, _DOS_TIME)
        struct.pack_into("<H", e, 24, _DOS_DATE)
        struct.pack_into("<H", e, 26, start)
        struct.pack_into("<I", e, 28, n)
        root[i * 32:(i + 1) * 32] = e
        next_clus += nclus

    img[RESERVED * BPS:(RESERVED + SEC_PER_FAT) * BPS] = fat
    img[(RESERVED + SEC_PER_FAT) * BPS:(RESERVED + 2 * SEC_PER_FAT) * BPS] = fat
    img[ROOT_START * BPS:(ROOT_START + ROOT_SECTORS) * BPS] = root
    return bytes(img)


# --- song selection ---------------------------------------------------------
def choose_songs(songs_dir: str, must_have: List[str], max_files: int,
                 clus_budget: int) -> List[FileSpec]:
    """Pick songs: the must-haves first, then smallest-first until budget/count is hit."""
    names = [n for n in os.listdir(songs_dir) if n.upper().endswith((".MCS", ".MCD"))]
    ordered = [m for m in must_have if m in names]
    rest = sorted((n for n in names if n not in ordered),
                  key=lambda n: os.path.getsize(os.path.join(songs_dir, n)))
    chosen: List[FileSpec] = []
    used = 0
    for n in ordered + rest:
        if len(chosen) >= max_files:
            break
        data = open(os.path.join(songs_dir, n), "rb").read()
        clus = max(1, (len(data) + BYTES_PER_CLUS - 1) // BYTES_PER_CLUS)
        if used + clus > clus_budget:
            continue
        chosen.append(FileSpec(n.upper(), data))
        used += clus
    return chosen


_AUTOEXEC = (
    "@ECHO OFF\r\n"
    "CLS\r\n"
    "ECHO ================================================\r\n"
    "ECHO  Music Construction Set (1984) - FreeDOS carrier\r\n"
    "ECHO ================================================\r\n"
    "ECHO  Type   MCSDISK   then ENTER to launch MCS.\r\n"
    "ECHO  In MCS:  CAT = list songs   LOAD MINUETG = open\r\n"
    "ECHO           SAVE TEST = write TEST.MCS to A:\r\n"
    "ECHO           Ctrl-X = quit back to DOS.\r\n"
    "ECHO.\r\n"
)
_FDCONFIG = "LASTDRIVE=Z\r\nFILES=20\r\nBUFFERS=10\r\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boot-template", required=True,
                    help="FreeDOS FAT12 boot floppy image to lift the boot sector from")
    ap.add_argument("--kernel-from-template", action="store_true",
                    help="extract KERNEL.SYS from the boot template (matched pair)")
    ap.add_argument("--kernel", help="explicit KERNEL.SYS path (if not from template)")
    ap.add_argument("--command", required=True, help="FreeCOM COMMAND.COM path")
    ap.add_argument("--mcsdisk", required=True, help="MCSDISK.EXE path")
    ap.add_argument("--songs", required=True, help="directory of .MCS/.MCD songs")
    ap.add_argument("--out", required=True, help="output .img path")
    ap.add_argument("--max-songs", type=int, default=40,
                    help="cap song count (MCS lists at most 63 files)")
    ap.add_argument("--song-clusters", type=int, default=110,
                    help="cluster budget for songs (leave the rest free for saving)")
    args = ap.parse_args(argv)

    template = open(args.boot_template, "rb").read()
    boot_sector = template[:BPS]
    if args.kernel_from_template:
        kernel = extract_file_from_image(template, "KERNEL.SYS")
    else:
        kernel = open(args.kernel, "rb").read()
    command = open(args.command, "rb").read()
    mcsdisk = open(args.mcsdisk, "rb").read()

    # System files first (KERNEL.SYS as the first root entry, per convention).
    files: List[FileSpec] = [
        FileSpec("KERNEL.SYS", kernel),
        FileSpec("COMMAND.COM", command),
        FileSpec("FDCONFIG.SYS", _FDCONFIG.encode("ascii")),
        FileSpec("AUTOEXEC.BAT", _AUTOEXEC.encode("ascii")),
        FileSpec("MCSDISK.EXE", mcsdisk),
    ]
    must = ["MINUETG.MCS", "DAISY.MCS", "JINGLE.MCS", "ENTERTAN.MCS", "MAPLLEAF.MCS"]
    songs = choose_songs(args.songs, must, args.max_songs, args.song_clusters)
    files += songs

    img = build_image(boot_sector, files)
    with open(args.out, "wb") as fh:
        fh.write(img)

    sys_clus = sum(max(1, (len(f.data) + BYTES_PER_CLUS - 1) // BYTES_PER_CLUS)
                   for f in files)
    print(f"wrote {args.out}  ({len(img)} bytes, 360K FAT12)")
    print(f"  system+tool files: KERNEL.SYS COMMAND.COM FDCONFIG.SYS AUTOEXEC.BAT MCSDISK.EXE")
    print(f"  songs: {len(songs)}  ->  {', '.join(s.name for s in songs)}")
    print(f"  clusters used: {sys_clus}/{TOTAL_CLUSTERS}  "
          f"(~{(TOTAL_CLUSTERS - sys_clus) * BYTES_PER_CLUS // 1024} KB free for saving)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
