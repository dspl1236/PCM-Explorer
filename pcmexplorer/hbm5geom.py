"""Screen geometry from HBM5 ``.mmi`` files -- real x/y/w/h for HMI elements.

The container reader in :mod:`hbm5` gets you strings.  This gets you the
screens: a schema-driven record decoder that resolves the drawable tree to
boxes on an 800x480 display.

## How a record is laid out

Every class in the file has a schema descriptor::

    u8  pad ; u8 nBases ; u16 nFields
    u32 classCUID
    u32 baseIndices        (one byte each, low byte first)
    u32 moreBases
    nFields x { u32 fieldCUID ; u32 typeCode }

A record is then its inherited base-class fields first, then its own, with no
prefix and with trailing fields omittable.  ``ptr + 16 + 8*nFields`` lands
exactly on the next descriptor for all 167 descriptors across the corpus.

Class and field names are recoverable rather than guessed: the CUID is a hash
of the name, computed by the routine at VA ``0x0889cad8`` in PCM3Reload, with
alternating rounds.  A class hashes from seed 0; a field hashes its own name
(leading dot included) seeded with the class's CUID.

## The varint trap

Two schemes coexist, and mixing them up is what once made these records look
like they carried a variable-length prefix.  **CPoint components** treat
``0x40-0x7f`` as the two-byte lead; **everything else** -- lengths, counts,
CSize, ordinary scalars -- uses ``0x80``.  Both share ``0xc0-0xef`` for three
bytes and an ``0xf0`` escape carrying four raw big-endian bytes.  On
single-field CPoint records, where nothing can be omitted to hide an error,
the CPoint scheme parses 473/473 exactly against 219/473 for the other.

## Position by reference

A drawable carries both an inline ``mPosition`` and an ``mPositionResID``.
When the ResID is non-zero the inline value is ``(0,0)`` and carries nothing --
the real position is behind the reference, and roughly one drawable in eight
is like this.  The reference usually points at a *descriptor* rather than a
payload, holding two CPoint variants under kinds 21 and 22 which share a y and
differ only in x: a left/right anchor pair.

This is worth spelling out because it is invisible to the obvious checks.
Exact-closure does not notice -- the field is consumed either way.  Box
plausibility does not notice -- ``(0,0)`` is on screen.  An earlier pass
concluded the indirection was used by three records out of ten thousand; it is
used by 1,256, and every one of them silently rendered in the top-left corner.
Those two oracles validate *parsing*, not *resolution*.
"""
import struct

from .hbm5 import Hbm5File

DISPLAY_W, DISPLAY_H = 800, 480

CUID_CDRAWOBJECT = 0x8E2F8293
CUID_CPOINT_RES = 0x2267FE33
CUID_CSIZE_RES = 0x0AA3495D

SCALAR1 = {0x81, 0x82, 0x84, 0x88, 0x89}
PAIR = {0x8C, 0x8D}                 # 0x8c = CPoint, 0x8d = CSize
BLOB = {0xA2, 0xA3}
ARRAY = 0xA1

# Kinds seen on a position descriptor: a left/right anchor pair.
ANCHOR_KINDS = (21, 22)

