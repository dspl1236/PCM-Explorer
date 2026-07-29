"""Command-line interface -- everything the GUI does, scriptable."""
import os
import sys

from . import version_string

from .core import (DiskImage, build_paths, hexdump, human, is_dir, is_link,
                   mode_str, safe_name)
from .decode import preview, summarise_disk, summarise_firmware
from .firmware import FirmwareImage, looks_like_ifs
from .efs import EfsImage, looks_like_efs, summarise_efs
from .hbm5 import Hbm5File, looks_like_hbm5, summarise_hbm5
from .updatedisc import UpdateDisc, looks_like_update_disc, summarise_update

USAGE = """PCM Explorer -- browse a Porsche PCM / Audi MMI hard-drive image.

  pcm-explorer <image>                      summary -- what is this image?
  pcm-explorer <image> parts                partitions + filesystem detection
  pcm-explorer <image> ls <part> [path]     list files (recursive)
  pcm-explorer <image> cat <part> <path>    print a file to stdout
  pcm-explorer <image> extract <part> <path> <dest>
                                            extract a file or whole folder
  pcm-explorer <image> verify <part>        filesystem consistency self-test
  pcm-explorer <image> hex <offset> [len]   hex dump at a byte offset
  pcm-explorer <image> salvage <part>       recover names without a superblock
  pcm-explorer <image> gui                  open the desktop UI

Update discs (a .ISO, or an already-extracted disc folder):

  pcm-explorer <disc>                       what is this disc, and what takes it?
  pcm-explorer <disc> units                 unit IDs -> which releases accept them
  pcm-explorer <disc> modules               modules per release, with payload counts
  pcm-explorer <disc> crc                   verify every CRC32 against its payload
  pcm-explorer <disc> sigs                  signature inventory
  pcm-explorer <disc> files [pattern]       list files on the disc
  pcm-explorer <disc> cat <path>            print a file from the disc
  pcm-explorer <disc> extract <path> <dest> pull a file or the whole disc out

Persistence images (PCM3_HBpersistence.efs and MMI efs-system.efs) open the
same way -- summary, ls, cat, extract.

HMI definitions (.mmi) hold the screens and every string the unit can display:

  pcm-explorer <x.mmi>                      what is in this HMI file
  pcm-explorer <x.mmi> strings [pattern]    every readable string, with its id
  pcm-explorer <x.mmi> langs [pattern]      keys resolved across all languages
  pcm-explorer <x.mmi> verify               container self-check
  pcm-explorer <x.mmi> screens [root]       drawables resolved to x/y/w/h

Compare any two of the above:

  pcm-explorer diff <a> <b>                 what changed between them
  pcm-explorer diff old.ifs new.ifs         between two firmware builds
  pcm-explorer diff ./car_hbp PCM3_HBpersistence.efs    a car vs the factory

Accepts a raw disk image, a firmware image (PCM3_IFS1.ifs / PCM3_IFS2.ifs),
or a PCM 3.1 update disc.

<part> is P1/P2/P3.  <offset> accepts 0x hex.  Everything is opened read-only.
"""


def _fw_ls(fw, sub):
    n = 0
    for pth, e in fw.entries():
        if sub and not pth.startswith(sub):
            continue
        kind = "/" if is_dir(e) else ("@" if is_link(e) else "")
        size = "" if is_dir(e) else human(e["size"])
        extra = (" -> " + fw.link_target(e)) if is_link(e) else ""
        print("  %s %10s  %-52s%s"
              % (mode_str(e["mode"]), size, safe_name(pth + kind), safe_name(extra)))
        n += 1
    print("")
    print("  %d entries" % n)
    return 0


def _fw_find(fw, path):
    want = "/" + path.lstrip("/")
    for pth, e in fw.entries():
        if pth == want:
            return pth, e
    return None, None


def _fs_or_die(img, pname):
    p = img.part(pname)
    if not p:
        print("no such partition: %s" % pname)
        return None, None
    fs = img.open_fs(p)
    if not fs:
        label, detail = img.detect_fs(p)
        print("%s is not QNX6 (%s -- %s).\nTry: salvage %s" % (pname, label, detail, pname))
        return p, None
    return p, fs


