"""Porsche PCM 3.1 update discs.

A third artefact type alongside disks and firmware images.  Opens either a raw
ISO9660 image or an already-extracted directory tree, and answers the questions
people actually have about an update disc: what version is this, which head units
will it install on, and is its content intact?

The format, in short:

  * ``HBUPDATE.DEF`` and one ``PCM31<REG><VER>.def`` per release variant hold the
    whole definition, in a plain-text grammar.
  * ``CONTROL`` is a dispatch table keyed on the unit's own hardware ID.  A unit
    whose ID has no ``STARTUPDATE`` block has no update path on that disc -- which
    is the entire mechanism behind the hard version ceilings.
  * ``ADR<addr>`` directory names are NOR-flash destination addresses.
  * ``CRC/*.CRC32`` records are plain zlib CRC-32 over the whole payload file.
  * ``<UNIT>_<MODULE>.sig`` files are RSA-1024 signatures, not checksums.
"""
import os
import re
import struct
import zlib

SECTOR = 2048
PVD_SECTOR = 16
DISC_MARKER = "pcm_update.disc"

# ADR<addr> directory name -> what lives there.  Verified against CRC/*.CRC32.
FLASH_MAP = {
    0x00000000: "IPL / bootloader",
    0x00100000: "emergency FPGA",
    0x001C0000: "IFS1 (QNX boot image, PCM3Root)",
    0x00BC0000: "emergency IFS",
    0x00FC0000: "IFS2 (HMI, PCM3Reload)",
    0x03000000: "HBpersistence EFS",
    0x03F00000: "UpdateHistory EFS",
}

MODULE_ROLE = {
    "IPL": "bootloader / initial program load",
    "BOL": "bootloader",
    "EMR": "emergency IFS + FPGA",
    "IOC": "I/O controller MCU + FPGA",
    "APP": "IFS1 + IFS2 + persistence overlay",
    "HDD": "hard-disk content",
    "HDA": "hard-disk content (alt)",
    "APA": "application (alt)",
    "CFG": "configuration",
    "FDC": "front display controller",
    "PWC": "power controller",
    "TRC": "trace configuration",
}


# --------------------------------------------------------------------------
# a minimal read-only ISO9660 walker -- enough to read an update disc in place
# --------------------------------------------------------------------------

