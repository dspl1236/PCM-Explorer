"""
Firmware images: open a PCM3_IFS1.ifs / PCM3_IFS2.ifs and browse inside it.

These are the files in an official update package. Being able to read them turns
"what does this firmware actually contain" from a reverse-engineering exercise
into a directory listing -- useful for comparing two releases, checking whether a
feature exists in a given version, or pulling a single binary out for analysis.

TWO CONTAINERS, both LZO1X-compressed:

  IFS1  a real QNX boot image. 256-byte startup header at offset 0 (magic
        0x00FF7EEB), then the startup code, then the imagefs. The payload is
        stored as a chain of length-prefixed LZO chunks.

  IFS2  not a QNX boot image at all -- no startup header. It is a single
        self-terminating LZO1X stream beginning at offset 0x40.

Once inflated, both hold a QNX **imagefs**: a signature block followed by a flat
table of directory entries, each carrying a FULL path. No hierarchy to rebuild.

  image_header   'imagefs' | flags | image_size u32 | hdr_size u32 | dir_offset u32
  image_dirent   size u16 | extattr u16 | ino u32 | mode u32 | gid u32 | uid u32
                 | mtime u32 | <type-specific> | path
                   file    : offset u32, size u32, then path
                   dir     : path
                   symlink : sym_offset u16, sym_size u16, path, then target

Read-only: nothing here writes to the source file.
"""
import os
import struct

from . import lzo1x
from .core import human, mode_str, S_IFMT, S_IFDIR, S_IFREG, S_IFLNK

