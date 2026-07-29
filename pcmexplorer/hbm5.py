"""HBM5 ``.mmi`` files -- the Harman MoCCA HMI definition.

The PCM 3.1 user interface is not compiled into the HMI binary.  It is data:
34 ``.mmi`` files sitting beside ``PCM3Reload`` in the IFS2 firmware image,
holding the screens, the element tree and every string the unit can display, in
every language it ships.  ``cayenne.mmi`` is one car's screens; ``en_us.mmi``
and ``ru_ru.mmi`` are the same interface in different words.

That makes an inventory possible without emulating anything: what screens exist,
what they say, and in which languages -- including strings no menu on the car
ever reaches.

## Container

A serialised C++ object graph, little-endian, with no compression at the
container level::

    0x00  'HBM5'
    0x04  u16   format version, 0x0100
    0x06  u16   nSchema        class-descriptor count
    0x08  u32   schemaOff      == 0x28 + 12*nDir
    0x0c  u32   rootOff        end of the schema table
    0x10  u32   0x28           constant
    0x14  u32   fileSize       exact
    0x18  u32   nDir
    0x1c  u32   0x28           constant
    0x20  u32   structSize     poolOff = structSize + 0x28
    0x24  u32   -- opaque (1, except moccaV2Target: v2=2, v3=5, v4=6)

    directory: nDir x 12 bytes at 0x28, id strictly ascending
      +0 u32 id
      +4 u32 absolute offset
      +8 u8  bit0 = 1 -> descriptor record, 0 -> payload
             bits 4-7 = k, allocation unit 16 << k
      +9 u8  class index into the schema table (file-local, not a global enum)
      +10 u8 ceil(blockBytes / (16 << k)) mod 256
      +11 u8 1 = payload is compressed

Block size is the distance to the next *distinct* directory offset -- offsets
are shared, because identical payloads are deduplicated.

A descriptor record is ``u32 a; u32 count; count x (u32 kind, u32 payloadId)``.
The ``kind`` is a language or variant selector, which is how one screen carries
nine translations: nine pairs under one descriptor.

A plain payload is a varint length followed by UTF-8 text.  The varint is
UTF-8-shaped: one byte below 0x80; ``(b & 0x3f) << 8 | next`` when the top bits
are ``10``; ``(b & 0x3f) << 16 | next << 8 | next2`` when they are ``11``.

## What this reader does not do

Payloads whose compressed flag is set are left alone.  The codec is Ross
Williams' LZRW -- PCM3Reload names it in its own strings
(``HBLZRW2Compression.cpp``, ``HBLZRW3ACompression.cpp``) -- and it is not any
stock LZ; zlib, deflate and LZO1X all reject it.  That blob holds the screen
graphics, so images stay out of reach until someone reverse-engineers the
decompressor out of the HMI binary.  Roughly 795 of ~44,800 strings are behind
it too, which is why a few keys resolve in eight languages rather than nine.

Layout geometry is likewise not decoded.  Plausible x/y/w/h values are present
and the shapes look right -- an 800x364 content area, 664x69 list rows -- but
records carry a variable-length prefix, so a correct box and a misaligned one
are indistinguishable.  Guessing would produce a convincing picture that cannot
be checked, which is worse than showing nothing.
"""
import struct
from collections import defaultdict

MAGIC = b"HBM5"
HDR_SIZE = 0x28
DIR_ENTRY = 12

# kind -> language, read off the cluster tables where all ten appear together
LANGUAGES = {
    1: "de", 2: "en_gb", 3: "en_us", 4: "fr", 5: "es",
    6: "it", 7: "nl", 8: "ru", 10: "zh", 11: "ar",
}


