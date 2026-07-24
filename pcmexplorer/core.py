"""
Core reader: raw disk images, MBR partitions, and the QNX6 ("power-safe")
filesystem used by Harman head units.

No GUI dependencies -- import this from scripts, or drive it from the CLI.

Read-only by construction: images are opened 'rb' and no function here writes
to them.

The interesting part is `scan_dirs`. Rather than trusting the superblock (which
on the Harman QNX 6.3.2 variant does not fully describe the on-disk layout), it
rebuilds the directory tree from the directory blocks themselves: every QNX6
directory block opens with '.' holding its own inode number and '..' holding its
parent's. Sweeping the partition for that signature recovers the hierarchy even
when the superblock chain is unusable or the drive is partly damaged.
"""
import os
import struct

SECTOR = 512
QNX6_MAGIC = 0x68191122
QNX4_MAGIC = b"QNX4FS"
INODE_SZ = 128
NIL = 0xFFFFFFFF
DIRENT_SZ = 32

PART_TYPES = {
    0x4d: "QNX", 0x4e: "QNX", 0x4f: "QNX",
    0x07: "NTFS/exFAT", 0x0b: "FAT32", 0x0c: "FAT32 (LBA)",
    0x83: "Linux", 0x82: "Linux swap",
}


# ------------------------------------------------------------------ helpers --
def human(n):
    """Byte count -> readable size."""
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
    kind = {0x8000: "-", 0x4000: "d", 0xA000: "l",
            0x2000: "c", 0x6000: "b", 0x1000: "p"}.get(mode & 0xF000, "?")
    bits = ""
    for shift in (6, 3, 0):
        p = (mode >> shift) & 7
        bits += ("r" if p & 4 else "-") + ("w" if p & 2 else "-") + ("x" if p & 1 else "-")
    return kind + bits


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
            lba = struct.unpack_from("<I", e, 8)[0]
            cnt = struct.unpack_from("<I", e, 12)[0]
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

    # -- filesystem identification --
    def detect_fs(self, p):
        """Returns (label, detail). 'unknown' does not mean unbrowsable."""
        sbs = self.superblocks(p)
        if sbs:
            s = sbs[0]
            return "QNX6", ("blocksize=%d inodes=%d/%d blocks=%d free=%d groups=%d"
                            % (s["blocksize"], s["num_inodes"] - s["free_inodes"],
                               s["num_inodes"], s["num_blocks"], s["free_blocks"],
                               s["allocgroup"]))
        head = self.read(p["base"], 4096)
        if QNX4_MAGIC in head:
            return "QNX4", "QNX4 signature"
        if head[3:11] == b"NTFS    ":
            return "NTFS", ""
        if b"FAT32" in head[:100] or b"FAT16" in head[:100]:
            return "FAT", ""
        return "unknown", "no superblock found (browsing may still work)"

    # -- QNX6 superblocks --
    def superblocks(self, p):
        """Both superblocks (power-safe keeps two), newest serial first."""
        base, plen = p["base"], p["length"]
        out = []
        for cand in (base + 0x2000, base + plen - 0x1000):
            d = self.read(cand, 256)
            if len(d) < 256 or struct.unpack_from("<I", d, 0)[0] != QNX6_MAGIC:
                continue
            out.append({
                "off": cand,
                "serial": struct.unpack_from("<Q", d, 8)[0],
                "blocksize": struct.unpack_from("<I", d, 0x30)[0],
                "num_inodes": struct.unpack_from("<I", d, 0x34)[0],
                "free_inodes": struct.unpack_from("<I", d, 0x38)[0],
                "num_blocks": struct.unpack_from("<I", d, 0x3c)[0],
                "free_blocks": struct.unpack_from("<I", d, 0x40)[0],
                "allocgroup": struct.unpack_from("<I", d, 0x44)[0],
                "inode_ptr": struct.unpack_from("<I", d, 0x50)[0],
                "inode_levels": d[0x90],
                "raw": d,
            })
        return sorted(out, key=lambda s: -s["serial"])

    def blocksize(self, p):
        sbs = self.superblocks(p)
        return sbs[0]["blocksize"] if sbs else 1024

    # -- directory tree, without the superblock --
    def scan_dirs(self, p, cap_mb=1200, progress=None, cancel=None):
        """Recover the directory tree from self-identifying directory blocks.

        Returns {inode: {parent, kids[(name, inode)], offset}}.
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
                    self_ino = struct.unpack_from("<I", buf, e)[0]
                    par_ino = struct.unpack_from("<I", buf, e + 32)[0]
                    if self_ino and self_ino not in dirs:
                        dirs[self_ino] = {
                            "parent": par_ino,
                            "kids": parse_dirents(buf, e + 64),
                            "offset": carry_off + e,
                        }
            carry = buf[-64:]
            carry_off += len(buf) - 64
            if progress:
                progress(done, total, len(dirs))
        return dirs

    # -- inodes (partial; see docs/QNX6-NOTES.md) --
    def find_inode_regions(self, p, cap_mb=64):
        """Locate regions that look like dense arrays of valid inode structs."""
        base = p["base"]
        bs = self.blocksize(p)
        regions = []
        run = None
        off = base
        end = base + min(p["length"], cap_mb * 1024 * 1024)
        per_block = max(1, bs // INODE_SZ)
        while off < end:
            blk = self.read(off, bs)
            if len(blk) < bs:
                break
            good = sum(1 for s in range(0, bs - INODE_SZ + 1, INODE_SZ)
                       if looks_like_inode(blk[s:s + INODE_SZ], p["length"]))
            if good >= max(2, per_block // 2):
                if run is None:
                    run = off
            elif run is not None:
                regions.append((run, off))
                run = None
            off += bs
        if run is not None:
            regions.append((run, end))
        return regions

    def resolve_inode(self, p, ino, regions=None):
        """Inode number -> (offset, struct), or None if it cannot be located.

        QNX6 scrambles inode numbers across allocation groups. On this Harman
        build the superblock's inode-file chain does not describe the mapping, so
        we try the plausible schemes and accept only a structurally valid result.
        Returns None rather than guessing.
        """
        if regions is None:
            regions = self.find_inode_regions(p)
        for rs, re_ in regions:
            for cand in (rs + (ino - 1) * INODE_SZ, rs + ino * INODE_SZ):
                if rs <= cand < re_:
                    raw = self.read(cand, INODE_SZ)
                    if looks_like_inode(raw, p["length"]):
                        return cand, parse_inode(raw)
        return None

    def read_file(self, p, inode, max_bytes=8 * 1024 * 1024):
        """Read file bytes via direct block pointers.

        Returns (data, warning). Files needing indirect blocks are reported
        rather than partially guessed.
        """
        bs = self.blocksize(p)
        if inode.get("filelevels", 0) != 0:
            return b"", "indirect blocks (filelevels=%d) not supported yet" % inode["filelevels"]
        out = bytearray()
        remaining = min(inode["size"], max_bytes)
        for bp in inode["block_ptr"]:
            if remaining <= 0:
                break
            if bp in (0, NIL):
                continue
            data = self.read(p["base"] + bp * bs, min(bs, remaining))
            if not data:
                break
            out += data
            remaining -= len(data)
        if len(out) < min(inode["size"], max_bytes):
            return bytes(out), "truncated (%d of %d bytes)" % (len(out), inode["size"])
        return bytes(out), None


# ------------------------------------------------------------ struct parsing --
def parse_dirents(buf, i):
    """Parse consecutive 32-byte QNX6 directory entries."""
    out = []
    while i + DIRENT_SZ <= len(buf):
        ino = struct.unpack_from("<I", buf, i)[0]
        nl = buf[i + 4]
        if nl == 0 or nl > 27:
            break
        nm = buf[i + 5:i + 5 + nl]
        if not nm or not all(33 <= c < 127 for c in nm):
            break
        out.append((nm.decode("latin-1"), ino))
        i += DIRENT_SZ
    return out


def parse_inode(raw):
    return {
        "size": struct.unpack_from("<Q", raw, 0x00)[0],
        "uid": struct.unpack_from("<I", raw, 0x08)[0],
        "gid": struct.unpack_from("<I", raw, 0x0c)[0],
        "mtime": struct.unpack_from("<I", raw, 0x14)[0],
        "mode": struct.unpack_from("<H", raw, 0x20)[0],
        "block_ptr": list(struct.unpack_from("<16I", raw, 0x24)),
        "filelevels": raw[0x64],
        "status": raw[0x65],
    }


def looks_like_inode(raw, part_len):
    """Structural plausibility test for a 128-byte inode."""
    if len(raw) < INODE_SZ or not any(raw):
        return False
    if struct.unpack_from("<Q", raw, 0x00)[0] > part_len:
        return False
    mode = struct.unpack_from("<H", raw, 0x20)[0]
    if (mode & 0xF000) not in (0x8000, 0x4000, 0xA000, 0x2000, 0x6000, 0x1000):
        return False
    max_blk = part_len // 1024
    for bp in struct.unpack_from("<16I", raw, 0x24):
        if bp not in (0, NIL) and bp > max_blk:
            return False
    return True


def build_paths(dirs):
    """inode -> full path, walked from the root of the recovered tree."""
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
