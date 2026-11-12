"""
NOR flash dumps: open the PCM 3.1 flash read off its two chips and say which
parts of it are intact.

The PCM 3.1 (DPEC board) keeps its whole /dev/fs0 in two 32 MB Spansion
S29GL256N chips -- 64 MB together. A unit that boot-loops on a corrupt region
(the classic log line is ``memifs2: Decompression failed``) can only be fixed
by reading those chips with a programmer, so the dump is the first thing anyone
has to work with. This opens it:

  * a merged 64 MB image, or the two chip files -- how the chips share the bus
    (concatenated, or interleaved 8/16/32 bits, either order, optionally
    byte-swapped by the programmer) is worked out from where the region
    headers land
  * every region checked on its own terms. IFS1 and the emergency IFS carry a
    QNX word checksum; IFS2 is decompressed and its imagefs checksummed. Those
    need no update disc, so a build nobody has a disc for can still be checked.
    A broken LZO stream is located to the flash address and to the chip and
    offset it came from.
  * every region matched against an update disc's CRC records and payloads
  * the IFS and EFS regions browse like the .ifs / .efs files they are

Read-only, like everything else here. Repairing a dump (splicing a region and
splitting it back into chip files) lives in PCM-Forge's tools/nor_repair.py,
so that the writing half stays out of a tool whose promise is that it writes
nothing.

Flash map (the ADR<address> folders of an OEM update disc):

  0x0000000 IPL         0x0100000 FPGA       0x01C0000 IFS1
  0x0BC0000 EMERGENCY   0x0FC0000 IFS2       0x3000000 HBPERSISTENCE
  0x3F00000 UPDATEHISTORY
  (the no-HDD LOW variant: DEBUG at 0x2B40000, HBPERSISTENCE at 0x2E00000)
"""
import os
import re
import struct
import zlib

from . import lzo1x
from .core import human

CHIP_SIZE = 32 * 1024 * 1024
FULL_SIZE = 2 * CHIP_SIZE

QNX_STARTUP = b"\xeb\x7e\xff\x00"     # startup_header signature 0x00ff7eeb, LE
HBCIFS = b"hbcifs\x00\x00"
EFS_HEAD = b"\x10\x00\x4c\xff"        # first bytes of every PCM F3S image
F3S_MAGIC = b"QSSL_F3S"
FPGA = b".HDG"

# kind decides how a region is checked and browsed
#        name             start      end        magic        kind     unit data
MAP_STD = [
    ("IPL",           0x0000000, 0x0100000, None,        "raw",  False),
    ("FPGA",          0x0100000, 0x01C0000, FPGA,        "raw",  False),
    ("IFS1",          0x01C0000, 0x0BC0000, QNX_STARTUP, "ifs1", False),
    ("EMERGENCY",     0x0BC0000, 0x0FC0000, QNX_STARTUP, "ifs1", False),
    ("IFS2",          0x0FC0000, 0x3000000, HBCIFS,      "ifs2", False),
    ("HBPERSISTENCE", 0x3000000, 0x3F00000, EFS_HEAD,    "efs",  True),
    ("UPDATEHISTORY", 0x3F00000, 0x4000000, EFS_HEAD,    "efs",  True),
]
MAP_LOW = MAP_STD[:4] + [
    ("IFS2",          0x0FC0000, 0x2B40000, HBCIFS,      "ifs2", False),
    ("DEBUG",         0x2B40000, 0x2E00000, EFS_HEAD,    "efs",  True),
    ("HBPERSISTENCE", 0x2E00000, 0x3F00000, EFS_HEAD,    "efs",  True),
    ("UPDATEHISTORY", 0x3F00000, 0x4000000, EFS_HEAD,    "efs",  True),
]
DESCRIPTION = {
    "IPL": "bootloader", "FPGA": "emergency FPGA bitstream",
    "IFS1": "QNX boot image (kernel, drivers, PCM3Root)",
    "EMERGENCY": "emergency boot image", "IFS2": "HMI image (PCM3Reload, apps)",
    "DEBUG": "debug flash filesystem",
    "HBPERSISTENCE": "persistence: this unit's VIN, activation, coding",
    "UPDATEHISTORY": "update history",
}
# Region headers both maps put in the same place: scoring a candidate layout
# counts how many of them it lands correctly.
PROBES = [(0x0100000, FPGA), (0x01C0000, QNX_STARTUP), (0x0BC0000, QNX_STARTUP),
          (0x0FC0000, HBCIFS), (0x3000000, EFS_HEAD), (0x2E00000, EFS_HEAD),
          (0x3F00000, EFS_HEAD)]
