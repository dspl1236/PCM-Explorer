# PCM Explorer

**Open a Porsche PCM or Audi MMI head unit — its drive, its firmware, its update discs,
its persistence flash, and its user interface — on your desktop, read-only.**

These units run QNX, and their drives use the **QNX6** "power-safe" filesystem — or **QNX4**
on older drives and on the navigation partition of every one. Windows can't read either.
macOS can't. Linux ships a read-only `qnx6` driver aimed at QNX 6.4+, which doesn't match the
Harman variant these head units actually use. So when a head unit dies and you image the
drive, you get a 40 GB file that nothing will open.

This opens it — and, as it turned out, everything else the unit is made of: the firmware
images, the update discs, the persistence flash, and the interface itself.

```
python explorer.py                                    # desktop UI
python explorer.py disk.img                           # summary -- what IS this image?
python explorer.py disk.img ls P2                     # list every file
python explorer.py disk.img cat P2 /HBdata/version.txt
python explorer.py disk.img extract P2 /Browser ./out  # a file or a whole folder
python explorer.py disk.img verify P2                 # filesystem self-test

python explorer.py PCM3_IFS1.ifs                      # firmware images too
python explorer.py PCM3_IFS2.ifs ls /mnt/ifs_app/HBproject

python explorer.py PCM_NA_20150721.ISO                # and update discs
python explorer.py update.iso units                   # which head units accept it
python explorer.py update.iso crc                     # verify every payload

python explorer.py PCM3_HBpersistence.efs             # factory persistence flash
python explorer.py moccaV2Target.mmi                  # the HMI itself
python explorer.py moccaV2Target.mmi langs            # every string, every language
python explorer.py moccaV2Target.mmi screens 33616    # a screen's elements, x/y/w/h

python explorer.py timeline disk.img P2               # what changed, and when
python explorer.py moccaV2Target.mmi svg 33616        # a screen as SVG

python explorer.py grep disk.img Burmester P2         # which file mentions this?
python explorer.py grep PCM3_IFS2.ifs --hex 48424d35  # or a byte pattern (HBM5)

python explorer.py diff old.ifs new.ifs               # what changed between builds
python explorer.py stock ./car_hbp "D:/PCM/ISO Extract"    # what is not factory
python explorer.py report disk.img unit.html          # one page you can send
```

## What it does

- **Tells you what an image *is*.** Not just a file list: drive variant, navigation
  packages, speech version, odometer, and — for firmware — the build string and whether
  that release supports a jukebox at all.
- **Opens firmware as well as drives.** `PCM3_IFS1.ifs` and `PCM3_IFS2.ifs` from an
  official update package browse exactly like a disk, so "does this release contain X"
  is a directory listing rather than a reverse-engineering session.
- **Reads update discs** — a `.ISO` directly, no extraction needed, or a folder you
  already extracted. Tells you which head units a disc will actually install on, what
  each module writes and where in flash, and verifies every CRC32 against its payload.
- **Opens the persistence flash** (`PCM3_HBpersistence.efs`, and MMI `efs-system.efs`)
  — the factory contents of `/HBpersistence`, which is the baseline to compare a real
  car against.
- **Reads the HMI itself.** The PCM interface is not compiled in; it is data, in
  `.mmi` files. Every string the unit can display in all nine languages — ten on the
  instrument cluster, which is the only place Chinese appears — and the screens, with
  real element geometry on an 800×480 display.
- **Searches file contents**, not just names — across a partition, a firmware image, a
  disc or a folder. Text patterns are tried as UTF-8 *and* UTF-16LE, because firmware
  carries both; hex patterns are there for hunting structures rather than words.
- **Compares any two of them.** `diff` works across kinds: two firmware builds, or a
  car's exported `/HBpersistence` folder against the factory flash image.
- **Answers "what on this unit is not factory"** in one step. The baseline lives in a
  `.efs` inside whichever release matches the car — `stock` finds it, and since a disc
  carries fourteen candidates it scores each against the unit rather than guessing.
- **Shows what changed and when.** `timeline` sorts by mtime and groups files into the
  writing sessions they belonged to, flagging the build cluster everything shares and
  the files written while the clock was unset -- a head unit with no GPS fix boots
  believing it is 2097.
- **Exports a screen as SVG**, so a custom app can be laid out on real OEM metrics
  instead of guessed ones -- open it in Inkscape and draw against it.
- **Writes one shareable HTML page** about an image: what it is, whether each partition
  read cleanly, what is non-stock, and the bootscreens inlined. Self-contained, so it
  attaches to a forum post and still works.
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

