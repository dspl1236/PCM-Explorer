"""A single shareable HTML page describing an image.

Someone posts "here is my unit, what is wrong with it" and attaches twelve
screenshots. This produces one file instead: what the image is, whether it read
cleanly, what software it runs, and -- if a factory baseline is available --
what on it is not stock.

Self-contained. Images are inlined as data URIs, so the file can be attached to
a forum post or an email and still work.
"""
import base64
import html
import os

from .core import DiskImage, human, is_dir, is_link
from .decode import summarise_disk, summarise_firmware
from .images import identify

CSS = """
:root{--bg:#0a0a0c;--surface:#131318;--surface2:#1a1a22;--border:#2a2a35;
--text:#e8e8ed;--text2:#8888a0;--accent:#c8a44e;--green:#4eca7a;--red:#e85454}
*{box-sizing:border-box}
body{margin:0;padding:32px;background:var(--bg);color:var(--text);
font:14px/1.55 "DM Sans",-apple-system,Segoe UI,sans-serif}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font:600 11px/1 ui-monospace,Consolas,monospace;letter-spacing:2px;
text-transform:uppercase;color:var(--accent);margin:34px 0 12px}
.sub{color:var(--text2);margin-bottom:8px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:18px 22px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border)}
th{color:var(--text2);font-weight:600;font-size:11px;letter-spacing:1px;
text-transform:uppercase}
tr:last-child td{border-bottom:none}
pre{background:var(--bg);border:1px solid var(--border);border-radius:8px;
padding:14px;overflow-x:auto;font:12px/1.5 ui-monospace,Consolas,monospace;
color:var(--text)}
.ok{color:var(--green)} .bad{color:var(--red)} .dim{color:var(--text2)}
.shots{display:flex;flex-wrap:wrap;gap:10px}
.shots figure{margin:0;width:200px}
.shots img{width:200px;border:1px solid var(--border);border-radius:6px;display:block}
.shots figcaption{font-size:11px;color:var(--text2);margin-top:4px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
footer{color:var(--text2);font-size:12px;margin-top:36px;
border-top:1px solid var(--border);padding-top:14px}
"""


def _esc(s):
    return html.escape(str(s), quote=False)


def _thumb(data, mime):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def build(path, out_path, baseline=None, max_shots=12):
    """Write an HTML report for ``path``.  Returns the output path."""
    from . import version_string
    parts = []
    name = os.path.basename(path)

    parts.append("<h1>%s</h1>" % _esc(name))
    parts.append('<div class="sub">%s</div>' % _esc(human(os.path.getsize(path))
                                                   if os.path.isfile(path) else path))

    img = DiskImage(path)
    parts.append("<h2>Summary</h2><div class='card'><pre>%s</pre></div>"
                 % _esc(summarise_disk(img)))

    # -- partitions and whether each read cleanly ------------------------
    rows = []
    shots = []
    for p in img.parts:
        fslabel, detail = img.detect_fs(p)
        fs = img.open_fs(p)
        status, nfiles = "<span class='dim'>not mounted</span>", ""
        if fs:
            try:
                walked = fs.walk()
                nfiles = str(len(walked))
                bad = [n for n, ok, _d in fs.verify() if not ok]
                status = ("<span class='ok'>read cleanly</span>" if not bad
                          else "<span class='bad'>%s</span>" % _esc(", ".join(bad)))
                # collect bootscreens for the gallery
                for pth, ent in walked:
                    if len(shots) >= max_shots or is_dir(ent) or is_link(ent):
                        continue
                    if "bootscreen" not in pth.lower():
                        continue
                    data = fs.read_file(ent)
                    what = identify(data)
                    if not what:
                        continue
                    mime = {"PNG": "image/png", "JPEG": "image/jpeg",
                            "GIF": "image/gif"}.get(what[0])
                    if mime:
                        shots.append((pth, _thumb(data, mime),
                                      "%s %dx%d" % (what[0], what[1], what[2])))
            except Exception as e:
                status = "<span class='bad'>%s</span>" % _esc(e)
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                    "<td>%s</td><td>%s</td></tr>"
                    % (_esc(p["name"]), _esc(p["type_name"]),
                       _esc(human(p["length"])), _esc(fslabel), nfiles, status))
    parts.append("<h2>Partitions</h2><div class='card'><table>"
                 "<tr><th>part</th><th>type</th><th>size</th><th>filesystem</th>"
                 "<th>files</th><th>integrity</th></tr>%s</table></div>"
                 % "".join(rows))

    # -- what is not stock ------------------------------------------------
    if baseline:
        try:
            from .diffimg import compare, find_baseline, open_side
            mine = open_side(path)
            label, base = (find_baseline(baseline, against=mine)
                           if os.path.isdir(baseline) or baseline.lower().endswith(".iso")
                           else (baseline, open_side(baseline)))
            if base is not None:
                added, removed, changed, same, _bn = compare(base, mine)
                parts.append(
                    "<h2>Against factory</h2><div class='card'>"
                    "<div class='sub'>baseline: %s</div><table>"
                    "<tr><th>identical</th><th>modified</th><th>added</th>"
                    "<th>missing</th></tr><tr><td>%d</td><td>%d</td><td>%d</td>"
                    "<td>%d</td></tr></table>" % (_esc(label), same, len(changed),
                                                  len(added), len(removed)))
                if changed:
                    parts.append("<pre>%s</pre>" % _esc(
                        "\n".join("%-44s %8d -> %d" % (c[0][:44], c[1], c[3])
                                  for c in changed[:40])))
                parts.append("</div>")
        except Exception as e:
            parts.append("<h2>Against factory</h2><div class='card'>"
                         "<span class='bad'>comparison failed: %s</span></div>"
                         % _esc(e))

    if shots:
        parts.append("<h2>Bootscreens</h2><div class='card'><div class='shots'>%s</div></div>"
                     % "".join('<figure><img src="%s" alt=""><figcaption>%s<br>%s'
                               '</figcaption></figure>'
                               % (src, _esc(p.rsplit("/", 1)[-1]), _esc(meta))
                               for p, src, meta in shots))

    parts.append("<footer>Generated by PCM Explorer %s &mdash; read-only; the "
                 "image was not modified.<br>A head-unit drive contains the VIN, "
                 "saved destinations, phone pairings and call history. Check this "
                 "report before sharing it.</footer>" % _esc(version_string()))

    doc = ("<!doctype html><meta charset='utf-8'><title>%s &mdash; PCM Explorer</title>"
           "<style>%s</style><div class='wrap'>%s</div>"
           % (_esc(name), CSS, "".join(parts)))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    img.close()
    return out_path
