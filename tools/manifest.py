# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Write the project manifest from the reference font into its data/.

  cmap.tsv     face, codepoint -> glyph, for every in-scope codepoint
  glyphs.tsv   one row per glyph in the shared pool: metrics, outline stats,
               how a PMincho glyph relates to Mincho's, bitmap coverage
  bitmaps.tsv  one row per glyph per strike: embedded bitmap metrics, and
               image_from: the strike whose image this bitmap reuses (the
               reference shares one image between neighbouring strikes)
  strikes.tsv  per strike 10-22: the EBLC line metrics and flags
  face0.ttx, face1.ttx
               the reference's face-level tables, for the build to copy

  manifest.py [--font F]

Glyph identity follows the reference: each reference glyph ID used by an
in-scope codepoint becomes one glyph. A glyph face 0 uses is named after
its codepoint (uni0041); one only later faces use takes the suffix of the
first face that uses it (".p" for PMincho; ".ui", ".p" for the Gothic
family's UI and P faces).

The pool also holds .notdef and every glyph the reference's GSUB reaches
from those (vertical forms, ccmp ligatures and contextual forms). Those have
no codepoint; they are named after their input glyphs and feature
(uni2010.vert, uni0254_uni0300; with a face suffix again when face 0 does
not reach them),
and the layout column records how they are reached.
"""

import argparse
import csv
import unicodedata

from common import STRIKES, Face, add_font_argument, block_name, char_label, in_scope, project

LINE_FIELDS = ["ascender", "descender", "widthMax", "caretSlopeNumerator", "caretSlopeDenominator",
               "caretOffset", "minOriginSB", "minAdvanceSB", "maxBeforeBL", "minAfterBL", "pad1", "pad2"]
FACE_TABLES = ["head", "hhea", "maxp", "OS/2", "post", "gasp", "vhea"]


def outline(glyf, name):
    g = glyf[name]
    if g.numberOfContours == 0:
        return None
    coords, ends, flags = g.getCoordinates(glyf)
    return [tuple(p) for p in coords], list(ends), [f & 1 for f in flags], g.isComposite()


def relation(m, p):
    """How an outline p relates to its counterpart m in an earlier face:
    same, shift (moved horizontally only), reshape (same point structure,
    points moved), redraw (different structure), empty."""
    if m is None and p is None:
        return "empty"
    if m is None or p is None or m[1] != p[1] or m[2] != p[2]:
        return "redraw"
    dx = {b[0] - a[0] for a, b in zip(m[0], p[0])}
    dy = {b[1] - a[1] for a, b in zip(m[0], p[0])}
    if dx == {0} and dy == {0}:
        return "same"
    if len(dx) == 1 and dy == {0}:
        return "shift"
    return "reshape"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_font_argument(ap)
    P = project(ap.parse_args().font)
    P.data.mkdir(parents=True, exist_ok=True)
    fonts = P.load_reference().fonts
    nfaces = len(fonts)
    cmaps = [f.getBestCmap() for f in fonts]
    revs = [f.getReverseGlyphMap() for f in fonts]
    glyf = fonts[0]["glyf"]
    hmtx = fonts[0]["hmtx"].metrics
    order = fonts[0].getGlyphOrder()

    # Reference GID -> {face: [codepoints]}.
    users = {}
    for fi, cmap in enumerate(cmaps):
        for cp, gname in cmap.items():
            if in_scope(cp):
                users.setdefault(revs[fi][gname], {}).setdefault(fi, []).append(cp)

    names = {}
    for gid, by_face in users.items():
        fi = min(by_face)
        names[gid] = P.glyph_name(min(by_face[fi]), fi)
    layout = {0: "notdef"}
    names[0] = ".notdef"
    users[0] = {fi: [] for fi in range(nfaces)}
    for gid, (name, desc, by_face) in layout_extras(P, fonts, revs, names).items():
        names[gid], layout[gid] = name, desc
        users[gid] = {fi: [] for fi in by_face}
    assert len(set(names.values())) == len(names), "glyph name collision"
    by_name = {n: g for g, n in names.items()}
    vmtx = fonts[0]["vmtx"].metrics

    cmap_rows = []
    for fi in range(nfaces):
        for cp in sorted(c for c in cmaps[fi] if in_scope(c)):
            gid = revs[fi][cmaps[fi][cp]]
            cmap_rows.append([fi, f"U+{cp:04X}", char_label(cp), block_name(cp), names[gid], gid])

    faces = [Face(P.reference, fi) for fi in range(nfaces)]
    image_from = shared_images(fonts)
    bitmap_rows, has_bitmap = [], {}
    for gid in sorted(users):
        fi = min(users[gid])
        for ppem in STRIKES:
            bm = faces[fi].embedded_bitmap(gid, ppem)
            if bm is None:
                continue
            px, left, top, adv = bm
            h, w = px.shape
            bitmap_rows.append([names[gid], ppem, w, h, left, top, adv, int(px.sum()), image_from[fi][gid, ppem]])
            has_bitmap.setdefault(gid, []).append(ppem)

    glyph_rows = []
    for gid in sorted(users, key=lambda g: (g != 0, g in layout, min(users[g]), names[g])):
        by_face = users[gid]
        gname = order[gid]
        o = outline(glyf, gname)
        g = glyf[gname]
        adv, lsb = hmtx[gname]
        faces_col = P.faces_code(by_face)
        cps = sorted({cp for cpl in by_face.values() for cp in cpl})

        # A glyph face 0 does not use is compared with its counterpart (the
        # glyph of the same codepoint, or of the same layout name) in each
        # earlier face; the first that is the same outline, or the same
        # moved horizontally, is recorded, else face 0's.
        rel_face = rel_outline = rel_advance = ""
        if 0 not in by_face:
            first = min(by_face)
            found = []
            for e in range(first):
                if gid in layout:
                    egid = by_name.get(P.strip_suffix(names[gid]) + P.suffixes[e])
                else:
                    egid = revs[e][cmaps[e][by_face[first][0]]]
                if egid is None:
                    found.append((e, "new", ""))
                    continue
                ename = order[egid]
                found.append((e, relation(outline(glyf, ename), o),
                              "same" if hmtx[ename][0] == adv else "diff"))
            rel_face, rel_outline, rel_advance = next(
                (f for f in found if f[1] in ("same", "shift")), found[0])

        # Canonical decomposition whose parts all map in the same face.
        decomp = ""
        d = unicodedata.decomposition(chr(cps[0])) if cps else ""
        if d and not d.startswith("<"):
            fi = min(by_face)
            parts = [int(x, 16) for x in d.split()]
            if all(p in cmaps[fi] for p in parts):
                decomp = "+".join(names[revs[fi][cmaps[fi][p]]] for p in parts)

        strikes = has_bitmap.get(gid, [])
        vadv, tsb = vmtx[gname]
        glyph_rows.append([
            names[gid], gid, faces_col, " ".join(f"U+{c:04X}" for c in cps),
            "".join(char_label(c) for c in cps),
            adv, lsb if o else 0,
            *((g.xMin, g.yMin, g.xMax, g.yMax) if o else ("", "", "", "")),
            len(o[1]) if o else 0, len(o[0]) if o else 0,
            int(o[3]) if o else 0, decomp, rel_face, rel_outline, rel_advance,
            f"{strikes[0]}-{strikes[-1]}" if strikes else "",
            layout.get(gid, ""), vadv, (g.yMax + tsb) if o else tsb,
        ])
        assert not strikes or strikes == list(range(strikes[0], strikes[-1] + 1))

    write(P.data / "cmap.tsv", ["face", "codepoint", "char", "block", "glyph", "ref_gid"], cmap_rows)
    write(P.data / "glyphs.tsv", [
        "glyph", "ref_gid", "faces", "codepoints", "chars", "advance", "lsb",
        "xmin", "ymin", "xmax", "ymax", "contours", "points", "ref_composite",
        "decomposition", "rel_face", "rel_outline", "rel_advance", "bitmap_strikes",
        "layout", "vadvance", "vorigin",
    ], glyph_rows)
    write(P.data / "bitmaps.tsv", ["glyph", "ppem", "width", "height", "left", "top", "advance", "ink", "image_from"], bitmap_rows)
    strike_rows = []
    for strike in fonts[0]["EBLC"].strikes:
        bst = strike.bitmapSizeTable
        if bst.ppemX not in STRIKES:
            continue
        strike_rows.append([bst.ppemX, bst.ppemY, bst.bitDepth, bst.flags, bst.colorRef,
                            *(getattr(getattr(bst, d), f) for d in ("hori", "vert") for f in LINE_FIELDS)])
    write(P.data / "strikes.tsv", ["ppemX", "ppemY", "bitDepth", "flags", "colorRef",
                                 *(f"{d}.{f}" for d in ("hori", "vert") for f in LINE_FIELDS)], strike_rows)
    for fi, font in enumerate(fonts):
        font.saveXML(str(P.data / f"face{fi}.ttx"), tables=FACE_TABLES)

    print(f"cmap rows {len(cmap_rows)}, glyphs {len(glyph_rows)}, bitmaps {len(bitmap_rows)}")


def layout_extras(P, fonts, revs, names):
    """Glyphs the reference's GSUB reaches from the in-scope glyphs, in
    either face, by reference glyph ID: {gid: (name, layout, faces)}.
    Glyph names differ between the faces' post tables, so each face's
    lookups are read with that face's names and compared by ID."""
    found = {}
    for fi, font in enumerate(fonts):
        order = font.getGlyphOrder()
        rev = revs[fi]
        gsub = font["GSUB"].table
        feature = {}
        for fr in gsub.FeatureList.FeatureRecord:
            for li in fr.Feature.LookupListIndex:
                feature[li] = fr.FeatureTag
        lookups = gsub.LookupList.Lookup
        for li, lookup in enumerate(lookups):
            if lookup.LookupType == 6 and li in feature:
                for st in lookup.SubTable:
                    for rec in st.SubstLookupRecord:
                        feature.setdefault(rec.LookupListIndex, feature[li])
        have = {g for g, by_face in users_of(fonts, revs, fi)}
        while True:
            new = []
            for li, lookup in enumerate(lookups):
                for st in lookup.SubTable:
                    if lookup.LookupType == 1:
                        for a, b in st.mapping.items():
                            if rev[a] in have and rev[b] not in have:
                                new.append((rev[b], feature[li], (rev[a],)))
                    elif lookup.LookupType == 4:
                        for a, ligs in st.ligatures.items():
                            for lig in ligs:
                                parts = (rev[a], *(rev[c] for c in lig.Component))
                                if all(p in have for p in parts) and rev[lig.LigGlyph] not in have:
                                    new.append((rev[lig.LigGlyph], feature[li], parts))
            if not new:
                break
            for gid, tag, parts in new:
                if gid in have:
                    continue
                have.add(gid)
                if gid in names:
                    continue
                entry = found.setdefault(gid, {"faces": set(), "how": (tag, parts)})
                entry["faces"].add(fi)
                label = lambda g: P.strip_suffix(names.get(g) or found[g]["name"])
                if "name" not in entry:
                    stem = label(parts[0]) + "." + tag if len(parts) == 1 else "_".join(label(p) for p in parts)
                    entry["name"] = stem
                    entry["desc"] = f"{tag} " + "+".join(label(p) for p in parts)
    out = {}
    for gid, e in found.items():
        name = e["name"] + P.suffixes[min(e["faces"])]
        out[gid] = (name, e["desc"], sorted(e["faces"]))
    return out


def users_of(fonts, revs, fi):
    cmap = fonts[fi].getBestCmap()
    return [(revs[fi][g], fi) for cp, g in cmap.items() if in_scope(cp)]


def shared_images(fonts):
    """Per face, {(gid, ppem): ppem of the first strike holding the same
    EBDT image}. fontTools hands back one object per image offset, so object
    identity is image identity."""
    out = []
    for font in fonts:
        rev = font.getReverseGlyphMap()
        eblc, ebdt = font["EBLC"], font["EBDT"]
        first, mapping = {}, {}
        for si, strike in enumerate(eblc.strikes):
            ppem = strike.bitmapSizeTable.ppemX
            for name, image in ebdt.strikeData[si].items():
                mapping[rev[name], ppem] = first.setdefault(id(image), ppem)
        out.append(mapping)
    return out


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


if __name__ == "__main__":
    main()
