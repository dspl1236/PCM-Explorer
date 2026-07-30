"""Search file *contents* across anything this project can open.

Listing and reading a file assumes you already know which one you want. Most
questions are the other way round -- which file mentions `servicebroker`, which
build introduced a string, where a config knob is actually read. Answering that
by hand means extracting a tree and reaching for grep, which is enough friction
that people guess instead.

Works on a disk partition, a firmware image, a persistence image, an update
disc or a plain folder, because they all reduce to the same thing: a set of
paths and a way to get bytes for one.

Matching is byte-oriented. A pattern is UTF-8 by default, but firmware carries
plenty of UTF-16 text, so `both` also tries the UTF-16LE form -- that alone
finds strings a naive grep misses in Windows-derived resources. Hex patterns
are for when you are hunting a structure rather than a word.
"""
import re

MAX_CONTEXT = 60
MAX_HITS_PER_FILE = 20


def as_patterns(text, mode="both", ignore_case=False):
    """Turn a user pattern into the byte patterns to look for.

    Returns [(label, bytes)] -- more than one when a string could be stored in
    more than one encoding.
    """
    if mode == "hex":
        cleaned = re.sub(r"[^0-9a-fA-F]", "", text)
        if len(cleaned) % 2:
            raise ValueError("hex pattern needs an even number of digits")
        if not cleaned:
            raise ValueError("empty hex pattern")
        return [("hex", bytes.fromhex(cleaned))]

    pats = [("utf-8", text.encode("utf-8"))]
    if mode == "both":
        # UTF-16LE with no BOM: how Windows-derived resources store text
        pats.append(("utf-16le", text.encode("utf-16-le")))
    if ignore_case:
        out = []
        for label, p in pats:
            out.append((label, p.lower()))
        return out
    return pats


def _find_all(hay, needle, ignore_case, limit):
    if ignore_case:
        hay = hay.lower()
    out, start = [], 0
    while len(out) < limit:
        i = hay.find(needle, start)
        if i < 0:
            break
        out.append(i)
        start = i + max(1, len(needle))
    return out


def _context(data, off, length):
    """A readable snippet around a hit -- text if it looks like text, else hex."""
    lo = max(0, off - 24)
    hi = min(len(data), off + length + 24)
    chunk = data[lo:hi]
    printable = sum(1 for c in chunk if 32 <= c < 127 or c in (9, 10, 13))
    if printable >= len(chunk) * 0.8:
        s = chunk.decode("latin-1")
        s = " ".join(s.split())            # collapse newlines so a hit is one line
        return s[:MAX_CONTEXT]
    return chunk[:24].hex()


def search_bytes(data, patterns, ignore_case=False, limit=MAX_HITS_PER_FILE):
    """[(offset, label, context)] for one blob."""
    hits = []
    for label, pat in patterns:
        if not pat:
            continue
        for off in _find_all(data, pat, ignore_case, limit - len(hits)):
            hits.append((off, label, _context(data, off, len(pat))))
            if len(hits) >= limit:
                return sorted(hits)
    return sorted(hits)


def search_side(side, patterns, ignore_case=False, path_filter=None,
                max_files=None, on_progress=None):
    """Search every file of an opened artifact.

    ``side`` is a :mod:`diffimg` Side -- it carries an index of paths and a
    reader, which is exactly what searching needs, so the two features share
    one abstraction rather than each growing their own.

    Yields (path, [(offset, label, context)]) for files that matched.
    """
    n = 0
    for path in sorted(side.index):
        if path_filter and path_filter.lower() not in path.lower():
            continue
        n += 1
        if max_files and n > max_files:
            break
        if on_progress and n % 200 == 0:
            on_progress(n, path)
        try:
            data = side.read(path)
        except Exception:
            continue
        if not data:
            continue
        hits = search_bytes(data, patterns, ignore_case)
        if hits:
            yield path, hits


def format_hits(path, hits, show=4):
    lines = ["  %s" % path]
    for off, label, ctx in hits[:show]:
        tag = "" if label == "utf-8" else "  [%s]" % label
        lines.append("      +0x%-8x %s%s" % (off, ctx, tag))
    if len(hits) > show:
        lines.append("      ... and %d more in this file" % (len(hits) - show))
    return "\n".join(lines)