PROBES_EXPECTED = 6          # a standard and a LOW image each place six


# ---------------------------------------------------------------- layouts
# A layout is (kind, width, first, swap):
#   "interleave": units of `width` bytes alternate between the chips, chip
#                 `first` holding the lower address of each pair
#   "concat":     chip `first` is the low 32 MB, the other the high 32 MB
#   swap:         each dump has its bytes swapped within 16-bit words (some
#                 programmers read x16 parts big-endian)

def _swap16(b):
    a = bytearray(b)
    a[0::2], a[1::2] = b[1::2], b[0::2]
    return bytes(a)


def combine(chips, layout):
    kind, width, first, swap = layout
    a, b = chips[first], chips[1 - first]
    if swap:
        a, b = _swap16(a), _swap16(b)
    if kind == "concat":
        return a + b
    out = bytearray(len(a) + len(b))
    step = 2 * width
    for k in range(width):
        out[k::step] = a[k::width]
        out[width + k::step] = b[k::width]
    return bytes(out)


def separate(img, layout):
    """Inverse of combine(): the two chip images a merged image came from."""
    kind, width, first, swap = layout
    half = len(img) // 2
    if kind == "concat":
        a, b = img[:half], img[half:]
    else:
        a, b = bytearray(half), bytearray(half)
        step = 2 * width
        for k in range(width):
            a[k::width] = img[k::step]
            b[k::width] = img[width + k::step]
        a, b = bytes(a), bytes(b)
    if swap:
        a, b = _swap16(a), _swap16(b)
    chips = [None, None]
    chips[first], chips[1 - first] = a, b
    return chips


def candidate_layouts():
    for swap in (False, True):
        for first in (0, 1):
            yield ("concat", 0, first, swap)
            for width in (1, 2, 4):
                yield ("interleave", width, first, swap)