# A field's CUID is hashed with its *class* CUID as the seed, so the same field
# name has a different hash in every class that declares it. These two groups
# therefore coexist rather than conflict: the drawable hierarchy below, and the
# resource classes it points at.
CLASS_NAMES = {
    # drawables
    0x8E2F8293: "CDrawObject",          0x90AA9E70: "CGUIElement",
    0xA806B8EE: "CAligningObject",      0xA3C213B8: "CBitmapObject",
    0x71B5DB06: "CTextBase",            0x3EBA6425: "CTextObject",
    0xBA066794: "CFormattedTextObject", 0x77F80FB9: "CTextArea",
    0x2267FE33: "<CPoint>",             0x0AA3495D: "<CSize>",
    # resources
    0xDA63396B: "CDisplay",             0x92AE6BAD: "CColor",
    0x57E931C3: "CAlignment",           0xC6D7DD90: "CBitmap",
    0xE0D04503: "CFontFormat",          0x0B60D4CA: "CFontFile",
    0x5C8F9492: "CResourceTable",       0x25B3AC9A: "CHBString",
    0x60436E39: "CFont",                0x8AC3362F: "CFontReferences",
    0x2F71594A: "CFontTextCombination", 0xE161265A: "CPlacementFontCombination",
    0x3DC09477: "CPlacementCombination", 0xB9106E94: "CBitmapColorCombination",
    0xD7CE702B: "CColors",              0xD8492763: "CColorMasks",
    0x1F53E9FF: "CColorReferences",     0x833EFC17: "PositionResource",
    0x8425447B: "AlignmentResource",
}

FIELD_NAMES = {
    # on the drawable classes
    0x0A2B4963: "mParentID",       0x7F9A5557: "mDrawOrder",
    0x7A1CC624: "mPositionResID",  0xF790B5F2: "mSizeResID",
    0xEBD70139: "mPosition",       0x45CF85C4: "mSize",
    0x5FFAEE1E: "m_childrenIDs",   0x1A2BEA44: "m_resourceTableID",
    0x5A894D17: "mAlignmentResID", 0x79CC0437: "mAlignment",
    0x94EA844C: "mBmpResID",       0xF9F3B843: "mColorsResID",
    0xD7C9A631: "mTextResID",      0xB397877F: "mFontResID",
    0x621C612D: "mFontResID",      0x9960974E: "point",
    0x26693065: "size",
    # on CFont / CFontFormat / CFontFile -- the chain a label's size comes from
    0xAFB73BC7: "mFontFormatResID", 0x650043A0: "mColorsResID",
    0x84477324: "mFontFileResID",   0xA2E58771: "mHeight",
    0xD080B74E: "mWidth",           0xEE757E54: "mEngine",
    0x92DD65C3: "mHorizontalDPI",   0x2E858ACE: "mVerticalDPI",
    0x70160B06: "mFileName",        0xFFF5F83A: "mOutlineWidth",
    0x1EB8A8EF: "mFontResID",       0x5F028ED0: "mFontResID",
    0x5F90203A: "mFonts",           0x5C8C1726: "mString",
    # on CBitmap
    0xEC9570D7: "mMode",            0x93DF2ACC: "mBitsPerPixel",
    0x78BDA50A: "mWidth",           0xE70B5DA7: "mHeight",
    0x0800845E: "mPaletteCount",    0x9A979BD7: "mPixelData",
    # on the resource-side placement classes
    0x8F1AC150: "mPoint",           0xC0654178: "mSize",
    0x10432267: "mPosition",        0x2B5B2DE3: "mSize",
    0xE4E168FB: "mPositionResID",   0xD967BDC7: "mSizeResID",
    0xE46C3005: "mBmpResID",        0x98165484: "mTextResID",
    0xA433A1BF: "mColorsResID",     0xF1BF6A3D: "mParentID",
    0xA3EFFD51: "m_childrenIDs",    0xD0B3623F: "m_resourceTableID",
    0x516D5D62: "mDrawOrder",
}


class Truncated(Exception):
    """A record ran off the end of its block -- expected for omitted tails."""


def _varint(d, o, point=False):
    """Read a varint.  ``point=True`` selects the CPoint-component scheme."""
    if o >= len(d):
        raise Truncated()
    b = d[o]
    if b >= 0xF0:
        if o + 4 >= len(d):
            raise Truncated()
        return int.from_bytes(d[o + 1:o + 5], "big"), o + 5
    if 0xC0 <= b < 0xF0:
        if o + 2 >= len(d):
            raise Truncated()
        return ((b & 0x3F) << 16) | (d[o + 1] << 8) | d[o + 2], o + 3
    lead = 0x40 if point else 0x80
    if b < lead:
        return b, o + 1
    if o + 1 >= len(d):
        raise Truncated()
    return ((b & 0x3F) << 8) | d[o + 1], o + 2


