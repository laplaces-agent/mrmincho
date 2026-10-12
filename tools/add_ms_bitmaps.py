# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
#!/usr/bin/env python3
"""Add Microsoft's embedded bitmaps to a copy of MR Mincho or MR Goth.

  add_ms_bitmaps.py FONT.ttc MS.ttc [-o OUT.ttc]

FONT.ttc is a released MR Mincho or MR Goth collection, which carries no
embedded bitmaps; its manifest (FONT.bitmaps.json, next to it) lists, for
every bitmap the original font has for our glyphs, where to find it in the
Microsoft font and a hash of the expected pixels and metrics. MS.ttc is your
own copy of msmincho.ttc or msgothic.ttc, the Windows font. Each bitmap is
looked up by the Microsoft glyph ID, then by character, and copied only when
its hash matches, so any version whose bitmaps agree works and nothing else
is copied. The result keeps the Microsoft font's embedding permissions
(OS/2 fsType), since it now holds Microsoft's data.

The output contains data from Microsoft's font and comes under that font's
license: keep it for your own use on the machine the font is licensed for,
and do not redistribute it.

Needs fontTools and freetype-py (pip install fonttools freetype-py).
"""

import argparse
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import freetype
from fontTools.ttLib import TTCollection, TTFont

LINE_FIELDS = ("ascender", "descender", "widthMax", "caretSlopeNumerator", "caretSlopeDenominator",
               "caretOffset", "minOriginSB", "minAdvanceSB", "maxBeforeBL", "minAfterBL", "pad1", "pad2")


def embedded_bitmap(face, gid, ppem):
    """The glyph's embedded bitmap at ppem as (rows, left, top, advance),
    rows a list of '#'/'.' strings top to bottom; None when the strike has
    no bitmap for this glyph."""
    face.set_pixel_sizes(0, ppem)
    face.load_glyph(gid, freetype.FT_LOAD_DEFAULT)
    slot = face.glyph
    if slot.format != freetype.FT_GLYPH_FORMAT_BITMAP:
        return None
    bm = slot.bitmap
    if bm.pixel_mode != freetype.FT_PIXEL_MODE_MONO:
        return None
    buf, pitch = bytes(bm.buffer), abs(bm.pitch)
    rows = []
    for y in range(bm.rows):
        line = buf[y * pitch:(y + 1) * pitch]
        rows.append("".join("#" if line[x >> 3] & (0x80 >> (x & 7)) else "." for x in range(bm.width)))
    if bm.pitch < 0:
        rows.reverse()
    return rows, slot.bitmap_left, slot.bitmap_top, slot.advance.x >> 6


def bitmap_hash(rows, left, top, advance):
    width = len(rows[0]) if rows else 0
    text = f"{width} {len(rows)} {left} {top} {advance}\n" + "\n".join(rows)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def bitmap_xml(order, strikes, bitmaps):
    """EBLC and EBDT as TTX: per strike, every glyph with a bitmap, image
    format 7 (bit-aligned, per-glyph metrics), in index subtables of
    consecutive glyph IDs. strikes holds each strike's line metrics by ppem;
    bitmaps holds (glyph, ppem, rows, left, top, advance)."""
    gid = {n: i for i, n in enumerate(order)}
    by_strike = {}
    for name, ppem, rows, left, top, advance in bitmaps:
        by_strike.setdefault(ppem, []).append((gid[name], name, rows, left, top, advance))

    eblc, ebdt = ['<EBLC>\n<header version="2.0"/>'], ['<EBDT>\n<header version="2.0"/>']
    index = 0
    for s in strikes:
        ppem = int(s["ppemX"])
        entries = sorted(by_strike.get(ppem, []), key=lambda e: e[0])
        if not entries:
            continue
        line = lambda d: "".join(f'<{f} value="{s[f"{d}.{f}"]}"/>' for f in LINE_FIELDS)
        eblc.append(
            f'<strike index="{index}"><bitmapSizeTable>'
            f'<sbitLineMetrics direction="hori">{line("hori")}</sbitLineMetrics>'
            f'<sbitLineMetrics direction="vert">{line("vert")}</sbitLineMetrics>'
            f'<colorRef value="{s["colorRef"]}"/><startGlyphIndex value="{entries[0][0]}"/>'
            f'<endGlyphIndex value="{entries[-1][0]}"/><ppemX value="{ppem}"/><ppemY value="{s["ppemY"]}"/>'
            f'<bitDepth value="{s["bitDepth"]}"/><flags value="{s["flags"]}"/></bitmapSizeTable>')
        runs = []
        for e in entries:
            if runs and e[0] == runs[-1][-1][0] + 1:
                runs[-1].append(e)
            else:
                runs.append([e])
        for run in runs:
            eblc.append(f'<eblc_index_sub_table_1 imageFormat="7" firstGlyphIndex="{run[0][0]}" '
                        f'lastGlyphIndex="{run[-1][0]}">')
            eblc.extend(f'<glyphLoc id="{e[0]}" name={quoteattr(e[1])}/>' for e in run)
            eblc.append("</eblc_index_sub_table_1>")
        eblc.append("</strike>")

        ebdt.append(f'<strikedata index="{index}">')
        index += 1
        for _, name, rows, left, top, advance in entries:
            w, h = (len(rows[0]) if rows else 0), len(rows)
            ebdt.append(
                f'<ebdt_bitmap_format_7 name={quoteattr(name)}><BigGlyphMetrics>'
                f'<height value="{h}"/><width value="{w}"/><horiBearingX value="{left}"/>'
                f'<horiBearingY value="{top}"/><horiAdvance value="{advance}"/>'
                f'<vertBearingX value="{-(w // 2)}"/><vertBearingY value="0"/><vertAdvance value="{ppem}"/>'
                f'</BigGlyphMetrics><bitwiseimagedata bitDepth="1" width="{w}" height="{h}">'
                + "".join(f'<row value="{row}"/>' for row in rows)
                + "</bitwiseimagedata></ebdt_bitmap_format_7>")
        ebdt.append("</strikedata>")
    eblc.append("</EBLC>")
    ebdt.append("</EBDT>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<ttFont>\n'
            + "\n".join(eblc) + "\n" + "\n".join(ebdt) + "\n</ttFont>\n")