def cmd_parts(img):
    print("\n  %-4s %-12s %-14s %-12s %s" % ("part", "type", "start", "size", "filesystem"))
    print("  " + "-" * 78)
    for p in img.parts:
        fs, detail = img.detect_fs(p)
        print("  %-4s %-12s %-14d %-12s %s  %s"
              % (p["name"], p["type_name"], p["base"], human(p["length"]), fs, detail))
    print()


def cmd_ls(img, pname, sub):
    p, fs = _fs_or_die(img, pname)
    if not fs:
        return 1
    n = 0
    for pth, i in fs.walk():
        if sub and not pth.startswith(sub):
            continue
        kind = "/" if is_dir(i) else ("@" if is_link(i) else "")
        size = "" if is_dir(i) else human(i["size"])
        extra = ""
        if is_link(i):
            try:
                extra = " -> " + fs.link_target(i)
            except Exception:
                pass
        print("  %s %10s  %-52s [ino %d]%s"
              % (mode_str(i["mode"]), size, safe_name(pth + kind), i["ino"],
                 safe_name(extra)))
        n += 1
    print("\n  %d entries" % n)
    return 0


def _find(fs, path):
    for pth, i in fs.walk():
        if pth == path or pth == "/" + path.lstrip("/"):
            return pth, i
    return None, None


def cmd_cat(img, pname, path):
    p, fs = _fs_or_die(img, pname)
    if not fs:
        return 1
    pth, i = _find(fs, path)
    if not i:
        print("not found: %s" % path)
        return 1
    if is_dir(i):
        print("%s is a directory" % pth)
        return 1
    data = fs.read_file(i)
    txt = preview(pth, data)
    if txt is not None:
        print(txt)
    else:
        sys.stdout.buffer.write(data)
    return 0


def cmd_extract(img, pname, path, dest):
    p, fs = _fs_or_die(img, pname)
    if not fs:
        return 1
    pth, i = _find(fs, path)
    if not i:
        print("not found: %s" % path)
        return 1
    if is_dir(i):
        n = total = 0
        for sub, si in fs.walk(i["ino"], pth):
            if is_dir(si) or is_link(si):
                continue
            rel = sub[len(pth):].lstrip("/") or ("inode_%d" % si["ino"])
            out = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            data = fs.read_file(si)
            with open(out, "wb") as fh:
                fh.write(data)
            n += 1
            total += len(data)
            print("  %s  (%s)" % (safe_name(rel), human(len(data))))
        print("\n  %d files, %s -> %s" % (n, human(total), dest))
    else:
        out = dest
        if os.path.isdir(dest):
            out = os.path.join(dest, pth.rsplit("/", 1)[-1])
        data = fs.read_file(i)
        with open(out, "wb") as fh:
            fh.write(data)
        print("  wrote %s (%s)" % (out, human(len(data))))
    return 0


