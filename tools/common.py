# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 The MR Mincho Project Authors
"""Shared helpers: the font projects, the in-scope codepoint set, and
FreeType-based bitmap reading and outline rendering."""

import unicodedata
from dataclasses import dataclass
from pathlib import Path

import freetype
import numpy as np
from fontTools.ttLib import TTCollection

ROOT = Path(__file__).resolve().parent.parent
UPEM = 256
STRIKES = range(10, 23)


@dataclass(frozen=True)
class Project:
    """One font family: its reference collection, manifest, source and
    names. Face 0's glyphs are named after their codepoints; a glyph only
    later faces use takes the suffix of the first face that uses it."""

    key: str
    reference_file: str  # the Microsoft font, under reference/; not distributed
    data: Path
    ufo: Path
    out: Path
    faces: tuple       # face labels, as in reports
    codes: str         # one letter per face, for the manifest's faces column
    suffixes: tuple    # glyph-name suffix per face
    names: tuple       # per face: (English family, Japanese family, PostScript name)
    copyright: str
    ltsh_faces: tuple  # faces whose reference has an LTSH table

    def faces_code(self, faces):
        return "".join(self.codes[fi] for fi in sorted(faces))

    def glyph_name(self, cp, face):
        base = f"uni{cp:04X}" if cp <= 0xFFFF else f"u{cp:05X}"
        return base + self.suffixes[face]

    def strip_suffix(self, name):
        for suffix in self.suffixes:
            if suffix and name.endswith(suffix):
                return name.removesuffix(suffix)
        return name

    @property
    def reference(self):
        """The reference font. Only the tools that compare with it, trace it
        or copy its bitmaps need it; the build does not."""
        path = ROOT / "reference" / self.reference_file
        if not path.exists():
            raise SystemExit(f"{path} not found: this step needs your own copy of "
                             f"{self.reference_file} (a Windows font) there")
        return path

    def load_reference(self):
        return TTCollection(str(self.reference), lazy=True)


PROJECTS = {
    "mincho": Project(
        key="mincho",
        reference_file="msmincho.ttc",
        data=ROOT / "data",
        ufo=ROOT / "src" / "MRMincho.ufo",
        out=ROOT / "work" / "mrmincho.ttc",
        faces=("Mincho", "PMincho"),
        codes="MP",
        suffixes=("", ".p"),
        names=(("MR Mincho", "ＭＲ 明朝", "MR-Mincho"),
               ("MR PMincho", "ＭＲ Ｐ明朝", "MR-PMincho")),
        copyright="Copyright 2026 The MR Mincho Project Authors",
        ltsh_faces=(1,),
    ),
}


def project(key):
    return PROJECTS[key]


def add_font_argument(ap):
    default = next(iter(PROJECTS))
    ap.add_argument("--font", choices=sorted(PROJECTS), default=default,
                    help=f"which family to work on (default {default})")


# Ranges left out of the project: ideographs, kana, half-width katakana,
# enclosed CJK letters and CJK compatibility squares.
EXCLUDED = (
    (0x2E80, 0x2FFF), (0x3040, 0x30FF), (0x31F0, 0x31FF), (0x3200, 0x33FF),
    (0x3400, 0x9FFF), (0xF900, 0xFAFF), (0xFF61, 0xFF9F),
    (0x1F200, 0x1F2FF), (0x20000, 0x2FFFF),
)

