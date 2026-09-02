# The AD5X image rootfs

The firmware image (`build_ad5x_image.sh`) embeds a MIPS rootfs as
`xz/buildroot.tar.xz`; the installer unpacks it into `/usr/data/.mod/.forge-x`
on the printer. That rootfs is ours, built from source by
[forgex-br](https://github.com/prestonbrown/forgex-br) - a Buildroot external
tree pinned to a specific Buildroot release, with the AD5X ABI
(o32 / hard-float FP64 / NaN2008) locked in its defconfig.

## Building one

On the build host (zeus), with forgex-br checked out:

```sh
cd forgex-br/buildroot
docker build -t forgex-br .
docker run --rm -u $(id -u):$(id -g) -v "$PWD:$PWD" -w "$PWD" \
    forgex-br ./build.sh ad5x
# -> buildroot/output/ad5x/images/rootfs.tar.xz
```

The first build is slow (the download cache under `buildroot/.dl/` dominates);
later builds are incremental. `./build.sh ad5x savedefconfig` writes config
changes back to the tracked defconfig.

## Recording it

Append the new rootfs's md5 to `rootfs.md5` beside this file:

```sh
md5sum rootfs.tar.xz   # -> <md5>  ... 
```

```
<md5>  KNOWN  forgex-br ad5x, buildroot <tag>, built <date>
```

`check_rootfs.sh` (run by `build_ad5x_image.sh`) verifies every
`BUILDROOT_TAR` against that list: `KNOWN` builds pass, the early bring-up
borrow (ZMOD's release rootfs) is `FORBIDDEN` and fails loudly, and an
unrecorded md5 needs `ALLOW_UNPINNED_ROOTFS=1` - intended for a local
test build, never a release.

## Why the borrow had to go

The first proof-of-install image embedded ZMOD's known-good MIPS rootfs
because it existed and ours did not. It is another project's release
artifact: we cannot rebuild it, patch it, or ship it, and our image's
userland was someone else's to change. Our buildroot erases that whole
class of dependency - and the gate in the builder keeps the borrow from
coming back silently.
