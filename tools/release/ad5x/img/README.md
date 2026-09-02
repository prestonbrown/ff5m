# AD5X flash-time framebuffer status frames

These four `*.raw.xz` files are the flash-time status screens the Forge-X
installer paints directly to the AD5X panel before the UI is up.

Unlike the AD5M frames (`skip.img.xz` / `uninstall.img.xz` at the repo root),
which are committed binaries with no source, these are **reproducible**: they
are generated from a committed script and regenerate to identical bytes.

## Frames

| File | Meaning | Background |
|------|---------|------------|
| `forgex-install.raw.xz`  | "Installing Forge-X"   | blue |
| `forgex-complete.raw.xz` | "Complete / Rebooting" | green |
| `forgex-error.raw.xz`    | "Update error"         | red |
| `forgex-nospace.raw.xz`  | "Not enough space (512 MB needed)" | amber |

## Format

- **Panel:** AD5X, portrait **480 x 800**, 32 bits/pixel.
- Each file is an **xz-compressed raw framebuffer dump**. It decompresses to
  exactly `480 * 800 * 4 = 1,536,000` bytes (one 4-byte pixel per position, row
  by row, no padding/stride beyond the pixels themselves).
- The installer paints a frame with:

  ```sh
  xzcat img/<name>.raw.xz > /dev/fb0
  ```

## Byte order

Pixels are written **`B, G, R, A`** (channel order `BGRA`, `A = 0xFF`). This
matches the common FlashForge/Allwinner 32bpp little-endian layout, where a
pixel `0xAARRGGBB` is stored little-endian as the bytes `B G R A`.

The order is a single named constant (`CHANNEL_ORDER`) at the top of the
generator. **If red and blue look swapped on the printer**, change it to
`"RGBA"` and regenerate — that one line is the only change needed.

These are cosmetic status screens for the first proof, so exact color fidelity
is not important; distinguishability and correct dimensions are.

## Regenerate

```sh
python3 tools/release/ad5x/gen_fb_frames.py
```

The generator is deterministic (no timestamps, no randomness): a second run
produces byte-identical output. Text is rendered with Pillow's bundled scalable
font when `PIL` is importable; without Pillow it falls back to a pure-stdlib
solid background plus a geometric marker, still emitting all four valid frames.

The generator asserts each buffer is exactly 1,536,000 bytes before compressing
and re-checks each file on disk after writing.

## Verify

Every frame must decompress to exactly 1,536,000 bytes:

```sh
for f in tools/release/ad5x/img/*.raw.xz; do
  printf '%s %s\n' "$f" "$(xzcat "$f" | wc -c)"   # expect 1536000
done
```

### On-rig verification (AD5X)

Copy a frame to the printer and paint it to the framebuffer:

```sh
xzcat forgex-install.raw.xz > /dev/fb0
```

The panel should show a solid blue screen with white "Installing Forge-X" text.
Confirm the background reads blue (not red) — if it reads red, the panel wants
`RGBA`: set `CHANNEL_ORDER = "RGBA"` in the generator, regenerate, and re-test.
