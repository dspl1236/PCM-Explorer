"""Compare two images and report what actually differs.

The question that comes up constantly with head-unit firmware is not "what is in
this image" but "what changed" -- between two releases, between a car and the
factory baseline, between a working unit and a broken one.  Answering it by hand
means extracting both sides and diffing trees, which is slow enough that people
skip it and guess instead.

Anything this project can read can be compared: a firmware image, an EFS
persistence image, an update disc, a partition inside a disk image, or a plain
folder on disk.  The two sides do not have to be the same kind of thing --
comparing a car's exported ``/HBpersistence`` folder against the factory
``PCM3_HBpersistence.efs`` from an update disc is the most useful case, and that
is a folder against a flash image.

Matching is by full path when both sides carry real paths.  An EFS image does
not (F3S keeps directory structure in extents this reader does not walk), so
when either side is flat the comparison falls back to matching on file name.
That is reported in the output, because it changes what a result means: two
files with the same name in different folders would be conflated.
"""
import hashlib
import os

from .firmware import FirmwareImage, looks_like_ifs
from .efs import EfsImage, looks_like_efs
from .updatedisc import UpdateDisc, looks_like_update_disc
from .core import DiskImage, is_dir, is_link


MAX_PAD = 3          # flash extents are 4-byte aligned, padded with 0xFF


def _normalise(b):
    """Ignore flash alignment padding when comparing.

    An EFS extent is padded to a 4-byte boundary with 0xFF, so the same file
    read from flash and from a folder differs by up to three bytes that were
    never part of it.  The reader deliberately keeps those bytes -- a file
    genuinely ending in 0xFF must not be truncated -- so the tolerance lives
    here, where a false 'changed' would otherwise bury the real differences.
    """
    n = len(b)
    cut = 0
    while cut < MAX_PAD and n - cut > 0 and b[n - cut - 1] == 0xFF:
        cut += 1
    return b[:n - cut] if cut else b


def _sha(b):
    return hashlib.sha1(_normalise(b)).hexdigest()[:12]


class Side(object):
    """One side of a comparison: a name, a {path: (size, digest)} index, and a reader."""

    def __init__(self, label, index, read, flat=False, kind=""):
        self.label = label
        self.index = index          # path -> (size, digest)
        self.read = read            # path -> bytes
        self.flat = flat            # True when paths are names only
        self.kind = kind


def _from_folder(path):
    index, store = {}, {}
    for dp, _d, fs in os.walk(path):
        for fn in fs:
            full = os.path.join(dp, fn)
            rel = "/" + os.path.relpath(full, path).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            index[rel] = (len(data), _sha(data))
            store[rel] = full
    return Side(os.path.basename(path.rstrip("/\\")) or path, index,
                lambda p: open(store[p], "rb").read(), kind="folder")


def _from_entries(label, obj, kind, flat=False):
    index = {}
    for pth, ent in obj.entries():
        if is_dir(ent) or is_link(ent):
            continue
        try:
            data = obj.read_file(ent)
        except Exception:
            continue
        index[pth] = (len(data), _sha(data))
    return Side(label, index, lambda p: obj.read_file(obj.find(p)), flat=flat,
                kind=kind)


def _from_disc(path):
    disc = UpdateDisc(path)
    index = {}
    for p in disc.files():
        data = disc.read(p) or b""
        index[p] = (len(data), _sha(data))
    return Side(os.path.basename(path.rstrip("/\\")) or path, index,
                lambda p: disc.read(p) or b"", kind="update disc")


def open_side(path, part=None):
    """Open anything comparable: folder, .ifs, .efs, update disc, or a partition."""
    if os.path.isdir(path):
        if looks_like_update_disc(path):
            return _from_disc(path)
        return _from_folder(path)
    if looks_like_efs(path):
        return _from_entries(os.path.basename(path), EfsImage(path), "EFS", flat=True)
    if looks_like_ifs(path):
        return _from_entries(os.path.basename(path), FirmwareImage(path), "firmware")
    if looks_like_update_disc(path):
        return _from_disc(path)
    img = DiskImage(path)
    if not img.parts:
        raise ValueError("%s: not a readable image, disc, firmware or folder" % path)
    p = img.part(part) if part else img.parts[0]
    if p is None:
        raise ValueError("%s: no partition %r" % (path, part))
    fs = img.open_fs(p)
    if fs is None:
        raise ValueError("%s: partition %s has no readable filesystem"
                         % (path, p["name"]))
    return _from_entries("%s:%s" % (os.path.basename(path), p["name"]), fs,
                         "partition")


