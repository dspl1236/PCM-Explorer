"""
Turn raw files into answers.

A directory listing tells you a file exists; it doesn't tell you the odometer
reading, which nav database is installed, or whether a drive is the 40 GB or the
100 GB variant. Everything here reads bytes the browser already has and says
what they MEAN.

Read-only, and defensive: a decoder that cannot make sense of its input says so
rather than guessing. Wrong answers are worse than no answer in a recovery tool.
"""
import os
import struct
import tempfile

from .core import human, is_dir, S_IFMT, S_IFREG

# ------------------------------------------------------------------ CVALUE --
# Harman coding values. Serialised C++ CValue objects, NOT persistence blobs --
# which is why persdump2 rejects them with "Invalid Header": they have no header.
CVALUE_TAGS = {0x02: "scalar", 0x09: "container", 0x0a: "typed"}
CODING_MAGIC = 0x65
CODING_HDRLEN = 0x14


def decode_cvalue(data):
    """Render a CVALUE*.CVA as text, or None if it isn't one."""
    if len(data) < 8:
        return None
    tag, id16 = struct.unpack_from("<HH", data, 0)
    length = struct.unpack_from("<I", data, 4)[0]
    if tag not in CVALUE_TAGS or 8 + length != len(data):
        return None
    out = ["CVALUE  tag=%s(0x%02x)  id=0x%04x  payload=%d bytes"
           % (CVALUE_TAGS[tag], tag, id16, length)]
    body = data[8:]
    # A coding-channel table announces itself with 0x65 / 0x14 after a crc32.
    if len(body) >= 20 and struct.unpack_from("<I", body, 4)[0] == CODING_MAGIC \
            and struct.unpack_from("<I", body, 8)[0] == CODING_HDRLEN:
        crc, count = struct.unpack_from("<I", body, 0)[0], struct.unpack_from("<I", body, 12)[0]
        out.append("coding table: %d channels, crc32=0x%08x" % (count, crc))
        o = 20
        while o + 8 <= len(body):
            cid, vlen, rev, flag = struct.unpack_from("<HHHH", body, o)
            if vlen > 0x1000 or o + 8 + vlen > len(body):
                break
            val = body[o + 8:o + 8 + vlen]
            out.append("  ch 0x%03x (%4d)  rev=%-4d%s  %s"
                       % (cid, cid, rev, " F" if flag else "  ", _render_value(val)))
            o += 8 + vlen
        return "\n".join(out)
    if _printable(body):
        out.append("value: %r" % body.rstrip(b"\x00").decode("latin-1"))
    else:
        out.append("opaque payload, first bytes: %s" % body[:32].hex(" "))
    return "\n".join(out)


def _printable(b):
    core = b[:-1] if b[-1:] == b"\x00" else b
    return bool(core) and all(32 <= c < 127 for c in core)


def _render_value(v):
    bits = []
    if _printable(v):
        bits.append('"%s"' % v.rstrip(b"\x00").decode("latin-1"))
    if len(v) == 1:
        bits.append("u8=%d" % v[0])
    elif len(v) == 2:
        bits.append("u16=%d" % struct.unpack("<H", v)[0])
    elif len(v) == 4:
        u = struct.unpack("<I", v)[0]
        f = struct.unpack("<f", v)[0]
        bits.append("u32=%d" % u)
        if 1e-6 < abs(f) < 1e9:
            bits.append("f32=%.4f" % f)
    return ("%-26s " % " ".join(bits) if bits else "") + "[%s]" % v[:16].hex(" ")


# ---------------------------------------------------------------- odometer --
# The driver's logbook is a SQLite database; mileage is stored in 0.1 km units.
KM_PER_UNIT = 0.1
MILES_PER_UNIT = 1.0 / 16.0934


def odometer_from_logbook(data):
    """(km, miles, trips) from LogBookSql.db bytes, or None."""
    if data[:15] != b"SQLite format 3":
        return None
    import sqlite3
    tmp = os.path.join(tempfile.gettempdir(), "_pcmx_logbook.db")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        con = sqlite3.connect(tmp)
        cur = con.cursor()
        raw = cur.execute("SELECT MAX(MAX(StartMileage), MAX(DestMileage)) FROM trips").fetchone()[0]
        trips = cur.execute("SELECT count(*) FROM trips").fetchone()[0]
        con.close()
        if not raw:
            return None
        return raw * KM_PER_UNIT, raw * MILES_PER_UNIT, trips
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ------------------------------------------------------------- file preview --
def preview(path, data, limit=4000):
    """Best-effort human rendering of a file the browser is showing."""
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(".cva"):
        d = decode_cvalue(data)
        if d:
            return d
    if name.startswith("logbooksql") or data[:15] == b"SQLite format 3":
        od = odometer_from_logbook(data)
        if od:
            km, mi, trips = od
            return ("SQLite database\nodometer: %.0f km / %.0f miles\ntrips recorded: %d"
                    % (km, mi, trips))
        return "SQLite database"
    if data[:4] == b"\x7fELF":
        machine = struct.unpack_from("<H", data, 18)[0] if len(data) > 20 else 0
        arch = {42: "SuperH (SH4)", 3: "x86", 40: "ARM"}.get(machine, "machine %d" % machine)
        return "ELF executable, %s, %s" % (arch, human(len(data)))
    # By magic, not by extension: the custom bootscreens are called
    # CustomBootscreen_NNN.bin and are JPEGs.
    from .images import describe as _describe_image
    img = _describe_image(data)
    if img:
        return "%s, %s" % (img, human(len(data)))
    if _printable(data[:200].replace(b"\n", b" ").replace(b"\r", b" ").replace(b"\t", b" ")):
        return data[:limit].decode("latin-1")
    return None


