# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Draw a comparison sheet: each glyph rendered from the reference and from a
build by FreeType with the same settings (anti-aliased, unhinted), plus an
overlay where ink only in the reference shows red, ink only in ours blue,
and shared ink black.

  sheet.py BUILT.ttc CHARS... [--font F] [--face N] [--size 96] [--out work/sheet.png]

--hinted renders each font hinted by its own instructions, 1-bit, as at
the sizes where gasp turns smoothing off; tiles are scaled up by --zoom.
"""

import argparse

import numpy as np
from fontTools.ttLib import TTCollection
from PIL import Image, ImageDraw

from common import ROOT, Face, add_font_argument, align, project
from trace import parse_chars


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("built")
    ap.add_argument("chars", nargs="+")
    ap.add_argument("--face", type=int, default=0)
    ap.add_argument("--size", type=int, default=96)
    ap.add_argument("--columns", type=int, default=8)
    ap.add_argument("--hinted", action="store_true")
    add_font_argument(ap)
    ap.add_argument("--zoom", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "work" / "sheet.png"))
    args = ap.parse_args()

    P = project(args.font)
    rf = P.load_reference().fonts[args.face]
    bf = TTCollection(args.built, lazy=True).fonts[args.face]
    rc, rr = rf.getBestCmap(), rf.getReverseGlyphMap()
    bc, br = bf.getBestCmap(), bf.getReverseGlyphMap()
    ref, ours = Face(P.reference, args.face), Face(args.built, args.face)

    zoom = args.zoom if args.hinted else 1
    cell = int(args.size * 1.3) * zoom
    tiles = []
    for cp in parse_chars(args.chars):
        render = (lambda f, g: f.render_hinted_mono(g, args.size)) if args.hinted else (
            lambda f, g: f.render_gray(g, args.size))
        a = render(ref, rr[rc[cp]])
        b = render(ours, br[bc[cp]])
        ca, cb = align(a, b)
        if zoom > 1 and ca.size:
            ca = ca.repeat(zoom, 0).repeat(zoom, 1)
            cb = cb.repeat(zoom, 0).repeat(zoom, 1)
        tile = Image.new("RGB", (cell * 3, cell + 16), "white")
        if ca.size:
            fa, fb = ca.astype(float) / 255, cb.astype(float) / 255
            overlay = np.stack([1 - fb, 1 - np.maximum(fa, fb), 1 - fa], axis=-1)
            for i, img in enumerate((1 - fa, 1 - fb, None)):
                arr = overlay if img is None else np.repeat(img[..., None], 3, axis=-1)
                pic = Image.fromarray((arr * 255).astype(np.uint8))
                tile.paste(pic, (i * cell + (cell - pic.width) // 2, 16 + (cell - pic.height) // 2))
        ImageDraw.Draw(tile).text((4, 2), f"U+{cp:04X}  reference | ours | overlay", fill="black")
        tiles.append(tile)

    cols = min(args.columns, len(tiles))
    rows = -(-len(tiles) // cols)
    sheet = Image.new("RGB", (cols * cell * 3 + (cols - 1) * 8, rows * (cell + 24)), (230, 230, 230))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * (cell * 3 + 8), (i // cols) * (cell + 24)))
    sheet.save(args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
