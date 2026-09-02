#!/bin/sh
# Gate: the mod paths must be reached through the platform descriptor, not a
# board-specific literal. Two invariants, deliberately different in reach.
#
#   1. /data/.mod (the chroot rootfs $MOD, and its $DATA_MNT/.mod parent) must
#      not appear as a literal ANYWHERE in the boot/install shell path. This
#      value is fully behind the descriptor now, so the ban is global. This is
#      the mod literal M1 missed.
#
#   2. /opt/config/mod (the mod source tree $MOD_ROOT) must not appear as a
#      non-bootstrap literal in the files this milestone descriptor-ized. It is
#      deliberately NOT a global ban: ~140 /opt/config/mod literals remain
#      across the tree (.shell/S00init alone has dozens, and M1's own
#      .shell/commands/zstart_klipper.sh keeps them). On the AD5X those resolve
#      through the `/opt` bind-mount that app_startup.sh sets up - a documented
#      compatibility decision (platform design spec, "Boot integration"), not an
#      oversight. Abstracting the whole tree is a later milestone; this gate
#      ratchets the files already converted so they cannot regress.
#
# A `source .../platform.sh` or `.../common.sh` line is the descriptor load
# point and is exempt from (2): it cannot reference a variable it has not
# sourced yet. Comments and /opt/config/mod_data (a separate value) are exempt.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

# strip full-line comments so a literal named in a comment does not trip a gate
no_comments() { grep -vE '^[[:space:]]*#' "$1"; }

# --- 1. No /data/.mod literal anywhere in the boot/install shell path --------
for f in $(git ls-files '.shell/*' 'sync_remote.sh' 'sync.sh'); do
    [ -f "$f" ] || continue
    grep -Iq . "$f" || continue   # skip binary blobs (zram/*.ko, swapon_prio)
    if no_comments "$f" | grep -qE '/data/\.mod'; then
        _t_fail "no /data/.mod literal: $f" \
                "the chroot rootfs must come from \$MOD (or \$DATA_MNT/.mod)"
    else
        _t_pass "no /data/.mod literal: $f"
    fi
done

# --- 2. No mod-source-tree literal in the descriptor-ized files -------------
# The files this milestone routed through $MOD_ROOT. The rest of the tree keeps
# the /opt/config/mod literal by design (see header).
MOD_ROOT_CLEAN=".shell/common.sh .shell/boot/init_swap.sh .shell/commands/zhttp.sh .shell/commands/zbackup.sh sync_remote.sh"
for f in $MOD_ROOT_CLEAN; do
    if [ ! -f "$f" ]; then
        _t_fail "mod-root-clean file present: $f" "no such file"
        continue
    fi
    # Drop comments and the descriptor bootstrap line, ignore /opt/config/mod_data,
    # then look for a bare mod-source-tree literal.
    if no_comments "$f" \
        | grep -vE '^[[:space:]]*(\.|source)[[:space:]].*(platform|common)\.sh' \
        | grep -qE '/opt/config/mod([^_A-Za-z0-9]|$)'; then
        _t_fail "no mod-tree literal: $f" \
                "the mod source tree must come from \$MOD_ROOT"
    else
        _t_pass "no mod-tree literal: $f"
    fi
done

finish