QNX_STARTUP_MAGIC = 0x00FF7EEB
IFS2_LZO_OFFSET = 0x40
IMAGEFS_SIG = b"imagefs"


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def looks_like_ifs(path):
    """Cheap check so the UI can offer firmware files without opening them fully."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x48)
    except OSError:
        return False
    if len(head) < 0x48:
        return False
    if _u32(head, 0) == QNX_STARTUP_MAGIC:
        return True
    # IFS2 has no header of its own; treat a .ifs name with a plausible LZO
    # first byte as a candidate and let the inflate confirm or reject it.
    return path.lower().endswith(".ifs")


class FirmwareImage:
    """An inflated PCM firmware image, browsable like a filesystem."""

    kind = "IFS"

    def __init__(self, path):
        self.path = path
        self.container = None          # "IFS1" or "IFS2"
        self.data = self._inflate(path)
        self.base, self.image_size, self.hdr_size, self.dir_offset = self._find_imagefs()
        self._entries = None

    # -- container handling --
    def _inflate(self, path):
        raw = open(path, "rb").read()
        if len(raw) > 4 and _u32(raw, 0) == QNX_STARTUP_MAGIC:
            self.container = "IFS1"
            return self._inflate_ifs1(raw)
        self.container = "IFS2"
        out, _ip, _reason = lzo1x.decompress_stream(raw, IFS2_LZO_OFFSET)
        if len(out) < 0x1000:
            raise ValueError("%s: not a recognisable firmware image" % os.path.basename(path))
        return bytes(out)

    def _inflate_ifs1(self, raw):
        """Inflate the chunked payload, keeping the uncompressed startup intact.

        The startup header and code are stored plain; only the imagefs that
        follows is chunked. Each chunk is a 2-byte BIG-ENDIAN length (note: the
        only big-endian field in an otherwise little-endian image) followed by
        that many LZO1X bytes. A zero length terminates the chain.
        """
        startup_size = _u32(raw, 0x20)
        stored_size = _u32(raw, 0x24)
        out = bytearray(raw[:startup_size])          # startup stays as-is
        pos = startup_size
        limit = min(stored_size, len(raw)) if stored_size else len(raw)
        while pos + 2 <= limit:
            clen = (raw[pos] << 8) | raw[pos + 1]
            pos += 2
            if clen == 0 or pos + clen > limit:
                break
            chunk, _ip, _r = lzo1x.decompress_stream(raw[pos:pos + clen], 0)
            out += chunk
            pos += clen
        return bytes(out)

    # -- imagefs location --
    def _find_imagefs(self):
        d = self.data
        cands = []
        if len(d) > 0x24 and _u32(d, 0) == QNX_STARTUP_MAGIC:
            cands.append(_u32(d, 0x20))              # startup_size: where it should be
        cands.append(0)
        start = 0
        while True:                                   # fall back to scanning
            i = d.find(IMAGEFS_SIG, start)
            if i < 0 or len(cands) > 40:
                break
            cands.append(i)
            start = i + 1
        for base in cands:
            if base + 20 > len(d) or d[base:base + 7] != IMAGEFS_SIG:
                continue
            image_size, hdr_size, dir_offset = struct.unpack_from("<III", d, base + 8)
            # sanity: a real header points at a dirent table inside the image
            if 0 < hdr_size < len(d) and 0 < dir_offset < hdr_size and image_size <= len(d) + (1 << 20):
                return base, image_size, hdr_size, dir_offset
        raise ValueError("%s: no imagefs header found" % os.path.basename(self.path))

    # -- entries --
    def entries(self):
        """[(path, inode-like dict)] for every file, directory and symlink."""
        if self._entries is not None:
            return self._entries
        d = self.data
        out = []
        off = self.base + self.dir_offset
        end = self.base + self.hdr_size
        while off + 24 <= end:
            size = _u16(d, off)
            if size < 24 or off + size > end:
                break
            mode = _u32(d, off + 0x08)
            ent = {
                "ino": _u32(d, off + 0x04),
                "mode": mode,
                "gid": _u32(d, off + 0x0C),
                "uid": _u32(d, off + 0x10),
                "mtime": _u32(d, off + 0x14),
                "levels": 0,
                "ptr": [],
                "size": 0,
                "_data_off": None,
                "_target": "",
            }
            fmt = mode & S_IFMT
            if fmt == S_IFREG:
                foff, fsize = struct.unpack_from("<II", d, off + 0x18)
                ent["_data_off"] = self.base + foff
                ent["size"] = fsize
                name = d[off + 0x20:off + size]
            elif fmt == S_IFLNK:
                sym_off, sym_len = struct.unpack_from("<HH", d, off + 0x18)
                name = d[off + 0x1C:off + size]
                t = off + 0x1C + sym_off
                ent["_target"] = d[t:t + sym_len].split(b"\x00")[0].decode("latin-1")
                ent["size"] = sym_len
            else:
                name = d[off + 0x18:off + size]
            path = name.split(b"\x00")[0].decode("latin-1")
            if path or ent["ino"] == 1:
                ent["path"] = "/" + path.lstrip("/")
                out.append((ent["path"], ent))
            off += size
        self._entries = out
        return out

    def walk(self, *_a, **_kw):
        """Same shape as the filesystem readers, so callers need no special case."""
        return self.entries()

    def find(self, path):
        """Look an entry up by path.  Tolerates a missing container prefix, so
        '/etc/foo.cfg' finds '/mnt/ifs-root/etc/foo.cfg'."""
        want = "/" + str(path).lstrip("/")
        for p, e in self.entries():
            if p == want:
                return e
        for p, e in self.entries():             # suffix match across the prefix
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
        if length is None:
            return data[start:]
        return data[start:start + length]

    def link_target(self, ent):
        return ent.get("_target", "")

    def dirents(self, _ent):
        return []

    # -- identity --
    def describe(self):
        n_file = sum(1 for _p, e in self.entries() if (e["mode"] & S_IFMT) == S_IFREG)
        n_dir = sum(1 for _p, e in self.entries() if (e["mode"] & S_IFMT) == S_IFDIR)
        n_lnk = sum(1 for _p, e in self.entries() if (e["mode"] & S_IFMT) == S_IFLNK)
        return ("%s container, %s inflated, %d files / %d dirs / %d symlinks"
                % (self.container, human(len(self.data)), n_file, n_dir, n_lnk))

    def verify(self):
        res = []
        res.append(("container", self.container in ("IFS1", "IFS2"), self.container))
        res.append(("imagefs header", self.data[self.base:self.base + 7] == IMAGEFS_SIG,
                    "at 0x%x" % self.base))
        ents = self.entries()
        res.append(("directory table", len(ents) > 0, "%d entries" % len(ents)))
        bad = 0
        for _p, e in ents:
            if (e["mode"] & S_IFMT) == S_IFREG:
                o = e.get("_data_off")
                if o is None or o + e["size"] > len(self.data):
                    bad += 1
        res.append(("file extents", bad == 0, "%d entries point outside the image" % bad))
        return res