def collect(manifest, ms_path):
    """Find every bitmap the manifest lists in the Microsoft font; returns
    (found, missing): found as bitmap_xml wants them, missing as
    (glyph, ppem, reason)."""
    faces = [freetype.Face(str(ms_path), fi) for fi in range(freetype.Face(str(ms_path)).num_faces)]
    cmaps = [f.getBestCmap() or {} for f in TTCollection(str(ms_path)).fonts]
    orders = [{n: i for i, n in enumerate(f.getGlyphOrder())} for f in TTCollection(str(ms_path)).fonts]
    found, missing = [], []
    for glyph, ppem, face, gid, codepoint, want in manifest["bitmaps"]:
        if face >= len(faces):
            missing.append((glyph, ppem, "face missing"))
            continue
        candidates = [gid]
        if codepoint is not None and codepoint in cmaps[face]:
            alt = orders[face][cmaps[face][codepoint]]
            if alt != gid:
                candidates.append(alt)
        for g in candidates:
            if g >= faces[face].num_glyphs:
                continue
            got = embedded_bitmap(faces[face], g, ppem)
            if got and bitmap_hash(*got) == want:
                found.append((glyph, ppem, *got))
                break
        else:
            missing.append((glyph, ppem, "not found" if not candidates else "differs"))
    return found, missing


def patch(font_path, ms_path, out_path, manifest_path=None):
    manifest_path = Path(manifest_path or Path(font_path).with_suffix(".bitmaps.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    collection = TTCollection(str(font_path))
    if any("EBDT" in f for f in collection.fonts):
        raise SystemExit(f"{font_path} already has embedded bitmaps")
    family = TTCollection(str(ms_path)).fonts[0]["name"].getDebugName(1)
    if family != manifest["family"]:
        raise SystemExit(f"{ms_path} is {family!r}; this font needs {manifest['family']!r}")
    order = collection.fonts[0].getGlyphOrder()
    found, missing = collect(manifest, ms_path)
    if not found:
        raise SystemExit(f"no matching bitmaps in {ms_path}: not a Microsoft font this manifest knows")
    fs_type = TTCollection(str(ms_path)).fonts[0]["OS/2"].fsType
    xml = bitmap_xml(order, manifest["strikes"], found)
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp, "bitmaps.ttx")
        xml_path.write_text(xml, encoding="utf-8")
        fonts = []
        for fi in range(len(collection.fonts)):
            buf = io.BytesIO()
            collection.fonts[fi].save(buf)
            font = TTFont(io.BytesIO(buf.getvalue()))
            font.importXML(str(xml_path))
            font["OS/2"].fsType = fs_type
            fonts.append(font)
        out = TTCollection()
        out.fonts = fonts
        out.save(str(out_path), shareTables=True)
    return len(found), missing, len(manifest["bitmaps"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("font", help="MR Mincho or MR Goth collection (.ttc)")
    ap.add_argument("ms", help="your msmincho.ttc or msgothic.ttc")
    ap.add_argument("-o", "--out", help="default FONT-ms.ttc next to FONT")
    ap.add_argument("--manifest", help="default FONT.bitmaps.json")
    args = ap.parse_args()
    out = Path(args.out) if args.out else Path(args.font).with_name(Path(args.font).stem + "-ms.ttc")
    count, missing, total = patch(args.font, args.ms, out, args.manifest)
    print(f"copied {count} of {total} bitmaps from {args.ms}")
    if missing:
        print(f"{len(missing)} not found or different in this version, left without a bitmap:", file=sys.stderr)
        for glyph, ppem, reason in missing[:20]:
            print(f"  {glyph} @{ppem}px: {reason}", file=sys.stderr)
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more", file=sys.stderr)
    print(f"wrote {out} (for your own use only: it contains Microsoft's bitmaps)")


if __name__ == "__main__":
    main()
