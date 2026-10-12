# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Freeze what the build needs from the reference into data/, so the build
runs without it.

  snapshot.py [--font F]

  layoutN.ttx    face N's GSUB, GPOS, GDEF, BASE and meta, read with the
                 reference glyph IDs renamed to pool names and subset to the
                 pool; bare coverages in format 3 contextual rules re-sorted
                 into our glyph order
  bitmaps.json   the bitmap manifest add_ms_bitmaps.py reads: the
                 reference's family name, the strike line metrics, and for
                 every bitmap in bitmaps.tsv the reference face and glyph ID
                 that hold it, a codepoint mapped to it in that face (None
                 for glyphs only layout reaches) and a hash of its pixels and
                 metrics, checked against bitmaps.tsv. No pixels.
"""

import argparse
import csv
import json

import freetype
from fontTools import subset
from fontTools.ttLib import TTCollection, TTFont, newTable

from add_ms_bitmaps import bitmap_hash, embedded_bitmap
from common import add_font_argument, project

LAYOUT_TABLES = ("GSUB", "GPOS", "GDEF", "BASE")


def read_tsv(P, name):
    with open(P.data / name, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def layout_font(P, fi, glyph_rows):
    ref = TTCollection(str(P.reference)).fonts[fi]
    pool = {int(r["ref_gid"]): name for name, r in glyph_rows.items()}
    renamed = [pool.get(gid, f"ref{gid:05d}") for gid in range(len(ref.getGlyphOrder()))]
    font = TTFont()
    font.setGlyphOrder(renamed)
    for tag in LAYOUT_TABLES:
        table = newTable(tag)
        table.decompile(ref.reader[tag], font)
        font[tag] = table
    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.layout_scripts = ["*"]
    opts.layout_closure = False
    opts.notdef_outline = True
    opts.glyph_names = True
    opts.drop_tables = []
    s = subset.Subsetter(opts)
    s.populate(glyphs=list(glyph_rows))
    s.subset(font)
    # Coverage must follow our glyph order, not the reference's. Format 3
    # contextual rules hold bare coverages, so sorting them is safe; other
    # subtables are rebuilt from their mappings when compiled.
    gid = {name: i for i, name in enumerate(glyph_rows)}
    for tag in ("GSUB", "GPOS"):
        for lookup in font[tag].table.LookupList.Lookup:
            for st in lookup.SubTable:
                if getattr(st, "Format", None) == 3:
                    for attr in ("InputCoverage", "BacktrackCoverage", "LookAheadCoverage", "Coverage"):
                        for cov in getattr(st, attr, None) or []:
                            cov.glyphs.sort(key=gid.__getitem__)
    meta = newTable("meta")
    meta.decompile(ref.reader["meta"], font)
    font["meta"] = meta
    return font


def bitmap_manifest(P, glyph_rows):
    ref_faces = [freetype.Face(str(P.reference), fi) for fi in range(len(P.faces))]
    codepoint = {}
    for r in read_tsv(P, "cmap.tsv"):
        codepoint.setdefault((int(r["face"]), r["glyph"]), int(r["codepoint"][2:], 16))
    bitmaps = []
    for r in read_tsv(P, "bitmaps.tsv"):
        name, ppem = r["glyph"], int(r["ppem"])
        fi = P.codes.index(glyph_rows[name]["faces"][0])
        gid = int(glyph_rows[name]["ref_gid"])
        rows, left, top, advance = embedded_bitmap(ref_faces[fi], gid, ppem)
        if ((len(rows[0]) if rows else 0), len(rows), left, top, advance) != (
                int(r["width"]), int(r["height"]), int(r["left"]), int(r["top"]), int(r["advance"])):
            raise SystemExit(f"{name}@{ppem}: reference bitmap does not match bitmaps.tsv")
        bitmaps.append([name, ppem, fi, gid, codepoint.get((fi, name)),
                        bitmap_hash(rows, left, top, advance)])
    strikes = [s for s in read_tsv(P, "strikes.tsv") if int(s["ppemX"]) in {b[1] for b in bitmaps}]
    family = P.load_reference().fonts[0]["name"].getDebugName(1)
    return {"family": family, "strikes": strikes, "bitmaps": bitmaps}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_font_argument(ap)
    P = project(ap.parse_args().font)
    glyph_rows = {r["glyph"]: r for r in read_tsv(P, "glyphs.tsv")}
    for fi in range(len(P.faces)):
        path = P.data / f"layout{fi}.ttx"
        layout_font(P, fi, glyph_rows).saveXML(str(path), tables=[*LAYOUT_TABLES, "meta"])
        print(f"wrote {path}")
    path = P.data / "bitmaps.json"
    path.write_text(json.dumps(bitmap_manifest(P, glyph_rows), ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