# ------------------------------------------------------------- disk summary --
# Which mount each partition type carries. Same type byte means different things
# on different platforms, so this is a hint that gets confirmed by content.
KNOWN_MOUNTS = {77: "/mnt/data (system)", 78: "/mnt/share (apps)",
                79: "/mnt/nav (navigation)", 80: "/mnt/pv-cache", 81: "/mnt/media (JUKEBOX)"}


def summarise_disk(img):
    """A plain-language answer to 'what is this drive?'"""
    lines = []
    total = img.size
    lines.append("Disk image: %s (%s)" % (os.path.basename(img.path), human(total)))
    if not img.parts:
        lines.append("  no partition table -- not a head-unit drive, or damaged")
        return "\n".join(lines)

    jukebox = None
    lines.append("")
    lines.append("Partitions")
    for p in img.parts:
        fs, _detail = img.detect_fs(p)
        role = KNOWN_MOUNTS.get(p["type"], "")
        lines.append("  %-4s type %-3d %-10s %-6s %s"
                     % (p["name"], p["type"], human(p["length"]), fs, role))
        if p["type"] == 81:
            jukebox = p

    # 40 GB vs 100 GB variant -- decided by whether a media partition exists.
    lines.append("")
    if jukebox:
        lines.append("Variant: 100 GB type -- has a jukebox (%s media partition)."
                     % human(jukebox["length"]))
        lines.append("         Jukebox is the LAST partition, so it can be grown on a")
        lines.append("         larger drive without moving anything else.")
    else:
        lines.append("Variant: 40 GB type -- NO media partition, so no jukebox.")
        lines.append("         Jukebox needs v4.00 firmware, which the early hardware")
        lines.append("         cannot run, so this drive cannot gain one.")

    # Content identity, read straight off the partitions.
    for p in img.parts:
        fs = img.open_fs(p)
        if not fs:
            continue
        try:
            entries = fs.walk()
        except Exception:
            continue
        for pth, e in entries:
            low = pth.lower()
            if is_dir(e) or (e["mode"] & S_IFMT) != S_IFREG:
                continue
            try:
                if low.endswith("masterhdd.info") or low.endswith("sss-version.txt"):
                    txt = fs.read_file(e).decode("latin-1", "replace").strip()
                    if txt:
                        lines.append("")
                        lines.append("%s:" % pth)
                        for l in txt.splitlines()[:6]:
                            lines.append("  " + l.strip())
                elif low.endswith("logbooksql.db"):
                    od = odometer_from_logbook(fs.read_file(e))
                    if od:
                        km, mi, trips = od
                        lines.append("")
                        lines.append("Odometer: %.0f km / %.0f miles (%d trips logged)" % (km, mi, trips))
            except Exception:
                continue
        # nav packages tell you the region and map version
        pkgs = sorted({pth.split("/pkgdb/")[1].split("/")[0]
                       for pth, _e in entries if "/pkgdb/" in pth})
        if pkgs:
            lines.append("")
            lines.append("Navigation packages (%d):" % len(pkgs))
            for k in pkgs[:12]:
                lines.append("  " + k)
    return "\n".join(lines)


def summarise_firmware(fw):
    """A plain-language answer to 'what is this firmware?'"""
    lines = ["Firmware image: %s" % os.path.basename(fw.path), "  " + fw.describe(), ""]
    ents = dict(fw.entries())
    for want in ("/mnt/ifs1/HBproject/version.txt", "/proc/boot/version.txt"):
        e = ents.get(want)
        if e:
            txt = fw.read_file(e).decode("latin-1", "replace").strip()
            lines.append("%s:" % want)
            for l in txt.splitlines()[:12]:
                lines.append("  " + l.strip())
            lines.append("")
            break
    # Jukebox support is a firmware-generation marker: pre-v4 has no idea the
    # media partition exists, so a drive upgrade cannot give it one.
    blob = fw.data
    has_media = blob.count(b"/mnt/media")
    has_mounter = "/proc/boot/hddmounter" in ents
    lines.append("Jukebox support: %s (%d references to /mnt/media)"
                 % ("YES" if has_media else "NO", has_media))
    lines.append("hddmounter present: %s" % ("yes" if has_mounter else "no"))
    big = sorted(((e["size"], p) for p, e in fw.entries()
                  if (e["mode"] & S_IFMT) == S_IFREG), reverse=True)[:8]
    if big:
        lines.append("")
        lines.append("Largest components:")
        for sz, p in big:
            lines.append("  %10s  %s" % (human(sz), p))
    return "\n".join(lines)
