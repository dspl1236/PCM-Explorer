"""Tkinter desktop UI for browsing a head-unit disk image."""
import os
import threading

from . import version_string

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .core import (DiskImage, build_paths, hexdump, human, is_dir, is_link,
                   mode_str, NIL)
from .decode import preview, summarise_disk, summarise_firmware
from .firmware import FirmwareImage, looks_like_ifs
from .efs import EfsImage, looks_like_efs, summarise_efs
from .hbm5 import Hbm5File, looks_like_hbm5, summarise_hbm5
from .updatedisc import UpdateDisc, looks_like_update_disc, summarise_update

# Palette shared with PCM-Forge (docs/index.html :root), so the desktop tool and
# the web toolkit read as one project.
BG, PANEL, LINE = "#0a0a0c", "#131318", "#2a2a35"
PANEL2 = "#1a1a22"
TEXT, DIM, ACCENT = "#e8e8ed", "#8888a0", "#c8a44e"
ACCENT2, GREEN, RED = "#e8c85e", "#4eca7a", "#e85454"

OPEN_TYPES = [("Head-unit images", "*.img *.dd *.raw *.bin *.ifs *.iso *.efs *.mmi"),
              ("Disk images", "*.img *.dd *.raw *.bin"),
              ("Firmware images", "*.ifs"),
              ("Update discs", "*.iso"),
              ("Persistence images", "*.efs"),
              ("HMI definitions", "*.mmi"),
              ("All files", "*.*")]
PREVIEW_BYTES = 4096


def _printable_ratio(b):
    if not b:
        return 0.0
    ok = sum(1 for c in b if 32 <= c < 127 or c in (9, 10, 13))
    return ok / len(b)


