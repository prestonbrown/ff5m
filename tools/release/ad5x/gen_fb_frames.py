#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the AD5X flash-time framebuffer status frames.

The AD5X touch panel is portrait 480x800 at 32 bits/pixel. A raw framebuffer
dump is therefore exactly 480 * 800 * 4 = 1,536,000 bytes. The flash-time
installer paints one of these to the panel with:

    xzcat img/<name>.raw.xz > /dev/fb0

so every frame this script emits is an xz-compressed raw framebuffer dump.

This is the reproducible source for the four Forge-X status frames, replacing
the source-less committed binaries the AD5M install path shipped
(skip.img.xz / uninstall.img.xz at the repo root). Run it with:

    python3 tools/release/ad5x/gen_fb_frames.py

It is deterministic: no timestamps, no randomness, same bytes on every run.
Text is rendered with Pillow's bundled scalable font when Pillow is importable;
otherwise it falls back to a pure-stdlib solid background plus a distinguishable
geometric marker so all four frames are still produced.
"""

import hashlib
import lzma
import os
import sys

# --- Panel format ------------------------------------------------------------
WIDTH = 480
HEIGHT = 800
BYTES_PER_PIXEL = 4
EXPECTED_SIZE = WIDTH * HEIGHT * BYTES_PER_PIXEL  # 1,536,000

# Byte order written per pixel. Common FlashForge/Allwinner panels are 32bpp
# little-endian, which in memory is byte order B,G,R,A (i.e. 0xAARRGGBB stored
# little-endian). If red and blue look swapped on the printer, change this to
# "RGBA" and regenerate -- it is the only line that needs to change.
CHANNEL_ORDER = "BGRA"

# Source channel index for each letter, so CHANNEL_ORDER alone drives byte order.
_CH_INDEX = {"R": 0, "G": 1, "B": 2, "A": 3}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img")

# --- Frame definitions -------------------------------------------------------
# bg is an (R, G, B) triple. Colors are chosen to be distinguishable at a
# glance; exact fidelity is not important for these cosmetic status screens.
FRAMES = [
    {
        "name": "forgex-install",
        "bg": (28, 92, 196),      # blue
        "title": "Installing Forge-X",
        "subtitle": "Please wait...",
    },
    {
        "name": "forgex-complete",
        "bg": (40, 158, 74),      # green
        "title": "Complete",
        "subtitle": "Rebooting...",
    },
    {
        "name": "forgex-error",
        "bg": (198, 48, 48),      # red
        "title": "Update error",
        "subtitle": "Installation failed",
    },
    {
        "name": "forgex-nospace",
        "bg": (232, 148, 30),     # amber / orange
        "title": "Not enough space",
        "subtitle": "512 MB needed",
    },
]

TEXT_COLOR = (255, 255, 255)


def reorder_to_channel_order(rgba: bytes) -> bytes:
    """Convert an RGBA byte buffer into CHANNEL_ORDER, plane by plane."""
    if len(rgba) != EXPECTED_SIZE:
        raise ValueError(
            f"source buffer is {len(rgba)} bytes, expected {EXPECTED_SIZE}"
        )
    out = bytearray(EXPECTED_SIZE)
    for out_pos, letter in enumerate(CHANNEL_ORDER):
        src_pos = _CH_INDEX[letter]
        out[out_pos::BYTES_PER_PIXEL] = rgba[src_pos::BYTES_PER_PIXEL]
    return bytes(out)


def render_rgba_pillow(frame: dict) -> bytes:
    """Render a frame with Pillow, returning a WIDTH*HEIGHT RGBA buffer."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (WIDTH, HEIGHT), frame["bg"])
    draw = ImageDraw.Draw(img)

    title_font = ImageFont.load_default(size=52)
    subtitle_font = ImageFont.load_default(size=32)

    def centered(text, font, y):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2 - bbox[0]
        draw.text((x, y), text, font=font, fill=TEXT_COLOR)

    # Title a little above center, subtitle just below it.
    centered(frame["title"], title_font, HEIGHT // 2 - 60)
    centered(frame["subtitle"], subtitle_font, HEIGHT // 2 + 20)

    return img.convert("RGBA").tobytes()


def render_rgba_stdlib(frame: dict) -> bytes:
    """Render a frame without Pillow: solid background plus a geometric marker.

    Produces a distinguishable frame using only the standard library: a solid
    background, a full-width horizontal band, and a bordered box centered on
    the panel. Still a valid 480x800 RGBA buffer.
    """
    r, g, b = frame["bg"]
    buf = bytearray(bytes((r, g, b, 255)) * (WIDTH * HEIGHT))

    def fill_rect(x0, y0, x1, y1, color):
        x0 = max(0, min(WIDTH, x0))
        x1 = max(0, min(WIDTH, x1))
        y0 = max(0, min(HEIGHT, y0))
        y1 = max(0, min(HEIGHT, y1))
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes((color[0], color[1], color[2], 255)) * (x1 - x0)
        for y in range(y0, y1):
            start = (y * WIDTH + x0) * BYTES_PER_PIXEL
            buf[start:start + len(row)] = row

    # Full-width horizontal band above center.
    fill_rect(0, HEIGHT // 2 - 140, WIDTH, HEIGHT // 2 - 60, TEXT_COLOR)

    # Bordered box below center: white border, background-colored interior.
    bx0, by0, bx1, by1 = 60, HEIGHT // 2 + 20, WIDTH - 60, HEIGHT // 2 + 160
    fill_rect(bx0, by0, bx1, by1, TEXT_COLOR)
    fill_rect(bx0 + 8, by0 + 8, bx1 - 8, by1 - 8, frame["bg"])

    return bytes(buf)


def build_frame(frame: dict, use_pillow: bool) -> bytes:
    rgba = render_rgba_pillow(frame) if use_pillow else render_rgba_stdlib(frame)
    if len(rgba) != EXPECTED_SIZE:
        raise ValueError(
            f"{frame['name']}: rendered {len(rgba)} bytes, "
            f"expected {EXPECTED_SIZE}"
        )
    raw = reorder_to_channel_order(rgba)
    if len(raw) != EXPECTED_SIZE:
        raise ValueError(
            f"{frame['name']}: framebuffer is {len(raw)} bytes, "
            f"expected {EXPECTED_SIZE} ({WIDTH}x{HEIGHT}x{BYTES_PER_PIXEL})"
        )
    return raw


def main() -> int:
    try:
        import PIL  # noqa: F401
        use_pillow = True
    except ImportError:
        use_pillow = False

    os.makedirs(OUT_DIR, exist_ok=True)

    mode = "text (Pillow)" if use_pillow else "geometric (stdlib fallback)"
    print(f"Rendering mode: {mode}")
    print(f"Channel order:  {CHANNEL_ORDER}")
    print(f"Output dir:     {OUT_DIR}")

    for frame in FRAMES:
        raw = build_frame(frame, use_pillow)

        # Deterministic xz container: fixed preset, no timestamps.
        compressed = lzma.compress(raw, format=lzma.FORMAT_XZ, preset=6)

        path = os.path.join(OUT_DIR, frame["name"] + ".raw.xz")
        with open(path, "wb") as fh:
            fh.write(compressed)

        # Verify on disk: decompress and check the exact byte count.
        with open(path, "rb") as fh:
            roundtrip = lzma.decompress(fh.read())
        if len(roundtrip) != EXPECTED_SIZE:
            raise ValueError(
                f"{path}: decompresses to {len(roundtrip)} bytes, "
                f"expected {EXPECTED_SIZE}"
            )

        sha = hashlib.sha256(compressed).hexdigest()
        print(
            f"  {frame['name']+'.raw.xz':24s} "
            f"decompressed={len(roundtrip)} bytes  sha256={sha}"
        )

    print("All frames verified: each decompresses to exactly "
          f"{EXPECTED_SIZE} bytes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
