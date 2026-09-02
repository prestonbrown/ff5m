# Building an AD5X Forge-X image

This builds the installable `.tgz` you flash from a USB stick. It is the same
mechanism the stock firmware update uses, so a build you made yourself installs
exactly the way a release does.

> An image you build here is **not a release**. It carries the same version as
> whatever `version.txt` says and is named identically, so keep track of which
> one you flashed - once installed, the printer cannot tell you.

## Quick start

```sh
git clone https://github.com/prestonbrown/ff5m.git
cd ff5m
git checkout feat/ad5x-support
tools/release/build_ad5x_image.sh
# -> dist/AD5X-ForgeX-<version>.tgz
```

The script prints progress to stderr and the finished artifact's path to
stdout, so `IMAGE=$(tools/release/build_ad5x_image.sh)` gives you just the path.

## What it needs

Ordinary GNU userland - `tar`, `xz`, `md5sum`, `awk`, plus `curl` or `wget` for
the rootfs download. Two things are less portable than they look:

- **`xz` must be XZ Utils.** The build uses `xz -T0`; BusyBox `xz` has no `-T`.
- **`md5sum` must be coreutils**, not BSD `md5`. On macOS the rootfs identity
  check fails on the name. Build on Linux.

Nothing is compiled. The MIPS toolchain lives in the rootfs build, which is a
separate thing you do not need for a normal image build (see below).

## Where the rootfs comes from

The image embeds a MIPS Buildroot rootfs as `xz/buildroot.tar.xz`; the installer
unpacks it into `/usr/data/.mod/.forge-x` and chroots into it, because the stock
AD5X userland is glibc 2.33 with no compiler and no package manager. That rootfs
is ours, built from source by
[forgex-buildroot](https://github.com/prestonbrown/forgex-buildroot).

You do not have to build it. With no `BUILDROOT_TAR` set, the image builder
downloads the pinned one (~18MB) into
`${XDG_CACHE_HOME:-~/.cache}/forgex-x/rootfs` and verifies its md5 before using
it. Later builds reuse the cache and touch the network not at all.

Two files govern this:

| File | What it holds |
|---|---|
| `tools/release/rootfs.pin` | the URL and md5 of the rootfs to download, and the forgex-buildroot commit it was built from |
| `tools/release/rootfs.md5` | which rootfs identities may be embedded at all |

`check_rootfs.sh` verifies every rootfs against `rootfs.md5` no matter how it
arrived, so a download that is not the pinned artifact is refused rather than
shipped. The pinned rootfs is recorded there as `KNOWN`, which is why the
download path needs no override.

## Building the rootfs yourself

```sh
BUILD_ROOTFS=1 tools/release/build_ad5x_image.sh
```

This clones forgex-buildroot at the pinned commit and builds the rootfs in
Docker, then builds the image around it. It needs **Docker** and a lot of
patience: the first build downloads and compiles a full cross toolchain and is
slow. Later builds are incremental.

Buildroot output is not bit-reproducible, so your rootfs will not match the
pinned md5. That is expected - the builder passes `ALLOW_UNPINNED_ROOTFS=1` for
a rootfs it built itself, and only for that. It does not extend the same trust
to a tarball you hand it.

Set `FORGEX_BR_DIR` to use a checkout you already have instead of the cached
clone.

## Supplying your own rootfs

```sh
BUILDROOT_TAR=/path/to/rootfs.tar.xz tools/release/build_ad5x_image.sh
```

This is the release path, and the identity gate applies in full: an
unrecognised rootfs is refused with exit 2. If it is a fresh forgex-buildroot
build you mean to ship, record it in `rootfs.md5` (see
[ROOTFS.md](../tools/release/ROOTFS.md)). To push one build through without
recording it, `ALLOW_UNPINNED_ROOTFS=1`.

## Environment reference

| Variable | Effect |
|---|---|
| `BUILDROOT_TAR` | use this rootfs; skips acquisition entirely |
| `BUILD_ROOTFS=1` | build the rootfs from source instead of downloading |
| `NO_AUTO_ROOTFS=1` | never acquire a rootfs; fail unless `BUILDROOT_TAR` is set |
| `FORCE_ROOTFS=1` | re-download even when the cache already holds a good copy |
| `ROOTFS_CACHE_DIR` | where downloads and the forgex-buildroot clone live |
| `FORGEX_BR_DIR` | an existing forgex-buildroot checkout to build in |
| `ALLOW_UNPINNED_ROOTFS=1` | accept a rootfs that is not recorded in `rootfs.md5` |
| `ENTWARE_TAR` | a real mipsel Entware tarball; omitted, the installer skips Entware |
| `OUT_DIR` | where the finished image is written (default `dist/`) |

## Installing what you built

Same as a release - see [INSTALL.md](INSTALL.md). Copy the `.tgz` to a USB
stick and let the printer's own update mechanism take it.

## A recovery stick

`tools/release/ad5x/build_rescue_stick.sh` packs a payload-free recovery
archive that restores the stock boot path. Build one before you flash anything;
see [RECOVERY.md](RECOVERY.md).
