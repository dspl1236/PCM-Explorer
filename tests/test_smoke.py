"""Smoke tests -- run against synthetic images, so no real disk image is needed.

    python tests/test_smoke.py

Exits non-zero on failure, which is what CI checks before it builds an exe.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcmexplorer.core import (DiskImage, RootNode, hexdump, human, mode_str,
                              parse_dirents_raw, BLKOFF, QNX6_MAGIC)

SEC = 512
CYL = 64 * 63          # small geometry keeps the test images tiny

_fails = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  -- " + detail) if detail else ""))
    if not cond:
        _fails.append(name)


def _entry(boot, ptype, start, count):
    return bytes([boot, 0, 0, 0, ptype, 0, 0, 0]) + struct.pack("<II", start, count)


def make_mmi_image(path):
    """A disk laid out the way DrGER's MMI3GP prep tool formats one:
    three primaries plus an extended partition holding five logicals."""
    logicals = [("gracenode", 12), ("mmebackup1", 4), ("persistence", 4),
                ("img-cache", 8), ("pv-cache", 6)]
    nav, media, sss = 20 * CYL, 30 * CYL, 6 * CYL
    ext_cyl = sum(c + 1 for _, c in logicals)
    p1 = CYL
    p2 = p1 + nav
    p3 = p2 + media
    p4 = p3 + sss

    mbr = bytearray(512)
    mbr[446:462] = _entry(0, 77, p1, nav)          # 0x4D nav
    mbr[462:478] = _entry(0, 78, p2, media)        # 0x4E mediadisk
    mbr[478:494] = _entry(0, 79, p3, sss)          # 0x4F sss
    mbr[494:510] = _entry(0, 5, p4, ext_cyl * CYL)  # extended
    mbr[510:512] = b"\x55\xaa"

    with open(path, "wb") as f:
        f.write(mbr)
        records = []
        rel = 0
        for i, (_nm, c) in enumerate(logicals):
            ebr = bytearray(512)
            ebr[446:462] = _entry(0, 187, CYL, c * CYL)
            if i + 1 < len(logicals):
                ebr[462:478] = _entry(0, 5, rel + (c + 1) * CYL,
                                      logicals[i + 1][1] * CYL)
            ebr[510:512] = b"\x55\xaa"
            records.append((p4 + rel, bytes(ebr)))
            rel += (c + 1) * CYL
        f.truncate((p4 + rel + CYL) * SEC)
        for pos, data in records:
            f.seek(pos * SEC)
            f.write(data)
    return len(logicals)


def test_partitions():
    print("\npartition table (MMI-style layout with an extended partition)")
    path = os.path.join(tempfile.gettempdir(), "pcmx_mmi.img")
    n_log = make_mmi_image(path)
    try:
        with DiskImage(path) as img:
            prim = [p for p in img.parts if not p["logical"]]
            log = [p for p in img.parts if p["logical"]]
            check("four primary partitions", len(prim) == 4, "got %d" % len(prim))
            check("five logical partitions", len(log) == n_log, "got %d" % len(log))
            check("logical names are L1..Ln",
                  [p["name"] for p in log] == ["L%d" % (i + 1) for i in range(n_log)])
            check("QNX types recognised",
                  all(p["type_name"].startswith("QNX") for p in prim if p["type"] != 5))
            check("extended flagged", any(p["type"] == 5 for p in prim))
            check("logicals sit inside the disk",
                  all(p["base"] + p["length"] <= img.size for p in log))
            check("no QNX6 superblock on a blank disk",
                  img.detect_fs(prim[0])[0] == "unknown")
            check("open_fs declines a non-QNX6 partition",
                  img.open_fs(prim[0]) is None)
    finally:
        os.remove(path)


def test_no_mbr():
    print("\ndegrades gracefully on a file that is not a disk image")
    path = os.path.join(tempfile.gettempdir(), "pcmx_junk.img")
    with open(path, "wb") as f:
        f.write(b"not a disk image" * 100)
    try:
        with DiskImage(path) as img:
            check("no partitions reported", img.parts == [])
    finally:
        os.remove(path)


def test_helpers():
    print("\nhelpers")
    check("human()", human(0) == "0 B" and human(1536) == "1.5 KB"
          and human(1024 ** 3) == "1.0 GB", human(1536))
    check("mode_str() regular file", mode_str(0o100644) == "-rw-r--r--",
          mode_str(0o100644))
    check("mode_str() directory", mode_str(0o040755) == "drwxr-xr-x",
          mode_str(0o040755))
    check("mode_str() symlink", mode_str(0o120777) == "lrwxrwxrwx",
          mode_str(0o120777))
    hd = hexdump(b"PCM\x00\xff", 0x1000)
    check("hexdump()", "00001000" in hd and "50 43 4d 00 ff" in hd and "PCM.." in hd)
    # 32-byte dirents: u32 inode, u8 namelen, name
    buf = struct.pack("<IB", 7, 3) + b"abc" + bytes(24)
    check("parse_dirents_raw()", parse_dirents_raw(buf, 0) == [("abc", 7)])
    check("block origin constant", BLKOFF == 0x3000, hex(BLKOFF))


def test_rootnode():
    print("\nsuperblock root node")
    d = bytearray(0x200)
    struct.pack_into("<Q", d, 0x48 + 0x00, 16384)              # size
    struct.pack_into("<16I", d, 0x48 + 0x08, *range(100, 116))  # ptr[16]
    d[0x48 + 0x48] = 2                                          # levels
    rn = RootNode(bytes(d), 0x48)
    check("size", rn.size == 16384)
    check("ptr[0]", rn.ptr[0] == 100)
    check("ptr[15]", rn.ptr[15] == 115)
    check("levels", rn.levels == 2)


def test_real_image_if_present():
    """Runs the full filesystem self-test if a real image happens to be around."""
    path = os.environ.get("PCM_TEST_IMAGE")
    if not path or not os.path.isfile(path):
        print("\nreal image: skipped (set PCM_TEST_IMAGE to enable)")
        return
    print("\nreal image: %s" % path)
    with DiskImage(path) as img:
        for p in img.parts:
            fs = img.open_fs(p)
            if not fs:
                continue
            for name, ok, detail in fs.verify():
                check("%s %s" % (p["name"], name), ok, detail)


def main():
    print("PCM Explorer smoke tests")
    test_partitions()
    test_no_mbr()
    test_helpers()
    test_rootnode()
    test_real_image_if_present()
    print("\n%s" % ("ALL PASSED" if not _fails
                    else "FAILED: " + ", ".join(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