def chip_location(addr, layout):
    """(chip index 0/1, offset in that chip) for a flash address."""
    kind, width, first, swap = layout
    if kind == "concat":
        c, off = (first, addr) if addr < CHIP_SIZE else (1 - first, addr - CHIP_SIZE)
    else:
        unit, within = divmod(addr, width)
        c = first if unit % 2 == 0 else 1 - first
        off = (unit // 2) * width + within
    if swap:
        off ^= 1
    return c, off


def score(chips, layout):
    """How many region headers a layout puts in place -- read through
    chip_location() so no 64 MB image is built per candidate."""
    hits = 0
    for addr, magic in PROBES:
        got = bytes(chips[c][o] for c, o in
                    (chip_location(addr + i, layout) for i in range(len(magic))))
        hits += got == magic
    return hits


def detect_layout(chips):
    """(layout, score, tied-layouts) -- the best-scoring way to combine two chips."""
    ranked = sorted(((score(chips, l), l) for l in candidate_layouts()),
                    key=lambda t: -t[0])
    best = ranked[0][0]
    return ranked[0][1], best, [l for s, l in ranked if s == best]


def describe_layout(layout, names=("A", "B")):
    if layout is None:
        return "single merged image"
    kind, width, first, swap = layout
    lo, hi = names[first], names[1 - first]
    s = ("concatenated (%s low, %s high)" % (lo, hi) if kind == "concat"
         else "%d-bit interleave (%s at even units)" % (width * 8, lo))
    return s + (", bytes swapped in 16-bit words" if swap else "")


# ---------------------------------------------------------------- checks

def wordsum(buf):
    """Sum of little-endian u32 words, mod 2**32 -- QNX's image checksum."""
    n = len(buf) // 4 * 4
    try:
        import numpy as np
        return int(np.frombuffer(buf[:n], "<u4").sum(dtype=np.uint64) & 0xFFFFFFFF)
    except ImportError:             # the frozen exe may ship without numpy
        from array import array
        code = next(c for c in "ILH" if array(c).itemsize == 4)
        a = array(code)
        a.frombytes(bytes(buf[:n]))
        if struct.pack("=I", 1) != struct.pack("<I", 1):
            a.byteswap()
        return sum(a) & 0xFFFFFFFF


def looks_like_nor(path):
    """A merged 64 MB dump, recognised by where its region headers sit."""
    try:
        if os.path.getsize(path) != FULL_SIZE:
            return False
        hits = 0
        with open(path, "rb") as f:
            for addr, magic in PROBES:
                f.seek(addr)
                hits += f.read(len(magic)) == magic
        return hits >= 3
    except OSError:
        return False


WRAP_MAX = 4096        # longest programmer over-read we accept (seen: 256 bytes)


def chip_wrap_extra(size):
    """Bytes past 32 MB a chip dump may carry. Some programmers read a little
    past the end of an S29GL256N and wrap to address 0, so the file is
    32 MB + 256 bytes whose tail repeats the chip's first 256 (seen on a real
    dump, 2026-11-12). Only that pattern is accepted -- see normalise_chip()."""
    return size - CHIP_SIZE if CHIP_SIZE < size <= CHIP_SIZE + WRAP_MAX else 0


def normalise_chip(data, name="chip"):
    """(32 MB of chip data, note) -- trims a programmer's wrap-around over-read,
    refuses anything else that is not exactly 32 MB."""
    if len(data) == CHIP_SIZE:
        return data, None
    extra = chip_wrap_extra(len(data))
    if extra and data[CHIP_SIZE:] == data[:extra]:
        return data[:CHIP_SIZE], ("%s: trimmed %d trailing bytes (the programmer read past "
                                  "the end and wrapped to address 0)" % (name, extra))
    raise ValueError("%s: %s, one S29GL256N chip is %s" % (name, human(len(data)), human(CHIP_SIZE))
                     + (" (the extra bytes do not repeat the chip's start, so they are "
                        "not a wrap-around read)" if extra else ""))


def looks_like_nor_chip(path):
    """One chip's dump: 32 MB, or 32 MB plus a programmer's wrap-around over-read.
    Whether it really is a chip only shows once it is paired with the other one
    and the layout scores."""
    try:
        size = os.path.getsize(path)
        if size == CHIP_SIZE:
            return True
        extra = chip_wrap_extra(size)
        if not extra:
            return False
        with open(path, "rb") as f:
            head = f.read(extra)
            f.seek(CHIP_SIZE)
            return f.read(extra) == head
    except OSError:
        return False


def imagefs_files(img):
    """[(offset, size, path)] of the regular files in a decompressed imagefs,
    sorted by offset. Same dirent layout as FirmwareImage.entries()."""
    if img[:7] != b"imagefs" or len(img) < 20:
        return []
    hdr_size, dir_off = struct.unpack_from("<II", img, 12)
    out, off, end = [], dir_off, min(hdr_size, len(img))
    while off + 24 <= end:
        size = struct.unpack_from("<H", img, off)[0]
        if size < 24 or off + size > end:
            break
        mode = struct.unpack_from("<I", img, off + 8)[0]
        if (mode & 0o170000) == 0o100000:
            foff, fsz = struct.unpack_from("<II", img, off + 0x18)
            name = img[off + 0x20:off + size].split(b"\0")[0].decode("latin-1")
            out.append((foff, fsz, "/" + name.lstrip("/")))
        off += size
    return sorted(out)


def locate_shift(img, shift):
    """Where did a decompressed imagefs gain or lose `shift` bytes?

    A corrupt length code inside the LZO stream can leave the stream intact to
    its end marker but `shift` bytes short (or long). Everything after the
    damage then sits `shift` bytes off from where the directory says. Files
    are followed by zero padding up to the next file's start, so the first
    file whose padding has the next file's start bytes shifted into it (or,
    for a gain, whose next file's start became zeros) is where it happened.
    Returns (path, offset) of that file, or None."""
    files = imagefs_files(img)
    if not files or shift == 0:
        return None
    k = abs(shift)
    for (foff, fsz, path), (nxt, _s, _p) in zip(files, files[1:]):
        pad_start = foff + fsz
        if nxt - pad_start < k or nxt > len(img) + k:
            continue                      # no padding to test, or past the data
        if shift < 0:
            if any(img[nxt - k:nxt]):     # next file's start moved into the padding
                return path, foff
        else:
            if not any(img[nxt:nxt + k]) and any(img[nxt + k:nxt + k + 16]):
                return path, foff         # next file's start pushed down by k
    return None


class Region(object):
    def __init__(self, name, start, end, magic, kind, unit):
        self.name, self.start, self.end = name, start, end
        self.magic, self.kind, self.unit = magic, kind, unit
        self.status = None      # OK / BAD / ERASED / UNVERIFIED, after check()
        self.detail = ""
        self.used = 0           # bytes the image actually occupies
        self.bad_at = None      # flash address a broken stream stopped at

    @property
    def size(self):
        return self.end - self.start

    @property
    def description(self):
        return DESCRIPTION.get(self.name, "")


class NorImage(object):
    """A PCM 3.1 NOR flash image, from one merged file or from two chip dumps."""

    kind = "NOR"

    def __init__(self, path, data=None, layout=None, chip_paths=None):
        self.path = path
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        if len(data) != FULL_SIZE:
            raise ValueError("%s: %s, a PCM 3.1 flash is %s"
                             % (os.path.basename(str(path)), human(len(data)),
                                human(FULL_SIZE)))
        self.data = data
        self.layout = layout            # None for a merged file
        self.chip_paths = chip_paths
        self.layout_score = None
        self.layout_ties = []
        self.notes = []            # e.g. a trimmed programmer over-read
        low = (data[0x2E00000:0x2E00004] == EFS_HEAD
               and data[0x3000000:0x3000004] != EFS_HEAD)
        self.variant = "LOW (no hard drive)" if low else "standard"
        self.regions = [Region(*r) for r in (MAP_LOW if low else MAP_STD)]

    @classmethod
    def from_chips(cls, path_a, path_b):
        chips, notes = [], []
        for p in (path_a, path_b):
            with open(p, "rb") as f:
                d, note = normalise_chip(f.read(), os.path.basename(p))
            chips.append(d)
            if note:
                notes.append(note)
        layout, best, ties = detect_layout(chips)
        if best < 3:
            raise ValueError("no way of combining these two files puts the PCM 3.1 "
                             "region headers in place (best %d of %d) -- are they "
                             "both chips of one PCM 3.1?" % (best, PROBES_EXPECTED))
        img = cls("%s + %s" % (os.path.basename(path_a), os.path.basename(path_b)),
                  data=combine(chips, layout), layout=layout,
                  chip_paths=(path_a, path_b))
        img.layout_score, img.layout_ties = best, ties
        img.notes = notes
        return img

    def close(self):
        pass

    def describe(self):
        return "NOR flash, %s map, %s" % (self.variant, describe_layout(self.layout))

    def region(self, name):
        for r in self.regions:
            if r.name == name.upper():
                return r
        return None

    def region_bytes(self, r):
        return self.data[r.start:r.end]

    def chip_name(self, idx):
        if self.chip_paths:
            return os.path.basename(self.chip_paths[idx])
        return "AB"[idx]

    def locate(self, addr):
        """'chip X offset 0x...' for a flash address, when the dump came as chips."""
        if self.layout is None:
            return None
        c, off = chip_location(addr, self.layout)
        return "chip %s offset 0x%07X" % (self.chip_name(c), off)

    # -- checks --
    def check(self, r):
        """Check one region; fills r.status / r.detail / r.used. IFS2 takes a
        few seconds (it is decompressed), the rest are instant."""
        reg = self.region_bytes(r)
        r.bad_at = None
        if reg.count(b"\xff") == len(reg):
            r.status, r.detail, r.used = "ERASED", "all 0xFF -- nothing written here", 0
            return r
        if r.magic is not None and reg[:len(r.magic)] != r.magic:
            r.status, r.used = "BAD", 0
            r.detail = ("header should start %s, found %s"
                        % (r.magic.hex(), reg[:8].hex()))
            return r
        if r.kind == "ifs1":
            stored = struct.unpack_from("<I", reg, 0x24)[0]
            if not 0 < stored <= len(reg):
                r.status, r.used = "BAD", 0
                r.detail = "startup stored_size 0x%X does not fit the region" % stored
                return r
            s = wordsum(reg[:stored])
            r.used = stored
            if s:
                r.status = "BAD"
                r.detail = ("QNX checksum over %s is 0x%08X, must be 0"
                            % (human(stored), s))
            else:
                r.status = "OK"
                r.detail = "QNX boot image, %s, checksum good" % human(stored)
            return r
        if r.kind == "ifs2":
            dsize, csize = struct.unpack_from("<II", reg, 8)
            if not 0 < csize <= len(reg) - 0x40:
                r.status, r.used = "BAD", 0
                r.detail = "compressed size 0x%X does not fit the region" % csize
                return r
            r.used = 0x40 + csize
            out, ip, why = lzo1x.decompress_stream(reg, 0x40)
            if why == "eof" and len(out) != dsize:
                # The stream decoded cleanly to its end marker but came out the
                # wrong length: a corrupt length code somewhere INSIDE it, not
                # damage at the end. The file directory can name where.
                r.status = "BAD"
                r.bad_at = None
                shift = len(out) - dsize
                r.detail = ("decompresses to its end marker but %d bytes %s (%s of %s): "
                            "the damage is inside the stream (a corrupted length "
                            "code), not where it ends"
                            % (abs(shift), "short" if shift < 0 else "long",
                               "{:,}".format(len(out)), "{:,}".format(dsize)))
                hit = locate_shift(bytes(out), shift)
                if hit:
                    r.detail += ("; every file after %s sits %d bytes off, so it "
                                 "happened inside that file" % (hit[0], abs(shift)))
                return r
            if why.startswith("error") or len(out) != dsize:
                r.status = "BAD"
                r.bad_at = r.start + ip
                r.detail = ("decompression stopped after %s of %s bytes (%s); the "
                            "stream breaks at flash 0x%07X -- the damage is at or "
                            "a little before that address"
                            % ("{:,}".format(len(out)), "{:,}".format(dsize),
                               why, r.bad_at))
                where = self.locate(r.bad_at)
                if where:
                    r.detail += ", %s" % where
                return r
            if out[:7] != b"imagefs":
                r.status = "BAD"
                r.detail = "decompresses, but not to an imagefs"
                return r
            isz = struct.unpack_from("<I", out, 8)[0]
            s = wordsum(bytes(out[:isz]))
            if s:
                r.status = "BAD"
                r.detail = ("decompresses to %s, but the imagefs checksum is 0x%08X "
                            "(must be 0): corrupt data inside the stream"
                            % (human(dsize), s))
            else:
                r.status = "OK"
                r.detail = ("%s compressed -> %s, decompresses and checksums"
                            % (human(csize), human(dsize)))
            return r
        if r.kind == "efs":
            r.used = len(reg)
            r.status = "UNVERIFIED"
            r.detail = "QNX flash filesystem; it has no image checksum to test"
            return r
        r.used = len(reg.rstrip(b"\xff"))
        r.status = "UNVERIFIED"
        r.detail = "no self-check known; match it against an update disc"
        return r

    def check_all(self, progress=None):
        for r in self.regions:
            if progress:
                progress(r)
            self.check(r)
        return self.regions

    # -- browsing --
    def open_region(self, r):
        """A FirmwareImage / EfsImage for a region, or None if it has no files."""
        from .firmware import FirmwareImage
        from .efs import EfsImage
        reg = self.region_bytes(r)
        label = "%s @0x%07X" % (r.name, r.start)
        if r.kind == "ifs1":
            stored = struct.unpack_from("<I", reg, 0x24)[0]
            return FirmwareImage(label, data=reg[:stored or len(reg)])
        if r.kind == "ifs2":
            csize = struct.unpack_from("<I", reg, 12)[0]
            return FirmwareImage(label, data=reg[:0x40 + csize])
        if r.kind == "efs" and F3S_MAGIC in reg[:0x200]:
            return EfsImage(label, data=reg)
        return None

    # -- update disc --
    def match_disc(self, disc, progress=None):
        """{region name: [what on the disc it equals]} by CRC-32.

        Uses the disc's .CRC32 records, and also hashes the ADR<address>
        payloads directly -- the disc carries no record for the IPL or FPGA.
        """
        cands = {}                          # start -> [(crc, length, label)]
        payloads = []
        for p in disc.files():
            parts = p.strip("/").split("/")
            rel = parts[0] if len(parts) > 1 else ""
            if p.upper().endswith(".CRC32"):
                from .updatedisc import parse_crc_record
                data = disc.read(p) or b""
                rec = parse_crc_record(data.decode("latin-1", "replace"))
                if rec and rec[0].startswith("/dev/fs0"):
                    _dev, addr, length, crc = rec
                    cands.setdefault(addr, []).append(
                        (crc, length, "%s %s" % (rel, parts[-1])))
            m = re.search(r"/ADR([0-9A-Fa-f]{7})/([^/]+)$", p)
            if m:
                payloads.append((int(m.group(1), 16), p, rel, m.group(2)))
        starts = {r.start: r for r in self.regions}
        for i, (addr, p, rel, fn) in enumerate(payloads):
            r = starts.get(addr)
            size = disc.size_of(p)
            if r is None or not size or size > r.size:
                continue
            if progress:
                progress(i, len(payloads), fn)
            data = disc.read(p)
            cands.setdefault(addr, []).append(
                (zlib.crc32(data) & 0xFFFFFFFF, len(data), "%s %s" % (rel, fn)))
        out = {}
        for r in self.regions:
            seen, hits = {}, set()
            for crc, length, label in cands.get(r.start, []):
                if length > r.size:
                    continue
                if length not in seen:
                    seen[length] = zlib.crc32(self.data[r.start:r.start + length]) & 0xFFFFFFFF
                if seen[length] == crc:
                    hits.add(label)
            out[r.name] = sorted(hits)
        return out


def compact_matches(labels):
    """['PCM31RDW400 PCM3_IFS1_MOPF.ifs', 'PCM31RDW400 IFS1_MOPF.CRC32', ...]
    -> 'PCM3_IFS1_MOPF.ifs (RDW400)'. A CRC record and its payload are the same
    match, so the record is only listed when no payload matched with it."""
    by_name = {}
    for lab in labels:
        rel, _, name = lab.partition(" ")
        by_name.setdefault(name, set()).add(rel.replace("PCM31", ""))
    payloads = [n for n in by_name if not n.upper().endswith(".CRC32")]
    keep = payloads or list(by_name)
    out = []
    for n in sorted(keep):
        rels = sorted(by_name[n])
        shown = ", ".join(rels) if len(rels) <= 4 else "%d releases" % len(rels)
        out.append("%s (%s)" % (n, shown))
    return "; ".join(out)


def summarise_nor(nor, matches=None):
    lines = ["PCM 3.1 NOR flash   %s" % human(len(nor.data)),
             "map      %s" % nor.variant,
             "layout   %s" % describe_layout(nor.layout)]
    if nor.chip_paths:
        lines.append("chips    A = %s\n         B = %s"
                     % (nor.chip_paths[0], nor.chip_paths[1]))
    for note in getattr(nor, "notes", []):
        lines.append("note     %s" % note)
    if nor.layout_ties and len(nor.layout_ties) > 1:
        lines.append("WARNING  %d layouts fit equally well; the regions may be "
                     "mis-assembled" % len(nor.layout_ties))
    lines.append("")
    for r in nor.regions:
        st = r.status or "-"
        lines.append("%-14s 0x%07X  %-10s %s" % (r.name, r.start, st,
                                               r.detail or r.description))
        if matches is not None:
            m = matches.get(r.name)
            lines.append("%28s %s" % ("disc:", compact_matches(m) if m else "no match"))
    bad = [r.name for r in nor.regions if r.status == "BAD"]
    checked = any(r.status for r in nor.regions)
    if checked:
        lines.append("")
        lines.append("BAD: %s" % ", ".join(bad) if bad else
                     "Every region that can check itself is intact.")
    if any(r.unit for r in nor.regions):
        lines.append("")
        lines.append("HBPERSISTENCE / UPDATEHISTORY belong to this unit -- never "
                     "replace them with another unit's.")
    return "\n".join(lines)
