#!/bin/bash

## Klipper plugin and patch overlay management
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

# Board-specific values. Sourced by a path relative to this file so it resolves
# both on-device and when a test sources this script from the checkout.
# shellcheck disable=SC1090,SC1091
. "$(dirname "${BASH_SOURCE[0]}")/platform.sh"

klipper_overlay_ignored() {
    local rel_path="$1"

    case "/$rel_path/" in
        */__pycache__/*) return 0 ;;
    esac

    case "$rel_path" in
        .*|*/.*|*.pyc) return 0 ;;
    esac

    return 1
}

klipper_overlay_restore_or_remove() {
    local target="$1"
    local backup

    for backup in \
        "$target.bak" \
        "$target.backup" \
        "$target.old" \
        "$target.orig"; do
        if [ -f "$backup" ] && [ ! -L "$backup" ]; then
            echo "// Restore klipper file backup: $backup"
            rm -f "$target" || return 1
            mv -f "$backup" "$target" || return 1
            return 0
        fi
    done

    echo "// Remove stale klipper symlink: $target"
    rm -f "$target"
}

klipper_overlay_plugin_link_is_current() {
    local link="$1"
    local source="$2"
    local src_dir="$3"
    local target_dir="$4"
    local rel_file expected

    case "$source" in
        "$src_dir/plugins/"*) ;;
        *) return 1 ;;
    esac

    rel_file=${source#"$src_dir/plugins/"}
    expected="$target_dir/extras/$rel_file"

    [ "$link" = "$expected" ] || return 1
    [ -f "$source" ] || return 1
    klipper_overlay_ignored "$rel_file" && return 1

    return 0
}

# Patches may come from the common `patches/` tree or, when a per-board override
# exists, from `patches.$PLATFORM/` (which wins). Given a symlink source, echo
# its path relative to whichever patch tree owns it (checking the override first)
# and return 0; return 1 if the source is not under any patch tree for this board.
klipper_overlay_patch_relpath() {
    local source="$1"
    local src_dir="$2"

    if [ -n "${PLATFORM:-}" ] && [ -d "$src_dir/patches.$PLATFORM" ]; then
        case "$source" in
            "$src_dir/patches.$PLATFORM/"*)
                printf '%s\n' "${source#"$src_dir/patches.$PLATFORM/"}"
                return 0
            ;;
        esac
    fi

    case "$source" in
        "$src_dir/patches/"*)
            printf '%s\n' "${source#"$src_dir/patches/"}"
            return 0
        ;;
    esac

    return 1
}

klipper_overlay_patch_link_is_current() {
    local link="$1"
    local source="$2"
    local src_dir="$3"
    local target_dir="$4"
    local rel_file expected

    rel_file=$(klipper_overlay_patch_relpath "$source" "$src_dir") || return 1
    expected="$target_dir/$rel_file"

    [ "$link" = "$expected" ] || return 1
    [ -f "$source" ] || return 1
    case "$rel_file" in
        *.py) ;;
        *) return 1 ;;
    esac
    klipper_overlay_ignored "$rel_file" && return 1

    return 0
}

klipper_overlay_clean_links() {
    local src_dir="$1"
    local target_dir="$2"
    local link source

    while IFS= read -r link; do
        source=$(readlink "$link") || return 1

        if [ ! -e "$link" ]; then
            klipper_overlay_restore_or_remove "$link" || return 1
            continue
        fi

        case "$source" in
            "$src_dir/plugins/"*)
                klipper_overlay_plugin_link_is_current \
                    "$link" "$source" "$src_dir" "$target_dir" \
                    || klipper_overlay_restore_or_remove "$link" \
                    || return 1
            ;;
            "$src_dir/"*)
                # Any patch tree for this board (patches/ or patches.$PLATFORM/).
                # A link into one that is no longer current is restored/removed;
                # links outside our trees (external) are left untouched.
                if klipper_overlay_patch_relpath "$source" "$src_dir" \
                        >/dev/null; then
                    klipper_overlay_patch_link_is_current \
                        "$link" "$source" "$src_dir" "$target_dir" \
                        || klipper_overlay_restore_or_remove "$link" \
                        || return 1
                fi
            ;;
        esac
    done < <(find "$target_dir" -type l)
}

