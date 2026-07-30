"""Smoke tests -- run against synthetic images, so no real disk image is needed.

    python tests/test_smoke.py

Exits non-zero on failure, which is what CI checks before it builds an exe.
"""
import os
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcmexplorer.core import (DiskImage, RootNode, hexdump, human, mode_str,
                              parse_dirents_raw, BLKOFF, QNX6_MAGIC)
from pcmexplorer.decode import decode_cvalue, preview, odometer_from_logbook
from pcmexplorer.firmware import looks_like_ifs, QNX_STARTUP_MAGIC, IFS2_LZO_OFFSET
from pcmexplorer.efs import EfsImage, looks_like_efs
from pcmexplorer.hbm5 import Hbm5File, looks_like_hbm5, read_varint
from pcmexplorer.hbm5geom import (ANCHOR_KINDS, DISPLAY_H, DISPLAY_W,
                                  _varint as _gvarint)
from pcmexplorer.images import (bmp_to_ppm, describe, identify,
                                raw_candidates, rgb565_to_ppm)
from pcmexplorer.search import as_patterns, search_bytes
from pcmexplorer.report import build as build_report
from pcmexplorer.diffimg import compare, format_report, open_side
from pcmexplorer.updatedisc import (FLASH_MAP, UpdateDisc, disc_root,
                                    looks_like_update_disc,
                                    parse_crc_record, parse_def, signature_kind,
                                    summarise_update)

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


def test_decoders():
    print("\ndecoders")
    # A CVALUE frame is u16 tag, u16 id, u32 length, payload -- and 8+length must
    # equal the file size. Build a coding-table one and check it round-trips.
    body = struct.pack("<III", 0xDEADBEEF, 0x65, 0x14)      # crc, magic, hdr len
    body += struct.pack("<II", 1, 0)                        # channel count, payload len
    body += struct.pack("<HHHH", 0x64, 4, 7, 0) + struct.pack("<I", 1234)
    blob = struct.pack("<HHI", 0x0a, 0x1234, len(body)) + body
    out = decode_cvalue(blob)
    check("decode_cvalue() recognises a coding table",
          out is not None and "coding table" in out)
    check("decode_cvalue() reads the channel", out is not None and "ch 0x064" in out)
    check("decode_cvalue() rejects a non-CVALUE",
          decode_cvalue(b"not a cvalue at all") is None)

    elf = b"\x7fELF" + bytes(14) + struct.pack("<H", 42) + bytes(40)
    check("preview() identifies an SH4 ELF", "SuperH" in (preview("/x", elf) or ""))
    # a real PNG carries the IHDR chunk at offset 12; the identifier requires it
    png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" +
           struct.pack(">II", 640, 480) + bytes(8))
    check("preview() identifies a PNG", "640x480" in (preview("/x.png", png) or ""))
    check("preview() passes text through",
          "hello" in (preview("/x.txt", b"hello world") or ""))
    check("odometer_from_logbook() rejects non-sqlite",
          odometer_from_logbook(b"nope") is None)


def test_firmware_detect():
    print("\nfirmware container detection")
    path = os.path.join(tempfile.gettempdir(), "pcmx_fake.ifs")
    with open(path, "wb") as f:                    # QNX startup magic marks an IFS1
        f.write(struct.pack("<I", QNX_STARTUP_MAGIC) + bytes(0x80))
    try:
        check("looks_like_ifs() accepts a QNX startup header", looks_like_ifs(path))
    finally:
        os.remove(path)
    junk = os.path.join(tempfile.gettempdir(), "pcmx_notfw.bin")
    with open(junk, "wb") as f:
        f.write(bytes(0x80))
    try:
        check("looks_like_ifs() rejects a non-.ifs blob", not looks_like_ifs(junk))
    finally:
        os.remove(junk)
    check("IFS2 payload starts at 0x40", IFS2_LZO_OFFSET == 0x40)



