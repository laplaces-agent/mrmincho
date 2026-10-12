# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Create a project's UFO (its ufo path in common.py): the glyph pool all
faces share, one glyph per
row of the manifest's glyphs.tsv, with the reference advance and no
outlines. Refuses to overwrite an existing UFO.

  mkufo.py [--font F] [--update]

--update adds the manifest glyphs an existing UFO lacks, sets every glyph's
width to its manifest advance and the glyph order to the manifest's, and
leaves outlines alone.

Each glyph's unicodes are its face 0 codepoints (glyphs only later faces
use have none); every face's character map is cmap.tsv, applied at build
time.
"""

import argparse
import csv
import sys

import ufoLib2

from common import UPEM, add_font_argument, project


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true")
    add_font_argument(ap)
    args = ap.parse_args()
    P = project(args.font)
    path = P.ufo
    rows = list(csv.DictReader(open(P.data / "glyphs.tsv", encoding="utf-8"), delimiter="\t"))
    face0_cps = {}
    for r in csv.DictReader(open(P.data / "cmap.tsv", encoding="utf-8"), delimiter="\t"):
        if r["face"] == "0":
            face0_cps.setdefault(r["glyph"], []).append(int(r["codepoint"][2:], 16))
    if args.update:
        update(path, rows, face0_cps)
        return
    if path.exists():
        sys.exit(f"{path} exists; not overwriting")

    font = ufoLib2.Font()
    info = font.info
    info.familyName = P.names[0][0]
    info.styleName = "Regular"
    info.unitsPerEm = UPEM
    info.ascender = 220
    info.descender = -36
    info.capHeight = 175
    info.xHeight = 116
    info.postscriptUnderlinePosition = -24
    info.postscriptUnderlineThickness = 12

    for r in rows:
        g = font.newGlyph(r["glyph"])
        g.width = int(r["advance"])
        g.unicodes = sorted(face0_cps.get(r["glyph"], []))
    font.glyphOrder = [r["glyph"] for r in rows]

    path.parent.mkdir(parents=True, exist_ok=True)
    font.save(path)
    print(f"wrote {path} with {len(rows)} glyphs")


def update(path, rows, face0_cps):
    font = ufoLib2.Font.open(path)
    added, rewidthed = [], []
    for r in rows:
        name = r["glyph"]
        if name not in font:
            g = font.newGlyph(name)
            g.unicodes = sorted(face0_cps.get(name, []))
            added.append(name)
        if font[name].width != int(r["advance"]):
            font[name].width = int(r["advance"])
            rewidthed.append(name)
    extra = [n for n in font.keys() if n not in {r["glyph"] for r in rows}]
    if extra:
        sys.exit(f"UFO glyphs not in the manifest: {' '.join(extra)}")
    font.glyphOrder = [r["glyph"] for r in rows]
    font.save()
    print(f"added {len(added)} glyphs; widths set on {len(rewidthed)}: {' '.join(rewidthed)}")


if __name__ == "__main__":
    main()