def find_baseline(disc_path, want="persistence", against=None):
    """Locate the factory baseline for a comparison inside an update disc.

    "What is non-stock on this unit" is the question people actually have, and
    answering it by hand means knowing that the factory ``/HBpersistence`` lives
    in a ``.efs`` at ``ADR3000000`` inside whichever release matches the car.
    That is three steps of tribal knowledge before the comparison even starts,
    so this does the finding.

    Returns (label, side) or (None, None).
    """
    disc = UpdateDisc(disc_path)
    files = disc.files()
    if want == "persistence":
        # A disc carries one baseline per release -- ARB, CHN, RDW, several
        # versions. Taking the first is a coin toss dressed as an answer, so
        # score each against the unit and use the best fit.
        import tempfile
        cands = [p for p in files
                 if p.lower().endswith(".efs") and "hbpersistence" in p.lower()]
        best, best_side, best_score = None, None, -1
        for i, p in enumerate(cands):
            data = disc.read(p)
            if not data:
                continue
            tmp = os.path.join(tempfile.gettempdir(), "pcmx_baseline_%d.efs" % i)
            with open(tmp, "wb") as f:
                f.write(data)
            try:
                side = _from_entries(p.rsplit("/", 1)[-1], EfsImage(tmp),
                                     "EFS", flat=True)
            except Exception:
                continue
            if against is None:
                return p, side                     # nothing to score against
            _a, _r, _c, same, _bn = compare(side, against)
            if same > best_score:
                best, best_side, best_score = p, side, same
        if best_side is not None:
            label = best
            if len(cands) > 1:
                label = "%s  (best of %d, %d files identical)" % (
                    best, len(cands), best_score)
            return label, best_side
        # fall back to the update's own overlay, which is a subset
        overlay = [p for p in files if "/FIL/HBpersistence/" in p]
        if overlay:
            index, store = {}, {}
            for p in overlay:
                data = disc.read(p) or b""
                key = "/" + p.split("/FIL/HBpersistence/", 1)[1]
                index[key] = (len(data), _sha(data))
                store[key] = p
            return ("FIL/HBpersistence overlay",
                    Side("update overlay", index,
                         lambda k: disc.read(store[k]) or b"", kind="disc overlay"))
    return None, None


def compare(a, b):
    """Return (added, removed, changed, same, by_name).

    ``added``/``removed`` are relative to *a*: present in b but not a, and vice
    versa.  ``changed`` carries both sides' size and digest.
    """
    by_name = a.flat or b.flat
    if by_name:
        ka = {p.rsplit("/", 1)[-1]: p for p in a.index}
        kb = {p.rsplit("/", 1)[-1]: p for p in b.index}
    else:
        ka = {p: p for p in a.index}
        kb = {p: p for p in b.index}

    # carry sizes along, so the report never has to look a key back up -- under
    # name matching the key is a basename and would not resolve against index
    removed = sorted((k, a.index[ka[k]][0]) for k in ka if k not in kb)
    added = sorted((k, b.index[kb[k]][0]) for k in kb if k not in ka)
    changed, same = [], 0
    for k in sorted(set(ka) & set(kb)):
        sa, da = a.index[ka[k]]
        sb, db = b.index[kb[k]]
        if da == db:
            same += 1
        else:
            changed.append((k, sa, da, sb, db))
    return added, removed, changed, same, by_name


def format_report(a, b, added, removed, changed, same, by_name, show=40):
    L = []
    L.append("A  %-46s %s, %d files" % (a.label, a.kind, len(a.index)))
    L.append("B  %-46s %s, %d files" % (b.label, b.kind, len(b.index)))
    L.append("")
    if by_name:
        L.append("Matching on file NAME, not path -- one side has no directory")
        L.append("structure (F3S keeps it in extents this reader does not walk).")
        L.append("Same-named files in different folders would be conflated.")
        L.append("")
    L.append("%d identical, %d changed, %d only in A, %d only in B"
             % (same, len(changed), len(removed), len(added)))
    L.append("")

    def block(title, rows, fmt):
        if not rows:
            return
        L.append("%s (%d)" % (title, len(rows)))
        for r in rows[:show]:
            L.append("  " + fmt(r))
        if len(rows) > show:
            L.append("  ... and %d more" % (len(rows) - show))
        L.append("")

    block("CHANGED", changed,
          lambda r: "%-46s %8d -> %-8d  %s -> %s"
                    % (r[0][:46], r[1], r[3], r[2], r[4]))
    block("ONLY IN A", removed, lambda r: "%-46s %8d" % (r[0][:46], r[1]))
    block("ONLY IN B", added, lambda r: "%-46s %8d" % (r[0][:46], r[1]))
    return "\n".join(L).rstrip() + "\n"