BLOCKS = (
    (0x0000, 0x007F, "Basic Latin"), (0x0080, 0x00FF, "Latin-1 Supplement"),
    (0x0100, 0x017F, "Latin Extended-A"), (0x0180, 0x024F, "Latin Extended-B"),
    (0x0250, 0x02AF, "IPA Extensions"), (0x02B0, 0x02FF, "Spacing Modifier Letters"),
    (0x0300, 0x036F, "Combining Diacritical Marks"), (0x0370, 0x03FF, "Greek and Coptic"),
    (0x0400, 0x04FF, "Cyrillic"), (0x0980, 0x09FF, "Bengali"), (0x0E00, 0x0E7F, "Thai"),
    (0x1780, 0x17FF, "Khmer"), (0x1E00, 0x1EFF, "Latin Extended Additional"),
    (0x1F00, 0x1FFF, "Greek Extended"), (0x2000, 0x206F, "General Punctuation"),
    (0x2070, 0x209F, "Superscripts and Subscripts"), (0x20A0, 0x20CF, "Currency Symbols"),
    (0x20D0, 0x20FF, "Combining Marks for Symbols"), (0x2100, 0x214F, "Letterlike Symbols"),
    (0x2150, 0x218F, "Number Forms"), (0x2190, 0x21FF, "Arrows"),
    (0x2200, 0x22FF, "Mathematical Operators"), (0x2300, 0x23FF, "Miscellaneous Technical"),
    (0x2400, 0x243F, "Control Pictures"), (0x2460, 0x24FF, "Enclosed Alphanumerics"),
    (0x2500, 0x257F, "Box Drawing"), (0x2580, 0x259F, "Block Elements"),
    (0x25A0, 0x25FF, "Geometric Shapes"), (0x2600, 0x26FF, "Miscellaneous Symbols"),
    (0x2700, 0x27BF, "Dingbats"), (0x2900, 0x297F, "Supplemental Arrows-B"),
    (0x2980, 0x29FF, "Miscellaneous Mathematical Symbols-B"),
    (0x3000, 0x303F, "CJK Symbols and Punctuation"),
    (0xFB00, 0xFB4F, "Alphabetic Presentation Forms"),
    (0xFB50, 0xFDFF, "Arabic Presentation Forms-A"), (0xFE30, 0xFE4F, "CJK Compatibility Forms"),
    (0xFF00, 0xFFEF, "Halfwidth and Fullwidth Forms"),
)


def in_scope(cp):
    return not any(a <= cp <= b for a, b in EXCLUDED)


def block_name(cp):
    for a, b, name in BLOCKS:
        if a <= cp <= b:
            return name
    raise KeyError(f"no block listed for U+{cp:04X}")


def char_label(cp):
    """Printable form of a codepoint for TSVs: the character itself, or its
    Unicode name when it is a control, space or combining mark."""
    ch = chr(cp)
    if unicodedata.category(ch)[0] in "CZM" or ch in "\t\n\r":
        return f"<{unicodedata.name(ch, f'U+{cp:04X}')}>"
    return ch


class Face:
    """A FreeType face from a font file, addressed by glyph ID."""

    def __init__(self, path, index):
        self.face = freetype.Face(str(path), index)

    def embedded_bitmap(self, gid, ppem):
        """The glyph's embedded bitmap at ppem as (pixels, left, top, advance),
        pixels a bool array with rows top to bottom; None when the strike has
        no bitmap for this glyph."""
        self.face.set_pixel_sizes(0, ppem)
        self.face.load_glyph(gid, freetype.FT_LOAD_DEFAULT)
        slot = self.face.glyph
        if slot.format != freetype.FT_GLYPH_FORMAT_BITMAP:
            return None
        return _unpack(slot.bitmap), slot.bitmap_left, slot.bitmap_top, slot.advance.x >> 6

    def render_outline(self, gid, ppem):
        """The unhinted outline rendered 1-bit at ppem with FreeType's mono
        rasterizer, as (pixels, left, top, advance). At ppem = UPEM * n every
        font unit is exactly n pixels and the origin sits on a pixel corner."""
        self.face.set_pixel_sizes(0, ppem)
        self.face.load_glyph(
            gid,
            freetype.FT_LOAD_NO_BITMAP | freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_TARGET_MONO,
        )
        slot = self.face.glyph
        slot.render(freetype.FT_RENDER_MODE_MONO)
        return _unpack(slot.bitmap), slot.bitmap_left, slot.bitmap_top, slot.advance.x >> 6


    def render_gray(self, gid, ppem):
        """The unhinted outline rendered anti-aliased at ppem, ignoring any
        embedded bitmap, as (coverage 0-255 uint8 array, left, top)."""
        self.face.set_pixel_sizes(0, ppem)
        self.face.load_glyph(gid, freetype.FT_LOAD_NO_BITMAP | freetype.FT_LOAD_NO_HINTING)
        slot = self.face.glyph
        slot.render(freetype.FT_RENDER_MODE_NORMAL)
        bm = slot.bitmap
        if bm.rows == 0 or bm.width == 0:
            return np.zeros((0, 0), np.uint8), slot.bitmap_left, slot.bitmap_top
        buf = np.frombuffer(bytes(bm.buffer), np.uint8).reshape(bm.rows, abs(bm.pitch))[:, :bm.width]
        return buf.copy(), slot.bitmap_left, slot.bitmap_top

    def render_hinted_mono(self, gid, ppem):
        """The outline hinted by its TrueType instructions and rendered 1-bit
        at ppem, as a renderer following the gasp table draws it at small
        sizes; (0/255 uint8 array, left, top)."""
        self.face.set_pixel_sizes(0, ppem)
        self.face.load_glyph(gid, freetype.FT_LOAD_NO_BITMAP | freetype.FT_LOAD_NO_AUTOHINT
                             | freetype.FT_LOAD_TARGET_MONO)
        slot = self.face.glyph
        slot.render(freetype.FT_RENDER_MODE_MONO)
        return _unpack(slot.bitmap).astype(np.uint8) * 255, slot.bitmap_left, slot.bitmap_top

    def hinted_advance(self, gid, ppem, mono):
        """The advance in 26.6 pixels after TrueType hinting at ppem,
        embedded bitmaps ignored; mono selects the black-and-white target."""
        self.face.set_pixel_sizes(0, ppem)
        target = freetype.FT_LOAD_TARGET_MONO if mono else freetype.FT_LOAD_TARGET_NORMAL
        self.face.load_glyph(gid, freetype.FT_LOAD_NO_BITMAP | freetype.FT_LOAD_NO_AUTOHINT | target)
        return self.face.glyph.advance.x


