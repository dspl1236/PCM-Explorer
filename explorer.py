#!/usr/bin/env python3
"""PCM Explorer launcher.

    python explorer.py                 open the desktop UI
    python explorer.py <image>         partition table
    python explorer.py <image> tree P2 directory tree
    python explorer.py --help          full usage
"""
import sys

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        from pcmexplorer.gui import run
        run()
    else:
        from pcmexplorer.cli import main
        sys.exit(main(args))
