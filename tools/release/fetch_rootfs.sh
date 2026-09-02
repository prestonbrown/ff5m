#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Acquire the MIPS rootfs the AD5X image embeds, and print its path.
#
#   fetch_rootfs.sh                  download the pinned rootfs (cached)
#   BUILD_ROOTFS=1 fetch_rootfs.sh   build it from source instead
#
# stdout carries the absolute path to a rootfs.tar.xz and nothing else, so a
# caller can write ROOTFS=$(fetch_rootfs.sh); progress and every diagnostic
# go to stderr.
#
# What is downloaded is decided by rootfs.pin beside this script, and is
# verified against the md5 recorded there - on arrival AND on every cache hit,
# so a truncated or swapped cache entry is caught rather than embedded. This
# is not the release gate: check_rootfs.sh still judges whatever comes back.
#
# Env seams:
#   ROOTFS_PIN_FILE   pin to read (default: rootfs.pin beside this script)
#   ROOTFS_CACHE_DIR  where the download and the source clone live
#                     (default: ${XDG_CACHE_HOME:-$HOME/.cache}/forgex-x/rootfs)
#   FORCE_ROOTFS=1    download again even when the cache already verifies
#   BUILD_ROOTFS=1    build from source instead of downloading
#   FORGEX_BR_DIR     an existing forgex-br checkout to build in
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
PIN_FILE=${ROOTFS_PIN_FILE:-$HERE/rootfs.pin}
CACHE_DIR=${ROOTFS_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/forgex-x/rootfs}

