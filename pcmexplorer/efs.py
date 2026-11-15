"""QNX F3S flash-filesystem images (``*.efs``).

An update package carries the head unit's persistence area as a flash image --
on a PCM 3.1 that is ``PCM3_HBpersistence.efs``, 15 MB, destined for NOR flash
at ``0x03000000``.  It is the factory contents of ``/HBpersistence``, which
makes it the reference to compare a real car against: what a firmware update
changed, and what on a given unit is not stock.

The container identifies itself as ``QSSL_F3S`` in its header.  Audi MMI images
(``efs-system.efs``) use the same format, so both open here.

Two readers, tried in this order:

**1. The F3S structure** (``_F3STree``), which is what a live unit needs.  The
image is a row of erase units.  Each unit starts with a 40-byte header:

    +0x00  u16 16, 'L', 0xFF        unit info: struct size, endianness
    +0x04  u16 unit_pow2            log2 of the unit size (18 = 256 KB)
    +0x08  u32 erase count
    +0x0C  u16 unit, u16 index      where the boot record lives
    +0x10  u16 24, u16 logical      this unit's LOGICAL number (0xFFFF = spare)
    +0x14  u32 age, u8 md5[16]

Extent headers (32 bytes each) grow down from the END of the unit: extent N's
header sits at ``unit_size - 32 * (N + 1)``.  A header holds the status (three
copies), then at +0x14 ``u16 text_offset / 4, u16 text_size``, then two extent
pointers (``u16 logical unit, u16 index``): ``next`` and ``super``.  A file or
directory is a chain along ``next``; when the unit rewrites an extent, the old
header's ``super`` points at its replacement, so every hop resolves through
``super`` first.  The boot record (``QSSL_F3S``) names the root dirent.

A dirent's text is ``u16 8, u8, u8 namelen, extent pointer, name (4-aligned),
stat_s(20)``.  For a directory the pointer is its first child, and the
children are a ``next`` chain; for a file it is the first data extent.  Size is
the sum of the data extents -- the stat has no size field.

A factory ``PCM3_HBpersistence.efs`` happens to be laid out in order (logical
unit 1 first, every file in one piece), which is why a byte scan worked on it.
A unit that has been in a car has rewritten its files many times: the boot
record moves to whichever physical unit now holds logical unit 1, and a file
is split across units.  Only this reader gets those right.

**2. A byte scan** (``_scan``), the fallback when the structure does not parse.
Every file is preceded by an 8-byte dirent header (``08 00 <pad> <namelen>``)
followed by the name and a 20-byte ``stat_s``.  Content runs from the end of
the stat to the start of the next dirent -- except for QNX-deflate-wrapped
payloads, which carry an ``iwlyfmbp`` header and a block chain whose end has to
be walked, because such a blob can contain byte sequences that look like
dirents.  The approach is taken from ``extract_f3s_efs.py`` in the author's
MMI3G-Toolkit.  It reports entries flat (no folders), and on a live unit's
flash it gets sizes and contents wrong, because files are not contiguous.
"""
import re
import struct

F3S_MAGIC = b"QSSL_F3S"
MAGIC_WINDOW = 0x200          # how far in to look for it

S_IFMT = 0o170000
S_IFREG, S_IFDIR, S_IFLNK = 0o100000, 0o040000, 0o120000

_FTYPE = {0x1: "FIFO", 0x2: "CHR", 0x4: "DIR", 0x6: "BLK",
          0x8: "FILE", 0xa: "LINK", 0xc: "SOCK"}

DEFLATE_MAGIC = b"iwlyfmbp"    # QNX-deflate wrapper
VALID_BLKSIZES = (4096, 8192, 16384, 32768)


UNIT_HEAD = b"\x10\x00\x4c\xff"   # unit info: struct size 16, 'L', pad
NO_PTR = 0xFFFFFFFF


