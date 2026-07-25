# PCM Explorer

**Open and browse the hard drive out of a Porsche PCM or Audi MMI head unit — on your desktop, read-only.**

These units run QNX, and their drives use the **QNX6** "power-safe" filesystem — or **QNX4**
on older drives and on the navigation partition of every one. Windows can't read either.
macOS can't. Linux ships a read-only `qnx6` driver aimed at QNX 6.4+, which doesn't match the
Harman variant these head units actually use. So when a head unit dies and you image the
drive, you get a 40 GB file that nothing will open.

This opens it.

```
python explorer.py                                    # desktop UI
python explorer.py disk.img                           # summary -- what IS this image?
python explorer.py disk.img ls P2                     # list every file
python explorer.py disk.img cat P2 /HBdata/version.txt
python explorer.py disk.img extract P2 /Browser ./out  # a file or a whole folder
python explorer.py disk.img verify P2                 # filesystem self-test

python explorer.py PCM3_IFS1.ifs                      # firmware images too
python explorer.py PCM3_IFS2.ifs ls /mnt/ifs_app/HBproject
```

## What it does

- **Tells you what an image *is*.** Not just a file list: drive variant, navigation
  packages, speech version, odometer, and — for firmware — the build string and whether
  that release supports a jukebox at all.
- **Opens firmware as well as drives.** `PCM3_IFS1.ifs` and `PCM3_IFS2.ifs` from an
  official update package browse exactly like a disk, so "does this release contain X"
  is a directory listing rather than a reverse-engineering session.
- **Decodes what it finds** — `CVALUE*.CVA` coding tables, the odometer inside the
  driver's-logbook database, ELF architecture, PNG dimensions.
- **Maps the partitions** and identifies the filesystem in each — **QNX6 and QNX4** — reading
  real geometry (block size, inode counts, allocation groups) from the QNX6 superblock.
- **Lists every file and folder**, with sizes, permissions and inode numbers.
- **Reads file contents** — preview in the UI, `cat` to stdout, or extract a single file
  or an entire folder tree to disk. Handles files of any size, sparse holes and symlinks.
- **Verifies itself** against the filesystem's own accounting (see below).
- **Salvage mode** for drives too damaged to mount, and a hex view of any byte offset.

## Does it actually work?

That's the right question to ask of a recovery tool, so it ships with a `verify` command
that checks the reader against the filesystem's own bookkeeping:

```
[PASS] geometry             (plen/bs) - num_blocks == 16
[PASS] inode census         376 live, superblock says 376
[PASS] block bitmap         546413 clear, superblock says 546413 free
[PASS] directory identity   20 directories checked, 0 inconsistent
```

The block-bitmap check is the interesting one: it reaches the bitmap through a different
root node and a different block chain than the inode logic, so it independently confirms
the reader is looking in the right place.

During development the extracted bytes were validated by content as well — every PNG
chunk CRC on the drive (970/970), ELF section tables on all 47 binaries (several ending
*exactly* at EOF, which proves the last block of a multi-level file map is correct),
`PRAGMA integrity_check` on the SQLite databases, and the ISO9660 descriptor of a 387 MB
image read correctly through two levels of indirection.

## Firmware images

An update package carries two compressed images, and both open here:

| | container | holds |
|---|---|---|
| `PCM3_IFS1.ifs` | QNX boot image, chunked LZO1X | the OS, `PCM3Root`, drivers, `hddmounter` |
| `PCM3_IFS2.ifs` | a single LZO1X stream at offset `0x40` | the HMI — `PCM3Reload`, `NavCore` |

Decompression is pure Python (the decoder is vendored in-tree), so reading firmware needs
no `liblzo2`, no compiler and no `python-lzo`.

The summary answers the question people actually have:

```
Firmware image: PCM3_IFS1.ifs
  IFS1 container, 18.1 MB inflated, 177 files / 1 dirs / 52 symlinks

/mnt/ifs1/HBproject/version.txt:
  Porsche_PCM3.1_MOPF_SOP_STEP9.6_15245AS9

Jukebox support: YES (7 references to /mnt/media)
hddmounter present: yes
```

## Salvage mode