def _unpack(bitmap):
    rows, width, pitch = bitmap.rows, bitmap.width, bitmap.pitch
    if rows == 0 or width == 0:
        return np.zeros((0, 0), dtype=bool)
    if bitmap.pixel_mode != freetype.FT_PIXEL_MODE_MONO:
        raise ValueError(f"unexpected pixel mode {bitmap.pixel_mode}")
    buf = np.frombuffer(bytes(bitmap.buffer), dtype=np.uint8).reshape(rows, abs(pitch))
    if pitch < 0:
        buf = buf[::-1]
    return np.unpackbits(buf, axis=1)[:, :width].astype(bool)


def placed(pixels, left, top):
    """The set of inked (x, y) pixel positions relative to the glyph origin,
    y counting up from the baseline."""
    ys, xs = np.nonzero(pixels)
    return {(left + x, top - 1 - y) for y, x in zip(ys.tolist(), xs.tolist())}


def align(a, b):
    """Place two (pixels, left, top) renders on one canvas; returns two
    arrays of the same shape and the renders' dtype."""
    dtype = a[0].dtype
    parts = [p for p in (a, b) if p[0].size]
    if not parts:
        return np.zeros((0, 0), dtype), np.zeros((0, 0), dtype)
    x0 = min(left for _, left, _ in parts)
    x1 = max(left + px.shape[1] for px, left, _ in parts)
    y1 = max(top for _, _, top in parts)
    y0 = min(top - px.shape[0] for px, _, top in parts)
    out = []
    for px, left, top in (a, b):
        c = np.zeros((y1 - y0, x1 - x0), dtype)
        if px.size:
            c[y1 - top:y1 - top + px.shape[0], left - x0:left - x0 + px.shape[1]] = px
        out.append(c)
    return out


def compare_outlines(ref, test, scale, perimeter):
    """Compare two (pixels, left, top) renders made at scale pixels per font
    unit. perimeter is the reference outline length in units.

    iou       intersection over union of inked pixels
    xor       area inked in exactly one, in square units
    mean_dev  xor / perimeter: the average distance between the outlines
    max_dev   the largest distance from a pixel inked in only one render to
              the nearest pixel inked in the other, in units
    """
    from scipy.ndimage import distance_transform_edt

    a, b = align(ref, test)
    if a.size == 0:
        return {"iou": 1.0, "xor": 0.0, "mean_dev": 0.0, "max_dev": 0.0}
    union = (a | b).sum()
    xor = a ^ b
    max_dev = 0.0
    if xor.any() and not (a.any() and b.any()):
        max_dev = float("inf")
    elif xor.any():
        for this, other in ((a, b), (b, a)):
            only = np.pad(this & ~other, 1)
            if only.any():
                max_dev = max(max_dev, distance_transform_edt(~np.pad(other, 1))[only].max())
    xor_area = xor.sum() / scale**2
    return {
        "iou": float((a & b).sum() / union) if union else 1.0,
        "xor": float(xor_area),
        "mean_dev": float(xor_area / perimeter) if perimeter else 0.0,
        "max_dev": float(max_dev / scale),
    }


def perimeter(glyphset, name):
    from fontTools.pens.perimeterPen import PerimeterPen

    pen = PerimeterPen(glyphset)
    glyphset[name].draw(pen)
    return pen.value


def grid(pixels):
    return "\n".join("".join("#" if v else "." for v in row) for row in pixels)