# The path printed on stdout must be usable from any directory, and callers
# hand it straight to cp. Prefixing beats canonicalising: it leaves an
# already-absolute cache dir spelled exactly as the caller spelled it.
case $CACHE_DIR in
    /*) ;;
    *)  CACHE_DIR=$PWD/$CACHE_DIR ;;
esac

say() { echo "fetch_rootfs: $*" >&2; }
die() { echo "fetch_rootfs: $*" >&2; exit 1; }

# Failing to acquire a rootfs is not a dead end, so say so in the same breath:
# the pin names the artifact that was being chased, and BUILDROOT_TAR is the
# way past this script entirely for someone who already has one.
die_acquire() {
    echo "fetch_rootfs: $*" >&2
    echo "  pin in use: $PIN_FILE" >&2
    echo "  Or supply one yourself: BUILDROOT_TAR=/path/to/rootfs.tar.xz" >&2
    echo "  (build one with BUILD_ROOTFS=1; see tools/release/ROOTFS.md)" >&2
    exit 1
}

# ---- the pin ---------------------------------------------------------------
# Parsed before anything reaches the network, so an incomplete pin costs a
# message rather than a download that lands with nothing to check it against.
[ -f "$PIN_FILE" ] || die "no pin file: $PIN_FILE"

pin_field() {
    awk -v k="$1" '$1 == k { print $2; exit }' "$PIN_FILE"
}

rootfs_url=$(pin_field rootfs_url)
rootfs_md5=$(pin_field rootfs_md5)
source_repo=$(pin_field source_repo)
source_ref=$(pin_field source_ref)

missing=
[ -n "$rootfs_url" ]   || missing="$missing rootfs_url"
[ -n "$rootfs_md5" ]   || missing="$missing rootfs_md5"
[ -n "$source_repo" ]  || missing="$missing source_repo"
[ -n "$source_ref" ]   || missing="$missing source_ref"
[ -n "$missing" ] && die "pin $PIN_FILE is missing:$missing"

md5_of() {
    _sum=$(md5sum "$1") || return 1
    echo "${_sum%% *}"
}

# ---- from source -----------------------------------------------------------
# Buildroot output is not bit-reproducible, so what this produces will not
# match rootfs_md5. The pin still governs WHICH source tree is built.
if [ "${BUILD_ROOTFS:-0}" = 1 ]; then
    command -v git >/dev/null 2>&1 || die "BUILD_ROOTFS=1 needs git, which is not installed."
    command -v docker >/dev/null 2>&1 || \
        die "BUILD_ROOTFS=1 builds in Docker, which is not installed."
    # An installed docker that cannot reach its daemon fails much later and
    # far less clearly, so find out now.
    docker info >/dev/null 2>&1 || \
        die "docker is installed but not runnable: start the daemon, or add yourself
  to the docker group (log out and back in), then re-run."

    BR_DIR=${FORGEX_BR_DIR:-$CACHE_DIR/forgex-br}
    case $BR_DIR in
        /*) ;;
        *)  BR_DIR=$PWD/$BR_DIR ;;
    esac

    if [ -d "$BR_DIR/.git" ]; then
        # Someone else's working tree is theirs. Building in it would mean
        # checking out over their edits, so stop and let them decide. Only
        # tracked changes count: the build writes output/ and .dl/ into the
        # tree, and treating those as edits would make every build after the
        # first one refuse to run.
        if [ -n "$(git -C "$BR_DIR" status --porcelain --untracked-files=no)" ]; then
            die "checkout has local modifications: $BR_DIR
  Commit or discard them, or point FORGEX_BR_DIR at another checkout."
        fi
        head=$(git -C "$BR_DIR" rev-parse HEAD)
        if [ "$head" != "$source_ref" ]; then
            die "checkout $BR_DIR is at $head, not the pinned $source_ref
  Check the pinned commit out yourself, or remove the checkout to re-clone."
        fi
    elif [ -e "$BR_DIR" ]; then
        die "$BR_DIR exists but is not a git checkout; move it aside."
    else
        say "cloning $source_repo -> $BR_DIR"
        mkdir -p "$(dirname "$BR_DIR")"
        # A repo the caller cannot read is indistinguishable from a missing one
        # over https, and git's answer to both is to ask for a password. In a
        # build script that is an unattended hang, so refuse the prompt and let
        # the clone fail with something a caller can act on.
        GIT_TERMINAL_PROMPT=0 git clone "$source_repo" "$BR_DIR" >&2 || \
            die "clone failed: $source_repo
  If it is private or the URL is wrong, git cannot say which. Check access, or
  point FORGEX_BR_DIR at a checkout you already have."
        git -C "$BR_DIR" checkout --detach "$source_ref" >&2 || \
            die "$source_repo has no commit $source_ref (pin: $PIN_FILE)"
    fi

    say "building in Docker; the first build compiles a cross toolchain and is slow"
    ( cd "$BR_DIR/buildroot" \
      && docker build -t forgex-br . >&2 \
      && docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:$PWD" -w "$PWD" \
             forgex-br ./build.sh ad5x >&2 ) || die "rootfs build failed in $BR_DIR/buildroot"

    built=$BR_DIR/buildroot/output/ad5x/images/rootfs.tar.xz
    [ -f "$built" ] || die "build reported success but produced no $built"
    say "built $built"
    echo "$built"
    exit 0
fi

# ---- the cache -------------------------------------------------------------
command -v md5sum >/dev/null 2>&1 || \
    die "md5sum is required to verify the rootfs, and is not installed."

CACHE_TAR=$CACHE_DIR/rootfs.tar.xz

if [ -f "$CACHE_TAR" ] && [ "${FORCE_ROOTFS:-0}" != 1 ]; then
    have=$(md5_of "$CACHE_TAR") || die "cannot read $CACHE_TAR"
    if [ "$have" = "$rootfs_md5" ]; then
        say "cache hit: $CACHE_TAR ($rootfs_md5)"
        echo "$CACHE_TAR"
        exit 0
    fi
    # Overwriting it would destroy whatever the user put there, and silently
    # redownloading would hide that this cache was not what it claimed.
    die "cached rootfs has md5 $have, pinned is $rootfs_md5
  Remove $CACHE_TAR and re-run, or set FORCE_ROOTFS=1 to replace it."
fi

# ---- the download ----------------------------------------------------------
mkdir -p "$CACHE_DIR" || die_acquire "cannot create cache directory $CACHE_DIR"
TMP_TAR=$CACHE_DIR/.rootfs.tar.xz.$$
trap 'rm -f "$TMP_TAR"' EXIT INT TERM

say "downloading $rootfs_url"
dl_ok=0
if command -v curl >/dev/null 2>&1; then
    if curl -fL --retry 2 -o "$TMP_TAR" "$rootfs_url" >&2; then dl_ok=1; fi
elif command -v wget >/dev/null 2>&1; then
    if wget -O "$TMP_TAR" "$rootfs_url" >&2; then dl_ok=1; fi
else
    die_acquire "no downloader found: install curl or wget."
fi
[ "$dl_ok" = 1 ] || die_acquire "download failed: $rootfs_url"

got=$(md5_of "$TMP_TAR") || die_acquire "downloaded file is unreadable"
[ "$got" = "$rootfs_md5" ] || \
    die_acquire "downloaded rootfs has md5 $got, pinned is $rootfs_md5"

# Only a verified file earns the cache name: an interrupted download leaves a
# dot-file behind instead of a cache hit that would be trusted next time.
mv "$TMP_TAR" "$CACHE_TAR"
say "verified and cached: $CACHE_TAR ($rootfs_md5)"
echo "$CACHE_TAR"
