"""Command-line interface -- everything the GUI does, scriptable."""
import os
import sys

from .core import (DiskImage, build_paths, hexdump, human, is_dir, is_link,
                   mode_str)

USAGE = """PCM Explorer -- browse a Porsche PCM / Audi MMI hard-drive image.

  pcm-explorer <image>                      partitions + filesystem detection
  pcm-explorer <image> ls <part> [path]     list files (recursive)
  pcm-explorer <image> cat <part> <path>    print a file to stdout
  pcm-explorer <image> extract <part> <path> <dest>
                                            extract a file or whole folder
  pcm-explorer <image> verify <part>        filesystem consistency self-test
  pcm-explorer <image> hex <offset> [len]   hex dump at a byte offset
  pcm-explorer <image> salvage <part>       recover names without a superblock
  pcm-explorer <image> gui                  open the desktop UI

<part> is P1/P2/P3.  <offset> accepts 0x hex.  The image is opened read-only.
"""


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
              % (mode_str(i["mode"]), size, pth + kind, i["ino"], extra))
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
    try:
        sys.stdout.write(data.decode("utf-8"))
    except UnicodeDecodeError:
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
            print("  %s  (%s)" % (rel, human(len(data))))
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
        print("  %-62s [ino %d]%s" % (pth, ino, "/" if ino in dirs else ""))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    path = argv[0]
    if not os.path.isfile(path):
        print("not a file: %s" % path)
        return 1
    cmd = argv[1] if len(argv) > 1 else "parts"

    if cmd == "gui":
        from .gui import run
        run(path)
        return 0

    img = DiskImage(path)
    try:
        print("%s  (%s)" % (os.path.basename(path), human(img.size)))
        if not img.parts:
            print("  no MBR partition table found")
            return 1
        a = argv[2:]
        if cmd == "parts":
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
