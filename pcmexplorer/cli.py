"""Command-line interface -- everything the GUI does, scriptable."""
import os
import sys

from .core import DiskImage, build_paths, hexdump, human


USAGE = """PCM Explorer -- browse a Porsche PCM / Audi MMI hard-drive image.

  pcm-explorer <image>                    partition table + filesystem detection
  pcm-explorer <image> tree <part> [depth] directory tree of a partition
  pcm-explorer <image> hex <offset> [len]  hex dump at a byte offset
  pcm-explorer <image> gui                 open the desktop UI
  pcm-explorer                             open the desktop UI

<part> is P1/P2/P3.  <offset> accepts 0x hex.  The image is opened read-only.
"""


def cmd_parts(img):
    print("\n  %-4s %-12s %-14s %-12s %s" % ("part", "type", "start", "size", "filesystem"))
    print("  " + "-" * 78)
    for p in img.parts:
        fs, detail = img.detect_fs(p)
        print("  %-4s %-12s %-14d %-12s %s  %s"
              % (p["name"], p["type_name"], p["base"], human(p["length"]), fs, detail))
    print()


def cmd_tree(img, pname, maxdepth):
    p = img.part(pname)
    if not p:
        print("no such partition: %s" % pname)
        return 1
    print("scanning %s ..." % pname)
    dirs = img.scan_dirs(p)
    print("%d directory blocks recovered\n" % len(dirs))
    paths = build_paths(dirs)
    for ino, pth in sorted(paths.items(), key=lambda kv: kv[1]):
        if pth.count("/") <= maxdepth:
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
        if cmd == "parts":
            cmd_parts(img)
        elif cmd == "tree":
            return cmd_tree(img, argv[2] if len(argv) > 2 else "P2",
                            int(argv[3]) if len(argv) > 3 else 3)
        elif cmd == "hex":
            off = int(argv[2], 0)
            n = int(argv[3], 0) if len(argv) > 3 else 512
            print(hexdump(img.read(off, n), off))
        else:
            print(USAGE)
            return 1
    finally:
        img.close()
    return 0


def entry():
    sys.exit(main(sys.argv[1:]))