klipper_overlay_link_plugins() {
    local src_dir="$1"
    local target_dir="$2"
    local file rel_file target parent current

    while IFS= read -r file; do
        rel_file=${file#"$src_dir/plugins/"}
        klipper_overlay_ignored "$rel_file" && continue

        target="$target_dir/extras/$rel_file"
        parent=$(dirname "$target")
        mkdir -p "$parent" || return 1

        if [ -L "$target" ]; then
            current=$(readlink "$target") || return 1
            [ "$current" = "$file" ] && continue
        fi

        if [ -e "$target" ] || [ -L "$target" ]; then
            echo "@@ Refusing to overwrite klipper plugin target: $target"
            return 1
        fi

        echo "// Link klipper plugin file: $file"
        ln -s "$file" "$target" || return 1
    done < <(find "$src_dir/plugins" -type f)
}

klipper_overlay_link_patches() {
    local src_dir="$1"
    local target_dir="$2"
    local rel_file target parent current winner arch_dir

    # The common tree, plus a per-board override tree when one exists. A file
    # present in both is taken from the override; a file present only in the
    # common tree comes from there. AD5M has no override tree, so this reduces
    # to the single `patches/` walk it has always done.
    arch_dir=""
    if [ -n "${PLATFORM:-}" ] && [ -d "$src_dir/patches.$PLATFORM" ]; then
        arch_dir="$src_dir/patches.$PLATFORM"
    fi

    while IFS= read -r rel_file; do
        klipper_overlay_ignored "$rel_file" && continue

        case "$rel_file" in
            *.py) ;;
            *) continue ;;
        esac

        if [ -n "$arch_dir" ] && [ -f "$arch_dir/$rel_file" ]; then
            winner="$arch_dir/$rel_file"
        else
            winner="$src_dir/patches/$rel_file"
        fi

        target="$target_dir/$rel_file"
        parent=$(dirname "$target")
        mkdir -p "$parent" || return 1

        if [ -L "$target" ]; then
            current=$(readlink "$target") || return 1
            [ "$current" = "$winner" ] && continue

            # A link already pointing into one of our patch trees (e.g. a base
            # link now shadowed by an override) is ours to re-point; only a link
            # to something outside our trees is treated as unmanaged.
            if klipper_overlay_patch_relpath "$current" "$src_dir" \
                    >/dev/null; then
                rm -f "$target" || return 1
            else
                echo "@@ Refusing to overwrite unmanaged klipper symlink: $target"
                return 1
            fi
        elif [ -e "$target" ]; then
            if [ ! -e "$target.bak" ] && [ ! -L "$target.bak" ]; then
                echo "// Create klipper file backup: $target"
                mv "$target" "$target.bak" || return 1
            else
                echo "// Remove overwritten klipper file: $target"
                rm -f "$target" || return 1
            fi
        elif [ ! -e "$target.bak" ] && [ ! -L "$target.bak" ]; then
            echo "@@ Missing klipper patch target and backup: $target"
            return 1
        fi

        echo "// Link patched klipper file: $winner"
        ln -s "$winner" "$target" || return 1
    done < <(
        {
            find "$src_dir/patches" -type f 2>/dev/null \
                | while IFS= read -r file; do
                    printf '%s\n' "${file#"$src_dir/patches/"}"
                done
            if [ -n "$arch_dir" ]; then
                find "$arch_dir" -type f 2>/dev/null \
                    | while IFS= read -r file; do
                        printf '%s\n' "${file#"$arch_dir/"}"
                    done
            fi
        } | sort -u
    )
}

apply_klipper_patches() {
    local src_dir="${KLIPPER_SRC_DIR:-/opt/config/mod/.py/klipper}"
    local target_dir="${KLIPPER_TARGET_DIR:-$KLIPPER_DIR/klippy}"
    local tune_cmd="${KLIPPER_TUNE_CMD:-$CMDS/ztune_klipper.sh}"

    klipper_overlay_clean_links "$src_dir" "$target_dir" || return 1

    sync
    echo "Linking extensions..."
    klipper_overlay_link_plugins "$src_dir" "$target_dir" || return 1

    sync
    echo "Apply patches..."
    klipper_overlay_link_patches "$src_dir" "$target_dir" || return 1

    sync
    echo "Apply fixes..."
    "$tune_cmd" apply || return 1

    sync
}
