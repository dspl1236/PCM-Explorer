"""
Core reader: raw disk images, MBR partitions, and the QNX6 ("power-safe")
filesystem used by Harman head units.

No GUI dependencies -- import this from scripts, or drive it from the CLI.
Read-only by construction: images are opened 'rb' and nothing here writes.

THE BLOCK ORIGIN
----------------
Every block number stored anywhere in this filesystem -- superblock root-node
pointers, indirect-block entries, inode block pointers -- is relative to the
start of the DATA AREA, not the partition:

    image_offset(block B) = partition_base + 0x3000 + B * blocksize

    0x0000-0x1FFF   boot block      (8 blocks)
    0x2000-0x2FFF   superblock area (4 blocks; the struct itself at 0x2000)
    0x3000          data block 0

Miss that 0x3000 and every indirect chain lands in unrelated file data and
reads as though it were full of holes -- which is exactly what made this
filesystem look undocumented. It is not: once the origin is right, the layout
matches the public QNX6 one.

Sanity check available at open time: (partition_length / blocksize) - num_blocks
== 16 (12 blocks reserved at the front, 4 for the tail superblock).

INODES
------
The inode table is itself a file, whose block map lives in the superblock's
first root node. Inode N (1-based) occupies byte (N-1)*128 of that file. There
is deliberately no closed-form inode->offset formula: this is a copy-on-write
filesystem, so rewritten metadata gets relocated and inode offsets are not even
monotonic. You have to walk the tree. (An earlier reading of this filesystem
mistook those COW relocations for inode numbers being "scrambled across
allocation groups" -- they are not.)

The same block-mapping routine drives file data, the inode table, the block
bitmap and the long-filename table; only the root pointers and level count
differ.
"""
import os
import struct

SECTOR = 512
QNX6_MAGIC = 0x68191122
QNX4_MAGIC = b"QNX4FS"
INODE_SZ = 128
DIRENT_SZ = 32
NIL = 0xFFFFFFFF

# Bytes from the start of a partition to data block 0.
BLKOFF = 0x3000
# Blocks reserved outside the data area: 12 at the front + 4 for the tail superblock.
RESERVED_BLOCKS = 16

# Inode status values. 0 = free, 2 = unlinked but not yet released.
LIVE_STATUS = (1, 3)

S_IFMT, S_IFDIR, S_IFREG, S_IFLNK = 0xF000, 0x4000, 0x8000, 0xA000

PART_TYPES = {
    0x4d: "QNX", 0x4e: "QNX", 0x4f: "QNX",
    0x07: "NTFS/exFAT", 0x0b: "FAT32", 0x0c: "FAT32 (LBA)",
    0x83: "Linux", 0x82: "Linux swap",
}


# ------------------------------------------------------------------ helpers --
def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def human(n):
    if n < 1024:
        return "%d B" % n
    v = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1024.0
        if v < 1024 or unit == "TB":
            return "%.1f %s" % (v, unit)