def test_update_disc():
    print("\nupdate-disc definitions")
    d = parse_def("""
DISCID = 12-JUN-2015-D;
SYSTEMRELEASEID = PCM31MOPF_V476_RDW;
CONTENTS
{
   PCMG02XX1221=
   {
      PCM31APP0115245A=
      {
         MODULETYPE=c;
         CRCFILE=\\PCM31RDW400\\HEADUNIT\\PCMG02XX1221_PCM31APP.sig;
         BASEDIR=\\PCM31RDW400\\HEADUNIT;
         FILES=
         {
            .\\ADR01C0000\\PCM3_IFS1_MOPF.ifs;
            .\\CRC\\IFS1_MOPF.CRC32;
         };
      };
   };
}
CONTROL
{
   STARTUPDATE PCMG02XX1221;
      UPDATE PCM31APP0115245A;
   ENDUPDATE;
};
""")
    check("parse_def() reads SYSTEMRELEASEID",
          d["systemreleaseid"] == "PCM31MOPF_V476_RDW", str(d["systemreleaseid"]))
    check("parse_def() reads DISCID", d["discid"] == "12-JUN-2015-D")
    check("parse_def() reads the CONTROL dispatch table",
          len(d["units"]) == 1 and d["units"][0]["id"] == "PCMG02XX1221")
    check("parse_def() links unit -> modules",
          d["units"][0]["modules"] == ["PCM31APP0115245A"])
    check("parse_def() reads the module payload list",
          len(d["modules"]["PCM31APP0115245A"]["files"]) == 2,
          str(d["modules"].get("PCM31APP0115245A", {}).get("files")))
    check("parse_def() types the module",
          d["modules"]["PCM31APP0115245A"]["type"] == "APP")

    rec = parse_crc_record("/dev/fs0, 001C0000, 008DFEA4, 26D8DF73\n"
                           "#File,  startadr, length, CRC\n")
    check("parse_crc_record() decodes address/length/CRC",
          rec == ("/dev/fs0", 0x001C0000, 0x008DFEA4, 0x26D8DF73), str(rec))
    check("parse_crc_record() rejects junk", parse_crc_record("nonsense") is None)

    # .sig files are RSA signatures, not checksums -- 264 bytes of ASCII
    sig = b"[RSA]=" + (b"ab" * 128) + b";\n"
    check("signature_kind() identifies RSA-1024",
          signature_kind(sig) == ("RSA", 128), str(signature_kind(sig)))
    check("signature_kind() flags our unsigned marker",
          signature_kind(b"[UNSIGNED] renumbered\n")[0] == "UNSIGNED")
    check("signature_kind() handles a missing file",
          signature_kind(None)[0] == "missing")

    check("flash map knows the IFS1 address",
          FLASH_MAP[0x001C0000].startswith("IFS1"))
    check("looks_like_update_disc() rejects a non-disc",
          not looks_like_update_disc(os.path.join(tempfile.gettempdir(),
                                                  "pcmx_does_not_exist")))

    # An extracted disc is a folder; a folder with a .def in it is a disc.
    d = os.path.join(tempfile.gettempdir(), "pcmx_disc")
    os.makedirs(d, exist_ok=True)
    try:
        with open(os.path.join(d, "PCM31RDW400.def"), "w") as f:
            f.write("SYSTEMRELEASEID = PCM31MOPF_V476_RDW;\n"
                    "CONTROL\n{\n   STARTUPDATE PCMG02XX1221;\n"
                    "      UPDATE PCM31APP0115245A;\n   ENDUPDATE;\n};\n")
        check("looks_like_update_disc() accepts an extracted folder",
              looks_like_update_disc(d))
        # People open the folder they are looking at, which is often one level
        # inside the disc -- that must resolve up, not error.
        inner = os.path.join(d, "PCM31RDW400", "HEADUNIT")
        os.makedirs(inner, exist_ok=True)
        check("a release folder resolves up to the disc",
              disc_root(os.path.join(d, "PCM31RDW400")) == os.path.abspath(d),
              str(disc_root(os.path.join(d, "PCM31RDW400"))))
        check("a folder deeper in still resolves", looks_like_update_disc(inner))
        check("an unrelated folder is not a disc",
              not looks_like_update_disc(tempfile.gettempdir()))
        with UpdateDisc(d) as disc:
            check("UpdateDisc.files() lists the folder", len(disc.files()) == 1)
            defs = disc.definitions()
            check("UpdateDisc.definitions() parses the folder",
                  len(defs) == 1 and list(defs.values())[0]["units"][0]["id"]
                  == "PCMG02XX1221")
            check("summarise_update() renders", "PCM31MOPF_V476_RDW"
                  in summarise_update(disc))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_efs_detect():
    print("\nEFS persistence images")
    path = os.path.join(tempfile.gettempdir(), "pcmx_fake.efs")
    with open(path, "wb") as f:                    # QSSL_F3S marks an F3S image
        f.write(bytes(0x2c) + b"QSSL_F3S" + bytes(0x100))
    try:
        check("looks_like_efs() accepts a QSSL_F3S header", looks_like_efs(path))
    finally:
        os.remove(path)
    junk = os.path.join(tempfile.gettempdir(), "pcmx_notefs.bin")
    with open(junk, "wb") as f:
        f.write(bytes(0x200))
    try:
        check("looks_like_efs() rejects a blank blob", not looks_like_efs(junk))
    finally:
        os.remove(junk)
    check("erase-slack threshold is set", EfsImage.ERASE_RUN >= 16,
          str(EfsImage.ERASE_RUN))
    check("extent alignment is 4", EfsImage.ALIGN == 4)


