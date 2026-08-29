#!/usr/bin/env python3

## Render the AD5X's "boot finished" panel image.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
## The AD5X has no working on-screen UI yet: Forge-X's splash binary is an ARM
## build and this board is MIPS, so nothing paints the framebuffer once the
## stock UI has been stopped, and the panel is left white. Until HelixScreen
## lands, this draws the boot logo plus a few status lines and the bring-up
## pushes it out with cmd_jpeg_display.
##
## Deliberately font-free: the only TTFs on a real AD5X belong to HelixScreen,
## and depending on those would invert the dependency. Pillow's bundled bitmap
## font upscaled with NEAREST reads as intentional against the logo's terminal
## subtitle, and it works on the printer's Pillow 7.0.0 as well as a modern one.

import argparse
import sys

from PIL import Image, ImageDraw, ImageFont

SCALE = 3                      # bitmap font is ~6x11; 3x is legible at 800x480
## The logo's own subtitle bar and its glow occupy roughly the lower-middle
## of the image, so status text lives under it. Anything taller than the gap
## gets clamped rather than drawn off the bottom edge.
SAFE_TOP = 424
TEXT = (233, 255, 255)         # matches the logo subtitle's near-white cyan
SHADOW = (0, 0, 0)
SHADOW_ALPHA = 140
MARGIN_X = 30
BOTTOM_PAD = 14
LINE_GAP = 4


def ink_box(draw, text, font):
    """Size and origin of the inked area.

    Pillow 7 has textsize and measures from the drawing origin; Pillow 10+
    replaced it with textbbox, whose top/left can be non-zero. Drawing at (0, 0)
    and sizing the canvas to bottom-top then clips the glyphs, so the offset has
    to come back with the size.
    """
    if hasattr(draw, "textbbox"):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top, left, top
    w, h = draw.textsize(text, font=font)
    return w, h, 0, 0


def render_line(text, font, probe, scale=SCALE):
    """One line, drawn small and scaled up so the pixels stay crisp."""
    w, h, off_x, off_y = ink_box(probe, text, font)
    w, h = max(w, 1), max(h, 1)
    cell = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(cell).text((-off_x, -off_y), text, font=font, fill=TEXT + (255,))
    return cell.resize((w * scale, h * scale), Image.NEAREST)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render the AD5X status panel")
    ap.add_argument("--base", required=True, help="background image (the boot logo)")
    ap.add_argument("--out", required=True, help="where to write the JPEG")
    ap.add_argument("--line", action="append", default=[],
                    help="a status line; repeat for more")
    ap.add_argument("--scale", type=int, default=SCALE)
    ap.add_argument("--quality", type=int, default=92)
    args = ap.parse_args(argv)

    base = Image.open(args.base).convert("RGB")
    font = ImageFont.load_default()
    probe = ImageDraw.Draw(base)

    cells = [render_line(t, font, probe, args.scale) for t in args.line if t]
    if cells:
        total = sum(c.height for c in cells) + LINE_GAP * (len(cells) - 1)
        y = base.height - BOTTOM_PAD - total
        ## Never ride up over the logo, and never run off the bottom.
        y = max(y, SAFE_TOP)
        for cell in cells:
            ## A one-pixel shadow keeps the text readable where the logo's
            ## reflection is bright underneath it.
            shadow = Image.new("RGBA", cell.size, (0, 0, 0, 0))
            shadow.paste(SHADOW + (SHADOW_ALPHA,), (0, 0), cell)
            if y + cell.height > base.height:
                break
            base.paste(shadow, (MARGIN_X + 2, y + 2), shadow)
            base.paste(cell, (MARGIN_X, y), cell)
            y += cell.height + LINE_GAP

    base.save(args.out, "JPEG", quality=args.quality)
    return 0


if __name__ == "__main__":
    sys.exit(main())
