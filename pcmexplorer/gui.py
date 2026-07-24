"""Tkinter desktop UI for browsing a head-unit disk image."""
import os
import threading

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .core import DiskImage, build_paths, hexdump, human, mode_str, NIL

BG, PANEL, LINE = "#14161a", "#1b1e24", "#2b3038"
TEXT, DIM, ACCENT = "#dfe3ea", "#8b93a1", "#d2a24c"

OPEN_TYPES = [("Disk images", "*.img *.dd *.raw *.bin"), ("All files", "*.*")]


class Explorer(tk.Tk):
    def __init__(self, initial=None):
        super().__init__()
        self.title("PCM Explorer")
        self.geometry("1180x760")
        self.configure(bg=BG)
        self.img = None
        self.dirs = {}
        self.paths = {}
        self.cur_part = None
        self._cancel = False

        self._style()
        self._build()
        if initial:
            self.load(initial)

    # -- chrome --
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Dim.TLabel", foreground=DIM)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=TEXT, rowheight=20, borderwidth=0)
        st.configure("Treeview.Heading", background=LINE, foreground=TEXT)
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", "#000000")])

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="Open image...", command=self.open_image).pack(side="left")
        self.lbl = ttk.Label(top, text="(no image loaded)", style="Dim.TLabel")
        self.lbl.pack(side="left", padx=12)

        pan = ttk.PanedWindow(self, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(pan)
        ttk.Label(left, text="Partitions").pack(anchor="w")
        self.plist = ttk.Treeview(left, columns=("type", "size", "fs"),
                                  show="tree headings", height=5)
        self.plist.heading("#0", text="part")
        self.plist.column("#0", width=60)
        for c, w in (("type", 90), ("size", 90), ("fs", 160)):
            self.plist.heading(c, text=c)
            self.plist.column(c, width=w)
        self.plist.pack(fill="x")
        self.plist.bind("<<TreeviewSelect>>", self.on_part)

        ttk.Label(left, text="Files").pack(anchor="w", pady=(10, 0))
        self.tree = ttk.Treeview(left, columns=("ino",), show="tree headings")
        self.tree.heading("#0", text="name")
        self.tree.column("#0", width=340)
        self.tree.heading("ino", text="inode")
        self.tree.column("ino", width=70)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_node)
        pan.add(left, weight=3)

        right = ttk.Frame(pan)
        ttk.Label(right, text="Details").pack(anchor="w")
        self.info = tk.Text(right, height=9, bg=PANEL, fg=TEXT, bd=0,
                            insertbackground=TEXT, font=("Consolas", 9))
        self.info.pack(fill="x")
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="Extract file...", command=self.extract).pack(side="left")
        ttk.Button(bar, text="Export tree...", command=self.export_tree).pack(side="left", padx=6)
        ttk.Label(right, text="Content / hex").pack(anchor="w")
        self.hexv = tk.Text(right, bg=PANEL, fg=TEXT, bd=0, wrap="none",
                            insertbackground=TEXT, font=("Consolas", 9))
        self.hexv.pack(fill="both", expand=True)
        pan.add(right, weight=4)

        self.status = tk.StringVar(value="Read-only — the image is never modified.")
        tk.Label(self, textvariable=self.status, bg=PANEL, fg=DIM, anchor="w",
                 padx=8, pady=4).pack(fill="x", side="bottom")

    def set_status(self, s):
        self.status.set(s)
        self.update_idletasks()

    # -- loading --
    def open_image(self):
        p = filedialog.askopenfilename(title="Open disk image", filetypes=OPEN_TYPES)
        if p:
            self.load(p)

    def load(self, path):
        try:
            if self.img:
                self.img.close()
            self.img = DiskImage(path)
        except Exception as e:
            messagebox.showerror("Could not open image", str(e))
            return
        self.lbl.config(text="%s   %s" % (os.path.basename(path), human(self.img.size)))
        self.plist.delete(*self.plist.get_children())
        self.tree.delete(*self.tree.get_children())
        self.dirs, self.paths, self.cur_part = {}, {}, None
        if not self.img.parts:
            self.set_status("No MBR partition table found in this image.")
            return
        for p in self.img.parts:
            fs, _ = self.img.detect_fs(p)
            self.plist.insert("", "end", iid=p["name"], text=p["name"],
                              values=(p["type_name"], human(p["length"]), fs))
        self.set_status("%d partitions — select one to scan its directory tree."
                        % len(self.img.parts))

    # -- partition selected --
    def on_part(self, _evt=None):
        sel = self.plist.selection()
        if not sel or not self.img:
            return
        p = self.img.part(sel[0])
        if not p:
            return
        self.cur_part = p
        fs, detail = self.img.detect_fs(p)
        self.info.delete("1.0", "end")
        self.info.insert("end",
                         "%s   type %s (0x%02x)\nbase   %d (0x%x)\nsize   %s\nfs     %s  %s\n"
                         % (p["name"], p["type_name"], p["type"], p["base"], p["base"],
                            human(p["length"]), fs, detail))
        for sb in self.img.superblocks(p):
            self.info.insert("end", "sb@0x%x serial=%d inode_ptr=0x%x levels=%d\n"
                             % (sb["off"], sb["serial"], sb["inode_ptr"], sb["inode_levels"]))
        self.hexv.delete("1.0", "end")
        self.hexv.insert("end", hexdump(self.img.read(p["base"], 512), p["base"]))
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self._scan, args=(p,), daemon=True).start()

    def _scan(self, p):
        self._cancel = False

        def prog(done, total, n):
            if done % (64 * 1024 * 1024) < 8 * 1024 * 1024:
                self.set_status("Scanning %s — %s of %s, %d directories found..."
                                % (p["name"], human(done), human(total), n))

        dirs = self.img.scan_dirs(p, progress=prog, cancel=lambda: self._cancel)
        self.dirs = dirs
        self.paths = build_paths(dirs)
        self.after(0, self._fill_tree, p)

    def _fill_tree(self, p):
        self.tree.delete(*self.tree.get_children())
        if not self.dirs:
            self.set_status("%s — no directory blocks found. The filesystem may "
                            "differ, or this area is empty." % p["name"])
            return
        roots = [1] if 1 in self.dirs else [
            i for i, d in self.dirs.items() if d["parent"] not in self.dirs]
        for r in roots[:8]:
            self._add("", r, "/" if r == 1 else "(root %d)" % r, set())
        self.set_status("%s — %d directories, %d paths recovered. Read-only."
                        % (p["name"], len(self.dirs), len(self.paths)))

    def _add(self, parent, ino, name, seen):
        node = self.tree.insert(parent, "end", text=name, values=(ino,))
        if ino in seen:
            return
        seen.add(ino)
        d = self.dirs.get(ino)
        if not d:
            return
        for nm, cino in sorted(d["kids"]):
            if nm in (".", ".."):
                continue
            if cino in self.dirs:
                self._add(node, cino, nm + "/", seen)
            else:
                self.tree.insert(node, "end", text=nm, values=(cino,))

    # -- node selected --
    def _sel(self):
        sel = self.tree.selection()
        if not sel:
            return None, None
        vals = self.tree.item(sel[0], "values")
        if not vals:
            return None, None
        return int(vals[0]), self.tree.item(sel[0], "text")

    def on_node(self, _evt=None):
        ino, name = self._sel()
        if ino is None or not self.cur_part:
            return
        self.info.delete("1.0", "end")
        self.info.insert("end", "%s\ninode  %d\npath   %s\n"
                         % (name, ino, self.paths.get(ino, "?")))
        if ino in self.dirs:
            d = self.dirs[ino]
            self.info.insert("end", "type   directory (%d entries)\nparent inode %d\n"
                             % (len(d["kids"]), d["parent"]))
            self.hexv.delete("1.0", "end")
            self.hexv.insert("end", hexdump(self.img.read(d["offset"], 512), d["offset"]))
            return
        res = self.img.resolve_inode(self.cur_part, ino)
        if not res:
            self.info.insert("end",
                             "type   file\nstatus inode struct not located\n"
                             "       QNX6 scrambles inode numbers across allocation\n"
                             "       groups; the map for this build is unsolved, so\n"
                             "       the contents cannot be read yet.\n")
            self.hexv.delete("1.0", "end")
            return
        off, node = res
        blocks = ", ".join(str(b) for b in node["block_ptr"] if b not in (0, NIL))
        self.info.insert("end", "type   file  %s\nsize   %s\ninode@ 0x%x\nblocks %s\n"
                         % (mode_str(node["mode"]), human(node["size"]), off, blocks))
        data, warn = self.img.read_file(self.cur_part, node, max_bytes=64 * 1024)
        if warn:
            self.info.insert("end", "note   %s\n" % warn)
        self.hexv.delete("1.0", "end")
        self.hexv.insert("end", hexdump(data[:4096], 0) if data else "(no data)")

    # -- actions --
    def extract(self):
        ino, name = self._sel()
        if ino is None or not self.cur_part:
            return
        res = self.img.resolve_inode(self.cur_part, ino)
        if not res:
            messagebox.showinfo(
                "Cannot extract this file",
                "The inode struct could not be located.\n\n"
                "Browsing works because directory blocks identify themselves, but "
                "turning an inode number into data blocks is not solved for this "
                "filesystem build yet, so there is nothing safe to write.")
            return
        _off, node = res
        data, warn = self.img.read_file(self.cur_part, node)
        if not data:
            messagebox.showinfo("Cannot extract this file", warn or "No data.")
            return
        dest = filedialog.asksaveasfilename(initialfile=name.rstrip("/"))
        if not dest:
            return
        with open(dest, "wb") as fh:
            fh.write(data)
        self.set_status("Wrote %s (%s)%s"
                        % (dest, human(len(data)), ("  — " + warn) if warn else ""))

    def export_tree(self):
        if not self.paths:
            return
        dest = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="tree.txt")
        if not dest:
            return
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("# %s  %s\n" % (os.path.basename(self.img.path),
                                     self.cur_part["name"] if self.cur_part else ""))
            for i, pth in sorted(self.paths.items(), key=lambda kv: kv[1]):
                fh.write("%-70s [ino %d]%s\n" % (pth, i, "/" if i in self.dirs else ""))
        self.set_status("Wrote %s (%d paths)" % (dest, len(self.paths)))


def run(initial=None):
    Explorer(initial).mainloop()
