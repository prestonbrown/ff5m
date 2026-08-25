# Forge-X platform rootfs

Buildroot configuration for the second userland that Forge-X runs its services
in. The output is a rootfs tarball that the installer unpacks into `$MOD` on
the printer.

```sh
docker build -t forgex-br --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
docker run --rm -v "$PWD/../..:/work" -w /work/platform/buildroot forgex-br \
    ./build.sh ad5x -j"$(nproc)"
```

Result: `output/ad5x/images/rootfs.tar.xz`.

---

## Why a second userland exists at all

The stock FlashForge firmware ships Python 3.8.2 and runs Klipper on it. A
current Moonraker needs 3.10 or newer, and several of its dependencies now
require 3.10 outright. Since the stock root filesystem is a 12.5 MB read-only
squashfs with no room and no package manager, the mod brings its own userland,
mounts it alongside, and enters it with `chroot`.

That boundary is worth stating precisely, because it constrains everything
below: **Klipper stays on the host, on stock Python.** Only Moonraker and the
web stack live in this rootfs. The two halves meet over a Unix socket in
`/tmp`, which is bind-mounted into the chroot. So this image does not need to
satisfy Klipper, numpy, or anything on the motion path - it needs to run one
Python service and serve some static files.

## Why this tree exists

The rootfs has historically been distributed as a prebuilt binary with no
published configuration. That is workable until you need to change something:
add a dependency Moonraker started requiring, pick up a CVE fix in OpenSSL,
or target a different CPU. Then there is nothing to edit.

This tree makes the rootfs an output rather than an input. The build is pinned
end to end - the Buildroot release in `build.sh`, the host toolchain in the
`Dockerfile` - so the same commit produces the same image next year, and CI
can rebuild it without anyone's workstation being involved.

---

## Target: FlashForge AD5X

Ingenic X2600 (XBurst II), MIPS32r5 little-endian, 2 cores, 485 MB RAM,
kernel 5.10.186.

### The ABI is fixed by the target, not chosen

Every parameter below was read off the printer. The kernel and the dynamic
loader enforce them, and a mismatch produces binaries that link, load, and
then behave incorrectly - which is a considerably worse failure than not
building at all.

| Parameter | Value | Established by |
|---|---|---|
| ABI | **o32** | `o32` token in `e_flags` on every stock binary; `GPR size: 32` |
| Float | **hard, FP64** | `Tag_GNU_MIPS_ABI_FP = 6`; `CPR1 size: 64`; FPU FIR `0x00f32000` bit 22 |
| NaN | **nan2008** | every stock object; FIR bit 23 (HAS2008) set in hardware |
| Loader | `/lib/ld-linux-mipsn8.so.1` | the `n8` suffix *is* the NaN2008 marker |
| Endian | little | `mipsel` |
| MSA | **unused** | 0 of 630 objects surveyed across the stock and mod rootfs trees |

glibc corroborates this independently: it reports its own ABI list as
`MIPS_PLT UNIQUE MIPS_O32_FP64 ABSOLUTE MIPS_XHASH`.

The corresponding defconfig lines are `BR2_mipsel`, `BR2_MIPS_FP32_MODE_64`,
`BR2_MIPS_ENABLE_NAN_2008`, and an explicit `# BR2_MIPS_SOFT_FLOAT is not set`
- see the traps section for why that last one is not optional.

---

## Decisions

### Buildroot 2025.02.4

Pinned in `build.sh`. Two requirements point at the same release, which is
lucky rather than clever: it is the current LTS, supported to February 2028
under Buildroot's extended LTS policy, and it is the last line shipping
**Python 3.12**. Everything newer has moved to 3.13 or 3.14.

Python 3.12 matters because on this target every compiled dependency is built
from source - no project publishes MIPS wheels - and 3.12 has years of
ecosystem support behind it where 3.14 does not. The release's own package
versions also land inside Moonraker's dependency pins, which Buildroot master
no longer does.

Consequence worth knowing: **the Python version is a property of the Buildroot
release.** Moving the tag moves the interpreter.

### MIPS32r5, not r2

The stock rootfs is r2. r5 is a step up and the CPU implements it, but "the
datasheet says yes" is weaker evidence than we want for something that only
fails on a customer's printer. The deciding fact is that ghzserg's ZMOD ships
an r5 build that runs on this exact SoC in the field, so r5 is proven here
rather than merely permitted.