class Iso9660(object):
    """Just enough ISO9660 to list and read files without extracting the image."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.f.seek(PVD_SECTOR * SECTOR)
        pvd = self.f.read(SECTOR)
        if pvd[1:6] != b"CD001":
            raise ValueError("not an ISO9660 image")
        self.volume_id = pvd[40:72].decode("latin-1").strip()
        self.created = pvd[813:830].decode("latin-1").strip("\x00 ")
        root = pvd[156:190]
        self.root_extent = struct.unpack("<I", root[2:6])[0]
        self.root_size = struct.unpack("<I", root[10:14])[0]
        self._cache = None

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _read_dir(self, extent, size):
        """Yield (name, extent, length, is_dir) for one directory."""
        self.f.seek(extent * SECTOR)
        data = self.f.read(size)
        pos = 0
        while pos < len(data):
            rlen = data[pos]
            if rlen == 0:
                # records never span a sector -- skip to the next one
                pos = (pos // SECTOR + 1) * SECTOR
                if pos >= len(data):
                    break
                continue
            rec = data[pos:pos + rlen]
            if len(rec) < 33:
                break
            ext = struct.unpack("<I", rec[2:6])[0]
            length = struct.unpack("<I", rec[10:14])[0]
            flags = rec[25]
            nlen = rec[32]
            name = rec[33:33 + nlen].decode("latin-1")
            if name not in ("\x00", "\x01"):
                name = name.split(";")[0]
                yield name, ext, length, bool(flags & 0x02)
            pos += rlen

    def walk(self):
        """Return {full_path: (extent, length)} for every file on the disc."""
        if self._cache is not None:
            return self._cache
        out = {}
        stack = [("", self.root_extent, self.root_size)]
        seen = set()
        while stack:
            prefix, ext, size = stack.pop()
            if (ext, size) in seen:
                continue
            seen.add((ext, size))
            for name, e, ln, isdir in self._read_dir(ext, size):
                full = prefix + "/" + name
                if isdir:
                    stack.append((full, e, ln))
                else:
                    out[full] = (e, ln)
        self._cache = out
        return out

    def read(self, path):
        entry = self.walk().get(path)
        if entry is None:
            return None
        ext, ln = entry
        self.f.seek(ext * SECTOR)
        return self.f.read(ln)


# --------------------------------------------------------------------------
# the definition-file grammar
# --------------------------------------------------------------------------

def parse_def(text):
    """Parse one PCM31<REG><VER>.def / HBUPDATE.DEF into a dict."""
    info = {"discid": None, "systemreleaseid": None, "units": [], "modules": {}}
    m = re.search(r"DISCID\s*=\s*([^;]+);", text)
    if m:
        info["discid"] = m.group(1).strip()
    m = re.search(r"SYSTEMRELEASEID\s*=\s*([^;]+);", text)
    if m:
        info["systemreleaseid"] = m.group(1).strip()

    ctl = re.search(r"\bCONTROL\b(.*)$", text, re.S)
    if ctl:
        for m in re.finditer(r"STARTUPDATE\s+(\S+?)\s*;(.*?)ENDUPDATE",
                             ctl.group(1), re.S):
            mods = re.findall(r"UPDATE\s+(\S+?)\s*;", m.group(2))
            info["units"].append({"id": m.group(1), "modules": mods})

    for m in re.finditer(r"(PCM31([A-Z]{3})\w*|HDDCHECK\w*)=\s*\{(.*?)FILES=\s*\{(.*?)\};",
                         text, re.S):
        mid, mtype, head, body = m.group(1), m.group(2), m.group(3), m.group(4)
        if mid in info["modules"]:
            continue
        crcf = re.search(r"CRCFILE\s*=\s*([^;]+);", head)
        files = [f.strip().lstrip(".\\").replace("\\", "/")
                 for f in body.split(";") if f.strip()]
        info["modules"][mid] = {
            "type": mtype,
            "crcfile": crcf.group(1).strip() if crcf else None,
            "files": files,
        }
    return info


def parse_crc_record(text):
    """'/dev/fs0, 001C0000, 008DFEA4, 26D8DF73' -> (device, addr, length, crc)."""
    line = text.splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return parts[0], int(parts[1], 16), int(parts[2], 16), int(parts[3], 16)
    except ValueError:
        return None


def signature_kind(data):
    """Classify a .sig file.  These are RSA signatures, not checksums."""
    if data is None:
        return "missing", None
    try:
        s = data.decode("latin-1")
    except Exception:
        return "binary", None
    m = re.match(r"\[(\w+)\]=([0-9a-fA-F]+);", s.strip())
    if m:
        algo, hexsig = m.group(1), m.group(2)
        return algo.upper(), len(hexsig) // 2
    if s.startswith("[UNSIGNED]"):
        return "UNSIGNED", None
    return "unknown", None


# --------------------------------------------------------------------------

class UpdateDisc(object):
    """An update disc, opened either as an ISO image or an extracted tree."""

    def __init__(self, path):
        self.path = path
        self.iso = None
        self.root = None
        self._filelist = None
        if os.path.isdir(path):
            # open the disc, not whichever folder inside it was selected
            self.root = disc_root(path) or path
        else:
            self.iso = Iso9660(path)

    def close(self):
        if self.iso:
            self.iso.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @property
    def volume_id(self):
        return self.iso.volume_id if self.iso else os.path.basename(self.path)

    @property
    def created(self):
        return self.iso.created if self.iso else None

    def files(self):
        """Every file on the disc, as absolute '/'-separated paths."""
        if self._filelist is not None:
            return self._filelist
        if self.iso:
            out = sorted(self.iso.walk().keys())
        else:
            out = []
            for dp, _d, fs in os.walk(self.root):
                for fn in fs:
                    full = os.path.join(dp, fn)
                    out.append("/" + os.path.relpath(full, self.root)
                               .replace(os.sep, "/"))
            out.sort()
        self._filelist = out
        return out

    _files = files          # the original private name, kept for callers

    def read(self, path):
        if self.iso:
            return self.iso.read(path)
        full = os.path.join(self.root, path.lstrip("/").replace("/", os.sep))
        if not os.path.exists(full):
            return None
        with open(full, "rb") as f:
            return f.read()

    def size_of(self, path):
        if self.iso:
            e = self.iso.walk().get(path)
            return e[1] if e else None
        full = os.path.join(self.root, path.lstrip("/").replace("/", os.sep))
        return os.path.getsize(full) if os.path.exists(full) else None

    def is_update_disc(self):
        names = [p.rsplit("/", 1)[-1].lower() for p in self._files()]
        return DISC_MARKER in names or any(n.endswith(".def") for n in names)

    def definitions(self):
        """{path: parsed-def} for every .def / .DEF on the disc."""
        out = {}
        for p in self._files():
            if p.lower().endswith(".def"):
                data = self.read(p)
                if data:
                    out[p] = parse_def(data.decode("latin-1", "replace"))
        return out

    def releases(self):
        """Top-level release directory names, e.g. PCM31RDW400."""
        seen = set()
        for p in self._files():
            parts = p.strip("/").split("/")
            # only a directory prefix counts -- a sibling PCM31X.def at the root
            # is a definition, not a release tree
            if len(parts) > 1 and parts[0].upper().startswith("PCM31"):
                seen.add(parts[0])
        return sorted(seen)

    def flash_layout(self):
        """[(address, description, [payload names])] from the ADR* directories."""
        found = {}
        for p in self._files():
            m = re.search(r"/ADR([0-9A-Fa-f]{7})/([^/]+)$", p)
            if m:
                addr = int(m.group(1), 16)
                found.setdefault(addr, set()).add(m.group(2))
        return [(a, FLASH_MAP.get(a, "unknown"), sorted(found[a]))
                for a in sorted(found)]

    def verify_crcs(self, release=None):
        """Check every .CRC32 record against the payload it describes.

        Returns [(record-name, status, detail)] where status is
        'pass' / 'fail' / 'absent'.
        """
        results = []
        payloads = {}
        for p in self._files():
            m = re.search(r"/ADR([0-9A-Fa-f]{7})/([^/]+)$", p)
            if m:
                payloads.setdefault(int(m.group(1), 16), []).append(p)

        for p in self._files():
            if not p.upper().endswith(".CRC32"):
                continue
            if release and release not in p:
                continue
            data = self.read(p)
            if not data:
                continue
            rec = parse_crc_record(data.decode("latin-1", "replace"))
            if not rec:
                results.append((p, "fail", "unparseable record"))
                continue
            _dev, addr, length, crc = rec
            cands = payloads.get(addr, [])
            match = None
            for c in cands:
                if self.size_of(c) == length:
                    match = c
                    break
            if match is None:
                results.append((p, "absent",
                                "no payload at %08X with length %d" % (addr, length)))
                continue
            blob = self.read(match)
            calc = zlib.crc32(blob) & 0xFFFFFFFF
            if calc == crc:
                results.append((p, "pass", "%s  %08X" % (match.rsplit("/", 1)[-1], crc)))
            else:
                results.append((p, "fail", "%s  declared %08X computed %08X"
                                % (match.rsplit("/", 1)[-1], crc, calc)))
        return results

    def signatures(self):
        """[(name, algorithm, size-in-bytes)] for every .sig on the disc."""
        out = []
        for p in self._files():
            if p.lower().endswith(".sig"):
                algo, nbytes = signature_kind(self.read(p))
                out.append((p.rsplit("/", 1)[-1], algo, nbytes))
        return out


def disc_root(path):
    """The disc root for a folder, or None.

    People open the folder they are looking at, which is often one release
    inside a disc (``PCM31RDW400``) or the module folder below it -- so accept
    a release tree by its ``HEADUNIT`` directory, and walk up a couple of
    levels to find the definitions rather than refusing.
    """
    if not os.path.isdir(path):
        return None
    here = os.path.abspath(path)
    fallback = None
    for _ in range(3):
        try:
            names = set(os.listdir(here))
        except OSError:
            break
        # a folder carrying the definitions is the real root: prefer it, since
        # units/modules/crc all need them, and keep looking up for one
        if DISC_MARKER in names or any(n.lower().endswith(".def") for n in names):
            return here
        if fallback is None and ("HEADUNIT" in names or "headunit" in names):
            fallback = here        # a release tree -- real content, no defs
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return fallback


def looks_like_update_disc(path):
    """Cheap probe: is this an update disc (ISO or extracted tree)?"""
    try:
        if os.path.isdir(path):
            return disc_root(path) is not None
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) < PVD_SECTOR * SECTOR + SECTOR:
            return False
        with open(path, "rb") as f:
            f.seek(PVD_SECTOR * SECTOR)
            if f.read(6)[1:6] != b"CD001":
                return False
        with UpdateDisc(path) as d:
            return d.is_update_disc()
    except Exception:
        return False


def summarise_update(disc):
    """The human summary -- what is this disc, and what will it install on?"""
    L = []
    L.append("Update disc: %s" % disc.volume_id)
    if disc.created:
        c = disc.created
        if len(c) >= 8:
            L.append("  created %s-%s-%s" % (c[0:4], c[4:6], c[6:8]))
    rels = disc.releases()
    L.append("  %d release variant%s" % (len(rels), "" if len(rels) == 1 else "s"))
    L.append("")

    defs = disc.definitions()
    if defs:
        L.append("Releases")
        rows = []
        for path in sorted(defs):
            d = defs[path]
            name = path.rsplit("/", 1)[-1]
            units = [u["id"] for u in d["units"]]
            rows.append((name, d["systemreleaseid"] or "-", d["discid"] or "-", units))
        w = max(len(r[0]) for r in rows)
        for name, srid, did, units in rows:
            L.append("  %-*s  %-34s %s" % (w, name, srid, did))
            if units:
                L.append("  %-*s  units: %s" % (w, "", ", ".join(units)))
        L.append("")

        # which unit IDs can take which releases -- the dispatch table, inverted
        by_unit = {}
        for path, d in defs.items():
            rel = path.rsplit("/", 1)[-1].replace(".def", "").replace(".DEF", "")
            for u in d["units"]:
                by_unit.setdefault(u["id"], set()).add(rel)
        if by_unit:
            L.append("Supported unit IDs")
            for uid in sorted(by_unit):
                gen = "MOPF" if uid[4:6] == "02" else "pre-facelift"
                L.append("  %-14s %-14s %s"
                         % (uid, gen, ", ".join(sorted(by_unit[uid]))))
            L.append("")

    layout = disc.flash_layout()
    if layout:
        L.append("Flash layout")
        for addr, desc, names in layout:
            L.append("  0x%08X  %-34s %s"
                     % (addr, desc, ", ".join(names[:3])
                        + (" +%d more" % (len(names) - 3) if len(names) > 3 else "")))
        L.append("")

    sigs = disc.signatures()
    if sigs:
        kinds = {}
        for _n, algo, nbytes in sigs:
            key = "%s-%d" % (algo, nbytes * 8) if nbytes else algo
            kinds[key] = kinds.get(key, 0) + 1
        L.append("Signatures")
        for k in sorted(kinds):
            L.append("  %-14s %d" % (k, kinds[k]))
        if any(a == "RSA" for _n, a, _b in sigs):
            L.append("  -- modules are RSA-signed; a modified module cannot be")
            L.append("     signed without Porsche's private key.")
    return "\n".join(L)