## The HMI

The PCM's interface is not compiled into the HMI binary. It is data — 34 `.mmi` files
sitting beside `PCM3Reload` in IFS2, in a container that identifies itself as `HBM5`.
`cayenne.mmi` is one car's screens; `en_us.mmi` and `ru_ru.mmi` are the same interface
in different words. So the whole UI can be read without emulating anything.

```
python explorer.py moccaV2Target.mmi strings Jukebox   # search every string
python explorer.py moccaV2Target.mmi langs             # keys across all languages
python explorer.py moccaV2Target.mmi screens           # what screens exist
python explorer.py moccaV2Target.mmi screens 33616     # one screen's elements
```

In the desktop UI a `screens/` group lists them by name, and **View screen** draws one
on an 800×480 canvas.

Some payloads are compressed. The codec is **stock LZRW2** — Ross Williams' public-domain
compressor, unmodified, which Harman's own class name (`HBLZRW2Compression.cpp`) says
plainly. Only the container framing is Harman's, and misreading it is what made the
algorithm look exotic: the header is three fields, and what looks like two more bytes is
the first control word, sixteen zero bits meaning sixteen literals. That is why these
blobs appear to begin with readable text. 4,810 of 4,810 blocks decode to exactly their
declared length.

Beware the declared length as an oracle, though — stock LZRW3-A also hits it on every
block while producing `von 123123456ungsanfang bis12345678901234ende` where the real
string reads `von Aufzeichnungsanfang bis Aufzeichnungsende`. The `123456789012345678`
you will find in the binary is the uninitialised-slot seed leaking through.

**What this does not do:** draw the actual pixels. Screens come out as labelled boxes,
not a render. Bitmaps are not in these files at all — four `CBitmap` records corpus-wide,
all degenerate — so the graphics live somewhere else and finding them is a separate hunt.
Nor is there any navigation: all 36 HMI classes are `NHBHMI::NDrawing::*`, purely
presentational. There is no screen, menu, button, event or transition class, because the
flow is compiled into `PCM3Reload` rather than stored as data. A wireframe is still enough
to recognise a screen and to lay out a custom one that matches the OEM metrics — list rows
are 664×69, buttons 66×57, the content area 800×364 at y=59.

## Is this unit stock?

The question most people actually have, and it used to take three steps of tribal
knowledge: know that the factory `/HBpersistence` lives in a `.efs` at flash address
`0x03000000`, know it sits inside whichever release matches the car, extract it, then
diff. Now:

```
python explorer.py stock ./exported_hbpersistence "D:/PCM/ISO Extract"
```

A disc carries a baseline per release — fourteen on the 2015 field-update disc — so
picking the first one is a coin toss dressed as an answer. It scores every candidate
against the unit and reports which it used:

```
baseline: /PCM31RDW400/HEADUNIT/ADR3000000/PCM3_HBpersistence.efs  (best of 14, 54 files identical)

  identical to factory         54
  modified                      9
  added (not in factory)       91
  missing (factory has it)     22
```

On a test comparison, taking the first match chose the **Arabic v3** baseline and
reported 31 files identical; scoring chose **RDW v4** and reported 54. Both look
entirely plausible in isolation, which is exactly why the tool shows its working.

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
`main`; numbered [releases](../../releases) are there if you'd rather pin a version, and
they also carry a `PCM-Explorer-<version>.exe` copy if you want the version in the
filename.

Every build shows its version and commit in the title bar and under `--version`, so a bug
report can name the exact build it came from. Every build also runs the test suite first,
so a broken reader never ships as a download, and each is published with its
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
synthetic disk built to that layout.

MMI **firmware** is confirmed working against a real image: the `ifs-root.ifs` from an
MMI 3G+ package (`8R0906961ES`, variant MU9411) opens and enumerates correctly — 43.7 MB
compressed, 99.4 MB inflated, 345 files. A real MMI *drive* image is still untested; if
you have one, an issue saying whether it worked would be genuinely useful.

## Contributing

Images from other units are welcome, particularly **Audi MMI** and **PCM 4.x**. If the tool
reports `unknown` on your image, that's a data point worth opening an issue over: the
partition table, sizes, and the first 512 bytes of each partition are enough to start.

Its sibling project [PCM-Forge](https://github.com/dspl1236/PCM-Forge) covers activation
codes and on-car tooling for the PCM 3.1.

MIT licensed. Research and personal use on hardware you own.
