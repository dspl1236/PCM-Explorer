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

# Harman reuses the QNX4-era type bytes for partitions that actually carry QNX6
# filesystems, and the same byte means different things on different platforms
# (0x4D is the system partition on a Porsche PCM but the nav partition on an Audi
# MMI). So the type is a hint only -- detect_fs() probes for a real superblock.
PART_TYPES = {
    0x4d: "QNX", 0x4e: "QNX", 0x4f: "QNX", 0xbb: "QNX (logical)",
    0x05: "extended", 0x0f: "extended (LBA)", 0x85: "extended (Linux)",
    0x07: "NTFS/exFAT", 0x0b: "FAT32", 0x0c: "FAT32 (LBA)",
    0x83: "Linux", 0x82: "Linux swap",
}

EXTENDED_TYPES = (0x05, 0x0f, 0x85)


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


def safe_name(s):
    """Filenames off a damaged or foreign drive can hold arbitrary bytes.

    Replace anything unprintable so a listing can't mangle the terminal or blow
    up on a console codepage that cannot represent it.
    """
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in s)


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
        """Primary partitions, plus any logical ones inside an extended partition.

        Audi MMI drives put five partitions (gracenode, mmebackup1, persistence,
        img-cache, pv-cache) inside an extended partition, so stopping at the four
        primary entries would silently miss most of the disk.
        """
        mbr = self.read(0, 512)
        parts = []
        if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
            return parts
        extended = None
        for i in range(4):
            e = mbr[446 + i * 16: 446 + (i + 1) * 16]
            ptype = e[4]
            lba = _u32(e, 8)
            cnt = _u32(e, 12)
            if ptype == 0 or cnt == 0:
                continue
            entry = {
                "name": "P%d" % (i + 1),
                "type": ptype,
                "type_name": PART_TYPES.get(ptype, "0x%02x" % ptype),
                "base": lba * SECTOR,
                "length": cnt * SECTOR,
                "bootable": e[0] == 0x80,
                "logical": False,
            }
            parts.append(entry)
            if ptype in EXTENDED_TYPES and extended is None:
                extended = lba * SECTOR
        if extended is not None:
            parts.extend(self._read_logicals(extended))
        return parts

    def _read_logicals(self, ext_base, max_chain=64):
        """Walk the extended-boot-record chain.

        Each EBR holds two useful entries: the logical partition itself (offset
        relative to that EBR) and a pointer to the next EBR (relative to the start
        of the extended partition).
        """
        out = []
        ebr = ext_base
        n = 0
        seen = set()
        while ebr and n < max_chain and ebr not in seen:
            seen.add(ebr)
            rec = self.read(ebr, 512)
            if len(rec) < 512 or rec[510:512] != b"\x55\xaa":
                break
            e0 = rec[446:462]
            ptype, lba, cnt = e0[4], _u32(e0, 8), _u32(e0, 12)
            if ptype and cnt:
                n += 1
                out.append({
                    "name": "L%d" % n,
                    "type": ptype,
                    "type_name": PART_TYPES.get(ptype, "0x%02x" % ptype),
                    "base": ebr + lba * SECTOR,
                    "length": cnt * SECTOR,
                    "bootable": e0[0] == 0x80,
                    "logical": True,
                })
            e1 = rec[462:478]
            nxt = _u32(e1, 8)
            if e1[4] == 0 or nxt == 0:
                break
            ebr = ext_base + nxt * SECTOR
        return out

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

    def is_qnx4(self, p):
        """QNX4 has no superblock -- the root inode sits at block 1, named '/'."""
        d = self.read(p["base"] + QNX4_BS, QNX4_ENTRY)   # block 2, 1-based
        return len(d) == QNX4_ENTRY and d[0:16].split(b"\x00")[0] == b"/"

    def detect_fs(self, p):
        """Returns (label, detail)."""
        sbs = self.superblocks(p)
        if sbs:
            s = sbs[0]
            return "QNX6", ("blocksize=%d inodes=%d/%d blocks=%d free=%d groups=%d"
                            % (s["blocksize"], s["num_inodes"] - s["free_inodes"],
                               s["num_inodes"], s["num_blocks"], s["free_blocks"],
                               s["allocgroup"]))
        if self.is_qnx4(p):
            root = self.read(p["base"] + QNX4_BS, QNX4_ENTRY)
            return "QNX4", "root at block %d, %d blocks" % (
                _u32(root, 0x14), _u32(root, 0x18))
        head = self.read(p["base"], 8192)
        if QNX4_MAGIC in head:
            return "QNX4", "QNX4 signature"
        if head[3:11] == b"NTFS    ":
            return "NTFS", ""
        if b"FAT32" in head[:100] or b"FAT16" in head[:100]:
            return "FAT", ""
        return "unknown", "no recognised filesystem (salvage scan may still work)"

    def open_fs(self, p):
        """Open the partition's filesystem (QNX6 or QNX4), or None."""
        try:
            return QNX6FS(self, p)
        except ValueError:
            pass
        try:
            return QNX4FS(self, p)
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

    kind = "QNX6"

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