def cmd_verify(img, pname):
    p, fs = _fs_or_die(img, pname)
    if not fs:
        return 1
    print("  live superblock @0x%x  serial %d  blocksize %d"
          % (fs.sboff, fs.serial, fs.bs))
    print("  inode root: levels=%d ptr0=0x%x size=%s\n"
          % (fs.rn_inodes.levels, fs.rn_inodes.ptr[0], human(fs.rn_inodes.size)))
    allok = True
    for name, ok, detail in fs.verify():
        allok &= ok
        print("  [%s] %-20s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("\n  %s" % ("all checks passed" if allok else "SOME CHECKS FAILED"))
    return 0 if allok else 2


def cmd_salvage(img, pname):
    p = img.part(pname)
    if not p:
        print("no such partition: %s" % pname)
        return 1
    print("salvage scan of %s (no metadata used)..." % pname)
    dirs = img.scan_dirs(p)
    paths = build_paths(dirs)
    print("%d directory blocks, %d paths recovered\n" % (len(dirs), len(paths)))
    for ino, pth in sorted(paths.items(), key=lambda kv: kv[1]):
        print("  %-62s [ino %d]%s" % (safe_name(pth), ino, "/" if ino in dirs else ""))
    return 0


def _disc_cmd(disc, cmd, a):
    """Update-disc subcommands."""
    if cmd in ("summary", "parts"):
        print(summarise_update(disc))
        return 0

    defs = disc.definitions()

    if cmd == "units":
        by_unit = {}
        for path, d in defs.items():
            rel = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            for u in d["units"]:
                by_unit.setdefault(u["id"], set()).add(rel)
        if not by_unit:
            print("no CONTROL dispatch entries found")
            return 1
        print("%-14s %-18s %s" % ("UNIT ID", "GENERATION", "ACCEPTED BY"))
        for uid in sorted(by_unit):
            gen = "MOPF (02)" if uid[4:6] == "02" else "pre-facelift (01)"
            print("%-14s %-18s %s" % (uid, gen, ", ".join(sorted(by_unit[uid]))))
        return 0

    if cmd == "modules":
        for path in sorted(defs):
            d = defs[path]
            print("\n%s   %s" % (path.rsplit("/", 1)[-1],
                                 d["systemreleaseid"] or ""))
            if d["units"]:
                order = d["units"][0]["modules"]
            else:
                order = sorted(d["modules"])
            for mid in order:
                info = d["modules"].get(mid)
                if not info:
                    print("    %-22s (not defined in CONTENTS)" % mid)
                    continue
                from .updatedisc import MODULE_ROLE
                print("    %-22s %-34s %d file%s"
                      % (mid, MODULE_ROLE.get(info["type"], ""),
                         len(info["files"]), "" if len(info["files"]) == 1 else "s"))
        return 0

    if cmd in ("crc", "verify"):
        rows = disc.verify_crcs(a[0] if a else None)
        if not rows:
            print("no CRC32 records found")
            return 1
        npass = nfail = nabs = 0
        for name, status, detail in rows:
            tag = {"pass": "PASS", "fail": "FAIL", "absent": "----"}[status]
            print("  [%s] %-30s %s" % (tag, name.rsplit("/", 1)[-1], detail))
            npass += status == "pass"
            nfail += status == "fail"
            nabs += status == "absent"
        print("\n%d passed, %d failed, %d payload absent" % (npass, nfail, nabs))
        return 1 if nfail else 0

    if cmd == "sigs":
        sigs = disc.signatures()
        if not sigs:
            print("no .sig files found")
            return 1
        kinds = {}
        for _n, algo, nbytes in sigs:
            key = "%s-%d" % (algo, nbytes * 8) if nbytes else algo
            kinds[key] = kinds.get(key, 0) + 1
        for k in sorted(kinds):
            print("  %-14s %d" % (k, kinds[k]))
        print("\n%d signature files" % len(sigs))
        if any(a2 == "RSA" for _n, a2, _b in sigs):
            print("RSA-signed: a modified module cannot be signed without the "
                  "private key.")
        return 0

    if cmd == "cat":
        if not a:
            print("usage: cat <path>")
            return 1
        data = disc.read(a[0])
        if data is None:
            # tolerate a bare filename rather than the full disc path
            hits = [p for p in disc.files() if p.rsplit("/", 1)[-1] == a[0]]
            if len(hits) == 1:
                data = disc.read(hits[0])
            elif len(hits) > 1:
                print("ambiguous -- %d files named %s:" % (len(hits), a[0]))
                for h in hits[:10]:
                    print("   %s" % h)
                return 1
        if data is None:
            print("not on this disc: %s" % a[0])
            return 1
        try:
            sys.stdout.buffer.write(data)
        except Exception:
            print(data.decode("latin-1", "replace"))
        return 0

    if cmd == "extract":
        if not a:
            print("usage: extract <path|all> <dest>")
            return 1
        dest = a[1] if len(a) > 1 else "."
        want = disc.files() if a[0] in ("all", "/") else \
            [p for p in disc.files() if p == a[0] or p.startswith(a[0].rstrip("/") + "/")
             or p.rsplit("/", 1)[-1] == a[0]]
        if not want:
            print("nothing matches: %s" % a[0])
            return 1
        n = 0
        for p in want:
            data = disc.read(p)
            if data is None:
                continue
            rel = p.lstrip("/").replace("/", os.sep)
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
            n += 1
        print("extracted %d file%s to %s" % (n, "" if n == 1 else "s", dest))
        return 0

    if cmd == "files":
        pat = a[0].lower() if a else None
        n = 0
        for p in disc._files():
            if pat and pat not in p.lower():
                continue
            sz = disc.size_of(p)
            print("  %10s  %s" % (human(sz) if sz is not None else "-", p))
            n += 1
        print("\n%d file%s" % (n, "" if n == 1 else "s"))
        return 0

    print("unknown update-disc command: %s" % cmd)
    print("try: summary, units, modules, crc, sigs, files, cat, extract")
    return 1


def _hbm5_cmd(m, cmd, a):
    """HMI-definition subcommands."""
    if cmd in ("summary", "parts"):
        print(summarise_hbm5(m))
        return 0

    if cmd == "verify":
        bad = 0
        for label, ok, detail in m.verify():
            print("  [%s] %-32s %s" % ("PASS" if ok else "FAIL", label, detail))
            bad += 0 if ok else 1
        print("\n%s" % ("container consistent" if not bad else "%d checks failed" % bad))
        return 1 if bad else 0

    if cmd in ("strings", "ls"):
        pat = a[0].lower() if a else None
        strs = m.strings()
        n = 0
        for rid in sorted(strs):
            t = strs[rid]
            if pat and pat not in t.lower():
                continue
            print("  %-8d %s" % (rid, t.replace("\n", "\\n")))
            n += 1
        print("\n%d string%s" % (n, "" if n == 1 else "s"))
        comp = m.compressed_ids()
        if comp:
            print("%d further payloads are LZRW-compressed and not decoded." % len(comp))
        return 0

    if cmd in ("langs", "translations"):
        want = a[0].lower() if a else None
        rows = [r for r in m.translations() if len(r[1]) > 1]
        if not rows:
            print("no multi-language keys in this file")
            return 1
        shown = 0
        for did, row in rows:
            if want and not any(want in v.lower() for v in row.values()):
                continue
            print("  key %d" % did)
            for k in sorted(row):
                print("      %-6s %s" % (k, row[k].replace("\n", "\\n")))
            shown += 1
        if want:
            print("\n%d of %d translated keys match %r" % (shown, len(rows), a[0]))
        else:
            print("\n%d translated key%s" % (shown, "" if shown == 1 else "s"))
        return 0

    if cmd in ("screens", "boxes"):
        from .hbm5geom import Screens
        sc = Screens(m)
        st = sc.stats()
        print("  %-26s %d" % ("drawables", st["drawables"]))
        print("  %-26s %d" % ("position by reference", st["position_by_reference"]))
        print("  %-26s %d" % ("references resolved", st["references_resolved"]))
        print("  %-26s %d" % ("boxes within 800x480", st["boxes_on_screen"]))
        if a:
            try:
                root = int(a[0], 0)
            except ValueError:
                print("usage: screens [root-id]")
                return 1
            seen, stack, rows = set(), [root], []
            while stack:
                rid = stack.pop()
                if rid in seen:
                    continue
                seen.add(rid)
                b = sc.box(rid)
                if b:
                    rows.append((rid, b, sc.label(rid)))
                stack.extend(sc.children(rid))
            print("\n  subtree %d: %d nodes, %d with boxes\n" % (root, len(seen), len(rows)))
            print("  %-8s %5s %5s %5s %5s  %-4s %s"
                  % ("id", "x", "y", "w", "h", "src", "label"))
            for rid, b, lab in sorted(rows, key=lambda r: (r[1][1], r[1][0]))[:60]:
                print("  %-8d %5d %5d %5d %5d  %-4s %s"
                      % (rid, b[0], b[1], b[2], b[3], b[4] or "-",
                         (lab or "")[:40]))
            if len(rows) > 60:
                print("  ... and %d more" % (len(rows) - 60))
        return 0

    print("unknown HMI command: %s" % cmd)
    print("try: summary, verify, strings, langs, screens")
    return 1


def cmd_diff(argv):
    """Compare two readable things and report what differs."""
    from .diffimg import compare, format_report, open_side
    if len(argv) < 2:
        print("usage: diff <a> <b>")
        print("  each side may be a folder, .ifs firmware, .efs persistence")
        print("  image, an update disc (.iso or folder), or a disk image")
        return 1
    try:
        a = open_side(argv[0])
        b = open_side(argv[1])
    except Exception as e:
        print("could not open: %s" % e)
        return 1
    added, removed, changed, same, by_name = compare(a, b)
    print(format_report(a, b, added, removed, changed, same, by_name))
    return 0


def main(argv):
    try:                       # console codepages vary; never crash on output
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if argv and argv[0] in ("-V", "--version", "version"):
        print("PCM Explorer %s" % version_string())
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("PCM Explorer %s\n" % version_string())
        print(USAGE)
        return 0
    # diff takes two operands rather than an image plus a verb
    if argv[0] == "diff":
        return cmd_diff(argv[1:])

    path = argv[0]
    # a directory is only meaningful as an extracted update-disc tree
    if not os.path.isfile(path) and not os.path.isdir(path):
        print("not a file: %s" % path)
        return 1
    cmd = argv[1] if len(argv) > 1 else "summary"

    if cmd == "gui":
        from .gui import run
        run(path)
        return 0

    # An HMI definition (.mmi) -- screens and every string the unit can show.
    if looks_like_hbm5(path):
        try:
            m = Hbm5File(path)
        except Exception as e:
            print("not a readable HMI definition: %s" % e)
            return 1
        return _hbm5_cmd(m, cmd, argv[2:])

    # An update disc -- an ISO, or a folder it was extracted to.
    if looks_like_update_disc(path):
        try:
            disc = UpdateDisc(path)
        except Exception as e:
            print("not a readable update disc: %s" % e)
            return 1
        try:
            return _disc_cmd(disc, cmd, argv[2:])
        finally:
            disc.close()

    if not os.path.isfile(path):
        print("not a file: %s" % path)
        return 1

    # A firmware image or an EFS persistence image is a different container but
    # browses the same way -- same entry shape, so one code path serves both.
    if looks_like_ifs(path) or looks_like_efs(path):
        try:
            fw = EfsImage(path) if looks_like_efs(path) else FirmwareImage(path)
        except Exception as e:
            print("not a readable firmware image: %s" % e)
            return 1
        if isinstance(fw, EfsImage) and cmd in ("parts", "summary"):
            print(summarise_efs(fw))
            return 0
        a = argv[2:]
        if cmd in ("parts", "summary"):
            print(summarise_firmware(fw))
        elif cmd == "ls":
            return _fw_ls(fw, a[0] if a else None)
        elif cmd == "cat":
            if not a:
                print("usage: cat <path>")
                return 1
            pth, e = _fw_find(fw, a[0])
            if not e:
                print("not found: %s" % a[0])
                return 1
            data = fw.read_file(e)
            txt = preview(pth, data)
            if txt is not None:
                print(txt)
            else:
                sys.stdout.buffer.write(data)
        elif cmd == "extract":
            if len(a) < 2:
                print("usage: extract <path> <dest>")
                return 1
            pth, e = _fw_find(fw, a[0])
            if not e:
                print("not found: %s" % a[0])
                return 1
            with open(a[1], "wb") as fh:
                fh.write(fw.read_file(e))
            print("  wrote %s (%s)" % (a[1], human(e["size"])))
        elif cmd == "verify":
            allok = True
            for n, ok, det in fw.verify():
                allok &= ok
                print("  [%s] %-18s %s" % ("PASS" if ok else "FAIL", n, det))
            return 0 if allok else 2
        else:
            print(USAGE)
            return 1
        return 0

    img = DiskImage(path)
    try:
        print("%s  (%s)" % (os.path.basename(path), human(img.size)))
        if not img.parts:
            print("  no MBR partition table found")
            return 1
        a = argv[2:]
        if cmd == "summary":
            print(summarise_disk(img))
        elif cmd == "parts":
            cmd_parts(img)
        elif cmd == "ls":
            return cmd_ls(img, a[0] if a else "P2", a[1] if len(a) > 1 else None)
        elif cmd == "cat":
            if len(a) < 2:
                print("usage: cat <part> <path>")
                return 1
            return cmd_cat(img, a[0], a[1])
        elif cmd == "extract":
            if len(a) < 3:
                print("usage: extract <part> <path> <dest>")
                return 1
            return cmd_extract(img, a[0], a[1], a[2])
        elif cmd == "verify":
            return cmd_verify(img, a[0] if a else "P2")
        elif cmd == "salvage":
            return cmd_salvage(img, a[0] if a else "P2")
        elif cmd == "hex":
            off = int(a[0], 0)
            n = int(a[1], 0) if len(a) > 1 else 512
            print(hexdump(img.read(off, n), off))
        else:
            print(USAGE)
            return 1
    finally:
        img.close()
    return 0


def entry():
    sys.exit(main(sys.argv[1:]))