If a drive is damaged badly enough that the superblock is unreadable, `salvage` recovers
the directory structure anyway. Every QNX6 directory block identifies itself — its first
entry is `.` holding its own inode number, its second is `..` holding its parent's — so
sweeping the raw partition for that signature rebuilds the hierarchy with no metadata at
all. You get names and structure but not contents, which is usually enough to know what
was on a drive and whether deeper recovery is worth it.

## How it works

The format is documented in [docs/QNX6-NOTES.md](docs/QNX6-NOTES.md) — enough to write your
own reader. The short version: everything hinges on one constant. Block numbers are
relative to the start of the *data area*, not the partition:

```
image_offset(block B) = partition_base + 0x3000 + B * blocksize
```

Miss that 12 KiB and every indirect chain lands in unrelated data and reads as though the
filesystem were full of holes — which is exactly why this variant looked proprietary. It
isn't. With the right origin it matches the public QNX6 layout.

## Install

Needs **Python 3.8+**. Tkinter ships with Python on Windows and macOS; on Linux you may need
`sudo apt install python3-tk`. No other dependencies.

```bash
git clone https://github.com/dspl1236/PCM-Explorer
cd PCM-Explorer
python explorer.py
```

### Windows without Python

Download **[PCM-Explorer.exe](https://github.com/dspl1236/PCM-Explorer/releases/download/latest/PCM-Explorer.exe)**
— a standalone build, no install required. That link always serves the current build of
`main`; numbered [releases](../../releases) are there if you'd rather pin a version.

Every build runs the test suite first, so a broken reader never ships as a download, and
each one is published with its
[SHA256](https://github.com/dspl1236/PCM-Explorer/releases/download/latest/PCM-Explorer.exe.sha256).

The exe is unsigned, so SmartScreen will warn on first run: **More info → Run anyway**.
If you'd rather not, running from source above does exactly the same thing.

### Tests

```bash
python tests/test_smoke.py                                    # synthetic images, no data needed
PCM_TEST_IMAGE=/path/to/disk.img python tests/test_smoke.py   # adds full filesystem verification
```

## Imaging a drive

Pull the 2.5" drive from the head unit and image it with any block copier — Win32DiskImager,
`dd`, Clonezilla, a USB dock with imaging software. Take a **full raw sector image**, not a
file-level backup:

```bash
dd if=/dev/sdX of=pcm.img bs=4M status=progress     # Linux/macOS
```

Then open `pcm.img`. The typical PCM 3.1 layout is three QNX partitions — a ~2 GB system
partition, a ~1 GB application partition, and the remainder for navigation data.

## Safety

The image is opened read-only (`rb`) and no code path in this project writes to it. Working
from a copy rather than the original drive is still the right habit.

**Privacy note:** a head-unit drive contains the VIN, saved navigation destinations, phone
pairings and call history. Treat an image — and any exported tree — as personal data. The
`.gitignore` here deliberately excludes `*.img` so you can't commit one by accident.

## Audi MMI

MMI 3G/3GP drives are also Harman-built and also **QNX6** — every partition on them is
created with `mkqnx6fs`, and they reuse the same partition type bytes (`0x4D`/`0x4E`/`0x4F`)
as the Porsche drives. The roles differ, though: on an MMI drive `0x4D` is the navigation
partition, while on a PCM it's the system partition. So the type byte is a hint, not an
answer — this tool probes each partition for a real superblock instead of trusting it.

MMI also places five further partitions (`gracenode`, `mmebackup1`, `persistence`,
`img-cache`, `pv-cache`) inside an **extended partition**, so PCM Explorer walks the
extended-boot-record chain and lists logical partitions as `L1`, `L2`, … Stopping at the
four primary entries would miss most of the disk, including `persistence`.

That layout is derived from [DrGER's MMI3G-HDD-Prep-Tool](https://github.com/DrGER2/MMI3G-HDD-Prep-Tool),
which formats these drives. The extended-partition handling is verified against a
synthetic disk built to that layout; **it has not yet been run against a real MMI image** —
if you have one, an issue saying whether it worked would be genuinely useful.

## Contributing

Images from other units are welcome, particularly **Audi MMI** and **PCM 4.x**. If the tool
reports `unknown` on your image, that's a data point worth opening an issue over: the
partition table, sizes, and the first 512 bytes of each partition are enough to start.

Its sibling project [PCM-Forge](https://github.com/dspl1236/PCM-Forge) covers activation
codes and on-car tooling for the PCM 3.1.

MIT licensed. Research and personal use on hardware you own.
