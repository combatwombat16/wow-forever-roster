#!/usr/bin/env python3
"""Phase F alignment probe: where the FIGURE of each doll silhouette actually lands.

Owner-authored class art (assets/silhouettes/*.svg) is 512x512 SQUARE, while generic.svg is
300x400 — and a CSS `background-size: contain` maps a square image inside the (at its widest)
420x420 doll box differently than it maps a 3:4 portrait. This probe measures, per class:

  raw   — the figure bbox under the bare `.wow-doll-figure` rule (generic mapping: contain,
          centred), and
  tuned — the figure bbox under the class rule AS COMMITTED in css/app.css (the page loads the
          real stylesheet; only the box geometry is set inline), so the numbers gate on what
          the browser will actually paint, never on a table in a script.

The figure is the set of pixels whose RGB is exactly #0a0a0a (the owner's single figure fill).
Exact matching is deliberate: the art's ground shadow is #0d0a07, an OPAQUE band near the
feet, and any tolerance window ≥ 3 absorbs it and reports the ground line as "feet".

Rendering is chrome-headless-shell (the playwright-bundled binary this repo's UI gates
already rely on being present); pixels are read with PIL. The probe writes JSON to stdout:

  {"boxes": {"ref": [W, H], "floor": [W, H]},
   "slugs": {"<slug>": {"raw": {"ref": {"top": y%, "bot": y%, "covered": bool}, ...},
                        "tuned": {...} or null if app.css has no is-<slug> rule}}}

y% is of the FIGURE positioning-area height (the doll's padding box — what background
positioning and the sockets' top:% resolve against). `--ref-only` skips the floor renders.

Usage:  python3 .pi/scripts/probe_doll_art.py [--ref-only] [--json OUTFILE]
Override the browser binary with DOLL_CHROME=/path/to/chrome-headless-shell.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

try:
    from PIL import Image
except ImportError:  # pragma: cover
    print("probe_doll_art.py needs Pillow (pip install pillow)", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SLUGS = ["druid", "hunter", "mage", "paladin", "priest", "rogue", "shaman", "warlock", "warrior"]
FIGURE_RGB = (10, 10, 10)  # #0a0a0a — the owner's figure fill, matched exactly


def find_chrome():
    env = os.environ.get("DOLL_CHROME")
    if env:
        return env
    candidates = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))
    return candidates[-1] if candidates else None


def doll_padding_box(css_text):
    """The reference (max-width) and floor (--doll-supported-min-w) padding boxes of .wow-doll.

    Derived from the committed stylesheet like verify_search_detail.mjs derives its box-model
    gate, so the probe cannot drift from the doll by keeping a remembered constant of its own.
    """
    body = re.search(r"\.wow-doll\s*\{([^{}]*)\}", css_text, re.S).group(1)
    body = re.sub(r"/\*[\s\S]*?\*/", "", body)
    height = float(re.search(r"height:\s*([\d.]+)px", body).group(1))
    max_w = float(re.search(r"max-width:\s*([\d.]+)px", body).group(1))
    floor = float(re.search(r"--doll-supported-min-w:\s*([\d.]+)px", body).group(1))
    border = float(re.search(r"border:\s*([\d.]+)px", body).group(1))
    return {"ref": [max_w - 2 * border, height - 2 * border],
            "floor": [floor - 2 * border, height - 2 * border]}


def page_html(slug, tuned, w, h):
    # The tuned pass is a check of the STYLESHEET: the class and is-<slug> token come from the
    # committed app.css, and inline styles set ONLY the geometry of the positioning area. The
    # raw pass cannot use the stylesheet at all — the bare `.wow-doll-figure` paints generic.svg,
    # not this class's art — so it reproduces the base rule's mapping (no-repeat, centre,
    # contain) inline over the class file and measures where the ART lands before any tuning.
    geo = ("position:relative;inset:auto;width:" f"{w}px;height:{h}px")
    if tuned:
        cls, paint = f"wow-doll-figure is-{slug}", ""
    else:
        cls = "wow-doll-figure"
        art = f"file://{os.path.join(ROOT, 'assets', 'silhouettes', slug + '.svg')}"
        paint = (f"background-image:url('{art}');background-repeat:no-repeat;"
                 "background-position:center;background-size:contain")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="file://{os.path.join(ROOT, 'css', 'app.css')}">
<style>html,body{{margin:0;padding:0;background:#fff;overflow:hidden}}
.f{{{geo}}}</style>
</head><body><div class="{cls} f" style="{paint}"></div></body></html>"""


def figure_bbox(png_path):
    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    px = im.load()
    ys = [y for y in range(h) if any(px[x, y] == FIGURE_RGB for x in range(w))]
    if not ys:
        return None
    # All four page-white corners mean the art does not cover the box (a navy/white gap under
    # the figure). A covered box has warm backdrop at every corner at the reference square.
    corner_white = all(px[c] == (255, 255, 255) for c in
                       [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)])
    return ys[0], ys[-1], w, h, not corner_white


def measure(chrome, slug, tuned, box, tmpdir, tag):
    html = os.path.join(tmpdir, f"{tag}.html")
    png = os.path.join(tmpdir, f"{tag}.png")
    with open(html, "w") as fh:
        fh.write(page_html(slug, tuned, int(box[0]), int(box[1])))
    subprocess.run([chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--virtual-time-budget=1500",
                    f"--window-size={int(box[0])},{int(box[1])}",
                    f"--screenshot={png}", f"file://{html}"],
                   capture_output=True, check=True)
    got = figure_bbox(png)
    if got is None:
        return None
    top, bot, w, h, covered = got
    return {"top": round(top / h * 100, 2), "bot": round((bot + 1) / h * 100, 2),
            "covered": covered, "px": [int(w), int(h)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-only", action="store_true", help="skip the floor-box renders")
    ap.add_argument("--json", help="also write the JSON to this file")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome or not os.path.exists(chrome):
        print("chrome-headless-shell not found (set DOLL_CHROME=/path/to/binary)", file=sys.stderr)
        sys.exit(2)

    with open(os.path.join(ROOT, "css", "app.css")) as fh:
        css = fh.read()
    boxes = doll_padding_box(css)
    modes = ["ref"] + ([] if args.ref_only else ["floor"])

    out = {"chrome": chrome, "boxes": boxes, "slugs": {}}
    with tempfile.TemporaryDirectory() as tmpdir:
        for slug in SLUGS:
            entry = {"raw": {}, "tuned": None}
            for mode in modes:
                entry["raw"][mode] = measure(chrome, slug, False, boxes[mode], tmpdir, f"raw-{slug}-{mode}")
            if re.search(r"\.wow-doll-figure\.is-" + slug + r"\b", css):
                entry["tuned"] = {}
                for mode in modes:
                    entry["tuned"][mode] = measure(chrome, slug, True, boxes[mode], tmpdir, f"tuned-{slug}-{mode}")
            out["slugs"][slug] = entry

    text = json.dumps(out, indent=1)
    if args.json:
        with open(args.json, "w") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
