"""QNX F3S flash-filesystem images (``*.efs``).

An update package carries the head unit's persistence area as a flash image --
on a PCM 3.1 that is ``PCM3_HBpersistence.efs``, 15 MB, destined for NOR flash
at ``0x03000000``.  It is the factory contents of ``/HBpersistence``, which
makes it the reference to compare a real car against: what a firmware update
changed, and what on a given unit is not stock.

The container identifies itself as ``QSSL_F3S`` in its header.  Audi MMI images
(``efs-system.efs``) use the same format, so both open here.

Reading it is scan-based rather than a full F3S traversal.  Every file is
preceded by an 8-byte dirent header (``08 00 <pad> <namelen>``, then the first
extent's unit and index) followed by the name and a 20-byte ``stat_s`` whose
top mode nibble gives the file type.  Content runs from the end of the stat to
the start of the next dirent -- except for QNX-deflate-wrapped payloads, which
carry an ``iwlyfmbp`` header and a block chain whose end has to be walked,
because such a blob can contain byte sequences that look like dirents.

The approach is taken from ``extract_f3s_efs.py`` in the author's MMI3G-Toolkit,
which solved this for Audi images first; it works unmodified on Porsche ones.

**Directory nesting is not reconstructed.**  A dirent records its own name and
first extent but not its parent -- the tree lives in each directory's extent
list, which this reader does not walk.  Entries are therefore reported flat, in
image order, with real names, modes and contents.  That is enough to say what a
file contains and whether it differs from another image, which is what the tool
is for; it is not enough to say which folder it sat in.
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


def looks_like_efs(path):
    """Cheap probe -- is this a QNX F3S flash image?"""
    try:
        with open(path, "rb") as f:
            head = f.read(MAGIC_WINDOW)
        return F3S_MAGIC in head
    except Exception:
        return False


def _u16(d, off):
    return struct.unpack_from("<H", d, off)[0]


def _u32(d, off):
    return struct.unpack_from("<I", d, off)[0]


class EfsImage(object):
    """A QNX F3S image, read-only, presenting the same surface as FirmwareImage."""

    container = "F3S EFS"
    ERASE_RUN = 64            # trailing 0xFF run treated as erase slack, not data
    ALIGN = 4                 # extents are 4-byte aligned, padded with 0xFF

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        if F3S_MAGIC not in self.data[:MAGIC_WINDOW]:
            raise ValueError("not a QNX F3S image (no %s header)"
                             % F3S_MAGIC.decode())
        self._entries = None

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
        """[(path, entry)] in image order, shaped like the firmware reader's."""
        if self._entries is not None:
            return self._entries

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
        return ("F3S EFS image, %d files / %d dirs, %.1f MB"
                % (nf, nd, len(self.data) / 1e6))


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
    L.append("Directory nesting is not reconstructed -- F3S keeps the tree in each")
    L.append("directory's extent list, which this reader does not walk. Names,")
    L.append("modes and contents are real; the folder each file sat in is not shown.")
    return "\n".join(L)
