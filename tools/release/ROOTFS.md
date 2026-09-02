# The AD5X image rootfs

## Why there is a second userland at all

The AD5X's stock userland is a fixed artifact: glibc 2.33, no compiler, no
package manager, and nothing on the printer builds anything. Whatever Forge-X
needs at runtime that the stock image does not already carry - a newer Python,
libffi, OpenSSL, curl - has to arrive prebuilt, and has to be linked against a
libc that exists on the machine.

So Forge-X ships its own userland alongside the stock one and runs its
processes inside it with a chroot. The stock rootfs is left untouched, which
is what makes the mod removable: nothing of ours overwrites a vendor file, and
uninstalling is unmounting rather than restoring. The cost is that anything
built for the chroot is linked against the chroot's glibc (2.41, tracking the
pinned Buildroot release) and cannot be loaded by the stock rootfs - a binary
that runs by hand inside the chroot and fails from outside it is that
mismatch, not a broken build.

### It is not a sealed box

The chroot shares the host's devices and data partitions at identical paths,
which is what makes it usable rather than an island. Measured on an AD5X:

    /dev/mmcblk0p7   ->  $MOD/data        and  $MOD/opt/config
    /dev/mmcblk0p6   ->  $MOD/opt/klipper
    proc, sysfs, configfs, devpts, /dev/shm, the adb functionfs,
    and tmpfs on /dev, /run and /tmp

`/opt/config` being bind-mounted at the SAME path is the load-bearing one: it
means one tree is addressable from both sides, so a host path like
`/opt/config/mod/.bin/...` resolves unchanged inside the chroot. Anything
installed into the chroot can use host paths verbatim - but they are bind
mounts, not copies, so a bring-up that stopped mounting them would break
in-chroot code in a confusing way rather than an obvious one.

Do not assume the AD5M's mount set matches. It does not: the AD5M binds
`/dev/root` where the AD5X binds real block devices, and it has a
`root/printer_data` bind that the AD5X does not have at all. Check the board
you are on.

The ABI the rootfs is built to is not a matter of taste - o32, hard-float
FP64, NaN2008, matching what the kernel and every stock binary already fix. A
default mipsel toolchain gets the last two wrong and names its loader
`/lib/ld.so.1`, which does not exist on this printer; the real one is
`/lib/ld-linux-mipsn8.so.1`, and the `n8` is the NaN2008 marker. The reasoning
and the exact settings - including the trap where Buildroot's MIPS soft-float
default silently removes both options - are documented in
[forgex-br](https://github.com/prestonbrown/forgex-br)'s
`buildroot/external/configs/ad5x_defconfig`, which is where someone changing
them would work. They are deliberately not duplicated here; two copies of an
ABI rationale is two copies to disagree.

## The image

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
