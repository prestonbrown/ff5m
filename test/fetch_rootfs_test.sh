#!/bin/sh
# fetch_rootfs.sh (tools/release) rootfs acquisition tests.
#
# The image builder needs a MIPS rootfs it did not build. Supplying one by
# hand is the release path; fetching the pinned one is what lets a fresh
# clone produce an image. Both must end at the same identity check, so these
# cases assert that a fetched rootfs is verified against the pin rather than
# trusted for having arrived, and that a cache hit needs no network at all.
#
# Every case here is offline: a seeded cache covers the success path, and the
# failure paths use a URL no downloader will dial.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

FETCH="$REPO_DIR/tools/release/fetch_rootfs.sh"
PIN="$REPO_DIR/tools/release/rootfs.pin"
BUILDER="$REPO_DIR/tools/release/build_ad5x_image.sh"

assert_file "fetch script exists" "$FETCH"
assert_file "pin file exists" "$PIN"

command -v md5sum >/dev/null 2>&1 || {
    echo "skip: md5sum absent"; finish; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pin_field() {
    awk -v k="$1" '$1 == k { print $2; exit }' "$PIN"
}

# --- the shipped pin is complete and agrees with the identity list ----------
# A pin missing a field, or naming an md5 the gate would reject, produces a
# build that fails only after a download - so both are checked here instead.
for field in rootfs_url rootfs_md5 source_repo source_ref; do
    assert_ne "pin names $field" "" "$(pin_field "$field")"
done

pinned_md5=$(pin_field rootfs_md5)
known=$(awk -v k="$pinned_md5" '$1 == k { print $2; exit }' \
        "$REPO_DIR/tools/release/rootfs.md5")
assert_eq "pinned rootfs is KNOWN to the identity gate" "KNOWN" "$known"

# --- a cache hit is used, and costs no network -----------------------------
# The fixture IS the pinned artifact as far as the pin is concerned: its real
# md5 is written into a fixture pin, so the match being asserted is computed,
# not declared.
CACHE="$WORK/cache"
mkdir -p "$CACHE"
printf 'pretend rootfs\n' > "$CACHE/rootfs.tar.xz"
fixture_md5=$(md5sum "$CACHE/rootfs.tar.xz"); fixture_md5=${fixture_md5%% *}

FIXTURE_PIN="$WORK/rootfs.pin"
{
    echo "# fixture pin"
    echo "rootfs_url   http://127.0.0.1:9/never-dialed.tar.xz"
    echo "rootfs_md5   $fixture_md5"
    echo "source_repo  https://example.invalid/forgex-buildroot.git"
    echo "source_ref   0000000000000000000000000000000000000000"
} > "$FIXTURE_PIN"

out=$(ROOTFS_PIN_FILE="$FIXTURE_PIN" ROOTFS_CACHE_DIR="$CACHE" \
      "$FETCH" 2>/dev/null); rc=$?
assert_eq "cache hit succeeds" 0 "$rc"
assert_eq "cache hit prints the cached path" "$CACHE/rootfs.tar.xz" "$out"

# --- a cached file that is not the pinned one is refused, not used ----------
printf 'tampered\n' > "$CACHE/rootfs.tar.xz"
out=$(ROOTFS_PIN_FILE="$FIXTURE_PIN" ROOTFS_CACHE_DIR="$CACHE" \
      "$FETCH" 2>&1); rc=$?
assert_ne "wrong-md5 cache entry does not succeed" 0 "$rc"
assert_contains "refusal names the md5 mismatch" "$out" "md5"

# --- an unreachable download says how to proceed by hand -------------------
rm -f "$CACHE/rootfs.tar.xz"
out=$(ROOTFS_PIN_FILE="$FIXTURE_PIN" ROOTFS_CACHE_DIR="$CACHE" \
      "$FETCH" 2>&1); rc=$?
assert_ne "failed download does not succeed" 0 "$rc"
assert_contains "failure points at the manual escape" "$out" "BUILDROOT_TAR"

# --- a pin with a missing field fails before any download ------------------
BAD_PIN="$WORK/bad.pin"
printf 'rootfs_url   http://127.0.0.1:9/x.tar.xz\n' > "$BAD_PIN"
out=$(ROOTFS_PIN_FILE="$BAD_PIN" ROOTFS_CACHE_DIR="$CACHE" \
      "$FETCH" 2>&1); rc=$?
assert_ne "incomplete pin does not succeed" 0 "$rc"
assert_contains "incomplete pin names the missing field" "$out" "rootfs_md5"

# --- a missing pin file is its own error -----------------------------------
out=$(ROOTFS_PIN_FILE="$WORK/absent.pin" ROOTFS_CACHE_DIR="$CACHE" \
      "$FETCH" 2>&1); rc=$?
assert_ne "missing pin does not succeed" 0 "$rc"
assert_contains "missing pin says which file" "$out" "absent.pin"

# --- builder wiring: no BUILDROOT_TAR now reaches the fetch ----------------
# The old behaviour was a hard stop demanding BUILDROOT_TAR. It must now try
# to acquire one, so the failure that surfaces is the fetch's, not that stop.
out=$(BUILDROOT_TAR= ROOTFS_PIN_FILE="$FIXTURE_PIN" ROOTFS_CACHE_DIR="$CACHE" \
      "$BUILDER" 2>&1); rc=$?
assert_ne "builder without a rootfs does not succeed" 0 "$rc"
assert_contains "builder reports the acquisition failure, not the old stop" \
    "$out" "$FIXTURE_PIN"

# --- NO_AUTO_ROOTFS restores the hard stop, and names itself ---------------
out=$(BUILDROOT_TAR= NO_AUTO_ROOTFS=1 ROOTFS_PIN_FILE="$FIXTURE_PIN" \
      ROOTFS_CACHE_DIR="$CACHE" "$BUILDER" 2>&1); rc=$?
assert_ne "opted-out builder does not succeed" 0 "$rc"
assert_contains "opt-out error still demands BUILDROOT_TAR" "$out" "BUILDROOT_TAR"
assert_contains "opt-out error names the opt-out" "$out" "NO_AUTO_ROOTFS"

# --- the identity gate still governs a rootfs the user supplied ------------
printf 'a stranger rootfs\n' > "$WORK/stranger.tar.xz"
out=$(BUILDROOT_TAR="$WORK/stranger.tar.xz" "$BUILDER" 2>&1); rc=$?
assert_ne "unknown user-supplied rootfs is refused" 0 "$rc"
assert_contains "refusal is the identity gate's" "$out" "ALLOW_UNPINNED_ROOTFS"

finish