def looks_like_efs(path):
    """Cheap probe -- is this a QNX F3S flash image?

    A factory image has ``QSSL_F3S`` near the start.  An image saved from a
    car's flash usually does not -- the boot record is wherever logical unit 1
    ended up -- but it still starts with an F3S unit header.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(MAGIC_WINDOW)
        return F3S_MAGIC in head or _is_unit_header(head)
    except Exception:
        return False


def _is_unit_header(b):
    """An F3S erase-unit header: the info magic and a sane unit size."""
    if len(b) < 0x14 or b[:4] != UNIT_HEAD:
        return False
    pow2 = struct.unpack_from("<H", b, 4)[0]
    return 12 <= pow2 <= 24 and b[0x10:0x12] in (b"\x18\x00", b"\xff\xff")


class _F3STree(object):
    """Walk an F3S image by its own structure (see the module docstring).

    Raises ValueError on anything that does not add up, so the caller can fall
    back to the byte scan.
    """

    HDR = 32
    MAX_HOPS = 4096           # super/next chains longer than this are corrupt

    def __init__(self, data):
        self.d = data
        if not _is_unit_header(data[:0x28]):
            raise ValueError("no F3S unit header")
        self.usize = 1 << struct.unpack_from("<H", data, 4)[0]
        self.phys = {}                       # logical unit -> byte offset
        for base in range(0, len(data) - self.usize + 1, self.usize):
            h = data[base:base + 0x28]
            if not _is_unit_header(h) or h[0x10:0x12] == b"\xff\xff":
                continue                     # not F3S, or a spare unit
            logi = struct.unpack_from("<H", h, 0x12)[0]
            self.phys.setdefault(logi, base)
        if 1 not in self.phys:
            raise ValueError("no logical unit 1")
        boot_logi, boot_idx = struct.unpack_from("<HH", data, self.phys[1] + 0x0C)
        boot = self.text(self.resolve(boot_logi, boot_idx))
        if boot[4:12] != F3S_MAGIC:
            raise ValueError("boot record has no %s" % F3S_MAGIC.decode())
        self.root = struct.unpack_from("<HH", boot, 20)

    def header(self, logi, idx):
        base = self.phys.get(logi)
        if base is None:
            raise ValueError("pointer to missing unit %d" % logi)
        o = base + self.usize - self.HDR * (idx + 1)
        if o < base + 0x28:
            raise ValueError("extent index %d out of unit %d" % (idx, logi))
        off4, size = struct.unpack_from("<HH", self.d, o + 0x14)
        nxt, sup = struct.unpack_from("<II", self.d, o + 0x18)
        text_at = base + off4 * 4
        if text_at + size > base + self.usize:
            raise ValueError("extent %d,%d runs past its unit" % (logi, idx))
        return {"text_at": text_at, "size": size, "next": nxt, "super": sup}

    def resolve(self, logi, idx):
        h = self.header(logi, idx)
        hops = 0
        while h["super"] != NO_PTR:
            h = self.header(h["super"] & 0xFFFF, h["super"] >> 16)
            hops += 1
            if hops > self.MAX_HOPS:
                raise ValueError("super chain loops")
        return h

    def text(self, h):
        return self.d[h["text_at"]:h["text_at"] + h["size"]]

    def chain(self, logi, idx):
        out = []
        while (logi, idx) != (0xFFFF, 0xFFFF):
            h = self.resolve(logi, idx)
            out.append(h)
            if h["next"] == NO_PTR or len(out) > self.MAX_HOPS:
                break
            logi, idx = h["next"] & 0xFFFF, h["next"] >> 16
        return out

    def entries(self):
        """[(path, entry)] for every dirent reachable from the root."""
        out = []
        self._walk(self.root, "", out, 0)
        if not out:
            raise ValueError("empty tree")
        return out

    def _walk(self, ptr, parent, out, depth):
        if depth > 64:
            raise ValueError("directory nesting too deep")
        for h in self.chain(*ptr):
            if h["size"] == 0:
                continue                     # a deleted entry left in the chain
            t = self.text(h)
            if len(t) < 8 or t[:2] != b"\x08\x00":
                raise ValueError("chain reached a non-dirent extent")
            nlen = t[3]
            first = struct.unpack_from("<HH", t, 4)
            name = t[8:8 + nlen].split(b"\x00")[0].decode("utf-8", "replace")
            so = 8 + ((nlen + 3) & ~3)
            if len(t) < so + 20 or t[so:so + 2] != b"\x14\x00":
                raise ValueError("dirent %r has no stat" % name)
            mode = _u16(t, so + 2)
            ftype = (mode >> 12) & 0xF
            path = parent + "/" + name.strip("/")
            ent = {"mode": mode, "uid": _u32(t, so + 4), "gid": _u32(t, so + 8),
                   "mtime": _u32(t, so + 12), "_ftype": _FTYPE.get(ftype, "?"),
                   "path": path, "_extents": []}
            if ftype == 0x4:
                out.append((path, ent))
                if first != (0xFFFF, 0xFFFF):
                    self._walk(first, path, out, depth + 1)
                continue
            if first != (0xFFFF, 0xFFFF):
                ent["_extents"] = [(x["text_at"], x["size"]) for x in self.chain(*first)]
            out.append((path, ent))


def _u16(d, off):
    return struct.unpack_from("<H", d, off)[0]


def _u32(d, off):
    return struct.unpack_from("<I", d, off)[0]


class EfsImage(object):
    """A QNX F3S image, read-only, presenting the same surface as FirmwareImage."""

    container = "F3S EFS"
    ERASE_RUN = 64            # trailing 0xFF run treated as erase slack, not data
    ALIGN = 4                 # extents are 4-byte aligned, padded with 0xFF

    def __init__(self, path, data=None):
        """Open a .efs file, or -- with ``data`` -- an image already in memory
        (a region of a NOR flash dump); ``path`` is then just its label."""
        self.path = path
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        self.data = bytes(data)
        if not (F3S_MAGIC in self.data[:MAGIC_WINDOW]
                or (_is_unit_header(self.data[:0x28]) and F3S_MAGIC in self.data)):
            raise ValueError("not a QNX F3S image (no %s header)"
                             % F3S_MAGIC.decode())
        self._entries = None
        self.structured = None    # True: real tree; False: byte-scan fallback
        self.structure_error = ""

    # -- container identity --
    @property
    def image_size(self):
        return len(self.data)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()

    # -- scanning --
    def _scan(self):
        """Find every dirent record in the image."""
        d = self.data
        out = []
        i, end = 0, len(d) - 24
        while i < end:
            # a stat_s starts with its own size (20) then the mode
            if d[i] == 0x14 and d[i + 1] == 0x00:
                mode = _u16(d, i + 2)
                ftype = (mode >> 12) & 0xF
                if ftype in _FTYPE:
                    hdr = self._dirent_for(i)
                    if hdr is not None:
                        hdr_off, name = hdr
                        out.append({
                            "hdr_off": hdr_off,
                            "stat_off": i,
                            "name": name,
                            "ftype": _FTYPE[ftype],
                            "mode": mode,
                            "uid": _u32(d, i + 4),
                            "gid": _u32(d, i + 8),
                            "mtime": _u32(d, i + 12),
                            "unit": _u16(d, hdr_off + 4),
                            "index": _u16(d, hdr_off + 6),
                        })
            i += 1
        return out

    def _dirent_for(self, stat_off):
        """Walk back from a stat_s to the dirent header that introduces it."""
        d = self.data
        for back in range(4, 140, 4):
            hdr_off = stat_off - 8 - back
            if hdr_off < 0:
                return None
            if d[hdr_off] != 0x08 or d[hdr_off + 1] != 0x00:
                continue
            namelen = d[hdr_off + 3]
            if namelen == 0 or namelen > 128:
                continue
            # the name is padded to a 4-byte boundary and must end at the stat
            if hdr_off + 8 + ((namelen + 3) & ~3) != stat_off:
                continue
            raw = d[hdr_off + 8:hdr_off + 8 + namelen]
            name = raw.rstrip(b"\x00").decode("utf-8", "replace")
            if not name or "\x00" in name[:-1]:
                continue
            return hdr_off, name
        return None

    def _wrapped_end(self, content_start):
        """End offset of a QNX-deflate blob, or None if this is not one.

        Needed because a compressed payload can contain byte patterns that look
        like dirents; without following the block chain those become phantom
        files and truncate the real one.
        """
        d, n = self.data, len(self.data)
        if content_start + 16 > n:
            return None
        if d[content_start:content_start + 8] != DEFLATE_MAGIC:
            return None
        declared = _u32(d, content_start + 8)
        blksize = _u16(d, content_start + 12)
        if blksize not in VALID_BLKSIZES:
            return None
        off, total, blocks = content_start + 16, 0, 0
        while off + 8 <= n:
            nxt, pusize, usize = _u16(d, off + 2), _u16(d, off + 4), _u16(d, off + 6)
            if nxt == 0:
                return off + 8                      # EOF marker
            if usize > blksize or pusize > blksize:
                return None                         # chain has gone off the rails
            if nxt < 9 or nxt > blksize + 256:
                return None
            total += usize
            if total > declared + blksize:
                return None
            off += nxt
            blocks += 1
            if blocks > 8192:
                return None
        return None

    def _trim_erased(self, start, stop):
        """Drop the erased-flash slack a file's extent is padded with.

        Content runs to the next dirent, but flash is erased to 0xFF and the
        remainder of the last block -- and the whole tail of the image -- is
        left that way.  Without trimming, the final entry claims megabytes of
        0xFF and every diff against it is noise.

        Cutting at the *start* of the slack rather than trimming backwards from
        the end, because the image does not necessarily finish on 0xFF -- the
        tail carries a few stray bytes, so a backward walk stops immediately.

        A binary could legitimately contain a long 0xFF run, so the cut is only
        taken when everything from there to the end of the extent is
        overwhelmingly 0xFF, which is what erased flash looks like and ordinary
        file data does not.
        """
        # (a) the big case: a long run that is followed by nothing but more of
        #     the same, i.e. the rest of the erase block or the image tail
        if stop - start >= self.ERASE_RUN:
            blob = self.data[start:stop]
            m = re.search(b"\xff{%d,}" % self.ERASE_RUN, blob)
            if m:
                rest = blob[m.start():]
                if rest.count(b"\xff") / float(len(rest)) >= 0.98:
                    stop = start + m.start()

        # Deliberately NOT stripping the 4-byte alignment padding here.  Extents
        # are padded with 0xFF, so a 5630-byte wav occupies 5632 -- but a file
        # whose real data happens to end in 0xFF is indistinguishable from one
        # that is padded, and trimming it would hand back short, corrupt data.
        # Returning two bytes too many is recoverable; returning one too few is
        # not.  ``diffimg`` normalises the padding when comparing, which is
        # where the ambiguity actually matters.
        return stop

    def entries(self):
        """[(path, entry)], shaped like the firmware reader's: the real tree
        when the F3S structure parses, otherwise the flat byte scan."""
        if self._entries is not None:
            return self._entries
        try:
            tree = _F3STree(self.data).entries()
        except (ValueError, struct.error, IndexError) as e:
            self.structured, self.structure_error = False, str(e)
            self._entries = self._scan_entries()
            return self._entries
        out = []
        for ino, (path, ent) in enumerate(tree, 1):
            ext = ent.pop("_extents")
            is_file = ent["_ftype"] == "FILE"
            ent.update({
                "ino": ino,
                "size": sum(n for _o, n in ext),
                "levels": 0,
                "ptr": [],
                "_data_off": ext[0][0] if ext else None,
                "_extents": ext,
                "_target": "",
            })
            if ent["_ftype"] == "LINK":
                ent["_target"] = b"".join(self.data[o:o + n] for o, n in ext) \
                    .split(b"\x00")[0].decode("utf-8", "replace")
            elif not is_file:
                ent["size"] = 0
            out.append((path, ent))
        self.structured = True
        self._entries = out
        return out

    def _scan_entries(self):
        """The byte-scan fallback: flat names, contiguous content."""
        found = sorted(self._scan(), key=lambda e: e["stat_off"])

        # Drop phantom dirents that live inside compressed blobs.
        kept, wrapped_end, skip_until = [], {}, 0
        for e in found:
            if e["hdr_off"] < skip_until:
                continue
            kept.append(e)
            if e["ftype"] == "FILE":
                end = self._wrapped_end(e["stat_off"] + 20)
                if end is not None:
                    wrapped_end[e["stat_off"]] = end
                    skip_until = end

        out = []
        for idx, e in enumerate(kept):
            start = e["stat_off"] + 20
            stop = kept[idx + 1]["hdr_off"] if idx + 1 < len(kept) else len(self.data)
            stop = wrapped_end.get(e["stat_off"], stop)
            is_file = e["ftype"] == "FILE"
            if is_file and e["stat_off"] not in wrapped_end:
                stop = self._trim_erased(start, stop)
            size = max(0, stop - start) if is_file else 0
            name = e["name"].lstrip("/")
            ent = {
                "ino": idx + 1,
                "mode": e["mode"],
                "uid": e["uid"],
                "gid": e["gid"],
                "mtime": e["mtime"],
                "size": size,
                "levels": 0,
                "ptr": [],
                "_data_off": start if is_file else None,
                "_target": "",
                "_ftype": e["ftype"],
                "unit": e["unit"],
                "index": e["index"],
                "path": "/" + name,
            }
            out.append((ent["path"], ent))
        self._entries = out
        return out

    def walk(self, *_a, **_kw):
        return self.entries()

    def find(self, path):
        want = "/" + str(path).lstrip("/")
        for p, e in self.entries():
            if p == want:
                return e
        for p, e in self.entries():
            if p.endswith(want):
                return e
        return None

    def read_file(self, ent):
        """Accepts an entry dict or a path string."""
        if isinstance(ent, str):
            ent = self.find(ent)
            if ent is None:
                return b""
        if ent.get("_data_off") is None:
            return b""
        if ent.get("_extents"):                 # the real tree: join the pieces
            return b"".join(self.data[o:o + n] for o, n in ent["_extents"])
        return self.data[ent["_data_off"]:ent["_data_off"] + ent["size"]]

    def read_range(self, ent, start=0, length=None):
        data = self.read_file(ent)
        return data[start:] if length is None else data[start:start + length]

    def link_target(self, ent):
        return ent.get("_target", "")

    def dirents(self, _ent):
        return []

    # -- identity --
    def describe(self):
        ents = self.entries()
        nf = sum(1 for _p, e in ents if e["_ftype"] == "FILE")
        nd = sum(1 for _p, e in ents if e["_ftype"] == "DIR")
        return ("F3S EFS image, %d files / %d dirs, %.1f MB%s"
                % (nf, nd, len(self.data) / 1e6,
                   "" if self.structured else " (byte scan: no folders)"))


def summarise_efs(efs):
    """What is this persistence image, and what stands out in it?"""
    ents = efs.entries()
    files = [(p, e) for p, e in ents if e["_ftype"] == "FILE"]
    L = ["EFS image: %s" % efs.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
         "  %s" % efs.describe(), ""]

    # The header names the flash region this image belongs to on a PCM 3.1.
    if len(efs.data) == 0xF00000:
        L.append("  15 MB -- matches the /HBpersistence flash region at 0x03000000")
        L.append("")

    interesting = ("debugTools.sh", "audiomixer", "CVALUE", "vin", "TouchCalib",
                   "diskid", "check_HDD", "PagSWAct", "CsiConfig")
    hits = [p for p, _e in files
            if any(k.lower() in p.lower() for k in interesting)]
    if hits:
        L.append("Notable files")
        for p in hits[:16]:
            L.append("  %s" % p)
        if len(hits) > 16:
            L.append("  ... and %d more" % (len(hits) - 16))
        L.append("")

    big = sorted(files, key=lambda x: -x[1]["size"])[:8]
    if big:
        L.append("Largest")
        for p, e in big:
            L.append("  %9s  %s" % ("%.1f KB" % (e["size"] / 1024.0), p))
        L.append("")
    if efs.structured:
        L.append("Read by the F3S structure: folders, sizes and contents are the")
        L.append("unit's own, including files a live unit has split across units.")
    else:
        L.append("Read by a byte scan (the F3S structure did not parse: %s)."
                 % (efs.structure_error or "unknown"))
        L.append("Names and modes are real, but folders are not shown, and on a")
        L.append("unit's live flash the sizes and contents can be wrong.")
    return "\n".join(L)