def test_diff():
    print("\ndiff")
    root = os.path.join(tempfile.gettempdir(), "pcmx_diff")
    a = os.path.join(root, "a")
    b = os.path.join(root, "b")
    for d in (a, b):
        os.makedirs(d, exist_ok=True)
    try:
        with open(os.path.join(a, "same.txt"), "wb") as f:
            f.write(b"identical")
        with open(os.path.join(b, "same.txt"), "wb") as f:
            f.write(b"identical")
        with open(os.path.join(a, "edited.txt"), "wb") as f:
            f.write(b"before")
        with open(os.path.join(b, "edited.txt"), "wb") as f:
            f.write(b"after!")
        with open(os.path.join(a, "gone.txt"), "wb") as f:
            f.write(b"only in a")
        with open(os.path.join(b, "new.txt"), "wb") as f:
            f.write(b"only in b")
        # a file that differs ONLY by flash alignment padding must read as same
        with open(os.path.join(a, "padded.bin"), "wb") as f:
            f.write(b"payload")
        with open(os.path.join(b, "padded.bin"), "wb") as f:
            f.write(b"payload\xff\xff")

        sa, sb = open_side(a), open_side(b)
        added, removed, changed, same, by_name = compare(sa, sb)
        check("identical files match", same == 2, "got %d" % same)
        check("edited file detected",
              [c[0] for c in changed] == ["/edited.txt"], str(changed))
        check("removal detected", [r[0] for r in removed] == ["/gone.txt"],
              str(removed))
        check("addition detected", [x[0] for x in added] == ["/new.txt"],
              str(added))
        check("0xFF alignment padding is not a difference",
              all(c[0] != "/padded.bin" for c in changed))
        rep = format_report(sa, sb, added, removed, changed, same, by_name)
        check("report renders", "CHANGED" in rep and "ONLY IN A" in rep)
        check("path matching used when both sides have paths", not by_name)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _hbm5_varint(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([0x80 | (n >> 8), n & 0xFF])
    return bytes([0xC0 | (n >> 16), (n >> 8) & 0xFF, n & 0xFF])


def test_hbm5():
    print("\nHBM5 HMI definitions")
    check("varint: 1-byte form", read_varint(b"\x05", 0) == (5, 1))
    check("varint: 2-byte form is 14-bit",
          read_varint(b"\x81\x00", 0) == (256, 2), str(read_varint(b"\x81\x00", 0)))
    # the real case that pinned the encoding: c2 6e ea -> 159,466
    check("varint: 3-byte form", read_varint(b"\xc2\x6e\xea", 0) == (159466, 3),
          str(read_varint(b"\xc2\x6e\xea", 0)))

    # Build a minimal but structurally valid file: one descriptor pointing at two
    # payloads, which is the shape every translated key has.
    texts = ["Aktuelles Ziel", "Current destination"]
    pool = b""
    offs = []
    for t in texts:
        offs.append(len(pool))
        raw = t.encode("utf-8")
        pool += _hbm5_varint(len(raw)) + raw
    desc = struct.pack("<II", 0, 2) + struct.pack("<II", 1, 10) + \
        struct.pack("<II", 3, 11)

    n_dir = 3
    hdr_and_dir = 0x28 + 12 * n_dir
    desc_off = hdr_and_dir
    pool_off = desc_off + len(desc)
    body = desc + pool
    size = pool_off + len(pool)

    def ent(rid, off, is_desc, blocklen):
        b0 = (0 << 4) | (1 if is_desc else 0)
        units = ((blocklen + 15) // 16) & 0xFF
        return struct.pack("<II", rid, off) + bytes([b0, 0, units, 0])

    d = bytearray()
    d += b"HBM5" + struct.pack("<HH", 0x0100, 1)
    d += struct.pack("<8I", hdr_and_dir, hdr_and_dir, 0x28, size, n_dir, 0x28,
                     pool_off - 0x28, 1)
    lens = [len(desc), len(pool) - offs[1], 0]
    d += ent(9, desc_off, True, len(desc))
    d += ent(10, pool_off + offs[0], False, offs[1] - offs[0])
    d += ent(11, pool_off + offs[1], False, len(pool) - offs[1])
    d += body

    path = os.path.join(tempfile.gettempdir(), "pcmx_fake.mmi")
    with open(path, "wb") as f:
        f.write(bytes(d))
    try:
        check("looks_like_hbm5() accepts the magic", looks_like_hbm5(path))
        m = Hbm5File(path)
        checks = dict((n, ok) for n, ok, _x in m.verify())
        check("container self-check passes", all(checks.values()), str(checks))
        strs = m.strings()
        check("strings decode as UTF-8",
              sorted(strs.values()) == sorted(texts), str(strs))
        desc_map = m.descriptors()
        check("descriptor parsed", desc_map.get(9) == [(1, 10), (3, 11)],
              str(desc_map))
        tr = m.translations()
        check("languages resolved via descriptor",
              tr and tr[0][1] == {"de": "Aktuelles Ziel",
                                  "en_us": "Current destination"}, str(tr))
    finally:
        os.remove(path)
    check("looks_like_hbm5() rejects a non-HBM5",
          not looks_like_hbm5(os.path.join(tempfile.gettempdir(), "pcmx_nope.mmi")))


def test_hbm5_geom():
    print("\nHBM5 geometry")
    # The two coexisting varint schemes. Getting these confused is what made
    # records look like they carried a variable-length prefix.
    check("scalar scheme: 0x7f is one byte", _gvarint(b"\x7f", 0) == (127, 1))
    check("scalar scheme: 0x81 0x00 is 256", _gvarint(b"\x81\x00", 0) == (256, 2))
    check("CPoint scheme: 0x7f 0x00 is two bytes",
          _gvarint(b"\x7f\x00", 0, point=True) == (0x3F00, 2),
          str(_gvarint(b"\x7f\x00", 0, point=True)))
    check("CPoint scheme: 0x3f is still one byte",
          _gvarint(b"\x3f", 0, point=True) == (63, 1))
    check("both share the 3-byte form",
          _gvarint(b"\xc2\x6e\xea", 0) == _gvarint(b"\xc2\x6e\xea", 0, point=True)
          == (159466, 3))
    check("both share the 0xf0 escape",
          _gvarint(b"\xf0\x00\x01\x00\x00", 0) == (65536, 5))
    check("display constants", (DISPLAY_W, DISPLAY_H) == (800, 480))
    check("anchor kinds are the left/right pair", ANCHOR_KINDS == (21, 22))


def test_images():
    print("\nimage recognition")
    png = b"\x89PNG\r\n\x1a\n" + bytes(4) + b"IHDR" + struct.pack(">II", 800, 480)
    check("PNG identified with size", identify(png) == ("PNG", 800, 480),
          str(identify(png)))
    gif = b"GIF89a" + struct.pack("<HH", 64, 32)
    check("GIF identified", identify(gif) == ("GIF", 64, 32), str(identify(gif)))
    # SOF0 frame header: ff c0, len, precision, height, width
    jpg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF" + bytes(10) +
           b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" +
           struct.pack(">HH", 480, 800) + bytes(6))
    check("JPEG size read from the frame header",
          identify(jpg) == ("JPEG", 800, 480), str(identify(jpg)))
    check("random bytes are not an image", identify(b"not an image at all") is None)

    # ★ extensions lie: the bootscreens are called .bin and are real images
    check("describe() ignores the filename",
          (describe(png) or "").startswith("PNG image"), describe(png))

    # 2x2 24-bit BMP, bottom-up, one red pixel top-left
    px = bytearray()
    for _ in range(2):                       # bottom row
        px += bytes((0, 0, 0))
    px += bytes(2)                           # pad to 4-byte stride
    px += bytes((0, 0, 255)) + bytes((0, 0, 0)) + bytes(2)   # top row: BGR red
    hdr = (b"BM" + struct.pack("<IHHI", 54 + len(px), 0, 0, 54) +
           struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, len(px), 0, 0, 0, 0))
    bmp = hdr + bytes(px)
    check("BMP identified", identify(bmp) == ("BMP", 2, 2), str(identify(bmp)))
    ppm = bmp_to_ppm(bmp)
    check("BMP decodes to PPM", ppm is not None and ppm.startswith(b"P6\n2 2\n255\n"),
          repr(ppm[:16]) if ppm else "None")
    # the file stores bottom row first, so after flipping the red pixel that
    # was written last must come out first, and BGR must have become RGB
    check("BMP rows are flipped to top-down and BGR swapped",
          ppm is not None and ppm[11:14] == bytes((255, 0, 0)),
          repr(ppm[11:14]) if ppm else "-")

    # raw RGB565: full-scale must reach 255, not 248
    raw = struct.pack("<H", 0xFFFF) + struct.pack("<H", 0x0000)
    ppm = rgb565_to_ppm(raw, 2, 1)
    check("RGB565 white expands to full range",
          ppm is not None and ppm.endswith(bytes((255, 255, 255, 0, 0, 0))),
          repr(ppm[-6:]) if ppm else "-")
    check("raw framebuffer size recognised",
          raw_candidates(800 * 480 * 2) == [(800, 480)],
          str(raw_candidates(800 * 480 * 2)))
    check("a wrong size is not a framebuffer", raw_candidates(12345) == [])