MSA stays off for the opposite reason: the CPU implements it, but nothing on
the printer uses it, so there is no precedent to lean on and nothing to gain.

### Only what Moonraker needs

The package list is derived from what Forge-X's vendored Moonraker actually
imports, not from a general-purpose appliance image. Deliberately absent:

- **a native toolchain and binutils.** Nothing on the printer compiles.
- **matplotlib, numpy, fontTools, contourpy.** Only the input-shaper graphs
  use these, and on this platform Klipper runs host-side on stock Python. If
  shaper graphs are wanted later they can come back as an option.
- **locales beyond the default.** Tens of megabytes for a headless service.
- **apprise** (push notifications) and **ldap3** (LDAP auth). Both are
  config-gated: Moonraker loads the component only if the matching section
  exists, and no default Forge-X config has one. apprise alone drags in ten
  transitive packages.
- **msgspec** and **uvloop**. Both are pure throughput optimisations that
  Moonraker guards with `contextlib.suppress(ImportError)` and falls back from
  cleanly. Easy to add if profiling ever justifies them.

Deliberately present, because the payload calls them inside the chroot:
`git` (the update manager, over HTTPS), `sqlite3` (the OTA restore script pipes
SQL into the CLI), `curl` (health polling and GitHub), and **`iproute2`**.

That last one is not obvious. Moonraker enumerates network interfaces with
`ip -json -det address`, and BusyBox's `ip` applet has no JSON output, so
without real iproute2 every network update raises `ShellCommandError` and
clients show no network information. It costs 3 MB and it was found by running
the server, not by reading the imports.

### Our own package recipes, not cross-built wheels

Seven of Moonraker's dependencies have no upstream Buildroot package. The
alternative to writing recipes is a crossenv or pip cross-build, which works
but puts an unpinned, unhashed download step inside the build.

Buildroot recipes keep the whole dependency set under one mechanism: pinned
versions, verified hashes, declared licences, and offline rebuilds from the
download cache. Six of the seven are pure Python and the recipes are a dozen
lines each. Only `streaming-form-data` compiles, and its sdist ships the
pre-Cythonized `_parser.c`, so it needs a C compiler but not Cython.

One carries a patch. `preprocess_cancellation` 0.2.1 has a poetry
`pyproject.toml` with no `[build-system]` table and no `setup.py`, so PEP 517
front-ends fall back to the legacy setuptools backend. That does not fail
loudly - it produces a `0.0.0` wheel with stub metadata and no entry point.
The patch declares the backend the project actually uses.

### The venv shares system site-packages

The payload launches `/root/moonraker-env/bin/python3`, so a venv has to exist
at that path. Two things about how it is made.

It is **built statically** by `post-build.sh` rather than by running
`python -m venv`, because that would mean executing a MIPS interpreter on an
x86 build host. The layout written is what `venv` produces on Linux, where
symlinked interpreters are the default.

It sets **`include-system-site-packages = true`**. Everything Buildroot builds
lands in `/usr/lib/python3.12/site-packages`; a sealed venv would hide all of
it and force the entire dependency set to be installed a second time inside
the venv. On an appliance running exactly one Python program there is nothing
for isolation to protect against, and the duplication is 25 MB or more.

### Bytecode only

`BR2_PACKAGE_PYTHON3_PYC_ONLY` ships `.pyc` without `.py`. This is a real size
win on the stdlib and dependency set, and it is proven against this exact
dependency stack on this platform rather than assumed.

The cost is that a traceback on a printer shows no source lines. That is a
genuine trade and the option is one line to flip if field debuggability turns
out to matter more than the megabytes.

### No init system

`BR2_INIT_NONE`. This rootfs is entered with `chroot` from a running system;
it never boots. Buildroot's default skeleton leaves service scripts in
`/etc/init.d`, and the payload's `.root/start.sh` runs every `S*` it finds
there - which would start a second syslogd and network stack alongside the
host's. `post-build.sh` removes them, and the directory stays as the extension
point the payload intends it to be.

### bash as `/bin/sh`

`.root/stop.sh` carries a `#!/bin/sh` shebang but uses process substitution.
Under a POSIX shell its user-service stop loop is a syntax error. Rather than
patch payload behaviour from the platform layer, the image provides a `/bin/sh`
that satisfies what the payload is actually written against.

