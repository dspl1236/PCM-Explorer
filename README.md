# PCM Explorer

**Open and browse the hard drive out of a Porsche PCM or Audi MMI head unit — on your desktop, read-only.**

These units run QNX, and their drives use the QNX6 "power-safe" filesystem. Windows can't
read it. macOS can't read it. Linux ships a read-only `qnx6` driver aimed at QNX 6.4+, which
doesn't fully match the Harman variant these head units actually use. So when a head unit
dies and you image the drive, you get a 40 GB file that nothing will open.

This opens it.

```
python explorer.py                      # desktop UI
python explorer.py disk.img             # partition table + filesystem detection
python explorer.py disk.img tree P2      # directory tree
```

## What it does

- **Maps the partitions** and identifies the filesystem in each, reading real geometry
  (block size, inode counts, allocation groups) out of the QNX6 superblock.
- **Rebuilds the directory tree** — every file and folder on the drive, with inode numbers.
- **Hex view** at any byte offset, for poking at things directly.
- **Exports the tree** to a text file so you can diff two drives or send someone a listing.

The tree reconstruction **does not depend on the superblock.** Every QNX6 directory block
identifies itself: its first entry is `.` holding its own inode number, its second is `..`
holding its parent's. PCM Explorer sweeps the partition for that signature and rebuilds the
hierarchy from the fragments. That means it still works on drives where the metadata chain
is damaged, partially zeroed, or simply doesn't match the documented layout — which is
exactly the situation you're in when a unit has failed.

## What it can't do yet

**Extracting file contents is not solved.** QNX6 scrambles inode numbers across allocation
groups, and on the Harman 6.3.2 build the superblock's inode-file chain doesn't describe
that mapping. Files whose inode can't be located are reported as such — the tool will not
write a file it isn't confident about, because a recovery tool that quietly produces garbage
is worse than one that admits the gap.

Work in progress; see [docs/QNX6-NOTES.md](docs/QNX6-NOTES.md) for what's been established
so far and where the remaining problem sits.

## Install

Needs **Python 3.8+**. Tkinter ships with Python on Windows and macOS; on Linux you may need
`sudo apt install python3-tk`. No other dependencies.

```bash
git clone https://github.com/dspl1236/PCM-Explorer
cd PCM-Explorer
python explorer.py
```

Windows users who'd rather not install Python can grab the prebuilt `.exe` from
[Releases](../../releases). It's unsigned, so SmartScreen will warn on first run —
"More info" → "Run anyway", or just use the Python version if you'd prefer not to.

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

## Contributing

Images from other units are genuinely useful, particularly **Audi MMI** (also Harman-built,
likely a similar layout — untested, we don't have one) and **PCM 4.x**. If the tool reports
`unknown` on your image, that's a data point worth opening an issue over: partition table,
sizes, and the first 512 bytes of each partition are enough to start.

Its sibling project [PCM-Forge](https://github.com/dspl1236/PCM-Forge) covers activation
codes and on-car tooling for the PCM 3.1.

MIT licensed. Research and personal use on hardware you own.