def test_search():
    print("\ncontent search")
    pats = as_patterns("abc", "both")
    check("a text pattern searches UTF-8 and UTF-16",
          [p for _l, p in pats] == [b"abc", b"a\x00b\x00c\x00"], str(pats))
    check("hex pattern decodes", as_patterns("89504e47", "hex")[0][1] == b"\x89PNG",
          str(as_patterns("89504e47", "hex")))
    check("odd-length hex is rejected",
          _raises(lambda: as_patterns("abc", "hex")))

    # padded with text, not NULs -- a context window that is half zeros is
    # correctly reported as hex, which would be testing the wrong branch
    data = b"the quick BROWN fox jumps over the lazy dog, brown again"
    hits = search_bytes(data, as_patterns("brown", "utf-8"))
    check("case-sensitive by default", len(hits) == 1, str(hits))
    hits = search_bytes(data, as_patterns("brown", "utf-8", True), ignore_case=True)
    check("case-insensitive finds both", len(hits) == 2, str(hits))
    check("offset is reported", hits[0][0] == 10, str(hits[0]))
    check("text context is readable", "quick" in hits[0][2], hits[0][2])

    # a hit inside binary data should come back as hex, not mojibake
    binary = bytes(range(0, 32)) * 3 + b"\xde\xad\xbe\xef" + bytes(range(0, 32))
    hits = search_bytes(binary, as_patterns("deadbeef", "hex"))
    check("binary context falls back to hex",
          len(hits) == 1 and all(c in "0123456789abcdef" for c in hits[0][2]),
          hits[0][2] if hits else "-")


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


