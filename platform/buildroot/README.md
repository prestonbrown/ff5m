# Forge-X platform rootfs

Buildroot configuration that produces the chroot rootfs Forge-X runs in.

```sh
docker build -t forgex-br --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
docker run --rm -v "$PWD/../..:/work" -w /work/platform/buildroot forgex-br ./build.sh ad5x -j"$(nproc)"
```

The result is `output/ad5x/images/rootfs.tar.xz`, which the installer unpacks
into `$MOD` (`/data/.mod/.forge-x` on AD5M, the equivalent on AD5X).

## Why this exists

Forge-X mounts a second rootfs and enters it with `chroot`, because the stock
FlashForge firmware ships a Python too old for a current Moonraker. That rootfs
has never been buildable from source: the released `.tgz` carries it as an
opaque `xz/buildroot.tar.xz`, and no configuration for it is published
anywhere.

It is also not Forge-X's own. Unpacking the 1.4.1 release and reading
`etc/os-release` gives:

```
NAME=Buildroot_zmod
PRETTY_NAME="zmod 1.0.5"
```

The shipped ARM rootfs is ZMOD's Buildroot image. That is a working
arrangement and nothing here is a criticism of it, but it does mean the
platform layer is currently a binary inherited from another project, which is
awkward for a GPL-3.0 firmware and impossible to extend to a new CPU
architecture. This tree replaces it with something that builds from source.

## Target: FlashForge AD5X

Ingenic X2600 (XBurst II), MIPS32r5 little-endian, 2 cores, 485 MB RAM,
kernel 5.10.186.

### The ABI is not a choice

Every parameter below was read off the printer, not inferred. Get one wrong
and you produce binaries that link, load, and then behave incorrectly.

| Parameter | Value | How it was established |
|---|---|---|
| ABI | **o32** | `o32` token in `e_flags` on every stock binary; `GPR size: 32` |
| Float | **hard, FP64** | `Tag_GNU_MIPS_ABI_FP = 6`; `CPR1 size: 64`; FPU FIR `0x00f32000` bit 22 set |
| NaN | **nan2008** | every stock object; FIR bit 23 (HAS2008) set in hardware |
| Loader | `/lib/ld-linux-mipsn8.so.1` | the `n8` suffix *is* the NaN2008 marker |
| Endian | little | `mipsel` |
| MSA | **not used** | 0 of 630 objects surveyed across both rootfs trees |

Corroborated independently by glibc itself, which reports its own ABI list as
`MIPS_PLT UNIQUE MIPS_O32_FP64 ABSOLUTE MIPS_XHASH`.

### Two traps worth knowing before you touch the defconfig

**Buildroot defaults MIPS to soft-float.** `BR2_MIPS_SOFT_FLOAT` is
`default y`, and both the FP-mode choice and the NaN-2008 option are
`depends on !BR2_MIPS_SOFT_FLOAT`. Omit the explicit disable and they vanish
from the config with no warning, leaving a soft-float legacy-NaN toolchain
whose loader is `/lib/ld.so.1` — a path that does not exist on this printer.

**`e_flags` cannot distinguish MIPS32r2 from r5.** Both encode as
`EF_MIPS_ARCH_32R2`; only r6 got a new value. The `ISA:` field in the
`.MIPS.abiflags` section is the one that tells them apart. Read the wrong one
and you will conclude a binary is r2 when it is r5.

### ISA level

Stock FlashForge is MIPS32r2 with glibc 2.33; ZMOD ships MIPS32r5 with glibc
2.40 and GCC 13.3, and both run on the same kernel on the same SoC. We take
r5, on the grounds that it is field-proven here rather than merely plausible.
Buildroot 2025.02.4 pins GCC 13.3.0, so that half matches ZMOD exactly.

## Buildroot release

Pinned to **2025.02.4** in `build.sh`. Two reasons, and they happen to point
the same way: 2025.02 is the current LTS (three years of support, to Feb 2028
— the first release under Buildroot's extended LTS policy), and it is the last
line shipping **Python 3.12**. Everything newer has moved to 3.13/3.14.

The Python version is a property of the Buildroot release, so moving the tag
moves the interpreter. Its package versions also land inside Moonraker's
dependency pins, which Buildroot master no longer does.

## What the image must provide

The Forge-X payload assumes a good deal about this rootfs. The load-bearing
parts:

- `/proc`, `/sys`, `/dev`, `/run`, `/tmp` — mount targets, created before any
  `mkdir` runs, so they must exist in the image
- `/root/moonraker-env/` — a working venv whose `bin/python3` runs Moonraker
- `/root/fluidd`, `/root/mainsail` — web bundles, moved to `/root/www/` on
  first init
- `/etc/init.d/` — **empty**. `.root/start.sh` runs every `S*` it finds there,
  so Buildroot's default service scripts would start a second syslogd and
  network stack alongside the host's. `post-build.sh` removes them.
- `bash` as `/bin/sh` — `.root/stop.sh` has a `#!/bin/sh` shebang but uses
  process substitution
- `/usr/bin/python` unsuffixed — `.py/backlight.py` has an
  `#!/usr/bin/env python` shebang. Buildroot never creates that name.
- no `/ZMOD` marker file — `zversion.sh` hard-fails on it

### The venv

`post-build.sh` builds it statically rather than running `python -m venv`,
which would mean executing a MIPS interpreter on an x86 build host. The layout
is what `venv` produces on Linux, where symlinked interpreters are the default.

`include-system-site-packages` is **true**, so the dependencies Buildroot
installs into `/usr/lib/pythonX.Y/site-packages` are visible to Moonraker.
ZMOD instead duplicates its whole dependency set into the venv, which costs
about 25 MB to no purpose on an appliance running exactly one Python program.

## Size

The reference ARM image is ~196 MB installed. (`du` reports 699 MB, but 137 of
those megabytes' worth of files are identical copies of the same 3.6 MB `git`
binary — `libexec/git-core` is a hardlink farm that survives the tar as
separate files.) ZMOD's own MIPS chroot is ~355 MB once its bind mounts are
subtracted from the naive 1 GB figure.

Most of both numbers is optional: a native GCC and binutils, 90 MB of locales,
Midnight Commander, and a matplotlib/numpy stack that only the input-shaper
graphs use. A rootfs that skips those has no business being over ~80 MB.
