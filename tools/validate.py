# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Compare a built font collection with the reference, using the manifest.

  validate.py BUILT.ttc [--font F] [--only U+0021-U+007E,...]
              [--faces 0] [--report PATH]

The glyph checks cover each face's in-scope codepoints and, unless --only
narrows the run, .notdef and the glyphs only layout reaches (vertical forms,
ccmp ligatures and contextual forms), by pool name.

Checks, per face:
  faces     face-level fields from data/faceN.ttx
  coverage  every in-scope codepoint mapped; extra in-scope codepoints;
            every layout glyph present
  sharing   a codepoint uses the same glyph in two faces exactly when the
            reference's does
  advance   hmtx advance equals the reference
  vertical  vmtx advance height equals the reference; the vertical origin
            (top of the outline plus top side bearing) within --bbox-tol
  hinted    the advance after TrueType hinting (FreeType; 1-bit or
            anti-aliased at each size as the reference's gasp selects)
            equals the
            reference's at every size 8-72 px: hinting must never change
            the metrics
  tables    BASE and meta equal to the reference's; LTSH in the same faces;
            every table the reference shares between faces shared between
            the same faces in ours (ours may share more, where the content
            came out identical)
  layout    HarfBuzz shapes the same strings in both fonts, horizontally
            and vertically: every in-scope character, every ccmp ligature's
            components, every character followed by U+20E3. Same glyphs
            (reference glyph IDs mapped to pool names) at the same
            positions, exactly
  bounds    ink bounds (the outline's true extent, curves included, not
            its control points) within --bbox-tol units of the reference's
  straight  where the reference outline has only straight segments, ours
            must too; where those are all horizontal or vertical (box
            drawing, block elements, rules) ours must also render
            identically at --outline-scale: edges that tile across glyphs
            have to meet exactly, which the distance bars would not ensure.
            Diagonal edges traced from pixels cannot land on the reference's
            vertices exactly, so they are held to the distance bars
  outline   rendered at --outline-scale pixels per unit: the largest
            distance between our outline and the reference's (max_dev, in
            font units) at most --max-dev; IoU and mean distance reported
  render    both fonts' outlines rendered by FreeType with the same settings
            (anti-aliased, unhinted, embedded bitmaps ignored) at each of
            --render-sizes. The largest coverage difference d (of 255) at
            size s px is read as an edge displacement of d/255 px, which is
            d/255 * 256/s font units; at most --max-render-dev units
  bitmap    each strike 10-22: same glyphs have bitmaps, same bitmap
            metrics (width, height, left, top, advance), same pixels. A
            release build carries no bitmaps (build.py's default); it must
            then have none at all, and the check is reported as skipped

Identity fields (vendor ID, embedding permissions) are listed for
information and never fail: they follow the project's own names and license.

Exits 1 when any check fails. Failures go to the report TSV.
"""

import argparse
import csv
import os
import sys

import numpy as np
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTCollection, TTFont

from common import ROOT, STRIKES, UPEM, Face, add_font_argument, align, compare_outlines, in_scope, perimeter, project

FACE_FIELDS = {
    "head": ["unitsPerEm", "flags", "macStyle", "lowestRecPPEM", "fontDirectionHint"],
    "hhea": ["ascent", "descent", "lineGap", "caretSlopeRise", "caretSlopeRun", "caretOffset"],
    "OS/2": [
        "version", "xAvgCharWidth", "usWeightClass", "usWidthClass",
        "ySubscriptXSize", "ySubscriptYSize", "ySubscriptXOffset", "ySubscriptYOffset",
        "ySuperscriptXSize", "ySuperscriptYSize", "ySuperscriptXOffset", "ySuperscriptYOffset",
        "yStrikeoutSize", "yStrikeoutPosition", "sFamilyClass", "panose",
        "ulUnicodeRange1", "ulUnicodeRange2", "ulUnicodeRange3", "ulUnicodeRange4",
        "fsSelection", "sTypoAscender", "sTypoDescender", "sTypoLineGap",
        "usWinAscent", "usWinDescent", "ulCodePageRange1", "ulCodePageRange2",
        "sxHeight", "sCapHeight", "usDefaultChar", "usBreakChar",
    ],
    "post": ["italicAngle", "underlinePosition", "underlineThickness", "isFixedPitch"],
    "gasp": ["gaspRange"],
    "vhea": ["tableVersion", "ascent", "descent", "lineGap", "caretSlopeRise", "caretSlopeRun", "caretOffset"],
}
HINTED_SIZES = range(8, 73)
BITMAP_TABLES = ("EBDT", "EBLC", "EBSC")
IDENTITY_FIELDS = {"OS/2": ["achVendID", "fsType"]}


def read_tsv(P, name):
    with open(P.data / name, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def parse_only(spec):
    if not spec:
        return None
    ranges = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        lo = int(lo.removeprefix("U+"), 16)
        ranges.append((lo, int(hi.removeprefix("U+"), 16) if hi else lo))
    return lambda cp: any(a <= cp <= b for a, b in ranges)


def field_value(table, field):
    v = getattr(table, field, None)
    if hasattr(v, "__dict__"):
        return dict(vars(v))
    return v


def ink_bounds(glyphset, name):
    pen = BoundsPen(glyphset)
    glyphset[name].draw(pen)
    return [round(v, 1) for v in pen.bounds]


def check_tables(ref_fonts, built_fonts, faces, check, absent=()):
    """BASE and meta equal to the reference's; LTSH in the same faces; every
    pair of faces that shares a table in the reference shares it in ours,
    except the tables in absent, which ours leaves out."""
    from io import StringIO
    from fontTools.misc.xmlWriter import XMLWriter

    def xml(font, tag):
        out = StringIO()
        writer = XMLWriter(out)
        font[tag].toXML(writer, font)
        return out.getvalue()

    for fi in faces:
        for tag in ("BASE", "meta"):
            ok = tag in built_fonts[fi] and xml(ref_fonts[fi], tag) == xml(built_fonts[fi], tag)
            check("tables", fi, tag, ok, "same as reference", "same" if ok else "differs")
    ltsh = ["LTSH" in f for f in built_fonts]
    want = ["LTSH" in f for f in ref_fonts]
    check("tables", None, "LTSH faces", ltsh == want, want, ltsh)
    offset = lambda f, tag: f.reader.tables[tag].offset if tag in f.reader else None
    for tag in sorted(set().union(*(f.reader.keys() for f in ref_fonts)) - set(absent)):
        pairs = [(a, b) for a in range(len(ref_fonts)) for b in range(a + 1, len(ref_fonts))
                 if offset(ref_fonts[a], tag) is not None
                 and offset(ref_fonts[a], tag) == offset(ref_fonts[b], tag)]
        missing = [fi for fi, f in enumerate(ref_fonts) if tag in f.reader and tag not in built_fonts[fi].reader]
        unshared = [(a, b) for a, b in pairs if offset(built_fonts[a], tag) != offset(built_fonts[b], tag)]
        check("tables", None, f"{tag} shared", not missing and not unshared,
              f"shared by faces {pairs}", f"missing in {missing}, not shared {unshared}")


def layout_strings(P, face_index, cmap_rows):
    """Strings that exercise the reference's GSUB for a face, from its
    lookups: each in-scope character alone (horizontal and vertical, which
    applies vert), each ccmp ligature's components, and each character
    followed by U+20E3, which the contextual ccmp rule looks ahead to."""
    ref = P.load_reference().fonts[face_index]
    cps = sorted(int(r["codepoint"][2:], 16) for r in cmap_rows if int(r["face"]) == face_index)
    by_glyph = {}
    for cp, g in ref.getBestCmap().items():
        if in_scope(cp):
            by_glyph.setdefault(g, cp)
    strings = [chr(cp) for cp in cps]
    for lookup in ref["GSUB"].table.LookupList.Lookup:
        if lookup.LookupType != 4:
            continue
        for st in lookup.SubTable:
            for first, ligs in st.ligatures.items():
                for lig in ligs:
                    parts = [first, *lig.Component]
                    if all(p in by_glyph for p in parts):
                        strings.append("".join(chr(by_glyph[p]) for p in parts))
    strings += [chr(cp) + "\u20e3" for cp in cps if cp != 0x20E3]
    return strings


def check_layout(P, built, glyphs, cmap_rows, faces, check):
    """Shape the same strings with HarfBuzz in the reference and in ours,
    horizontally and vertically, and require the same glyphs (reference IDs
    mapped to pool names) at the same positions."""
    import uharfbuzz as hb

    pool = {int(r["ref_gid"]): r["glyph"] for r in glyphs.values()}
    built_order = TTCollection(built, lazy=True).fonts[0].getGlyphOrder()
    for fi in faces:
        fonts = [hb.Font(hb.Face(hb.Blob.from_file_path(str(path)), fi)) for path in (P.reference, built)]
        names = [lambda gid: pool.get(gid, f"reference glyph {gid}"), lambda gid: built_order[gid]]
        for text in layout_strings(P, fi, cmap_rows):
            for direction in ("ltr", "ttb"):
                runs = []
                for font, name in zip(fonts, names):
                    buf = hb.Buffer()
                    buf.add_str(text)
                    buf.guess_segment_properties()
                    buf.direction = direction
                    hb.shape(font, buf, {})
                    runs.append([(name(i.codepoint), p.x_advance, p.y_advance, p.x_offset, p.y_offset)
                                 for i, p in zip(buf.glyph_infos, buf.glyph_positions)])
                item = " ".join(f"U+{ord(c):04X}" for c in text) + f" {direction}"
                check("layout", fi, item, runs[0] == runs[1], runs[0], runs[1])


def straight_only(font, name):
    """True when the glyph has contours and no off-curve points."""
    g = font["glyf"][name]
    if g.numberOfContours <= 0:
        return False
    return all(flag & 1 for flag in g.flags)


def rectilinear(font, name):
    """True when every segment of the glyph is horizontal or vertical."""
    if not straight_only(font, name):
        return False
    g = font["glyf"][name]
    start = 0
    for end in g.endPtsOfContours:
        pts = list(g.coordinates[start:end + 1])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
            if x0 != x1 and y0 != y1:
                return False
        start = end + 1
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("built")
    ap.add_argument("--only", help="codepoint ranges, e.g. U+0021-U+007E,U+00A0")
    ap.add_argument("--report", default=str(ROOT / "work" / "validate-report.tsv"))
    ap.add_argument("--bbox-tol", type=int, default=2)
    ap.add_argument("--outline-scale", type=int, default=4)
    ap.add_argument("--max-dev", type=float, default=1.0, help="outline distance bar, font units")
    ap.add_argument("--render-sizes", default="12,16,24,48")
    ap.add_argument("--max-render-dev", type=float, default=1.5,
                    help="render bar, font units: the outline bar plus anti-aliasing slack")
    ap.add_argument("--skip", default="", help="comma-separated checks to skip")
    ap.add_argument("--faces", help="faces to check, e.g. 0 (default all)")
    add_font_argument(ap)
    args = ap.parse_args()
    only = parse_only(args.only)
    skip = set(filter(None, args.skip.split(",")))

    P = project(args.font)
    FACES = P.faces
    glyphs = {r["glyph"]: r for r in read_tsv(P, "glyphs.tsv")}
    faces = [int(f) for f in args.faces.split(",")] if args.faces else list(range(len(FACES)))
    cmap_rows = [r for r in read_tsv(P, "cmap.tsv")
                 if int(r["face"]) in faces and (only is None or only(int(r["codepoint"][2:], 16)))]
    ref_fonts = P.load_reference().fonts
    built_fonts = TTCollection(args.built, lazy=True).fonts
    if len(built_fonts) != len(FACES):
        sys.exit(f"expected {len(FACES)} faces, found {len(built_fonts)}")
    with_bitmaps = [any(tag in f for tag in BITMAP_TABLES) for f in built_fonts]
    if any(with_bitmaps) and not all(with_bitmaps):
        sys.exit(f"embedded bitmaps in some faces only: {with_bitmaps}")
    release = not any(with_bitmaps)

    failures = []
    counts = {}
    identity = []

    def check(name, face, item, ok, expected="", got=""):
        c = counts.setdefault(name, [0, 0])
        c[0] += 1
        if not ok:
            c[1] += 1
            failures.append([name, FACES[face] if face is not None else "", item, expected, got])

    # faces
    if "faces" not in skip:
        for fi in faces:
            ref = TTFont()
            ref.importXML(str(P.data / f"face{fi}.ttx"))
            built = built_fonts[fi]
            for tag, fields in FACE_FIELDS.items():
                for field in fields:
                    want = field_value(ref[tag], field)
                    got = field_value(built[tag], field) if tag in built else None
                    check("faces", fi, f"{tag}.{field}", want == got, want, got)
            for tag, fields in IDENTITY_FIELDS.items():
                for field in fields:
                    want = field_value(ref[tag], field)
                    got = field_value(built[tag], field) if tag in built else None
                    if want != got:
                        identity.append(f"{FACES[fi]} {tag}.{field}: reference {want!r}, built {got!r}")
        want = [bool(f["post"].isFixedPitch) for f in ref_fonts]
        got = [bool(f["post"].isFixedPitch) for f in built_fonts]
        check("faces", None, "face order", want == got, f"fixed pitch {want}", got)

    built_cmaps = [f.getBestCmap() for f in built_fonts]
    built_revs = [f.getReverseGlyphMap() for f in built_fonts]
    built_gid = lambda fi, cp: built_revs[fi][built_cmaps[fi][cp]]

    # coverage and sharing
    present = []
    for r in cmap_rows:
        fi, cp = int(r["face"]), int(r["codepoint"][2:], 16)
        ok = cp in built_cmaps[fi]
        check("coverage", fi, r["codepoint"], ok, r["glyph"], "" if ok else "missing")
        if ok:
            present.append((fi, cp, r))
    wanted = {(int(r["face"]), int(r["codepoint"][2:], 16)) for r in cmap_rows}
    for fi in faces:
        for cp in built_cmaps[fi]:
            if in_scope(cp) and (only is None or only(cp)) and (fi, cp) not in wanted:
                check("coverage", fi, f"U+{cp:04X}", False, "not in scope set", "extra")
    for fi, cp, r in present:
        for e in range(fi):
            if cp in built_cmaps[e]:
                code = glyphs[r["glyph"]]["faces"]
                shared_ref = P.codes[e] in code and P.codes[fi] in code
                shared_built = built_gid(e, cp) == built_gid(fi, cp)
                item = r["codepoint"] + (f" with {FACES[e]}" if fi > 1 else "")
                check("sharing", fi, item, shared_ref == shared_built, shared_ref, shared_built)

    # Every glyph to compare: the in-scope codepoints of each face, then (on
    # a run not narrowed by --only) .notdef and the glyphs only layout
    # reaches, in each face that uses them. (face, item, manifest row,
    # reference glyph ID, built glyph name)
    built_orders = [f.getGlyphOrder() for f in built_fonts]
    targets = [(fi, r["codepoint"], glyphs[r["glyph"]], int(r["ref_gid"]), built_cmaps[fi][cp])
               for fi, cp, r in present]
    if only is None:
        for g in glyphs.values():
            if g["layout"]:
                for fi in faces:
                    if P.codes[fi] in g["faces"]:
                        ok = g["glyph"] in built_orders[fi]
                        check("coverage", fi, g["glyph"], ok, "layout glyph", "" if ok else "missing")
                        if ok:
                            targets.append((fi, g["glyph"], g, int(g["ref_gid"]), g["glyph"]))

    # advance, vertical metrics and bounds
    ref_orders = [f.getGlyphOrder() for f in ref_fonts]
    ref_glyphsets = [f.getGlyphSet() for f in ref_fonts]
    built_glyphsets = [f.getGlyphSet() for f in built_fonts]
    for fi, item, g, rgid, name in targets:
        font = built_fonts[fi]
        rname = ref_orders[fi][rgid]
        adv, _ = font["hmtx"][name]
        check("advance", fi, item, adv == int(g["advance"]), g["advance"], adv)
        bg = font["glyf"][name]
        if "vertical" not in skip:
            vadv, tsb = font["vmtx"][name]
            bg.recalcBounds(font["glyf"])
            vorigin = (bg.yMax + tsb) if bg.numberOfContours else tsb
            ok = vadv == int(g["vadvance"]) and abs(vorigin - int(g["vorigin"])) <= args.bbox_tol
            check("vertical", fi, item, ok, f"vadvance {g['vadvance']}, origin {g['vorigin']}",
                  f"vadvance {vadv}, origin {vorigin}")
        if "bounds" in skip:
            continue
        if g["xmin"] == "":
            check("bounds", fi, item, bg.numberOfContours == 0, "empty", bg.numberOfContours)
            continue
        if bg.numberOfContours == 0:
            check("bounds", fi, item, False, "outline", "empty")
            continue
        want = ink_bounds(ref_glyphsets[fi], rname)
        got = ink_bounds(built_glyphsets[fi], name)
        worst = max(abs(a - b) for a, b in zip(want, got))
        check("bounds", fi, item, worst <= args.bbox_tol, want, got)

    # outline overlap and bitmaps, rendered with FreeType
    ref_faces = [Face(P.reference, fi) for fi in range(len(FACES))]
    built_faces = [Face(args.built, fi) for fi in range(len(FACES))]
    gasp = sorted(ref_fonts[0]["gasp"].gaspRange.items())
    # 1-bit unless the reference's gasp asks for grey-scale (flag 2) at that size.
    mono_at = lambda size: not next(flags for top, flags in gasp if size <= top) & 2
    built_revs_all = [f.getReverseGlyphMap() for f in built_fonts]
    ppem = UPEM * args.outline_scale
    for fi, item, g, rgid, name in targets:
        rname = ref_orders[fi][rgid]
        bgid = built_revs_all[fi][name]
        if "outline" not in skip:
            a = ref_faces[fi].render_outline(rgid, ppem)[:3]
            b = built_faces[fi].render_outline(bgid, ppem)[:3]
            length = perimeter(ref_glyphsets[fi], rname)
            m = compare_outlines(a, b, args.outline_scale, length)
            check("outline", fi, item, m["max_dev"] <= args.max_dev, f"max_dev <= {args.max_dev}",
                  f"max_dev {m['max_dev']:.3f}, mean_dev {m['mean_dev']:.3f}, iou {m['iou']:.5f}")
        if "straight" not in skip and straight_only(ref_fonts[fi], rname):
            xor = 0
            if rectilinear(ref_fonts[fi], rname):
                a = ref_faces[fi].render_outline(rgid, ppem)[:3]
                b = built_faces[fi].render_outline(bgid, ppem)[:3]
                ca, cb = align(a, b)
                xor = int((ca ^ cb).sum())
            curved = not straight_only(built_fonts[fi], name)
            check("straight", fi, item, xor == 0 and not curved, "straight, rectilinear exact",
                  f"{xor} px differ at {args.outline_scale} px/unit" + (", has curves" if curved else ""))
        if "render" not in skip:
            for size in map(int, args.render_sizes.split(",")):
                ca, cb = align(ref_faces[fi].render_gray(rgid, size), built_faces[fi].render_gray(bgid, size))
                worst = int(np.abs(ca.astype(int) - cb.astype(int)).max()) if ca.size else 0
                dev = worst / 255 * UPEM / size
                check("render", fi, f"{item} @{size}", dev <= args.max_render_dev,
                      f"<= {args.max_render_dev} units", f"{dev:.2f} units (max diff {worst}/255)")
        if "hinted" not in skip:
            bad = []
            for size in HINTED_SIZES:
                mono = mono_at(size)
                a = ref_faces[fi].hinted_advance(rgid, size, mono)
                b = built_faces[fi].hinted_advance(bgid, size, mono)
                if a != b:
                    bad.append(f"{size}px {a / 64:g}/{b / 64:g}")
            check("hinted", fi, item, not bad, "same hinted advance at 8-72 px", "; ".join(bad[:6]))
        if "bitmap" in skip or release:
            continue
        for size in STRIKES:
            a = ref_faces[fi].embedded_bitmap(rgid, size)
            b = built_faces[fi].embedded_bitmap(bgid, size)
            bitem = f"{item} @{size}"
            if a is None or b is None:
                check("bitmap", fi, bitem, a is None and b is None,
                      "bitmap" if a else "none", "bitmap" if b else "none")
                continue
            ma = (*a[0].shape[::-1], *a[1:])
            mb = (*b[0].shape[::-1], *b[1:])
            ca, cb = align(a[:3], b[:3])
            xor = int((ca ^ cb).sum())
            check("bitmap", fi, bitem, ma == mb and xor == 0,
                  f"w,h,left,top,adv {ma}", f"{mb}, {xor} px differ")

    if "tables" not in skip:
        check_tables(ref_fonts, built_fonts, faces, check, BITMAP_TABLES if release else ())
    if "layout" not in skip:
        check_layout(P, args.built, glyphs, cmap_rows, faces, check)

    report = args.report
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["check", "face", "item", "expected", "got"])
        w.writerows(failures)

    print(f"{'check':10} {'tested':>8} {'failed':>8}")
    for name, (n, bad) in counts.items():
        print(f"{name:10} {n:8} {bad:8}")
    if release:
        print("bitmap     skipped: release build, no embedded bitmaps")
    for line in identity:
        print(f"identity (not failed): {line}")
    print(f"report: {report}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
