# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Trace reference glyphs: render the outline 1-bit at a whole number of
pixels per font unit, vectorise it with potrace, convert the cubic curves to
TrueType quadratics and round every point to the font-unit grid.

  trace.py [--font F] [--face N] [settings] CHARS...
                                     trace and report accuracy
  trace.py ... --into-ufo CHARS...   also write the traces into the
                                     project's UFO

CHARS are characters, U+XXXX codepoints, U+XXXX-U+YYYY ranges, or pool
glyph names from the manifest's glyphs.tsv prefixed with "/" (/uni2010.vert, /.notdef),
which reach the glyphs no codepoint maps to. A codepoint is resolved through
the given face's character map. Each is traced and compared with the reference at --check-scale
pixels per unit: max_dev and mean_dev are distances between the outlines in
font units; points counts stored TrueType points against the reference's.

--into-ufo never replaces a glyph that already has contours (a trace that
was fixed afterwards) unless --force is given. Settings that differ from the
defaults are recorded in the glyph's lib under org.mincho-trace.settings.
"""

import argparse
import csv
import math
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from dataclasses import dataclass

from fontTools.misc.bezierTools import solveQuadratic, splitCubicAtT
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.reverseContourPen import ReverseContourPen
from fontTools.pens.roundingPen import RoundingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.svgLib.path import parse_path
from PIL import Image

from common import UPEM, Face, add_font_argument, compare_outlines, perimeter, project

SVG_NS = "{http://www.w3.org/2000/svg}"


@dataclass
class Settings:
    scale: int = 8            # render pixels per font unit
    turdsize: int = 2         # potrace: drop specks up to this many pixels
    alphamax: float = 0.8     # potrace: corner threshold, higher = smoother
    opttolerance: float = 2.0  # potrace: curve-joining tolerance
    cu2qu_err: float = 0.5    # max distance in units, cubic to quadratics
    line_tol: float = 0.5     # cubic whose controls lie within this many
                              # units of its chord becomes a line; 0 = off


def straighten(rec, tol):
    """Replace near-straight cubic segments with lines, then drop line
    joints that lie within tol of the line through their neighbours."""
    out, cur = RecordingPen(), None
    for op, args in rec.value:
        if op == "curveTo" and tol > 0 and cur is not None:
            (c1, c2, end) = args
            if _dist_to_segment(c1, cur, end) <= tol and _dist_to_segment(c2, cur, end) <= tol:
                op, args = "lineTo", (end,)
        if (op == "lineTo" and tol > 0 and len(out.value) >= 2 and out.value[-1][0] == "lineTo"
                and out.value[-2][0] in ("moveTo", "lineTo", "curveTo")):
            start, mid = out.value[-2][1][-1], out.value[-1][1][0]
            if _dist_to_segment(mid, start, args[0]) <= tol:
                out.value.pop()
        out.value.append((op, args))
        if op in ("moveTo", "lineTo", "curveTo", "qCurveTo"):
            cur = args[-1]
    return out


def split_at_extrema(rec):
    """Split cubic segments where x or y turns, so every horizontal and
    vertical extreme of the outline is an on-curve point (and, after
    rounding, the glyph's bounding box is its ink's)."""
    out, cur = RecordingPen(), None
    for op, args in rec.value:
        if op == "curveTo" and cur is not None:
            p0, (p1, p2, p3) = cur, args
            ts = set()
            for axis in (0, 1):
                a = 3 * (-p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis])
                b = 6 * (p0[axis] - 2 * p1[axis] + p2[axis])
                c = 3 * (p1[axis] - p0[axis])
                ts.update(t for t in solveQuadratic(a, b, c) if 0.02 < t < 0.98)
            if ts:
                for seg in splitCubicAtT(p0, p1, p2, p3, *sorted(ts)):
                    out.value.append(("curveTo", tuple(seg[1:])))
                cur = p3
                continue
        out.value.append((op, args))
        if op in ("moveTo", "lineTo", "curveTo", "qCurveTo"):
            cur = args[-1]
    return out


def _dist_to_segment(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def trace(face, gid, settings=Settings()):
    """Trace one glyph of a Face. Returns a RecordingPen of quadratic
    contours in font units, TrueType (clockwise outer) direction, every
    point an integer. An empty glyph gives an empty recording."""
    scale = settings.scale
    potrace_args = ("--turdsize", str(settings.turdsize), "--alphamax", str(settings.alphamax),
                    "--opttolerance", str(settings.opttolerance))
    pixels, left, top, _ = face.render_outline(gid, UPEM * scale)
    rec = RecordingPen()
    if not pixels.any():
        return rec
    with tempfile.TemporaryDirectory() as tmp:
        pbm, svg = Path(tmp, "g.pbm"), Path(tmp, "g.svg")
        Image.fromarray(~pixels).convert("1").save(pbm)
        subprocess.run(["potrace", "-s", *potrace_args, "-o", str(svg), str(pbm)], check=True)
        root = ET.parse(svg).getroot()

    group = root.find(f"{SVG_NS}g")
    tx, ty, sx, sy = map(float, re.fullmatch(
        r"translate\(([-\d.]+),([-\d.]+)\) scale\(([-\d.]+),([-\d.]+)\)", group.get("transform")).groups())
    height = pixels.shape[0]
    assert (tx, ty) == (0.0, float(height)), group.get("transform")
    # potrace coordinates: (sx * x, ty + sy * y) is the SVG pixel position,
    # y down from the image top; the image's top-left pixel corner sits at
    # (left, top) pixels from the glyph origin.
    transform = (sx / scale, 0, 0, -sy / scale, (tx + left) / scale, (top - ty) / scale)

    cubic = RecordingPen()
    tpen = TransformPen(cubic, transform)
    for path in group.iter(f"{SVG_NS}path"):
        parse_path(path.get("d"), tpen)

    cubic = split_at_extrema(straighten(cubic, settings.line_tol))
    area = AreaPen()
    cubic.replay(area)
    quad = RecordingPen()
    # AreaPen is positive for counter-clockwise; TrueType outers run clockwise.
    target = ReverseContourPen(quad) if area.value > 0 else quad
    cubic.replay(Cu2QuPen(target, settings.cu2qu_err, all_quadratic=True))
    rec_rounded = RecordingPen()
    quad.replay(RoundingPen(rec_rounded))
    return tidy(rec_rounded)


def tidy(rec):
    """Remove what rounding to the grid leaves degenerate: off-curve
    points on top of their neighbours, quadratic segments with no off-curve
    left (now lines), and segments of zero length."""
    out, cur = RecordingPen(), None
    for op, args in rec.value:
        if op == "qCurveTo":
            end = args[-1]
            offs = [p for i, p in enumerate(args[:-1]) if p != end and p != (args[i - 1] if i else cur)]
            op, args = ("qCurveTo", (*offs, end)) if offs else ("lineTo", (end,))
        if op == "lineTo" and args[0] == cur:
            continue
        out.value.append((op, args))
        if op in ("moveTo", "lineTo", "qCurveTo"):
            cur = args[-1]
    return out


def build_test_font(path, glyphs):
    """Write a minimal TrueType font holding glyphs: {name: (recording,
    advance, codepoint)}. Only for rendering checks."""
    order = [".notdef", *glyphs]
    fb = FontBuilder(UPEM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({cp: name for name, (_, _, cp) in glyphs.items() if cp is not None})
    ttglyphs, metrics = {".notdef": TTGlyphPen(None).glyph()}, {".notdef": (UPEM // 2, 0)}
    for name, (rec, advance, _) in glyphs.items():
        pen = TTGlyphPen(None)
        rec.replay(pen)
        ttglyphs[name] = pen.glyph()
    fb.setupGlyf(ttglyphs)
    glyf = fb.font["glyf"]
    for name, (_, advance, _) in glyphs.items():
        g = glyf[name]
        metrics[name] = (advance, g.xMin if g.numberOfContours else 0)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=220, descent=-36)
    fb.setupNameTable({"familyName": "Trace Test", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=220, sTypoDescender=-36, usWinAscent=220, usWinDescent=36)
    fb.setupPost()
    fb.save(str(path))


def parse_chars(items):
    out = []
    for item in items:
        m = re.fullmatch(r"U\+([0-9A-Fa-f]{4,6})(?:-U\+([0-9A-Fa-f]{4,6}))?", item)
        if m:
            lo = int(m.group(1), 16)
            out.extend(range(lo, int(m.group(2) or m.group(1), 16) + 1))
        else:
            out.extend(ord(c) for c in item)
    return out


LIB_KEY = "org.mincho-trace.settings"


def resolve(P, items, face_index):
    """[(label, pool glyph name, reference glyph ID)] for CHARS."""
    rows = {}
    with open(P.data / "glyphs.tsv", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows[r["glyph"]] = r
    by_cp = {}
    with open(P.data / "cmap.tsv", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            if int(r["face"]) == face_index:
                by_cp[int(r["codepoint"][2:], 16)] = r["glyph"]
    out = []
    for item in items:
        if item.startswith("/") and len(item) > 1:
            name = item[1:]
            if name not in rows:
                raise SystemExit(f"no pool glyph {name}")
            out.append((name, name, int(rows[name]["ref_gid"])))
        else:
            for cp in parse_chars([item]):
                name = by_cp[cp]
                out.append((chr(cp), name, int(rows[name]["ref_gid"])))
    return out


def write_into_ufo(P, traced, force, settings):
    import ufoLib2

    font = ufoLib2.Font.open(P.ufo)
    written, kept = [], []
    for name, rec in traced.items():
        glyph = font[name]
        if len(glyph.contours) and not force:
            kept.append(name)
            continue
        glyph.clearContours()
        rec.replay(glyph.getPen())
        changed = {k: v for k, v in vars(settings).items() if v != getattr(Settings(), k)}
        if changed:
            glyph.lib[LIB_KEY] = changed
        else:
            glyph.lib.pop(LIB_KEY, None)
        written.append(name)
    font.save()
    print(f"ufo: wrote {len(written)} glyphs" + (f", kept {len(kept)} existing: {' '.join(kept)}" if kept else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chars", nargs="+")
    ap.add_argument("--face", type=int, default=0)
    ap.add_argument("--check-scale", type=int, default=4)
    ap.add_argument("--into-ufo", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    add_font_argument(ap)
    defaults = Settings()
    for field in defaults.__dataclass_fields__:
        ap.add_argument("--" + field.replace("_", "-"), type=type(getattr(defaults, field)),
                        default=getattr(defaults, field))
    args = ap.parse_args()
    settings = Settings(**{f: getattr(args, f) for f in defaults.__dataclass_fields__})

    # The faces share glyf and hmtx, so a glyph ID means the same outline in
    # either; only the glyph names (from each face's post) differ.
    P = project(args.font)
    font = P.load_reference().fonts[args.face]
    order = font.getGlyphOrder()
    hmtx = font["hmtx"].metrics
    ref = Face(P.reference, args.face)
    targets = resolve(P, args.chars, args.face)
    traced = {name: trace(ref, gid, settings) for _, name, gid in targets}
    glyphs = {f"g{i:05d}": (traced[name], hmtx[order[gid]][0], None) for i, (_, name, gid) in enumerate(targets)}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "trace.ttf")
        build_test_font(path, glyphs)
        test = Face(path, 0)
        from fontTools.ttLib import TTFont
        test_glyf = TTFont(str(path))["glyf"]
        glyphset = font.getGlyphSet()
        ppem = UPEM * args.check_scale
        totals = {"pts": 0, "ref_pts": 0, "max_dev": 0.0, "mean_dev": []}
        if not args.quiet:
            print("char  points  ref_pts  max_dev  mean_dev  iou")
        for i, (name, (label, _, gid)) in enumerate(zip(glyphs, targets), start=1):
            gname = order[gid]
            m = compare_outlines(ref.render_outline(gid, ppem)[:3], test.render_outline(i, ppem)[:3],
                                 args.check_scale, perimeter(glyphset, gname))
            g = test_glyf[name]
            pts = len(g.getCoordinates(test_glyf)[0]) if g.numberOfContours else 0
            rg = font["glyf"][gname]
            ref_pts = len(rg.getCoordinates(font["glyf"])[0]) if rg.numberOfContours else 0
            totals["pts"] += pts
            totals["ref_pts"] += ref_pts
            totals["max_dev"] = max(totals["max_dev"], m["max_dev"])
            if ref_pts:
                totals["mean_dev"].append(m["mean_dev"])
            if not args.quiet:
                print(f"{label!s:4}  {pts:6}  {ref_pts:7}  {m['max_dev']:7.3f}  {m['mean_dev']:8.3f}  {m['iou']:.5f}")
        md = totals["mean_dev"]
        print(f"total points {totals['pts']} vs reference {totals['ref_pts']} "
              f"({totals['pts'] / max(totals['ref_pts'], 1):.2f}x); worst max_dev {totals['max_dev']:.3f}; "
              f"mean of mean_dev {sum(md) / max(len(md), 1):.3f}")

    if args.into_ufo:
        write_into_ufo(P, traced, args.force, settings)


if __name__ == "__main__":
    main()
