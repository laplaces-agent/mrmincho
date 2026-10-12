# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Fill glyphs that face 0 does not use, whose reference outline equals
their counterpart in an earlier face (rel_outline "same") or is that outline
moved horizontally ("shift"), by copying our traced counterpart, moved by
the reference's offset. Trace settings recorded on the counterpart are
copied too. The counterpart is in face rel_face: the glyph of the same
codepoint there, or for a glyph only layout reaches, the glyph of the same
name with that face's suffix. Reshaped, redrawn and new glyphs are traced
with trace.py --face N.

  derive.py [--font F] [--force]

Never replaces a glyph that already has contours unless --force. A
counterpart that is itself derived is filled first.
"""

import argparse
import csv

import ufoLib2
from fontTools.pens.transformPen import TransformPen

from common import add_font_argument, project
from trace import LIB_KEY


def read_tsv(P, name):
    with open(P.data / name, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    add_font_argument(ap)
    args = ap.parse_args()
    P = project(args.font)

    # All faces share glyf, so reference glyph IDs address outlines alike.
    ref = P.load_reference().fonts[0]
    glyf, order = ref["glyf"], ref.getGlyphOrder()
    rows = {r["glyph"]: r for r in read_tsv(P, "glyphs.tsv")}
    by_cp = {}
    for r in read_tsv(P, "cmap.tsv"):
        by_cp[int(r["face"]), r["codepoint"]] = r["glyph"]
    font = ufoLib2.Font.open(P.ufo)

    def counterpart(g):
        e = int(g["rel_face"])
        if g["layout"]:
            return P.strip_suffix(g["glyph"]) + P.suffixes[e]
        return by_cp[e, g["codepoints"].split()[0]]

    # Derive in face order, so a counterpart derived from an earlier face
    # is filled before the glyphs copied from it.
    todo = sorted((g for g in rows.values() if g["rel_outline"] in ("same", "shift")),
                  key=lambda g: P.codes.index(g["faces"][0]))
    written, kept, untraced = {"same": 0, "shift": 0}, [], []
    for g in todo:
        source_name = counterpart(g)
        source = font[source_name]
        target = font[g["glyph"]]
        if not len(source.contours):
            untraced.append(source_name)
            continue
        if len(target.contours) and not args.force:
            kept.append(g["glyph"])
            continue
        m = glyf[order[int(rows[source_name]["ref_gid"])]].getCoordinates(glyf)[0]
        p = glyf[order[int(g["ref_gid"])]].getCoordinates(glyf)[0]
        dx = p[0][0] - m[0][0]
        target.clearContours()
        source.draw(TransformPen(target.getPen(), (1, 0, 0, 1, dx, 0)))
        if LIB_KEY in source.lib:
            target.lib[LIB_KEY] = dict(source.lib[LIB_KEY])
        else:
            target.lib.pop(LIB_KEY, None)
        written[g["rel_outline"]] += 1
    font.save()
    print(f"ufo: copied {written['same']} same, {written['shift']} shifted"
          + (f"; kept {len(kept)} existing" if kept else "")
          + (f"; source not traced: {' '.join(untraced)}" if untraced else ""))


if __name__ == "__main__":
    main()
