# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Build a family's font collection from its sources.

  build.py [--font F] [--out PATH]
           [--bitmaps none|copy|hinted|unhinted] [--no-hinting]

The collection goes to the project's out path (work/mrmincho.ttc,
work/mrgoth.ttc), with its bitmap manifest beside it (.bitmaps.json in place
of .ttc), which add_ms_bitmaps.py uses to add Microsoft's bitmaps to a copy.
Only
data/ and the UFO are read; the reference font is not needed, except by
--bitmaps copy. All faces share
one glyph pool (glyph order: the manifest's glyphs.tsv). A table whose
content comes out the same in every face is stored once in the collection,
as the reference stores its shared tables.

  outlines        the UFO (quadratic contours)
  advances        glyphs.tsv (the reference's; the UFO must agree)
  vertical        glyphs.tsv vadvance, and vorigin, the reference's
                  vertical origin height, from which each top side bearing
                  is computed against our outline
  character maps  cmap.tsv
  face tables     faceN.ttx: head, hhea, maxp, OS/2, post, gasp and vhea
                  keep the reference's values; name is the project's own
  layout          layoutN.ttx: GSUB, GPOS, GDEF, BASE and meta from the
                  reference face, mapped to the pool by snapshot.py
  LTSH            in the faces whose reference has one (the proportional
                  ones, the project's ltsh_faces): every glyph 1, as in the
                  reference
  hinting         the reference hints nearly every glyph with its own
                  TrueType programs, which address its points and cannot
                  carry over to traced outlines. ttfautohint hints face 0
                  instead (glyphs face 0 does not map fall back to Latin);
                  all faces share the result, glyf, fpgm, prep and cvt, as
                  the reference shares its own, and gasp stays the
                  reference's
  bitmaps         none by default: the released fonts carry only our own
                  work. The manifest (bitmaps.json, from snapshot.py) lists
                  every bitmap in bitmaps.tsv with the reference glyph that
                  holds it and a hash of its pixels and metrics; the build
                  copies it next to the collection. Only --bitmaps copy
                  needs the reference font: it adds the reference's through
                  add_ms_bitmaps.py, exactly as a user would; hinted and
                  unhinted render our own outlines 1-bit at each strike's
                  size, in the reference's box grown to take any ink outside
                  it, with the reference's advance; all with the strike line
                  metrics from strikes.tsv
"""

import argparse
import csv
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np

import ufoLib2
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from add_ms_bitmaps import bitmap_xml, patch
from common import Face, add_font_argument, project

REVISION = 0.1
LICENSE = ("This Font Software is licensed under the SIL Open Font License, Version 1.1. "
           "This license is available with a FAQ at: https://openfontlicense.org")
LICENSE_URL = "https://openfontlicense.org"
VERSION = f"Version {REVISION}"


def read_tsv(P, name):
    with open(P.data / name, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def build_outlines(P, order, glyph_rows):
    ufo = ufoLib2.Font.open(P.ufo)
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    metrics, vmetrics = {}, {}
    for name in order:
        pen = TTGlyphPen(None)
        ufo[name].draw(pen)
        g = pen.glyph()
        glyf.glyphs[name] = g
        row = glyph_rows[name]
        advance = int(row["advance"])
        if ufo[name].width != advance:
            raise SystemExit(f"{name}: UFO width {ufo[name].width} != reference advance {advance}")
        g.recalcBounds(glyf)
        metrics[name] = (advance, g.xMin if g.numberOfContours else 0)
        vorigin = int(row["vorigin"])
        vmetrics[name] = (int(row["vadvance"]), vorigin - g.yMax if g.numberOfContours else vorigin)
    hmtx = newTable("hmtx")
    hmtx.metrics = metrics
    vmtx = newTable("vmtx")
    vmtx.metrics = vmetrics
    return glyf, hmtx, vmtx


def build_cmap(mapping):
    cmap = newTable("cmap")
    cmap.tableVersion = 0
    cmap.tables = []
    for fmt, pid, eid in ((4, 3, 1), (12, 3, 10)):
        st = CmapSubtable.newSubtable(fmt)
        st.platformID, st.platEncID, st.language = pid, eid, 0
        st.cmap = dict(mapping)
        cmap.tables.append(st)
    return cmap


def build_name(P, fi):
    """Names in the reference's layout, except that the reference repeats
    every Windows record under encoding 10 as well: 1-6 in Macintosh
    English, and 0-6 in Windows English and Japanese (encoding 1); the
    license (13) and its URL (14) in Windows English. Encoding-10 copies add
    nothing (the strings are UTF-16 either way) and some tools decode them
    as UCS-4, which garbles them."""
    family, family_ja, ps = P.names[fi]
    name = newTable("name")
    name.names = []
    english = {0: P.copyright, 1: family, 2: "Regular", 3: f"{ps};{REVISION}", 4: family, 5: VERSION, 6: ps}
    japanese = {**english, 1: family_ja, 2: "標準", 4: family_ja}
    for nid, value in english.items():
        if nid:
            name.setName(value, nid, 1, 0, 0)
    for lang, values in ((0x409, english), (0x411, japanese)):
        for nid, value in values.items():
            name.setName(value, nid, 3, 1, lang)
    name.setName(LICENSE, 13, 3, 1, 0x409)
    name.setName(LICENSE_URL, 14, 3, 1, 0x409)
    return name


def rendered_bitmaps(P, path, order, hinted):
    """Each bitmap in bitmaps.tsv rendered 1-bit from the outlines in the
    built font at path, as bitmap_xml wants it: the reference's box grown
    to hold all the ink, the render placed in it by the glyph origin, and
    the reference's advance."""
    face = Face(path, 0)
    gid = {n: i for i, n in enumerate(order)}
    render = face.render_hinted_mono if hinted else face.render_outline
    out = []
    for r in read_tsv(P, "bitmaps.tsv"):
        px, left, top = render(gid[r["glyph"]], int(r["ppem"]))[:3]
        bl, bt = int(r["left"]), int(r["top"])
        br, bb = bl + int(r["width"]), bt - int(r["height"])
        if px.any():
            ys, xs = np.nonzero(px)
            bl, br = min(bl, left + xs.min()), max(br, left + xs.max() + 1)
            bt, bb = max(bt, top - ys.min()), min(bb, top - ys.max() - 1)
        box = np.zeros((bt - bb, br - bl), bool)
        for y, x in zip(*np.nonzero(px)):
            box[bt - top + y, left + x - bl] = True
        rows = ["".join("#" if v else "." for v in row) for row in box]
        out.append((r["glyph"], int(r["ppem"]), rows, int(bl), int(bt), int(r["advance"])))
    return out


def autohint(path):
    import io

    import ttfautohint

    data = ttfautohint.ttfautohint(in_buffer=path.read_bytes(), fallback_script="latn",
                                   hinting_limit=0, no_info=True)
    return TTFont(io.BytesIO(data))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="default: the project's out path in common.py")
    ap.add_argument("--bitmaps", choices=("none", "copy", "hinted", "unhinted"), default="none",
                    help="none (release), the reference's, or our outlines rendered")
    ap.add_argument("--no-hinting", action="store_true")
    add_font_argument(ap)
    args = ap.parse_args()
    P = project(args.font)
    nfaces = len(P.faces)

    glyph_rows = {r["glyph"]: r for r in read_tsv(P, "glyphs.tsv")}
    order = list(glyph_rows)
    if order[0] != ".notdef":
        raise SystemExit("glyphs.tsv must start with .notdef")
    glyf, hmtx, vmtx = build_outlines(P, order, glyph_rows)
    cmaps = [{} for _ in range(nfaces)]
    for r in read_tsv(P, "cmap.tsv"):
        cmaps[int(r["face"])][int(r["codepoint"][2:], 16)] = r["glyph"]

    with tempfile.TemporaryDirectory() as tmp:
        fonts = []
        for fi in range(nfaces):
            font = TTFont(recalcBBoxes=True, recalcTimestamp=True)
            font.importXML(str(P.data / f"face{fi}.ttx"))
            font.setGlyphOrder(order)
            font["glyf"], font["hmtx"], font["vmtx"] = glyf, hmtx, vmtx
            font.importXML(str(P.data / f"layout{fi}.ttx"))
            if fi in P.ltsh_faces:
                ltsh = newTable("LTSH")
                ltsh.yPels = {name: 1 for name in order}
                font["LTSH"] = ltsh
            font["loca"] = newTable("loca")
            font["cmap"] = build_cmap(cmaps[fi])
            font["name"] = build_name(P, fi)
            post = font["post"]
            post.formatType, post.extraNames, post.mapping = 2.0, [], {}
            font["OS/2"].achVendID = "NONE"
            # Installable embedding: the OFL places no restriction on it.
            font["OS/2"].fsType = 0
            font["head"].fontRevision = REVISION
            fonts.append(font)

        # Compile each face once so derived fields (maxp, hhea extremes,
        # head bounds) are filled in identically before the tables are shared.
        compiled = []
        for fi, font in enumerate(fonts):
            path = Path(tmp, f"face{fi}.ttf")
            font.save(str(path))
            compiled.append(TTFont(str(path)))
        if not args.no_hinting:
            hinted = autohint(Path(tmp, "face0.ttf"))
            for fi, font in enumerate(compiled):
                for tag in ("glyf", "fpgm", "prep", "cvt "):
                    font[tag] = hinted[tag]
                for field in ("maxZones", "maxTwilightPoints", "maxStorage", "maxFunctionDefs",
                              "maxInstructionDefs", "maxStackElements", "maxSizeOfInstructions"):
                    setattr(font["maxp"], field, getattr(hinted["maxp"], field))
                path = Path(tmp, f"face{fi}.hinted.ttf")
                font.save(str(path))
                compiled[fi] = TTFont(str(path))
        out = Path(args.out) if args.out else P.out
        out.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = out.with_suffix(".bitmaps.json")
        shutil.copyfile(P.data / "bitmaps.json", manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ttc = TTCollection()
        ttc.fonts = compiled
        if args.bitmaps == "none":
            ttc.save(str(out), shareTables=True)
        else:
            release = Path(tmp, "release.ttc")
            ttc.save(str(release), shareTables=True)
            if args.bitmaps == "copy":
                count, missing, _ = patch(release, P.reference, out, manifest_path)
                if missing:
                    raise SystemExit(f"{len(missing)} reference bitmaps did not match the manifest")
                print(f"bitmaps: {count} copied from the reference by add_ms_bitmaps.py")
            else:
                bitmaps = rendered_bitmaps(P, release, order, args.bitmaps == "hinted")
                xml_path = Path(tmp, "bitmaps.ttx")
                xml_path.write_text(bitmap_xml(order, manifest["strikes"], bitmaps), encoding="utf-8")
                fonts = []
                for fi in range(nfaces):
                    font = TTFont(str(release), fontNumber=fi)
                    font.importXML(str(xml_path))
                    fonts.append(font)
                ttc = TTCollection()
                ttc.fonts = fonts
                ttc.save(str(out), shareTables=True)
                print(f"bitmaps: {len(bitmaps)} rendered from our outlines ({args.bitmaps})")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