class Explorer(tk.Tk):
    def __init__(self, initial=None):
        super().__init__()
        self.title("PCM Explorer %s" % version_string())
        self.geometry("1200x780")
        self.configure(bg=BG)
        self.img = None
        self.fw = None            # FirmwareImage when a .ifs is open
        self.disc = None          # UpdateDisc when an .iso / disc folder is open
        self.hmi = None           # Hbm5File when a .mmi is open
        self.fs = None            # QNX6FS/QNX4FS when the partition mounts
        self.nodes = {}           # tree item id -> inode dict
        self.salvage = {}         # fallback directory map
        self.cur_part = None
        self._cancel = False

        self._style()
        self._build()
        self._wheel()
        if initial:
            self.load(initial)

    def show_screen(self, root=None):
        """Draw an HMI screen's element boxes on an 800x480 canvas.

        A wireframe, not a render: positions and sizes are decoded, but the
        bitmaps are not -- they are not in these files at all.  Labelled boxes
        are still enough to recognise a screen and to lay a custom one out so
        it matches.
        """
        if not self.hmi:
            messagebox.showinfo("No HMI file open",
                                "Open a .mmi file to view its screens.")
            return
        from .hbm5geom import Screens, DISPLAY_W, DISPLAY_H
        if getattr(self, "_screens", None) is None:
            self.set_status("Decoding screen geometry...")
            self._screens = Screens(self.hmi)
        sc = self._screens
        if root is None:
            sel = self.tree.selection()
            ent = self.nodes.get(sel[0]) if sel else None
            root = (ent or {}).get("_hmi_node")
        if root is None:
            messagebox.showinfo("Pick a screen",
                                "Select a screen under \"screens/\" first.")
            return

        seen, stack, boxes = set(), [root], []
        while stack:
            rid = stack.pop()
            if rid in seen or len(seen) > 20000:
                continue
            seen.add(rid)
            b = sc.box(rid)
            if b:
                boxes.append((rid, b, sc.label(rid)))
            stack.extend(sc.children(rid))

        win = tk.Toplevel(self)
        win.title("Screen %d -- %d elements  (wireframe: bitmaps not decoded)"
                  % (root, len(boxes)))
        win.configure(bg=BG)
        cv = tk.Canvas(win, width=DISPLAY_W, height=DISPLAY_H, bg=PANEL,
                       highlightthickness=1, highlightbackground=LINE)
        cv.pack(padx=10, pady=10)
        cv.create_rectangle(0, 0, DISPLAY_W, DISPLAY_H, outline=ACCENT)
        # biggest first, so small elements land on top of their containers
        drawn = 0
        for _rid, b, lab in sorted(boxes, key=lambda r: -(r[1][2] * r[1][3])):
            x, y, w, h, src = b
            if w <= 0 or h <= 0 or x > DISPLAY_W or y > DISPLAY_H:
                continue
            colour = ACCENT if "P" in src else DIM
            cv.create_rectangle(x, y, x + w, y + h, outline=colour)
            if lab and w > 26 and h > 12:
                cv.create_text(x + 3, y + 2, anchor="nw", text=lab[:24],
                               fill=TEXT, font=("Consolas", 7))
            drawn += 1
        ttk.Label(win, style="Dim.TLabel",
                  text="%d of %d elements drawn   %dx%d   gold = position "
                       "resolved by reference" % (drawn, len(boxes),
                                                  DISPLAY_W, DISPLAY_H)
                  ).pack(pady=(0, 8))
        self.set_status("Screen %d: %d elements drawn." % (root, drawn))

    def _show_image(self, data, nbytes=None):
        """Draw the file in the content pane if it is a picture.  True if shown.

        Identified by magic, so a bootscreen named ``.bin`` still renders. The
        PhotoImage is kept on self -- Tk does not own it, and letting Python
        collect it leaves an empty pane with no error.
        """
        from .images import describe, raw_candidates, to_photoimage
        n = nbytes if nbytes is not None else len(data)
        raw = None
        if not describe(data):
            cands = raw_candidates(n)
            if not cands:
                return False
            raw = cands[0]
        photo = to_photoimage(data, tk, raw_size=raw)
        if photo is None:
            what = describe(data, n)
            if what and "JPEG" in what:
                self.hexv.insert(
                    "end", "%s\n\n(JPEG needs Pillow to display; install it with\n"
                           " pip install pillow\nThe file itself extracts fine.)\n"
                           % what)
                return True
            return False
        self._photo = photo                     # keep a reference or it vanishes
        self.hexv.insert("end", " ")
        self.hexv.image_create("end", image=photo)
        self.hexv.insert("end", "\n\n%s   %dx%d\n"
                         % (describe(data, n) or "image",
                            photo.width(), photo.height()))
        return True

    def _wheel(self):
        """Scroll whatever is under the pointer, not whatever has focus.

        Tk sends the wheel to the focused widget, so hovering a pane and
        scrolling does nothing until you click it first -- which is not how
        anyone expects a file browser to behave.
        """
        def on_wheel(ev):
            w = self.winfo_containing(ev.x_root, ev.y_root)
            while w is not None:
                if hasattr(w, "yview_scroll"):
                    w.yview_scroll(-1 if ev.delta > 0 else 1, "units")
                    return "break"
                w = getattr(w, "master", None)
            return None
        self.bind_all("<MouseWheel>", on_wheel)          # Windows / macOS
        self.bind_all("<Button-4>", lambda e: on_wheel(type("E", (), {
            "x_root": e.x_root, "y_root": e.y_root, "delta": 120})))
        self.bind_all("<Button-5>", lambda e: on_wheel(type("E", (), {
            "x_root": e.x_root, "y_root": e.y_root, "delta": -120})))

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
        # PCM-Forge marks section titles in gold monospace; echo that here.
        st.configure("Title.TLabel", foreground=ACCENT,
                     font=("Consolas", 9, "bold"))
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=TEXT, rowheight=21, borderwidth=0)
        st.configure("Treeview.Heading", background=PANEL2, foreground=DIM,
                     borderwidth=0, font=("Consolas", 9))
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", BG)])
        st.configure("TButton", background=PANEL2, foreground=TEXT,
                     borderwidth=1, focusthickness=0, padding=(10, 5))
        st.map("TButton",
               background=[("active", LINE), ("pressed", ACCENT)],
               foreground=[("pressed", BG)])
        st.configure("TPanedwindow", background=BG)
        st.configure("Vertical.TScrollbar", background=LINE, troughcolor=BG,
                     borderwidth=0, arrowcolor=DIM)
        st.configure("Horizontal.TScrollbar", background=LINE, troughcolor=BG,
                     borderwidth=0, arrowcolor=DIM)

    @staticmethod
    def _scrolled(parent, make, horizontal=False, **pack_kw):
        """Put a widget in a frame with scrollbars and return the widget.

        Everything here can outgrow its pane -- a disc has thousands of files,
        a module's FILES block runs to hundreds of lines -- so nothing is
        usefully scrollable by wheel alone: you cannot see how far down you are.
        """
        box = ttk.Frame(parent)
        w = make(box)
        vs = ttk.Scrollbar(box, orient="vertical", command=w.yview)
        w.configure(yscrollcommand=vs.set)
        w.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        if horizontal:
            hs = ttk.Scrollbar(box, orient="horizontal", command=w.xview)
            w.configure(xscrollcommand=hs.set)
            hs.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        box.pack(**pack_kw)
        return w

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="Open image...", command=self.open_image).pack(side="left")
        ttk.Button(top, text="Open folder...", command=self.open_folder).pack(side="left",
                                                                             padx=(6, 0))
        self.lbl = ttk.Label(top, text="(no image loaded)", style="Dim.TLabel")
        self.lbl.pack(side="left", padx=12)
        # Version in the corner: the first thing to ask when someone reports a bug.
        ttk.Label(top, text=version_string(), style="Dim.TLabel").pack(side="right")

        pan = ttk.PanedWindow(self, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(pan)
        ttk.Label(left, text="Partitions", style="Title.TLabel").pack(anchor="w")
        self.plist = self._scrolled(
            left,
            lambda b: ttk.Treeview(b, columns=("type", "size", "fs"),
                                   show="tree headings", height=6),
            fill="x")
        self.plist.heading("#0", text="part")
        self.plist.column("#0", width=55)
        for c, w in (("type", 80), ("size", 85), ("fs", 165)):
            self.plist.heading(c, text=c)
            self.plist.column(c, width=w)
        self.plist.bind("<<TreeviewSelect>>", self.on_part)

        ttk.Label(left, text="Files", style="Title.TLabel").pack(anchor="w", pady=(10, 0))
        self.tree = self._scrolled(
            left,
            lambda b: ttk.Treeview(b, columns=("size", "ino"), show="tree headings"),
            horizontal=True, fill="both", expand=True)
        self.tree.heading("#0", text="name")
        self.tree.column("#0", width=300)
        self.tree.heading("size", text="size")
        self.tree.column("size", width=90, anchor="e")
        self.tree.heading("ino", text="inode")
        self.tree.column("ino", width=65, anchor="e")
        self.tree.bind("<<TreeviewSelect>>", self.on_node)
        pan.add(left, weight=3)

        right = ttk.Frame(pan)
        ttk.Label(right, text="Details", style="Title.TLabel").pack(anchor="w")
        self.info = self._scrolled(
            right,
            lambda b: tk.Text(b, height=10, bg=PANEL, fg=TEXT, bd=0,
                              insertbackground=TEXT, font=("Consolas", 9)),
            fill="x")
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="Extract file...", command=self.extract).pack(side="left")
        ttk.Button(bar, text="Extract folder...", command=self.extract_dir).pack(side="left", padx=6)
        ttk.Button(bar, text="Export listing...", command=self.export_tree).pack(side="left")
        ttk.Button(bar, text="Verify", command=self.verify).pack(side="left", padx=6)
        ttk.Button(bar, text="Summary", command=self.show_summary).pack(side="left")
        ttk.Button(bar, text="View screen", command=self.show_screen).pack(side="left", padx=6)
        ttk.Label(right, text="Content", style="Title.TLabel").pack(anchor="w")
        # wrap="none" so a hex dump keeps its columns -- hence a horizontal bar
        self.hexv = self._scrolled(
            right,
            lambda b: tk.Text(b, bg=PANEL, fg=TEXT, bd=0, wrap="none",
                              insertbackground=TEXT, font=("Consolas", 9)),
            horizontal=True, fill="both", expand=True)
        pan.add(right, weight=4)

        self.status = tk.StringVar(value="Read-only — the image is never modified.")
        tk.Label(self, textvariable=self.status, bg=PANEL2, fg=DIM, anchor="w",
                 padx=8, pady=4).pack(fill="x", side="bottom")

    def set_status(self, s):
        self.status.set(s)
        self.update_idletasks()

    # -- loading --
    def open_image(self):
        p = filedialog.askopenfilename(title="Open disk image", filetypes=OPEN_TYPES)
        if p:
            self.load(p)

    def open_folder(self):
        """An update disc that has already been extracted is a folder, not a file."""
        p = filedialog.askdirectory(title="Open an extracted update-disc folder")
        if p:
            self.load(p)

    def _load_disc(self, path):
        """An update disc -- an ISO, or a folder it was extracted to."""
        try:
            disc = UpdateDisc(path)
        except Exception as e:
            messagebox.showerror("Could not open update disc", str(e))
            return
        if self.img:
            self.img.close()
        self.img, self.fw, self.fs, self.disc = None, None, None, disc
        # say which folder it actually opened -- selecting a release inside a
        # disc resolves up to the disc, and silently doing so would confuse
        shown = disc.root or path
        self.lbl.config(text="%s   update disc"
                        % os.path.basename(str(shown).rstrip("/\\")))
        self.plist.delete(*self.plist.get_children())
        defs = disc.definitions()
        for dpath in sorted(defs):
            d = defs[dpath]
            self.plist.insert("", "end", iid=dpath,
                              text=dpath.rsplit("/", 1)[-1],
                              values=(d["systemreleaseid"] or "-",
                                      "%d units" % len(d["units"]),
                                      d["discid"] or ""))
        self._fill_disc(disc)
        self.info.delete("1.0", "end")
        self.info.insert("end", summarise_update(disc))
        self.set_status("Update disc - %d files, %d definitions. Read-only."
                        % (len(disc.files()), len(defs)))

    def _show_definition(self, dpath):
        """A release definition selected in the left pane: who can install it, and what."""
        from .updatedisc import MODULE_ROLE
        d = self.disc.definitions().get(dpath)
        self.info.delete("1.0", "end")
        self.hexv.delete("1.0", "end")
        if not d:
            return
        self.info.insert("end", "%s\n\nSYSTEMRELEASEID  %s\nDISCID           %s\n\n"
                         % (dpath, d["systemreleaseid"] or "-", d["discid"] or "-"))
        if d["units"]:
            self.info.insert("end", "Installs on %d unit%s:\n"
                             % (len(d["units"]), "" if len(d["units"]) == 1 else "s"))
            for u in d["units"]:
                gen = "MOPF" if u["id"][4:6] == "02" else "pre-facelift"
                self.info.insert("end", "  %-14s %s\n" % (u["id"], gen))
            self.info.insert("end", "\nModules:\n")
            for mid in d["units"][0]["modules"]:
                info = d["modules"].get(mid)
                role = MODULE_ROLE.get(info["type"], "") if info else "(not in CONTENTS)"
                nfile = len(info["files"]) if info else 0
                self.info.insert("end", "  %-22s %-34s %d files\n" % (mid, role, nfile))
        else:
            self.info.insert("end", "No CONTROL dispatch entries -- nothing installs "
                                    "from this definition.\n")
        data = self.disc.read(dpath)
        if data:
            self.hexv.insert("end", data.decode("latin-1", "replace"))

    def _fill_disc(self, disc):
        """Discs are a flat path list, so rebuild the folder structure to browse."""
        self.tree.delete(*self.tree.get_children())
        self.nodes = {}
        folders = {"": ""}
        for pth in disc.files():
            parts = [x for x in pth.split("/") if x]
            if not parts:
                continue
            parent, walked = "", ""
            for seg in parts[:-1]:
                walked += "/" + seg
                if walked not in folders:
                    folders[walked] = self.tree.insert(parent, "end", text=seg + "/",
                                                       values=("", ""))
                parent = folders[walked]
            sz = disc.size_of(pth)
            iid = self.tree.insert(parent, "end", text=parts[-1],
                                   values=(human(sz) if sz is not None else "", ""))
            self.nodes[iid] = {"_disc_path": pth, "size": sz or 0}

    def _load_hmi(self, path):
        """An HMI definition: screens and every string the unit can display."""
        try:
            m = Hbm5File(path)
        except Exception as e:
            messagebox.showerror("Could not open HMI definition", str(e))
            return
        if self.img:
            self.img.close()
        self.img, self.fw, self.fs, self.disc = None, None, None, None
        self.hmi = m
        self.lbl.config(text="%s   %s" % (os.path.basename(path), m.describe()))
        self.plist.delete(*self.plist.get_children())
        self.plist.insert("", "end", iid="HBM5", text="HBM5",
                          values=("v%04x" % m.version, human(len(m.data)),
                                  "%d classes" % m.n_schema))
        # Left tree lists translated keys; selecting one shows every language.
        self.tree.delete(*self.tree.get_children())
        self.nodes = {}
        rows = [r for r in m.translations() if len(r[1]) > 1]
        strs = m.strings()
        if rows:
            grp = self.tree.insert("", "end", text="translated keys/",
                                   values=("%d" % len(rows), ""))
            for did, row in rows:
                label = row.get("en_us") or row.get("en_gb") or row.get("de") or ""
                iid = self.tree.insert(grp, "end", text=label[:60] or ("key %d" % did),
                                       values=("%d langs" % len(row), did))
                self.nodes[iid] = {"_hmi_key": did, "_row": row}
        # Screens: the drawable roots, biggest subtree first. Decoding geometry
        # is slow enough to be worth doing once, lazily, on first use.
        self._screens = None
        try:
            from .hbm5geom import Screens
            sc = Screens(m)
            self._screens = sc
            found = sc.screens()
            if found:
                grp = self.tree.insert("", "end", text="screens/",
                                       values=("%d" % len(found), ""))
                for r, n in found:
                    b = sc.box(r)
                    # name a screen by the first label inside it -- the root is
                    # usually an unlabelled container
                    lab = sc.name_of(r) or ""
                    shape = "%dx%d" % (b[2], b[3]) if b else "?"
                    iid = self.tree.insert(
                        grp, "end",
                        text=(lab[:44] or "screen %d" % r) + "  [%s]" % shape,
                        values=("%d elems" % n, r))
                    self.nodes[iid] = {"_hmi_node": r}
        except Exception:
            pass                      # geometry is a bonus; strings still work

        plain = self.tree.insert("", "end", text="strings/",
                                 values=("%d" % len(strs), ""))
        for rid in sorted(strs):
            iid = self.tree.insert(plain, "end", text=strs[rid][:60],
                                   values=(len(strs[rid]), rid))
            self.nodes[iid] = {"_hmi_str": rid, "_text": strs[rid]}
        self.info.delete("1.0", "end")
        self.info.insert("end", summarise_hbm5(m))
        self.set_status("HMI definition - %d strings, %d translated keys. Read-only."
                        % (len(strs), len(rows)))

    def load(self, path):
        # An update disc -- ISO or extracted folder -- before the disk-image path,
        # since an ISO would otherwise be probed for an MBR it does not have.
        if looks_like_update_disc(path):
            self._load_disc(path)
            return
        self.disc = None
        self.hmi = None
        # A folder that nothing recognised must not fall through to DiskImage --
        # it would try to read a directory as a file and report a bare OS error.
        if os.path.isdir(path):
            messagebox.showerror(
                "Not something PCM Explorer can open",
                "%s is a folder, but it does not look like an update disc.\n\n"
                "Open the disc's top folder (the one holding HBUPDATE.DEF or\n"
                "pcm_update.disc), or a release folder inside it.\n\n"
                "For a drive, firmware, persistence or HMI file, use\n"
                "\"Open image...\" and pick the file itself."
                % os.path.basename(path.rstrip("/\\")))
            return
        if looks_like_hbm5(path):
            self._load_hmi(path)
            return
        # Firmware (.ifs) and persistence (.efs) are different containers that
        # browse the same way -- same entry shape, so one branch serves both.
        if looks_like_ifs(path) or looks_like_efs(path):
            try:
                fw = EfsImage(path) if looks_like_efs(path) else FirmwareImage(path)
            except Exception as e:
                messagebox.showerror("Could not open firmware image", str(e))
                return
            if self.img:
                self.img.close()
            self.img, self.fw, self.fs = None, fw, None
            self.lbl.config(text="%s   %s" % (os.path.basename(path), fw.describe()))
            self.plist.delete(*self.plist.get_children())
            self.plist.insert("", "end", iid="IFS", text="IFS",
                              values=(fw.container, human(len(fw.data)), "imagefs"))
            self._fill_firmware(fw)
            self.info.delete("1.0", "end")
            self.info.insert("end", summarise_firmware(fw))
            self.set_status("Firmware image - %d entries. Read-only." % len(fw.entries()))
            return
        try:
            if self.img:
                self.img.close()
            self.img = DiskImage(path)
            self.fw = None
        except Exception as e:
            messagebox.showerror("Could not open image", str(e))
            return
        self.lbl.config(text="%s   %s" % (os.path.basename(path), human(self.img.size)))
        self.plist.delete(*self.plist.get_children())
        self.tree.delete(*self.tree.get_children())
        self.fs, self.nodes, self.salvage, self.cur_part = None, {}, {}, None
        if not self.img.parts:
            self.set_status("No MBR partition table found in this image.")
            return
        for p in self.img.parts:
            fs, _ = self.img.detect_fs(p)
            self.plist.insert("", "end", iid=p["name"], text=p["name"],
                              values=(p["type_name"], human(p["length"]), fs))
        self.set_status("%d partitions — select one to open it."
                        % len(self.img.parts))

    def _fill_firmware(self, fw):
        """Firmware dirents carry full paths, so rebuild folders for display."""
        self.tree.delete(*self.tree.get_children())
        self.nodes = {}
        folders = {"": ""}
        for pth, ent in sorted(fw.entries()):
            parts = [x for x in pth.split("/") if x]
            if not parts:
                continue
            parent, walked = "", ""
            for seg in parts[:-1]:
                walked += "/" + seg
                if walked not in folders:
                    folders[walked] = self.tree.insert(parent, "end", text=seg + "/",
                                                       values=("", ""))
                parent = folders[walked]
            label = parts[-1] + ("@" if is_link(ent) else "")
            size = "" if is_dir(ent) else human(ent["size"])
            iid = self.tree.insert(parent, "end", text=label, values=(size, ent["ino"]))
            self.nodes[iid] = ent

    def show_summary(self):
        """Answer 'what is this image?' rather than just listing files."""
        self.info.delete("1.0", "end")
        try:
            if self.hmi:
                self.info.insert("end", summarise_hbm5(self.hmi))
            elif self.disc:
                self.info.insert("end", summarise_update(self.disc))
            elif self.fw:
                self.info.insert("end", summarise_firmware(self.fw))
            elif self.img:
                self.set_status("Building summary...")
                self.info.insert("end", summarise_disk(self.img))
                self.set_status("Summary ready.")
        except Exception as e:
            self.info.insert("end", "summary failed: %s" % e)

    # -- partition selected --
    def on_part(self, _evt=None):
        sel = self.plist.selection()
        if not sel:
            return
        if self.disc is not None:
            self._show_definition(sel[0])
            return
        if not self.img:
            return
        p = self.img.part(sel[0])
        if not p:
            return
        self.cur_part = p
        fs_label, detail = self.img.detect_fs(p)
        self.info.delete("1.0", "end")
        self.info.insert("end", "%s   type %s (0x%02x)\nbase   %d (0x%x)\nsize   %s\nfs     %s  %s\n"
                         % (p["name"], p["type_name"], p["type"], p["base"], p["base"],
                            human(p["length"]), fs_label, detail))
        self.hexv.delete("1.0", "end")
        self.hexv.insert("end", hexdump(self.img.read(p["base"], 512), p["base"]))
        self.tree.delete(*self.tree.get_children())
        self.nodes, self.salvage = {}, {}
        threading.Thread(target=self._open_part, args=(p,), daemon=True).start()

    def _open_part(self, p):
        self._cancel = False
        fs = self.img.open_fs(p)
        self.fs = fs
        if fs:
            self.set_status("%s — reading filesystem..." % p["name"])
            try:
                tree = fs.walk()
            except Exception as e:
                self.after(0, lambda: self.set_status("%s — read failed: %s" % (p["name"], e)))
                return
            self.after(0, self._fill_fs, p, fs, tree)
        else:
            # No QNX6 superblock: fall back to scavenging directory blocks.
            self.set_status("%s — no superblock; salvage scan..." % p["name"])

            def prog(done, total, n):
                if done % (64 * 1024 * 1024) < 8 * 1024 * 1024:
                    self.set_status("Salvage scan %s — %s of %s, %d directories..."
                                    % (p["name"], human(done), human(total), n))

            dirs = self.img.scan_dirs(p, progress=prog, cancel=lambda: self._cancel)
            self.salvage = dirs
            self.after(0, self._fill_salvage, p, dirs)

    def _fill_fs(self, p, fs, tree):
        """Populate from a properly mounted filesystem."""
        self.tree.delete(*self.tree.get_children())
        self.nodes = {}
        by_path = {pth: i for pth, i in tree}
        items = {}
        for pth, inode in tree:
            if pth == "/":
                iid = self.tree.insert("", "end", text="/", values=("", inode["ino"]), open=True)
                items["/"] = iid
                self.nodes[iid] = inode
                continue
            parent_path = pth.rsplit("/", 1)[0] or "/"
            parent = items.get(parent_path, "")
            name = pth.rsplit("/", 1)[1]
            if is_dir(inode):
                label, size = name + "/", ""
            elif is_link(inode):
                label, size = name + "@", human(inode["size"])
            else:
                label, size = name, human(inode["size"])
            iid = self.tree.insert(parent, "end", text=label,
                                   values=(size, inode["ino"]))
            items[pth] = iid
            self.nodes[iid] = inode
        dirs = sum(1 for _, i in tree if is_dir(i))
        self.set_status("%s — %d entries (%d directories). Filesystem read cleanly."
                        % (p["name"], len(tree), dirs))

    def _fill_salvage(self, p, dirs):
        """Populate from the brute-force scan (no metadata available)."""
        self.tree.delete(*self.tree.get_children())
        self.nodes = {}
        if not dirs:
            self.set_status("%s — no filesystem and no directory blocks found." % p["name"])
            return
        paths = build_paths(dirs)
        roots = [1] if 1 in dirs else [i for i, d in dirs.items() if d["parent"] not in dirs]
        for r in roots[:8]:
            self._add_salvage("", r, "/" if r == 1 else "(root %d)" % r, set(), dirs)
        self.set_status("%s — SALVAGE MODE: %d directories, %d paths. Names only; "
                        "file contents are not available without a superblock."
                        % (p["name"], len(dirs), len(paths)))

    def _add_salvage(self, parent, ino, name, seen, dirs):
        node = self.tree.insert(parent, "end", text=name, values=("", ino))
        if ino in seen:
            return
        seen.add(ino)
        d = dirs.get(ino)
        if not d:
            return
        for nm, cino in sorted(d["kids"]):
            if nm in (".", ".."):
                continue
            if cino in dirs:
                self._add_salvage(node, cino, nm + "/", seen, dirs)
            else:
                self.tree.insert(node, "end", text=nm, values=("", cino))

    # -- selection --
    def _sel(self):
        sel = self.tree.selection()
        if not sel:
            return None, None, None
        iid = sel[0]
        return iid, self.nodes.get(iid), self.tree.item(iid, "text")

    def _path_of(self, iid):
        parts = []
        while iid:
            t = self.tree.item(iid, "text").rstrip("/@")
            if t and t != "/":
                parts.append(t)
            iid = self.tree.parent(iid)
        return "/" + "/".join(reversed(parts))

    def _show_disc_node(self, iid, ent, name):
        """Disc entries are plain paths, not inodes -- preview them directly."""
        pth = ent["_disc_path"]
        self.info.insert("end", "%s\npath   %s\nsize   %s\n"
                         % (name, pth, human(ent["size"])))
        data = self.disc.read(pth) or b""
        if pth.lower().endswith((".def", ".crc32", ".sig", ".cfg", ".txt", ".csv")):
            self.hexv.insert("end", data.decode("latin-1", "replace"))
            return
        txt = None
        try:
            txt = preview(pth, data[:PREVIEW_BYTES])
        except Exception:
            txt = None
        if txt is not None:
            self.hexv.insert("end", txt)
        elif _printable_ratio(data[:512]) > 0.9:
            self.hexv.insert("end", data[:PREVIEW_BYTES].decode("latin-1", "replace"))
        else:
            self.hexv.insert("end", hexdump(data[:PREVIEW_BYTES], 0))

    def on_node(self, _evt=None):
        iid, inode, name = self._sel()
        if iid is None:
            return
        self.info.delete("1.0", "end")
        self.hexv.delete("1.0", "end")
        if self.hmi is not None:
            ent = self.nodes.get(iid)
            if not ent:
                self.info.insert("end", "%s\n\n(group)\n" % name)
                return
            if "_hmi_node" in ent:
                self.info.insert(
                    "end",
                    "screen root %d\n\nUse \"View screen\" to draw its "
                    "elements.\n" % ent["_hmi_node"])
                return
            if "_row" in ent:
                self.info.insert("end", "key %d -- %d languages\n\n"
                                 % (ent["_hmi_key"], len(ent["_row"])))
                for k in sorted(ent["_row"]):
                    self.hexv.insert("end", "%-6s  %s\n" % (k, ent["_row"][k]))
            else:
                self.info.insert("end", "string id %d\n%d characters\n"
                                 % (ent["_hmi_str"], len(ent["_text"])))
                self.hexv.insert("end", ent["_text"])
            return
        if self.disc is not None:
            ent = self.nodes.get(iid)
            if ent and "_disc_path" in ent:
                self._show_disc_node(iid, ent, name)
            else:
                self.info.insert("end", "%s\n\n(folder)\n" % name)
            return
        if inode is None:
            self.info.insert("end", "%s\n\nSalvage mode — this entry's name was recovered\n"
                                    "from a directory block, but without a readable\n"
                                    "superblock its contents cannot be located.\n" % name)
            return
        self.info.insert("end", "%s\npath   %s\ninode  %d\ntype   %s\nsize   %s\n"
                         % (name, self._path_of(iid), inode["ino"],
                            mode_str(inode["mode"]), human(inode["size"])))
        self.info.insert("end", "levels %d   uid %d gid %d\n"
                         % (inode["levels"], inode["uid"], inode["gid"]))
        if is_link(inode):
            try:
                self.info.insert("end", "target %s\n" % (self.fw or self.fs).link_target(inode))
            except Exception:
                pass
            return
        if is_dir(inode):
            try:
                kids = (self.fw or self.fs).dirents(inode)
                self.info.insert("end", "%d entries\n" % len(kids))
            except Exception:
                pass
            return
        try:
            data = (self.fw or self.fs).read_range(inode, 0, PREVIEW_BYTES)
        except Exception as e:
            self.hexv.insert("end", "read failed: %s" % e)
            return
        txt = None
        try:
            txt = preview(self._path_of(iid), data)
        except Exception:
            txt = None
        if self._show_image(data, inode.get("size", len(data))):
            if txt:
                self.hexv.insert("end", "\n" + txt)
            return
        if txt is not None:
            self.hexv.insert("end", txt)
        elif _printable_ratio(data[:512]) > 0.9:
            self.hexv.insert("end", data.decode("latin-1"))
        else:
            self.hexv.insert("end", hexdump(data, 0))

    # -- actions --
    def extract(self):
        iid, inode, name = self._sel()
        if inode is None or not (self.fw or self.fs):
            messagebox.showinfo("Cannot extract",
                                "Select a file in a filesystem that opened cleanly.")
            return
        if is_dir(inode):
            return self.extract_dir()
        dest = filedialog.asksaveasfilename(initialfile=name.rstrip("/@"))
        if not dest:
            return
        try:
            data = (self.fw or self.fs).read_file(inode)
            with open(dest, "wb") as fh:
                fh.write(data)
        except Exception as e:
            messagebox.showerror("Extract failed", str(e))
            return
        self.set_status("Wrote %s (%s)" % (dest, human(len(data))))

    def extract_dir(self):
        iid, inode, name = self._sel()
        if inode is None or not self.fs or not is_dir(inode):
            messagebox.showinfo("Extract folder", "Select a folder first.")
            return
        dest = filedialog.askdirectory(title="Extract into...")
        if not dest:
            return
        root = self._path_of(iid)
        threading.Thread(target=self._extract_dir_worker,
                         args=(inode, root, dest), daemon=True).start()

    def _extract_dir_worker(self, inode, root, dest):
        n = 0
        total = 0
        try:
            for pth, i in self.fs.walk(inode["ino"], root):
                if is_dir(i) or is_link(i):
                    continue
                rel = pth[len(root):].lstrip("/") or ("inode_%d" % i["ino"])
                out = os.path.join(dest, *rel.split("/"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                data = self.fs.read_file(i)
                with open(out, "wb") as fh:
                    fh.write(data)
                n += 1
                total += len(data)
                if n % 10 == 0:
                    self.set_status("Extracted %d files (%s)..." % (n, human(total)))
        except Exception as e:
            self.set_status("Extract stopped after %d files: %s" % (n, e))
            return
        self.set_status("Extracted %d files (%s) to %s" % (n, human(total), dest))

    def export_tree(self):
        if not self.tree.get_children():
            return
        dest = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="listing.txt")
        if not dest:
            return
        lines = []

        def walk(iid, depth):
            for c in self.tree.get_children(iid):
                inode = self.nodes.get(c)
                txt = self.tree.item(c, "text")
                size = human(inode["size"]) if inode and not is_dir(inode) else ""
                lines.append("%-64s %10s %s"
                             % ("  " * depth + txt, size,
                                ("[ino %d]" % inode["ino"]) if inode else ""))
                walk(c, depth + 1)

        walk("", 0)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("# %s  %s\n" % (os.path.basename(self.img.path),
                                     self.cur_part["name"] if self.cur_part else ""))
            fh.write("\n".join(lines))
        self.set_status("Wrote %s (%d lines)" % (dest, len(lines)))

    def verify(self):
        if self.fw:
            res = self.fw.verify()
            txt = "\n".join("[%s] %-18s %s" % ("PASS" if ok else "FAIL", n, d)
                            for n, ok, d in res)
            messagebox.showinfo("Firmware verification", txt)
            return
        if not self.fs:
            messagebox.showinfo("Verify", "This partition did not open as QNX6.")
            return
        self.set_status("Verifying...")
        threading.Thread(target=self._verify_worker, daemon=True).start()

    def _verify_worker(self):
        try:
            res = self.fs.verify()
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Verify failed", str(e)))
            return
        txt = "\n".join("[%s] %-20s %s" % ("PASS" if ok else "FAIL", n, d)
                        for n, ok, d in res)
        allok = all(ok for _, ok, _ in res)
        self.after(0, lambda: messagebox.showinfo(
            "Filesystem verification", txt +
            ("\n\nAll checks passed." if allok else "\n\nSome checks FAILED.")))
        self.after(0, lambda: self.set_status(
            "Verification: %s" % ("all checks passed" if allok else "FAILURES — see dialog")))


def run(initial=None):
    Explorer(initial).mainloop()