### Tar output, with hardlinks intact

The image ships as `rootfs.tar.xz` because the installer unpacks it into a
directory on an existing filesystem; there is no partition to image.

Worth checking if the format is ever changed: `usr/libexec/git-core` is a farm
of ~140 names pointing at one 3.6 MB binary. A tar that does not record
hardlinks turns that into ~500 MB of duplicate files on flash. Buildroot's
output preserves them (148 entries), and `tar tv | grep -c '^h'` is the check.

---

## The payload contract

What the image must provide, derived from reading the payload's own scripts:

- `/proc`, `/sys`, `/dev`, `/run`, `/tmp` - mount targets, created before any
  `mkdir` runs, so they must exist in the image
- `/root/moonraker-env/` - a venv whose `bin/python3` runs Moonraker
- `/root/fluidd`, `/root/mainsail` - web bundles, moved to `/root/www/` on
  first init
- `/etc/init.d/` - empty, for the reason above
- `/usr/bin/python` unsuffixed - `.py/backlight.py` has an
  `#!/usr/bin/env python` shebang, and Buildroot never creates that name
- `/version.txt` - read as the flashed core version; when absent the boot path
  stalls 30 seconds, so `post-build.sh` always stamps it
- no `/ZMOD` marker file - `zversion.sh` hard-fails on it

### Not in the image yet

Three contract items are still missing, each belonging to a later milestone
rather than to Buildroot:

- **Fluidd and Mainsail bundles.** Release zips, not source builds. Moonraker's
  `update_manager` keeps them current once they exist, but the image has to
  ship an initial copy.
- **`uinput.ko`.** Loaded from *inside* the chroot, so it is the one genuine
  binary artifact the image must carry, built against the printer's exact
  kernel.
- **The display stack.** On AD5X this is HelixScreen's territory, and the
  payload's backlight control speaks Allwinner sunxi ioctls with no MIPS
  equivalent.

---

## Two traps when editing the defconfig

**Buildroot defaults MIPS to soft-float.** `BR2_MIPS_SOFT_FLOAT` is
`default y`, and both the FP-mode choice and the NaN-2008 option are
`depends on !BR2_MIPS_SOFT_FLOAT`. Omit the explicit disable and they vanish
from the config with no warning, leaving a soft-float legacy-NaN toolchain
whose loader is `/lib/ld.so.1` - a path that does not exist on this printer.

More generally: **kconfig accepts invisible and unknown symbols in silence.**
The user-settable choice is the lowercase `BR2_mips_32r5`;
`BR2_MIPS_CPU_MIPS32R5` is a promptless bool that the choice *selects*, so
setting it in a defconfig does nothing at all. After any defconfig change,
read back the generated `.config` rather than trusting the input. CI asserts
the four ABI values and the loader path for exactly this reason.

**`e_flags` cannot distinguish MIPS32r2 from r5.** Both encode as
`EF_MIPS_ARCH_32R2`; only r6 got a new value. The `ISA:` field in the
`.MIPS.abiflags` section is the one that tells them apart.

---

## How this is verified

Static checks are not sufficient here, because the failure mode that matters
is a rootfs that builds cleanly and does not run. The gates, in order:

1. **CI asserts the ABI** - `BR2_GCC_TARGET_NAN`, `FP32_MODE`, `ABI`, `ARCH`,
   and the presence of `/lib/ld-linux-mipsn8.so.1`.
2. **It executes on the printer.** Unpack, `chroot`, run busybox and bash.
   `awk 'BEGIN { printf "%.6f", 2.0/3.0 }'` returning `0.666667` exercises the
   FPU through the ABI, which a soft-float or wrong-FP-ABI build would not
   survive.
3. **Every unconditional import resolves**, including the compiled extensions
   and the ctypes-based `libnacl`, which proves `libsodium.so` is findable at
   runtime - a silent-at-build, fatal-at-boot failure otherwise.
4. **Moonraker runs and serves its API.** `/server/info` returning 23
   components with `failed_components: []` is the milestone's actual success
   criterion, and it is what surfaced the missing `iproute2`.

## Size

18 MB compressed, roughly 85 MB installed. That figure is a result of the
package choices above rather than a target that was optimised toward; the
largest single contributors are the Python stdlib and Pillow.