def looks_like_hbm5(path):
    """Cheap probe -- is this a MoCCA HMI definition?"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except Exception:
        return False


def read_varint(data, off, point=False):
    """(value, next_offset).  UTF-8-shaped, 1 to 5 bytes.

    Two schemes coexist in these files and they disagree about one range.
    Lengths, counts, CSize and ordinary scalars use ``0x80`` as the two-byte
    lead (``point=False``, the default).  The components of a CPoint instead
    treat ``0x40-0x7f`` as the two-byte lead (``point=True``) -- reading a
    CPoint with the wrong scheme is what made record geometry look like it
    carried a variable-length prefix.

    Both share ``0xc0-0xef`` for the three-byte form and an ``0xf0`` escape
    carrying four raw big-endian bytes.
    """
    b = data[off]
    if b == 0xF0:                       # escape: four raw big-endian bytes
        return (int.from_bytes(data[off + 1:off + 5], "big"), off + 5)
    if 0xC0 <= b <= 0xEF:
        return ((b & 0x3F) << 16) | (data[off + 1] << 8) | data[off + 2], off + 3
    if point:
        if 0x40 <= b <= 0x7F:
            return ((b & 0x3F) << 8) | data[off + 1], off + 2
        if b < 0x40:
            return b, off + 1
        return ((b & 0x3F) << 8) | data[off + 1], off + 2
    if b < 0x80:
        return b, off + 1
    return ((b & 0x3F) << 8) | data[off + 1], off + 2


class Hbm5File(object):
    """One .mmi file, read-only."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        if d[:4] != MAGIC:
            raise ValueError("not an HBM5 file")
        (self.version, self.n_schema) = struct.unpack_from("<HH", d, 0x04)
        (self.schema_off, self.root_off, _c1, self.file_size,
         self.n_dir, _c2, self.struct_size, self.tail) = \
            struct.unpack_from("<8I", d, 0x08)
        self.pool_off = self.struct_size + HDR_SIZE
        self._dir = None
        self._blocks = None

    # -- container --
    def directory(self):
        """[(id, offset, kind_byte, class_idx, size_units, compressed)]."""
        if self._dir is not None:
            return self._dir
        d, out = self.data, []
        for i in range(self.n_dir):
            o = HDR_SIZE + i * DIR_ENTRY
            if o + DIR_ENTRY > len(d):
                break
            rid, off = struct.unpack_from("<II", d, o)
            b0, b1, b2, b3 = d[o + 8], d[o + 9], d[o + 10], d[o + 11]
            out.append((rid, off, b0, b1, b2, b3))
        self._dir = out
        return out

    def block_sizes(self):
        """id -> byte length, from the distance to the next distinct offset."""
        if self._blocks is not None:
            return self._blocks
        entries = self.directory()
        offs = sorted({e[1] for e in entries})
        nxt = {}
        for i, o in enumerate(offs):
            nxt[o] = offs[i + 1] if i + 1 < len(offs) else self.file_size
        self._blocks = {e[0]: max(0, nxt[e[1]] - e[1]) for e in entries}
        return self._blocks

    def verify(self):
        """Check the container's own accounting -- the format's self-test."""
        out = []
        d = self.data
        out.append(("header size field", self.file_size == len(d),
                    "%d vs %d" % (self.file_size, len(d))))
        out.append(("schemaOff == 0x28 + 12*nDir",
                    self.schema_off == HDR_SIZE + DIR_ENTRY * self.n_dir,
                    "0x%X" % self.schema_off))
        ids = [e[0] for e in self.directory()]
        out.append(("directory ids ascending", ids == sorted(ids),
                    "%d entries" % len(ids)))
        sizes = self.block_sizes()
        ok = bad = 0
        for rid, _off, b0, _b1, b2, _b3 in self.directory():
            unit = 16 << (b0 >> 4)
            want = ((sizes[rid] + unit - 1) // unit) & 0xFF
            if want == b2:
                ok += 1
            else:
                bad += 1
        out.append(("block size units match", bad == 0, "%d ok / %d bad" % (ok, bad)))
        return out

    # -- records --
    def descriptors(self):
        """id -> [(kind, payload_id)].  A descriptor gathers a key's variants."""
        d, out = self.data, {}
        sizes = self.block_sizes()
        for rid, off, b0, _b1, _b2, _b3 in self.directory():
            if not (b0 & 1):
                continue
            n = sizes.get(rid, 0)
            if n < 8 or off + 8 > len(d):
                continue
            _a, count = struct.unpack_from("<II", d, off)
            if count <= 0 or 8 + 8 * count > n or off + 8 + 8 * count > len(d):
                continue
            pairs = []
            for i in range(count):
                k, pid = struct.unpack_from("<II", d, off + 8 + 8 * i)
                pairs.append((k, pid))
            out[rid] = pairs
        return out

    def payload(self, rid):
        """Raw bytes of a payload block, or None."""
        for e in self.directory():
            if e[0] == rid:
                n = self.block_sizes().get(rid, 0)
                return self.data[e[1]:e[1] + n]
        return None

    def strings(self):
        """id -> text for every uncompressed payload that is valid UTF-8.

        A payload is a varint length then the text, and the two must land exactly
        on the block end -- that exactness is what makes the decode trustworthy
        rather than a guess, so blocks that do not satisfy it are skipped.
        """
        d, out = self.data, {}
        sizes = self.block_sizes()
        for rid, off, b0, _b1, _b2, comp in self.directory():
            if (b0 & 1) or comp:
                continue                       # descriptor, or compressed
            n = sizes.get(rid, 0)
            if n <= 0 or off + n > len(d):
                continue
            try:
                ln, p = read_varint(d, off)
            except IndexError:
                continue
            if ln < 0 or (p - off) + ln > n:
                continue
            raw = d[p:p + ln]
            try:
                out[rid] = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
        return out

    def compressed_ids(self):
        return [e[0] for e in self.directory() if e[5] and not (e[2] & 1)]

    def translations(self):
        """[(descriptor_id, {lang: text})] for every multi-variant key."""
        strs = self.strings()
        out = []
        for did, pairs in sorted(self.descriptors().items()):
            row = {}
            for kind, pid in pairs:
                txt = strs.get(pid)
                if txt is not None:
                    row[LANGUAGES.get(kind, "k%d" % kind)] = txt
            if row:
                out.append((did, row))
        return out

    def describe(self):
        dirs = self.directory()
        nd = sum(1 for e in dirs if e[2] & 1)
        npay = len(dirs) - nd
        ncomp = sum(1 for e in dirs if e[5] and not (e[2] & 1))
        return ("HBM5 v%04x, %d records (%d descriptors, %d payloads, "
                "%d compressed), %d classes, %.1f KB"
                % (self.version, len(dirs), nd, npay, ncomp, self.n_schema,
                   len(self.data) / 1024.0))


def summarise_hbm5(m):
    """What is this HMI file, and what is in it?"""
    L = []
    name = m.path.replace("\\", "/").rsplit("/", 1)[-1]
    L.append("HMI definition: %s" % name)
    L.append("  %s" % m.describe())
    L.append("")

    for label, ok, detail in m.verify():
        L.append("  [%s] %-30s %s" % ("PASS" if ok else "FAIL", label, detail))
    L.append("")

    strs = m.strings()
    comp = m.compressed_ids()
    L.append("Strings: %d readable" % len(strs))
    if comp:
        L.append("  %d payloads are compressed (LZRW, not decoded) and not counted"
                 % len(comp))
    L.append("")

    rows = m.translations()
    multi = [r for r in rows if len(r[1]) > 1]
    if multi:
        langs = defaultdict(int)
        for _did, row in multi:
            for k in row:
                langs[k] += 1
        L.append("Translated keys: %d with more than one language" % len(multi))
        L.append("  " + ", ".join("%s %d" % (k, langs[k]) for k in sorted(langs)))
        L.append("")
        L.append("Sample")
        for did, row in multi[:3]:
            L.append("  key %d" % did)
            for k in sorted(row)[:6]:
                L.append("      %-6s %s" % (k, row[k]))
        L.append("")

    sample = [t for t in strs.values() if 3 < len(t) < 60][:12]
    if sample and not multi:
        L.append("Sample strings")
        for s in sample:
            L.append("  %s" % s)
    return "\n".join(L).rstrip()