class Schema(object):
    """The file's class table: field lists, with inheritance flattened."""

    def __init__(self, m):
        d = m.data
        self.ptr = [struct.unpack_from("<I", d, m.schema_off + 4 * i)[0]
                    for i in range(m.n_schema)]
        self.cls = []
        for i, p in enumerate(self.ptr):
            nbase = d[p + 1]
            nf = struct.unpack_from("<H", d, p + 2)[0]
            cuid, bx, by = struct.unpack_from("<III", d, p + 4)
            bases = list(struct.pack("<II", bx, by))[:nbase]
            fields = [struct.unpack_from("<II", d, p + 16 + 8 * j)
                      for j in range(nf)]
            self.cls.append({"idx": i, "cuid": cuid, "bases": bases,
                             "fields": fields})
        self._flat = {}

    def flat(self, i, seen=None):
        """[(owner_idx, fieldCUID, typeCode)] -- bases first, then own."""
        if i in self._flat:
            return self._flat[i]
        seen = seen or set()
        if i in seen or i >= len(self.cls):
            return []
        seen = seen | {i}
        out = []
        for b in self.cls[i]["bases"]:
            out.extend(self.flat(b, seen))
        for fc, ft in self.cls[i]["fields"]:
            out.append((i, fc, ft))
        self._flat[i] = out
        return out


