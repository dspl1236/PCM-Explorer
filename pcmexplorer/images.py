"""Recognise and display the images on a head unit.

Two things make this less obvious than it sounds.

**Extensions lie.** The custom bootscreens on a PCM drive are called
``CustomBootscreen_099.bin`` and are JPEGs -- ``ff d8 ff e0 … JFIF``. Anything
here identifies by magic bytes and ignores the filename, so a ``.bin`` that is
really a picture is shown as one.

**No dependencies, by design.** Tk 8.6 decodes PNG and GIF itself, so those
always work. BMP is decoded here in pure Python, because it is simple and
appears in the firmware. JPEG needs a real decoder: if Pillow happens to be
installed it is used, and if not the image is still *identified* with its true
dimensions rather than silently failing. Nothing is required to be installed
for the tool to work.

Raw framebuffers are supported too, since the display pipeline works in RGB565
and a dump of it has no header at all -- only a size that matches width x
height x 2.
"""
import struct

DISPLAY_SIZES = ((800, 480), (400, 240), (640, 480), (320, 240))


def _u16le(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _u32le(d, o):
    return struct.unpack_from("<I", d, o)[0]


def identify(data):
    """(kind, width, height) by magic, or None.  Width/height may be 0."""
    if not data or len(data) < 8:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            return ("PNG", struct.unpack_from(">I", data, 16)[0],
                    struct.unpack_from(">I", data, 20)[0])
        return ("PNG", 0, 0)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ("GIF", _u16le(data, 6), _u16le(data, 8))
    if data[:2] == b"\xff\xd8":
        return ("JPEG",) + _jpeg_size(data)
    if data[:2] == b"BM" and len(data) >= 26:
        return ("BMP", _u32le(data, 18) & 0xFFFFFFFF,
                abs(struct.unpack_from("<i", data, 22)[0]))
    return None


def _jpeg_size(data):
    """Walk JPEG markers to the frame header.  Cheap, and needs no decoder."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack_from(">H", data, i + 2)[0]
        # SOF0..SOF15, excluding the non-frame markers in that range
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = struct.unpack_from(">H", data, i + 5)[0]
            w = struct.unpack_from(">H", data, i + 7)[0]
            return (w, h)
        i += 2 + seglen
    return (0, 0)


def raw_candidates(nbytes):
    """Display sizes whose RGB565 framebuffer is exactly ``nbytes``."""
    return [(w, h) for (w, h) in DISPLAY_SIZES if w * h * 2 == nbytes]


def _ppm_from_rgb(w, h, rows):
    """P6 PPM -- the one raw format Tk's photo image reads directly."""
    out = bytearray(b"P6\n%d %d\n255\n" % (w, h))
    for r in rows:
        out += r
    return bytes(out)


def bmp_to_ppm(data):
    """Uncompressed 24/32-bit BMP -> PPM, or None if it is some other flavour."""
    if len(data) < 54 or data[:2] != b"BM":
        return None
    off = _u32le(data, 10)
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    bpp = _u16le(data, 28)
    comp = _u32le(data, 30)
    if comp != 0 or bpp not in (24, 32) or w <= 0 or w > 8192 or abs(h) > 8192:
        return None
    bottom_up = h > 0
    h = abs(h)
    stride = ((w * bpp // 8) + 3) & ~3
    if off + stride * h > len(data):
        return None
    rows = []
    for y in range(h):
        src = off + stride * y
        row = bytearray()
        for x in range(w):
            p = src + x * (bpp // 8)
            row += bytes((data[p + 2], data[p + 1], data[p]))   # BGR -> RGB
        rows.append(bytes(row))
    if bottom_up:
        rows.reverse()
    return _ppm_from_rgb(w, h, rows)


def rgb565_to_ppm(data, w, h):
    """Raw RGB565 framebuffer -> PPM, expanding 5/6/5 to full 8-bit range."""
    if len(data) < w * h * 2:
        return None
    rows = []
    for y in range(h):
        base = y * w * 2
        row = bytearray()
        for x in range(w):
            v = _u16le(data, base + x * 2)
            r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
            # replicate the high bits so full-scale maps to 255, not 248
            row += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4),
                          (b << 3) | (b >> 2)))
        rows.append(bytes(row))
    return _ppm_from_rgb(w, h, rows)


def to_photoimage(data, tk_module, raw_size=None):
    """A Tk PhotoImage for this data, or None.

    Caller must keep a reference -- Tk does not own the image and it vanishes
    the moment Python garbage-collects it, which shows up as a blank pane.
    """
    import base64

    def photo(**kw):
        try:
            return tk_module.PhotoImage(**kw)
        except Exception:
            return None

    if raw_size:
        ppm = rgb565_to_ppm(data, raw_size[0], raw_size[1])
        return photo(data=base64.b64encode(ppm)) if ppm else None

    what = identify(data)
    if not what:
        return None
    kind = what[0]

    if kind in ("PNG", "GIF"):                 # Tk 8.6 decodes both itself
        return photo(data=base64.b64encode(data))

    if kind == "BMP":
        ppm = bmp_to_ppm(data)
        return photo(data=base64.b64encode(ppm)) if ppm else None

    if kind == "JPEG":
        try:                                   # optional, never required
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGB")
            ppm = _ppm_from_rgb(im.width, im.height,
                                [im.crop((0, y, im.width, y + 1)).tobytes()
                                 for y in range(im.height)])
            return photo(data=base64.b64encode(ppm))
        except Exception:
            return None
    return None


def describe(data, nbytes=None):
    """One line about an image, whatever its extension claims."""
    what = identify(data)
    if what:
        kind, w, h = what
        size = "%dx%d" % (w, h) if w and h else "size unknown"
        return "%s image, %s" % (kind, size)
    cands = raw_candidates(nbytes if nbytes is not None else len(data))
    if cands:
        w, h = cands[0]
        return "raw RGB565 framebuffer, %dx%d" % (w, h)
    return None
