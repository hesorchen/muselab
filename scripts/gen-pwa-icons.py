#!/usr/bin/env python3
"""Generate PWA icon PNGs from scratch using only Python stdlib.

iOS apple-touch-icon must be PNG (Safari ignores SVG icons), and
Android maskable icons are best delivered as PNG too so the platform
mask receives clean pixels. Everything else (favicon, manifest "any"
icons) can use the SVG source in frontend/assets/icon.svg directly.

Re-run after editing the design constants below — no external deps.

  python3 scripts/gen-pwa-icons.py

Outputs into frontend/assets/:
  apple-touch-icon.png   180x180  (iOS home-screen)
  icon-512.png           512x512  (PWA "any" purpose, fallback)
  icon-512-maskable.png  512x512  (Android adaptive icon safe-zone)
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "frontend" / "assets"

# Design space is 200×200 (matches icon.svg viewBox); all geometry below
# is in those units and scaled per target size.
#
# Brand mark: pure "M" zigzag (Candidate F, user-selected 2026-05-25).
# Five vertices, 4 stroke segments, rounded line-caps + joins.
ACCENT = (0x60, 0x93, 0xFF)
WHITE = (255, 255, 255)

# Standard ("any" purpose) variant — rounded corners
ROUNDED_RADIUS = 44.0
# M vertices (left-bottom, left-top, middle-bottom-of-V, right-top, right-bottom)
M_PTS = [(56.0, 144.0), (56.0, 56.0), (100.0, 100.0), (144.0, 56.0), (144.0, 144.0)]
M_STROKE = 20.0

# Maskable variant — square, M inset to 80% safe zone (40..160)
MASK_RADIUS = 0.0
MASK_PTS = [(60.0, 140.0), (60.0, 60.0), (100.0, 100.0), (140.0, 60.0), (140.0, 140.0)]
MASK_STROKE = 16.0

AA = 4  # 4x4 super-sampling — clean edges on small (32, 180) sizes


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode raw RGBA scanlines (filter=0, color_type=6) as a PNG file."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    magic = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = bytearray()
    bpr = width * 4
    for y in range(height):
        raw.append(0)  # filter "None" per scanline
        raw.extend(rgba[y * bpr : (y + 1) * bpr])
    idat = zlib.compress(bytes(raw), 9)
    return magic + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def dist_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Min distance from point P to segment AB. Used to check whether a
    pixel lies within stroke/2 of one of the M's line segments — gives
    rounded line-caps and joins for free."""
    dx = bx - ax
    dy = by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-9:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    fx = ax + t * dx
    fy = ay + t * dy
    return math.hypot(px - fx, py - fy)


def rounded_inside(px: float, py: float, radius: float) -> bool:
    """Test if (px, py) ∈ [0, 200]² rounded rect with given corner radius."""
    if px < 0 or py < 0 or px > 200 or py > 200:
        return False
    rx = max(radius - px, 0.0, px - (200.0 - radius))
    ry = max(radius - py, 0.0, py - (200.0 - radius))
    return rx * rx + ry * ry <= radius * radius


def render(size: int, *, maskable: bool) -> bytes:
    """Render an RGBA image. Coordinates are computed in 200-unit design
    space then mapped to `size` pixels with AA×AA super-sampling.

    Foreground = "M" zigzag drawn as 4 line segments; pixel is on the
    stroke iff its distance to any segment <= stroke/2. Rounded line
    caps + joins are inherent to the SDF check."""
    s = size / 200.0  # pixels per design unit
    pts = MASK_PTS if maskable else M_PTS
    stroke = MASK_STROKE if maskable else M_STROKE
    radius = MASK_RADIUS if maskable else ROUNDED_RADIUS
    half_stroke = stroke / 2.0

    pixels = bytearray(size * size * 4)
    accent_r, accent_g, accent_b = ACCENT

    # Precompute sub-sample offsets in design space
    sub = [(i + 0.5) / AA for i in range(AA)]

    for y in range(size):
        for x in range(size):
            i = (y * size + x) * 4
            shape_hits = 0
            fg_hits = 0

            for sy in sub:
                py_des = (y + sy) / s
                for sx in sub:
                    px_des = (x + sx) / s

                    # 1) shape mask: rounded rect (or full square for maskable)
                    if maskable:
                        in_shape = True
                    else:
                        in_shape = rounded_inside(px_des, py_des, radius)
                    if not in_shape:
                        continue
                    shape_hits += 1

                    # 2) foreground = on any of the 4 M segments
                    on_fg = False
                    for k in range(4):
                        ax, ay = pts[k]
                        bx, by = pts[k + 1]
                        if dist_seg(px_des, py_des, ax, ay, bx, by) <= half_stroke:
                            on_fg = True
                            break
                    if on_fg:
                        fg_hits += 1

            total = AA * AA
            if shape_hits == 0:
                # fully transparent — leave the bytearray zeroed
                continue

            shape_alpha = shape_hits / total
            fg_ratio = fg_hits / shape_hits  # foreground share of the in-shape pixels
            # Composite: white over accent within the shape, then apply shape alpha
            r = round(255 * fg_ratio + accent_r * (1 - fg_ratio))
            g = round(255 * fg_ratio + accent_g * (1 - fg_ratio))
            b = round(255 * fg_ratio + accent_b * (1 - fg_ratio))
            a = round(255 * shape_alpha)

            pixels[i] = r
            pixels[i + 1] = g
            pixels[i + 2] = b
            pixels[i + 3] = a

    return bytes(pixels)


TARGETS = [
    ("apple-touch-icon.png", 180, False),
    ("icon-192.png", 192, False),
    ("icon-192-maskable.png", 192, True),
    ("icon-512.png", 512, False),
    ("icon-512-maskable.png", 512, True),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for fname, size, maskable in TARGETS:
        print(f"  rendering {fname} ({size}x{size}{', maskable' if maskable else ''})",
              flush=True)
        rgba = render(size, maskable=maskable)
        png = encode_png(size, size, rgba)
        (OUT / fname).write_bytes(png)
        print(f"    wrote {len(png)} bytes")
    print(f"Done — {len(TARGETS)} icons in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