def hexdump(data, base_off=0, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hx = " ".join("%02x" % c for c in chunk)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        out.append("%08x  %-*s  %s" % (base_off + i, width * 3 - 1, hx, asc))
    return "\n".join(out)


def mode_str(mode):
    kind = {S_IFREG: "-", S_IFDIR: "d", S_IFLNK: "l",
            0x2000: "c", 0x6000: "b", 0x1000: "p"}.get(mode & S_IFMT, "?")
    bits = ""
    for shift in (6, 3, 0):
        p = (mode >> shift) & 7
        bits += ("r" if p & 4 else "-") + ("w" if p & 2 else "-") + ("x" if p & 1 else "-")
    return kind + bits


def is_dir(inode):
    return (inode["mode"] & S_IFMT) == S_IFDIR


def is_link(inode):
    return (inode["mode"] & S_IFMT) == S_IFLNK


# ---------------------------------------------------------------- root node --
class RootNode:
    """An 80-byte root node in the superblock: inode table, bitmap, longfile."""

    __slots__ = ("size", "ptr", "levels", "mode")

    def __init__(self, d, o):
        self.size = _u64(d, o + 0x00)
        self.ptr = list(struct.unpack_from("<16I", d, o + 0x08))
        self.levels = d[o + 0x48]
        self.mode = d[o + 0x49]

    def __repr__(self):
        return "RootNode(size=%d levels=%d ptr0=0x%x)" % (self.size, self.levels, self.ptr[0])


# --------------------------------------------------------------- disk image --
class DiskImage:
    """Read-only view of a raw disk image. Never loads the whole file."""

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        self.f = open(path, "rb")
        self.parts = self._read_mbr()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def read(self, off, n):
        if off < 0 or off >= self.size:
            return b""
        self.f.seek(off)
        return self.f.read(n)

    # -- partitions --
    def _read_mbr(self):
        mbr = self.read(0, 512)
        parts = []
        if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
            return parts
        for i in range(4):
            e = mbr[446 + i * 16: 446 + (i + 1) * 16]
            ptype = e[4]
            lba = _u32(e, 8)
            cnt = _u32(e, 12)
            if ptype == 0 or cnt == 0:
                continue
            parts.append({
                "name": "P%d" % (i + 1),
                "type": ptype,
                "type_name": PART_TYPES.get(ptype, "0x%02x" % ptype),
                "base": lba * SECTOR,
                "length": cnt * SECTOR,
                "bootable": e[0] == 0x80,
            })
        return parts

    def part(self, name):
        for p in self.parts:
            if p["name"] == name:
                return p
        return None

    # -- QNX6 superblocks --
    def superblocks(self, p):
        """Both superblocks (power-safe keeps two), newest serial first.

        Which copy is live varies per partition -- do not assume the head one.
        """
        base, plen = p["base"], p["length"]
        out = []
        for cand in (base + 0x2000, base + plen - 0x1000):
            d = self.read(cand, 0x200)
            if len(d) < 0x200 or _u32(d, 0) != QNX6_MAGIC:
                continue
            out.append({
                "off": cand,
                "serial": _u64(d, 8),
                "blocksize": _u32(d, 0x30),
                "num_inodes": _u32(d, 0x34),
                "free_inodes": _u32(d, 0x38),
                "num_blocks": _u32(d, 0x3c),
                "free_blocks": _u32(d, 0x40),
                "allocgroup": _u32(d, 0x44),
                "raw": d,
            })
        return sorted(out, key=lambda s: -s["serial"])

    def detect_fs(self, p):
        """Returns (label, detail)."""
        sbs = self.superblocks(p)
        if sbs:
            s = sbs[0]
            return "QNX6", ("blocksize=%d inodes=%d/%d blocks=%d free=%d groups=%d"
                            % (s["blocksize"], s["num_inodes"] - s["free_inodes"],
                               s["num_inodes"], s["num_blocks"], s["free_blocks"],
                               s["allocgroup"]))
        head = self.read(p["base"], 8192)
        if QNX4_MAGIC in head:
            return "QNX4", "QNX4 signature (not yet browsable)"
        if head[3:11] == b"NTFS    ":
            return "NTFS", ""
        if b"FAT32" in head[:100] or b"FAT16" in head[:100]:
            return "FAT", ""
        return "unknown", "no QNX6 superblock (salvage scan may still work)"

    def open_fs(self, p):
        """Return a QNX6FS for this partition, or None if it isn't QNX6."""
        try:
            return QNX6FS(self, p)
        except ValueError:
            return None

    # -- salvage mode: rebuild directories without any metadata --
    def scan_dirs(self, p, cap_mb=1200, progress=None, cancel=None):
        """Recover directory blocks by brute-force scan, ignoring all metadata.

        Every QNX6 directory block identifies itself: entry 0 is '.' holding its
        own inode number, entry 1 is '..' holding its parent's. Sweeping for that
        signature rebuilds the hierarchy even when the superblock or inode table
        is unreadable.

        This is the FALLBACK. When the filesystem mounts cleanly, QNX6FS.walk()
        is better: it resolves long filenames, sizes and file contents, which a
        raw scan cannot.
        """
        base, plen = p["base"], p["length"]
        rem = min(plen, cap_mb * 1024 * 1024)
        total = rem
        self.f.seek(base)
        dirs = {}
        carry = b""
        carry_off = base
        done = 0
        while rem > 0:
            if cancel is not None and cancel():
                break
            chunk = self.f.read(min(8 * 1024 * 1024, rem))
            if not chunk:
                break
            rem -= len(chunk)
            done += len(chunk)
            buf = carry + chunk
            j = 0
            while True:
                k = buf.find(b"\x01.", j)
                if k < 0:
                    break
                e = k - 4
                j = k + 2
                if e < 0 or e + 64 > len(buf):
                    continue
                if buf[e + 36] == 0x02 and buf[e + 37:e + 39] == b"..":
                    self_ino = _u32(buf, e)
                    if self_ino and self_ino not in dirs:
                        dirs[self_ino] = {
                            "parent": _u32(buf, e + 32),
                            "kids": parse_dirents_raw(buf, e + 64),
                            "offset": carry_off + e,
                        }
            carry = buf[-64:]
            carry_off += len(buf) - 64
            if progress:
                progress(done, total, len(dirs))
        return dirs


# ------------------------------------------------------------------- QNX6FS --
class QNX6FS:
    """A mounted QNX6 filesystem: inodes, files, directories, symlinks."""

    def __init__(self, img, part, cache_blocks=512):
        self.img = img
        self.part = part
        self.base = part["base"]
        self.plen = part["length"]

        sbs = img.superblocks(part)
        if not sbs:
            raise ValueError("%s: no QNX6 superblock" % part["name"])
        live = sbs[0]
        self.sboff = live["off"]
        self.serial = live["serial"]
        self.bs = live["blocksize"]
        self.num_inodes = live["num_inodes"]
        self.free_inodes = live["free_inodes"]
        self.num_blocks = live["num_blocks"]
        self.free_blocks = live["free_blocks"]
        self.allocgroup = live["allocgroup"]

        d = live["raw"]
        self.rn_inodes = RootNode(d, 0x48)
        self.rn_bitmap = RootNode(d, 0x98)
        self.rn_longfile = RootNode(d, 0xE8)
        self.rn_spare = RootNode(d, 0x138)

        self.ppb = self.bs // 4          # block pointers per indirect block
        self._cache = {}
        self._cache_max = cache_blocks

    # -- geometry --
    def boff(self, b):
        """Block number -> byte offset in the image."""
        return self.base + BLKOFF + b * self.bs

    def block(self, b):
        """Read one block, with a small LRU cache (indirect blocks repeat a lot)."""
        hit = self._cache.get(b)
        if hit is not None:
            return hit
        data = self.img.read(self.boff(b), self.bs)
        if len(self._cache) >= self._cache_max:
            self._cache.clear()
        self._cache[b] = data
        return data

    def geometry_ok(self):
        """True if reserved-block arithmetic agrees with the superblock."""
        return (self.plen // self.bs) - self.num_blocks == RESERVED_BLOCKS

    # -- the block map (drives inodes, files, bitmap and long names alike) --
    def map_block(self, ptrs, levels, n):
        """Logical block n of a file -> physical block, or None for a hole."""
        span = self.ppb ** levels
        top = n // span
        if top >= 16:
            return None
        b = ptrs[top]
        n %= span
        for _ in range(levels):
            if b == NIL:
                return None
            span //= self.ppb
            blk = self.block(b)
            if len(blk) < self.bs:
                return None
            b = _u32(blk, (n // span) * 4)
            n %= span
        return None if b == NIL else b

    # -- inodes --
    def inode_off(self, ino):
        """Inode number -> byte offset of its 128-byte struct, or None."""
        if ino < 1:
            return None
        fo = (ino - 1) * INODE_SZ
        if fo >= self.rn_inodes.size:
            return None
        db = self.map_block(self.rn_inodes.ptr, self.rn_inodes.levels, fo // self.bs)
        if db is None:
            return None
        return self.boff(db) + (fo % self.bs)

    def inode(self, ino):
        off = self.inode_off(ino)
        if off is None:
            return None
        d = self.img.read(off, INODE_SZ)
        if len(d) < INODE_SZ:
            return None
        return {
            "ino": ino,
            "off": off,
            "size": _u64(d, 0x00),
            "uid": _u32(d, 0x08),
            "gid": _u32(d, 0x0C),
            "ftime": _u32(d, 0x10),
            "mtime": _u32(d, 0x14),
            "atime": _u32(d, 0x18),
            "ctime": _u32(d, 0x1C),
            "mode": _u16(d, 0x20),
            "ptr": list(struct.unpack_from("<16I", d, 0x24)),
            "levels": d[0x64],
            "status": d[0x65],
        }

    def live_inodes(self):
        """Every allocated inode. Should total num_inodes - free_inodes."""
        out = []
        for ino in range(1, self.num_inodes + 1):
            i = self.inode(ino)
            if i and i["status"] in LIVE_STATUS:
                out.append(i)
        return out

    # -- file data --
    def read_range(self, inode, start=0, length=None):
        """Read bytes from a file. Handles every indirection level uniformly.

        Unmapped blocks are sparse holes and read as zeros, which is legal.
        """
        size = inode["size"]
        if length is None:
            length = size
        end = min(start + length, size)
        out = bytearray()
        off = start
        while off < end:
            lb, sub = divmod(off, self.bs)
            db = self.map_block(inode["ptr"], inode["levels"], lb)
            chunk = bytes(self.bs) if db is None else self.block(db)
            take = min(self.bs - sub, end - off)
            out += chunk[sub:sub + take]
            off += take
        return bytes(out)

    def read_file(self, inode):
        return self.read_range(inode, 0, inode["size"])

    def link_target(self, inode):
        """A symlink stores its target as its file contents."""
        return self.read_file(inode).rstrip(b"\x00").decode("latin-1")

    # -- names --
    def longname(self, index):
        """Resolve a long (>27 char) filename out of the longfile tree."""
        db = self.map_block(self.rn_longfile.ptr, self.rn_longfile.levels, index)
        if db is None:
            return None
        b = self.block(db)
        if len(b) < 2:
            return None
        ln = _u16(b, 0)
        if ln > self.bs - 2:
            return None
        return b[2:2 + ln].decode("latin-1")

    def dirents(self, inode):
        """Directory entries as [(name, inode)], skipping deleted ones."""
        out = []
        data = self.read_file(inode)
        for o in range(0, max(0, len(data) - DIRENT_SZ + 1), DIRENT_SZ):
            e = data[o:o + DIRENT_SZ]
            ino = _u32(e, 0)
            nl = e[4]
            if ino == 0:
                continue                       # tombstone: deleted entry
            if nl == 0xFF:                     # long name, stored out of line
                idx = _u32(e, 8)
                nm = self.longname(idx) or "<long:%d>" % idx
            else:
                if nl == 0 or nl > 27:
                    continue
                nm = e[5:5 + nl].decode("latin-1")
            out.append((nm, ino))
        return out

    # -- traversal --
    def walk(self, ino=1, path="", seen=None, out=None, max_entries=200000):
        """Depth-first walk from the root. Returns [(path, inode)]."""
        if seen is None:
            seen = set()
        if out is None:
            out = []
        if ino in seen or len(out) >= max_entries:
            return out
        seen.add(ino)
        i = self.inode(ino)
        if i is None:
            return out
        i = dict(i)
        i["path"] = path or "/"
        out.append((path or "/", i))
        if is_dir(i):
            for nm, cino in self.dirents(i):
                if nm in (".", ".."):
                    continue
                self.walk(cino, path + "/" + nm, seen, out, max_entries)
        return out

    # -- self-test --
    def verify(self):
        """Independent consistency checks. Returns [(name, ok, detail)]."""
        res = []
        res.append(("geometry", self.geometry_ok(),
                    "(plen/bs) - num_blocks == %d" % RESERVED_BLOCKS))

        want = self.num_inodes - self.free_inodes
        got = len(self.live_inodes())
        res.append(("inode census", got == want, "%d live, superblock says %d" % (got, want)))

        # Free-block count from the bitmap -- uses the same block map but no inode logic.
        clear = 0
        nblk = (self.num_blocks + 7) // 8
        need = (nblk + self.bs - 1) // self.bs
        for lb in range(need):
            db = self.map_block(self.rn_bitmap.ptr, self.rn_bitmap.levels, lb)
            if db is None:
                continue
            for byte in self.block(db):
                clear += 8 - bin(byte).count("1")
        res.append(("block bitmap", clear == self.free_blocks,
                    "%d clear, superblock says %d free" % (clear, self.free_blocks)))

        bad = 0
        checked = 0
        for _pth, i in self.walk():
            if is_dir(i):
                d = self.read_range(i, 0, 64)
                if len(d) >= 64:
                    checked += 1
                    if _u32(d, 0) != i["ino"] or d[36] != 2:
                        bad += 1
        res.append(("directory identity", bad == 0,
                    "%d directories checked, %d inconsistent" % (checked, bad)))
        return res


# ------------------------------------------------------------ struct parsing --
def parse_dirents_raw(buf, i):
    """Short-name dirents straight out of a raw buffer (salvage mode only)."""
    out = []
    while i + DIRENT_SZ <= len(buf):
        ino = _u32(buf, i)
        nl = buf[i + 4]
        if nl == 0 or nl > 27:
            break
        nm = buf[i + 5:i + 5 + nl]
        if not nm or not all(33 <= c < 127 for c in nm):
            break
        out.append((nm.decode("latin-1"), ino))
        i += DIRENT_SZ
    return out


def build_paths(dirs):
    """inode -> path, for the salvage-mode directory map."""
    roots = [1] if 1 in dirs else [i for i, d in dirs.items() if d["parent"] not in dirs]
    paths = {}
    for r in roots:
        stack = [(r, "" if len(roots) == 1 else "/(root %d)" % r)]
        seen = set()
        while stack:
            ino, prefix = stack.pop()
            if ino in seen:
                continue
            seen.add(ino)
            paths.setdefault(ino, prefix or "/")
            for nm, cino in dirs.get(ino, {}).get("kids", []):
                if nm in (".", ".."):
                    continue
                child = prefix + "/" + nm
                paths.setdefault(cino, child)
                if cino in dirs:
                    stack.append((cino, child))
    return paths