class Screens(object):
    """Decoded drawables from one .mmi file."""

    def __init__(self, path_or_file):
        self.m = (path_or_file if isinstance(path_or_file, Hbm5File)
                  else Hbm5File(path_or_file))
        self.d = self.m.data
        self.sch = Schema(self.m)
        sizes = self.m.block_sizes()
        self.ent = {}
        for rid, off, b0, ci, _b2, comp in self.m.directory():
            self.ent[rid] = (off, b0, ci, comp, sizes.get(rid, 0))
        self.strs = self.m.strings()
        self.desc = self.m.descriptors()
        self._cache = {}
        self._kids = None
        self._roots = []

    # -- record decoding --
    def _field(self, o, ftype, depth, end):
        t0 = ftype & 0xFF
        if t0 < 0x80:
            if depth > 6:
                raise Truncated()
            return self._record(o, t0, depth + 1, None)
        if t0 in PAIR:
            pt = (t0 == 0x8C)                  # only CPoint uses the 0x40 lead
            a, o = _varint(self.d, o, pt)
            b, o = _varint(self.d, o, pt)
            return (a, b), o
        if t0 in BLOB:
            n, o = _varint(self.d, o)
            if o + n > len(self.d):
                raise Truncated()
            return self.d[o:o + n], o + n
        if t0 == ARRAY:
            ln, o = _varint(self.d, o)
            stop = o + ln
            if stop > len(self.d):
                raise Truncated()
            cnt, o = _varint(self.d, o)
            el = (ftype >> 16) & 0xFF
            vals = []
            for _ in range(cnt):
                if o >= stop:
                    break
                v, o = self._field(o, el, depth + 1, stop)
                vals.append(v)
            return vals, stop
        if t0 in SCALAR1:
            return _varint(self.d, o)
        raise Truncated()

    def _record(self, o, ci, depth=0, end=None):
        out = []
        for (_k, fc, ft) in self.sch.flat(ci):
            if end is not None and o >= end:
                break
            if o >= len(self.d):
                break
            try:
                v, o = self._field(o, ft, depth, end)
            except Truncated:
                break                          # trailing fields may be omitted
            out.append((fc, v))
        return out, o

    def record(self, rid):
        """Decoded payload record as a dict, or None for descriptors/compressed."""
        if rid in self._cache:
            return self._cache[rid]
        e = self.ent.get(rid)
        if e is None:
            return None
        off, b0, ci, comp, n = e
        if (b0 & 1) or comp or n <= 0:
            self._cache[rid] = None
            return None
        vals, endoff = self._record(off, ci, 0, off + n)
        r = {"__cuid": self.sch.cls[ci]["cuid"],
             "__cls": CLASS_NAMES.get(self.sch.cls[ci]["cuid"]),
             "__exact": endoff == off + n}
        for fc, v in vals:
            r[FIELD_NAMES.get(fc, "f%08x" % fc)] = v
        self._cache[rid] = r
        return r

    # -- resolution --
    def _typed(self, rid, cuid, key, anchor=0):
        """Resolve a ResID to a CPoint/CSize value, following a descriptor.

        The direct case is a payload of the right class.  The indirect case --
        about one drawable in eight -- points at a descriptor holding anchor
        variants, and must be followed rather than treated as absent.
        """
        r = self.record(rid)
        if r is not None:
            return r.get(key) if r.get("__cuid") == cuid else None
        pairs = self.desc.get(rid)
        if not pairs:
            return None
        want = ANCHOR_KINDS[anchor] if anchor < len(ANCHOR_KINDS) else None
        chosen = None
        for kind, pid in pairs:
            sub = self.record(pid)
            if sub is None or sub.get("__cuid") != cuid:
                continue
            val = sub.get(key)
            if val is None:
                continue
            if kind == want:
                return val
            if chosen is None:
                chosen = val
        return chosen

    def position(self, rid, anchor=0):
        return self._typed(rid, CUID_CPOINT_RES, "point", anchor)

    def size(self, rid, anchor=0):
        return self._typed(rid, CUID_CSIZE_RES, "size", anchor)

    def box(self, rid, anchor=0):
        """(x, y, w, h, source) local to the parent, or None if not a drawable.

        ``source`` records where the numbers came from: ``P``/``S`` for a
        resolved position/size reference, ``p``/``s`` for the inline value.
        """
        r = self.record(rid)
        if not r or "mPosition" not in r:
            return None
        pos, siz, src = r.get("mPosition") or (0, 0), r.get("mSize") or (0, 0), ""
        pr, sr = r.get("mPositionResID") or 0, r.get("mSizeResID") or 0
        if pr:
            p = self.position(pr, anchor)
            pos, src = (p, src + "P") if p else (pos, src + "p")
        if sr:
            s = self.size(sr, anchor)
            siz, src = (s, src + "S") if s else (siz, src + "s")
        return (pos[0], pos[1], siz[0], siz[1], src)

    def text(self, rid):
        """Text behind a ResID, following a descriptor's language variants."""
        if rid in self.strs:
            return self.strs[rid]
        for _kind, pid in self.desc.get(rid, ()):
            if pid in self.strs:
                return self.strs[pid]
        return None

    def label(self, rid):
        r = self.record(rid)
        t = r.get("mTextResID") if r else None
        return self.text(t) if t else None

    # -- tree --
    def drawables(self):
        """Every record id that decodes as a drawable."""
        out = []
        for rid, (_o, b0, ci, comp, n) in self.ent.items():
            if (b0 & 1) or comp or n <= 0:
                continue
            owners = {k for k, _f, _t in self.sch.flat(ci)}
            if any(self.sch.cls[k]["cuid"] == CUID_CDRAWOBJECT for k in owners):
                out.append(rid)
        return sorted(out)

    def _tree(self):
        """parent -> [children], built from ``mParentID``.

        Not from ``m_childrenIDs``.  That field is an array and does not decode
        reliably -- it yields ids of 0 and 8, nodes listing themselves, 28,951
        "children" for one element and 48,312 parents for another.  The parent
        pointer is a single scalar and comes out clean: 10,259 of 10,268 resolve
        to a real drawable, nine sit at top level, none is self-parented, and the
        busiest node has 67 children.

        The resulting roots are exactly the screen furniture you would expect --
        an 800x80 bar at the top, an 800x364 content area at y=59, an 800x58 bar
        at y=422 -- which is the check that this is the real linkage.
        """
        if self._kids is not None:
            return self._kids
        drawables = set(self.drawables())
        kids, roots = {}, []
        for rid in drawables:
            rec = self.record(rid)
            p = rec.get("mParentID") if rec else None
            if isinstance(p, int) and p != rid and p in drawables:
                kids.setdefault(p, []).append(rid)
            else:
                roots.append(rid)
        for v in kids.values():
            v.sort()
        self._kids, self._roots = kids, sorted(roots)
        return self._kids

    def children(self, rid):
        return list(self._tree().get(rid, ()))

    def roots(self):
        """Drawables with no resolvable parent -- the top of each screen."""
        self._tree()
        return list(self._roots)

    def subtree(self, rid, limit=30000):
        """Every drawable reachable from ``rid``, itself included.

        Guarded rather than recursive: the graph shares nodes heavily and has
        cycles, so a plain walk does not terminate on its own.
        """
        seen, stack = set(), [rid]
        while stack and len(seen) < limit:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(self.children(x))
        return seen

    def screens(self, min_elements=4, limit=400):
        """[(root_id, element_count)] -- screen-sized subtrees, biggest first.

        Roots first, then any child large enough to be a screen in its own
        right, so a full-display container and the panels inside it can both
        be inspected.
        """
        self._tree()
        out = []
        for r in self.roots():
            out.append((r, len(self.subtree(r))))
        for r in self.roots():
            for k in self.children(r):
                n = len(self.subtree(k))
                if n >= min_elements:
                    out.append((k, n))
        seen, uniq = set(), []
        for rid, n in sorted(out, key=lambda t: -t[1]):
            if rid in seen:
                continue
            seen.add(rid)
            uniq.append((rid, n))
            if len(uniq) >= limit:
                break
        return uniq

    def text_height(self, rid, box=None):
        """Height to draw a text element at.

        Text records store a width -- the wrap width -- and a height of zero,
        because the real height comes from the font and is resolved when the
        unit lays the screen out. Every labelled element in a screen therefore
        has ``h == 0``, and a renderer that filters on height draws no text at
        all. Substituting the font height is what the unit effectively does.
        """
        b = box or self.box(rid)
        if b and b[3] > 0:
            return b[3]
        return self.font_px(rid) or 0

    def font_px(self, rid):
        """Pixel height of the font an element draws in, or None.

        The chain is element -> mFontResID -> CFont -> mFontFormatResID ->
        CFontFormat.mHeight, and CFontFormat also names the .ttf via CFontFile,
        so the real typeface is knowable too -- the files are in IFS2.  Using the
        recorded size rather than one fixed size is what stops every screen
        looking alike: a heading and a list row differ by more than their text.
        """
        r = self.record(rid)
        if not r:
            return None
        fid = r.get("mFontResID") or 0
        if not fid:
            return None
        f = self.record(fid)
        if f is None:                       # a descriptor: follow its variants
            for _kind, pid in self.desc.get(fid, ()):
                f = self.record(pid)
                if f is not None:
                    break
        if not f:
            return None
        ffid = f.get("mFontFormatResID") or 0
        fmt = self.record(ffid) if ffid else None
        if fmt is None and ffid:
            for _kind, pid in self.desc.get(ffid, ()):
                fmt = self.record(pid)
                if fmt is not None:
                    break
        h = (fmt or {}).get("mHeight")
        if isinstance(h, int) and 4 <= h <= 96:
            return h
        return None

    def font_file(self, rid):
        """The .ttf an element's font resolves to, if the chain is complete."""
        r = self.record(rid)
        fid = (r or {}).get("mFontResID") or 0
        f = self.record(fid) if fid else None
        ffid = (f or {}).get("mFontFormatResID") or 0
        fmt = self.record(ffid) if ffid else None
        fileid = (fmt or {}).get("mFontFileResID") or 0
        ff = self.record(fileid) if fileid else None
        name = (ff or {}).get("mFileName")
        if isinstance(name, bytes):
            try:
                return name.decode("utf-8", "replace").strip("\x00")
            except Exception:
                return None
        return name if isinstance(name, str) else None

    def name_of(self, rid, scan=600):
        """A human name for a screen: the first text found inside it.

        Roots are unlabelled containers, so a screen has to be named by its
        contents.  Breadth-first, because the heading is usually nearer the top
        of the tree than the button captions.
        """
        queue, seen, n = [rid], set(), 0
        while queue and n < scan:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            n += 1
            t = self.label(cur)
            if t:
                # collapse newlines: HMI strings wrap, list rows do not
                t = " ".join(t.split())
                # skip format-only strings like "%1" -- they name nothing
                if len(t) > 1 and not t.startswith("%"):
                    return t
            queue.extend(self.children(cur))
        return None

    def to_svg(self, root, anchor=0, labels=True):
        """One screen as SVG -- openable in a browser or a vector editor.

        The point is not the picture. It is that a custom app can be laid out
        on top of the real OEM metrics instead of guessed ones: drop this into
        Inkscape, draw against it, and list rows land at 664x69 and buttons at
        66x57 because that is what the unit actually uses.

        Boxes whose position came from a resolved reference are marked, so it
        is visible which numbers were read and which were defaulted.
        """
        import xml.sax.saxutils as sx

        seen, stack, items = set(), [root], []
        while stack and len(seen) < 20000:
            rid = stack.pop()
            if rid in seen:
                continue
            seen.add(rid)
            b = self.box(rid, anchor)
            if b:
                items.append((rid, b, self.label(rid), self.font_px(rid)))
            stack.extend(self.children(rid))
        # biggest first so children paint over their containers
        items.sort(key=lambda t: -(t[1][2] * t[1][3]))

        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
               'viewBox="0 0 %d %d">' % (DISPLAY_W, DISPLAY_H,
                                         DISPLAY_W, DISPLAY_H),
               '<desc>PCM 3.1 screen %d, %d elements. Geometry is decoded; '
               'bitmaps are not in the .mmi files and are not drawn.</desc>'
               % (root, len(items)),
               '<rect width="%d" height="%d" fill="#101014"/>'
               % (DISPLAY_W, DISPLAY_H)]
        for rid, (x, y, w, h, src), lab, px in items:
            if w <= 0:
                continue
            # a text element stores h == 0; its height is the font's
            hh = h if h > 0 else (px or 0)
            if hh <= 0:
                continue
            stroke = "#c8a44e" if "P" in src else "#4a4a58"
            out.append('<g id="e%d">' % rid)
            if h > 0:
                out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" '
                           'stroke="%s" stroke-width="1"/>' % (x, y, w, h, stroke))
            if labels and lab and w > 12:
                size = max(6, min(px or 14, hh))
                out.append('<text x="%d" y="%d" fill="#e8e8ed" font-size="%d" '
                           'font-family="Segoe UI, DejaVu Sans, sans-serif" '
                           'text-anchor="middle" dominant-baseline="middle">%s</text>'
                           % (x + w // 2, y + hh // 2, size,
                              sx.escape(lab[:48])))
            out.append('</g>')
        out.append('</svg>')
        return "\n".join(out)

    def stats(self):
        """How well resolution is doing -- the number to watch after a change."""
        n = indirect = resolved = onscreen = 0
        for rid in self.drawables():
            b = self.box(rid)
            if not b:
                continue
            n += 1
            if "P" in b[4] or "p" in b[4]:
                indirect += 1
                if "P" in b[4]:
                    resolved += 1
            if 0 <= b[0] <= DISPLAY_W and 0 <= b[1] <= DISPLAY_H \
               and b[2] <= DISPLAY_W and b[3] <= DISPLAY_H:
                onscreen += 1
        return {"drawables": n, "position_by_reference": indirect,
                "references_resolved": resolved, "boxes_on_screen": onscreen}