def test_report():
    print("\nHTML report")
    d = os.path.join(tempfile.gettempdir(), "pcmx_rep")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "tiny.img")
    make_mmi_image(src)
    out = os.path.join(d, "out.html")
    try:
        build_report(src, out)
        html = open(out, encoding="utf-8").read()
        check("report is written", os.path.exists(out))
        check("self-contained (no external refs)",
              "http://" not in html and "https://" not in html)
        check("names the image", "tiny.img" in html)
        check("has the partition table", "<table>" in html and "part" in html)
        check("carries the privacy warning", "VIN" in html)
        check("states it is read-only", "read-only" in html)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_version():
    print("\nversion reporting")
    import pcmexplorer
    check("__version__ is set", bool(pcmexplorer.__version__),
          pcmexplorer.__version__)
    check("version_string() includes the version",
          pcmexplorer.__version__ in pcmexplorer.version_string(),
          pcmexplorer.version_string())
    check("build_id() is a string (empty when run from source)",
          isinstance(pcmexplorer.build_id(), str))


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
    test_decoders()
    test_firmware_detect()
    test_update_disc()
    test_efs_detect()
    test_diff()
    test_hbm5()
    test_hbm5_geom()
    test_images()
    test_search()
    test_report()
    test_version()
    test_real_image_if_present()
    print("\n%s" % ("ALL PASSED" if not _fails
                    else "FAILED: " + ", ".join(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
