# Where the pinned stock trees came from

`merge.sh` derives the AD5X Klipper patch set from three inputs. Two of them are
vendor code and are pinned here so the merge is reproducible without a printer.

## `stock/ad5x/` - FlashForge AD5X

Extracted from the factory image, which FlashForge ships with the Klipper host in
`other/klippy.tar`:

    tar xf AD5X-1.1.7-1.1.0-3.0.6-20250912-Factory.tgz ./other/klippy.tar
    tar xf other/klippy.tar

    sha256  46c151bb2f9beee195965ec9f012af78f511704883302f63c6c8da2e6f6a0730
    from    https://github.com/ghzserg/FF/releases/download/R/AD5X-1.1.7-1.1.0-3.0.6-20250912-Factory.tgz

Despite the `.tgz` and `.tar.xz` suffixes, every archive in a FlashForge image is
plain uncompressed tar. `tar xzf` and `tar xJf` both fail on them.

**The whole `klippy/` tree is byte-identical between 1.1.7 (2025-09-12) and 1.2.3
(2026-02-05)**, checked file by file - not just the files we patch. So the Klipper
layer is stable across the stock releases we care about, and tracking current stock
rather than pinning to the factory image costs nothing here.

    1.2.3  sha256  d2f5f30ce051d7707aa9bd8a51d328f7bdf25ccc0f9e0cf322a8356bd8647d2b

## `stock/ad5m/` - FlashForge AD5M, the merge base

This is what Forge-X's own patches were written against, and it is the base the
three-way merge attributes changes from.

It is **not** in the AD5M factory image. Unlike AD5X, the AD5M image carries no
Klipper host at all - `boot.img`, `control-*`, `kernel-*`, `library-*` and
`software-*` contain zero `klippy` entries. On that platform Klipper lives in the
printer's own rootfs. So this copy was taken off a printer rather than unpacked
from an image.

Its provenance is checked a different way, and the check is strong: diff each file
against the **first commit in which Forge-X added it**. Every difference is a
change Forge-X's own commit message claims. For `virtual_sdcard.py` (Forge-X
`2a78fa6`, "Skip hidden files while searching for gcode") the entire delta is the
hidden-file skip, re-enabling the case-insensitive filename fallback, dropping
`'gx'`, and dropping `errors='ignore'`. For `statistics.py` (`69f5753`, "Skip some
of Klipper debug logging") it is only the `disabled` option. Nothing unexplained
is left over, which is what a true merge base looks like.

## Licensing

Klipper is GPL-3.0 and these are GPL-3.0 derivatives, redistributable on those
terms. Forge-X already ships modified copies of the same files; these are the
unmodified ones, kept so the modification is reviewable.
