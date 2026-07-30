"""What changed on this unit, and when.

Inspection tells you what a drive contains. Diagnosis usually needs the other
axis: a unit stopped working last Tuesday, so what was written around then.
Every filesystem here records mtimes, and sorting by them turns a file listing
into a history -- an update, a crash, a settings change and a botched install
all leave differently-shaped clusters.

Two things make raw mtimes misleading, and both are reported rather than
silently cleaned up:

**The clock is often wrong.** A head unit with no GPS fix and a flat backup
battery boots believing it is 1970, or 2004, or whatever the RTC held. Files
written in that state carry timestamps that are real but meaningless, and a
bench unit does this constantly.

**Most files never change.** A firmware image writes its whole tree at build
time, so thousands of files share one timestamp. That single cluster is not
interesting; the handful that differ from it are.
"""
import time
from collections import Counter

from .core import is_dir, is_link

# Anything outside this is a clock that was not set, not a real date.
PLAUSIBLE_FROM = 1104537600      # 2005-01-01, before any of this hardware
PLAUSIBLE_TO = 2145916800        # 2038-01-01


def _stamp(t):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))
    except (OSError, ValueError, OverflowError):
        return "?"


def collect(walker):
    """[(mtime, path, size)] for every file, newest first.

    ``walker`` yields (path, entry) -- the shape every reader here uses, so a
    disk partition, a firmware image and a persistence image all work.
    """
    out = []
    for path, ent in walker:
        if is_dir(ent) or is_link(ent):
            continue
        mt = ent.get("mtime") or 0
        out.append((mt, path, ent.get("size", 0)))
    out.sort(reverse=True)
    return out


def bulk_stamp(rows, min_share=0.15):
    """The timestamp most files share, if one dominates -- the build time.

    Returns (mtime, count) or (None, 0).  Everything at this stamp was written
    together and says nothing about what happened to the unit afterwards.
    """
    if not rows:
        return None, 0
    counts = Counter(mt for mt, _p, _s in rows)
    mt, n = counts.most_common(1)[0]
    return (mt, n) if n >= max(3, len(rows) * min_share) else (None, 0)


def implausible(rows):
    """Files whose timestamp cannot be a real date -- an unset clock."""
    return [r for r in rows if not (PLAUSIBLE_FROM <= r[0] <= PLAUSIBLE_TO)]


def clusters(rows, gap=3600):
    """Group files into writing sessions separated by at least ``gap`` seconds.

    A cluster is what a single event looks like from the filesystem's side: an
    update writes many files in one burst, a settings change writes one.
    """
    out = []
    for mt, path, size in rows:
        if out and abs(out[-1]["last"] - mt) <= gap:
            c = out[-1]
            c["files"].append((mt, path, size))
            c["last"] = mt
        else:
            out.append({"first": mt, "last": mt, "files": [(mt, path, size)]})
    for c in out:
        c["first"] = max(f[0] for f in c["files"])
        c["last"] = min(f[0] for f in c["files"])
    return out


def format_timeline(rows, show=40, group=True):
    """Human rendering: the odd timestamps first, then the recent history."""
    if not rows:
        return "no files with timestamps"
    L = []
    bulk_mt, bulk_n = bulk_stamp(rows)
    bad = implausible(rows)

    L.append("%d files with timestamps" % len(rows))
    if bulk_mt is not None:
        L.append("  %d of them share %s -- written together, most likely the build"
                 % (bulk_n, _stamp(bulk_mt)))
    if bad:
        L.append("  %d carry a date outside 2005-2038: the clock was not set when"
                 % len(bad))
        L.append("     they were written, so their order is still meaningful but")
        L.append("     the dates are not")
    L.append("")

    interesting = [r for r in rows if r[0] != bulk_mt]
    if not interesting:
        L.append("Every file shares one timestamp -- nothing has been written since.")
        return "\n".join(L)

    if group:
        cs = clusters(interesting)
        L.append("Writing sessions, newest first")
        for c in cs[:12]:
            span = ("%s" % _stamp(c["first"]) if c["first"] == c["last"]
                    else "%s .. %s" % (_stamp(c["last"]), _stamp(c["first"])))
            L.append("")
            L.append("  %-34s %d file%s" % (span, len(c["files"]),
                                            "" if len(c["files"]) == 1 else "s"))
            for mt, path, size in c["files"][:8]:
                L.append("      %10d  %s" % (size, path))
            if len(c["files"]) > 8:
                L.append("      ... and %d more" % (len(c["files"]) - 8))
        if len(cs) > 12:
            L.append("\n  ... and %d older sessions" % (len(cs) - 12))
    else:
        L.append("%-18s %10s  %s" % ("modified", "size", "path"))
        for mt, path, size in interesting[:show]:
            L.append("%-18s %10d  %s" % (_stamp(mt), size, path))
    return "\n".join(L)