# -------------------------------------------------------------------- QNX4 --
# Older Harman drives (and the nav partition on every PCM 3.1 drive) use QNX4
# rather than QNX6. It is a much simpler format: 512-byte blocks, and a 64-byte
# directory entry that carries the inode inline -- name, size and extents all in
# the one record. There is no superblock; the root directory's inode sits at
# block 1 with the name "/".
QNX4_BS = 512
QNX4_NAME = 16          # inline name length in a directory entry
QNX4_LINK_NAME = 48     # name length in a link entry (how QNX4 does long names)
QNX4_ENTRY = 64

QNX4_FILE_USED = 0x01
QNX4_FILE_MODIFIED = 0x02
QNX4_FILE_BUSY = 0x04
QNX4_FILE_LINK = 0x08
QNX4_FILE_INODE = 0x10
# Only the low five bits are defined. A directory's extent usually runs past its
# last real entry into unallocated space, and that tail is full of stale file
# data whose "status" byte often has bit 0 set by chance -- so requiring the high
# bits to be clear is what separates real entries from noise.
QNX4_STATUS_MASK = 0xE0

_QNX4_XBLK_SIG = b"IamXblk"


class QNX4FS:
    """A QNX4 filesystem. Mirrors the QNX6FS interface so callers don't branch."""

    kind = "QNX4"

    def __init__(self, img, part):
        self.img = img
        self.part = part
        self.base = part["base"]
        self.plen = part["length"]
        self.bs = QNX4_BS
        root = self._entry_at(self.boff(2), 0)
        if root is None or root["name"] != "/":
            raise ValueError("%s: no QNX4 root inode" % part["name"])
        root["path"] = "/"
        root["ino"] = QNX4_BS  # synthetic, stable: block*64 + index
        self.root = root

    # -- geometry --
    def boff(self, b):
        """QNX4 block numbers are 1-BASED: block 1 is the first block.

        Getting this wrong is quietly destructive rather than obviously broken --
        directory listings still look plausible because they simply lose their
        first block and gain a block of unallocated noise, and extracted files
        come out shifted by 512 bytes.
        """
        return self.base + (b - 1) * QNX4_BS

    # -- entries --
    def _entry_at(self, off, idx, require_name=True):
        d = self.img.read(off + idx * QNX4_ENTRY, QNX4_ENTRY)
        if len(d) < QNX4_ENTRY:
            return None
        return self._parse_entry(d, off, idx, require_name)

    def _parse_entry(self, d, off, idx, require_name=True):
        status = d[0x3F]
        if status & QNX4_FILE_LINK:
            # A link entry: it carries the long name, and the real inode lives
            # elsewhere on the disk. That target inode has no inline name of its
            # own -- the name only exists here -- so don't demand one from it.
            name = d[0:QNX4_LINK_NAME].split(b"\x00")[0]
            if not name:
                return None
            blk = _u32(d, QNX4_LINK_NAME)
            ndx = d[QNX4_LINK_NAME + 4]
            target = self._entry_at(self.boff(blk), ndx, require_name=False)
            if target is None:
                return None
            target = dict(target)
            target["name"] = name.decode("latin-1")
            target["ino"] = blk * 64 + ndx
            return target
        name = d[0:QNX4_NAME].split(b"\x00")[0]
        if not name and require_name:
            return None
        return {
            "name": name.decode("latin-1") if name else "",
            "size": _u32(d, 0x10),
            "first": _u32(d, 0x14),          # first extent: start block
            "xsize": _u32(d, 0x18),          # first extent: block count
            "xblk": _u32(d, 0x1C),           # extent-block chain, if any
            "ftime": _u32(d, 0x20),
            "mtime": _u32(d, 0x24),
            "num_xtnts": _u16(d, 0x30),
            "mode": _u16(d, 0x32),
            "uid": _u16(d, 0x34),
            "gid": _u16(d, 0x36),
            "status": status,
            "levels": 0,                      # for display parity with QNX6
            "ptr": [],
            "ino": (off - self.base) // QNX4_BS * 64 + idx,
        }

    # -- extents --
    def extents(self, inode):
        """[(start_block, block_count)] for a file, following any extent blocks."""
        out = []
        if inode["xsize"]:
            out.append((inode["first"], inode["xsize"]))
        xblk = inode.get("xblk") or 0
        seen = set()
        while xblk and xblk not in seen and len(out) < 4096:
            seen.add(xblk)
            # Extent block: u32 next, u32 prev, u8 count, .., 60 extents at 0x10,
            # then an 8-byte "IamXblk" signature at 0x1F0.
            d = self.img.read(self.boff(xblk), QNX4_BS)
            if len(d) < QNX4_BS or not d[0x1F0:0x1F8].startswith(_QNX4_XBLK_SIG):
                break
            count = d[0x08]
            for i in range(min(count, 60)):
                o = 0x10 + i * 8
                blk, cnt = _u32(d, o), _u32(d, o + 4)
                if cnt:
                    out.append((blk, cnt))
            xblk = _u32(d, 0x00)              # next extent block
        return out

    # -- data --
    def read_range(self, inode, start=0, length=None):
        size = inode["size"]
        if length is None:
            length = size
        end = min(start + length, size)
        out = bytearray()
        pos = 0
        for blk, cnt in self.extents(inode):
            run = cnt * QNX4_BS
            if pos + run <= start:
                pos += run
                continue
            data = self.img.read(self.boff(blk), run)
            take_from = max(0, start - pos)
            out += data[take_from:take_from + (end - max(start, pos))]
            pos += run
            if len(out) >= end - start:
                break
        return bytes(out[:max(0, end - start)])

    def read_file(self, inode):
        return self.read_range(inode, 0, inode["size"])

    def link_target(self, inode):
        return self.read_file(inode).rstrip(b"\x00").decode("latin-1")

    # -- directories --
    def dirents(self, inode):
        out = []
        for blk, cnt in self.extents(inode):
            for b in range(blk, blk + cnt):
                off = self.boff(b)
                d = self.img.read(off, QNX4_BS)
                if len(d) < QNX4_BS:
                    break
                for i in range(QNX4_BS // QNX4_ENTRY):
                    raw = d[i * QNX4_ENTRY:(i + 1) * QNX4_ENTRY]
                    st = raw[0x3F]
                    # Reject unallocated tail: undefined status bits mean noise.
                    if st & QNX4_STATUS_MASK:
                        continue
                    if not st & (QNX4_FILE_USED | QNX4_FILE_LINK):
                        continue
                    e = self._parse_entry(raw, off, i)
                    if e is None:
                        continue
                    nm = e["name"]
                    if nm in (".", "..") or not nm:
                        continue
                    # Housekeeping files QNX4 keeps in every directory.
                    if nm in (".longfilenames", "IamTHE.inodeFILE", ".inodes",
                              ".bitmap", ".boot", ".altboot"):
                        continue
                    out.append(e)
        return out

    def walk(self, start=None, path="", max_entries=200000):
        root = start if isinstance(start, dict) else self.root
        out = [(path or "/", root)]
        stack = [(root, path)]
        seen = set()
        while stack and len(out) < max_entries:
            node, prefix = stack.pop()
            key = (node["first"], node["ino"])
            if key in seen:
                continue
            seen.add(key)
            if not is_dir(node):
                continue
            for e in self.dirents(node):
                p = prefix + "/" + e["name"]
                e = dict(e)
                e["path"] = p
                out.append((p, e))
                if is_dir(e):
                    stack.append((e, p))
        return out

    def verify(self):
        res = []
        res.append(("root inode", self.root["name"] == "/",
                    "block 1 holds the root directory"))
        ents = self.dirents(self.root)
        res.append(("root readable", len(ents) > 0, "%d entries" % len(ents)))
        bad = sum(1 for _p, i in self.walk()
                  if not is_dir(i) and i["size"] and not self.extents(i))
        res.append(("extents resolve", bad == 0,
                    "%d files with no extent" % bad))
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
