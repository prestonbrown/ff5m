#!/bin/bash

## Build a complete ForgeX firmware image with HelixScreen
##
## This script assembles the flashable .tgz image by combining:
## 1. All mod files from this repository (→ /opt/config/mod/)
## 2. HelixScreen AD5M release from GitHub (→ /opt/helixscreen/)
##
## Usage:
##   ./build-image.sh                  # Build only
##   ./build-image.sh --release        # Build + create GitHub release
##   ./build-image.sh --helix-release path/to/helixscreen-ad5m.tar.gz
##
## Requires: gh (GitHub CLI) for automatic download and --release.
##
## Copyright (C) 2025, HelixScreen <https://github.com/HelixScreen>
##
## This file may be distributed under the terms of the GNU GPLv3 license

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION=$(cat "$SCRIPT_DIR/version.txt" 2>/dev/null || echo "unknown")
HELIX_RELEASE=""
HELIX_REPO="prestonbrown/helixscreen"
FORK_REPO="prestonbrown/ff5m"
OUTPUT_DIR="$SCRIPT_DIR/dist"
DO_RELEASE=false

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  --release                  Build and publish as GitHub release"
    echo "  --helix-release <path>     Path to local helixscreen-ad5m*.tar.gz"
    echo "  --helix-repo <owner/repo>  GitHub repo for HelixScreen (default: $HELIX_REPO)"
    echo "  --fork-repo <owner/repo>   GitHub repo for release publishing (default: $FORK_REPO)"
    echo "  --output-dir <path>        Output directory (default: ./dist)"
    echo "  --help                     Show this help"
    echo ""
    echo "By default, downloads the latest AD5M release from GitHub using 'gh'."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --release)
            DO_RELEASE=true; shift
            ;;
        --helix-release)
            HELIX_RELEASE="$2"; shift 2
            ;;
        --helix-repo)
            HELIX_REPO="$2"; shift 2
            ;;
        --fork-repo)
            FORK_REPO="$2"; shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"; shift 2
            ;;
        --help)
            usage; exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage; exit 1
            ;;
    esac
done

# Require gh for everything (download + optional release)
if ! command -v gh >/dev/null 2>&1; then
    echo -e "${RED}Error: 'gh' (GitHub CLI) is required${NC}"
    echo "Install: https://cli.github.com/"
    exit 1
fi

echo -e "${GREEN}Building ForgeX + HelixScreen image (ForgeX v${VERSION})${NC}"
echo ""

# Create temp working directory
WORK_DIR=$(mktemp -d)
trap "rm -rf '$WORK_DIR'" EXIT

# --- Step 1: Copy mod files ---
echo -e "${BLUE}[1/4] Copying mod files...${NC}"

MOD_DIR="$WORK_DIR/opt/config/mod"
mkdir -p "$MOD_DIR"

rsync -a \
    --exclude='.git' \
    --exclude='.github' \
    --exclude='.idea' \
    --exclude='.vscode' \
    --exclude='.DS_Store' \
    --exclude='dist/' \
    --exclude='build-image.sh' \
    --exclude='sync.sh' \
    --exclude='sync_remote.sh' \
    --exclude='*.tar.gz' \
    "$SCRIPT_DIR/" "$MOD_DIR/"

echo "  Mod files copied."

# --- Step 2: Get HelixScreen release ---
echo -e "${BLUE}[2/4] Preparing HelixScreen...${NC}"

HELIX_DIR="$WORK_DIR/opt/helixscreen"
mkdir -p "$HELIX_DIR"

if [ -n "$HELIX_RELEASE" ]; then
    if [ ! -f "$HELIX_RELEASE" ]; then
        echo -e "${RED}Error: HelixScreen release not found: $HELIX_RELEASE${NC}"
        exit 1
    fi
    echo "  Using local release: $HELIX_RELEASE"
    TMP_RELEASE="$HELIX_RELEASE"
    HELIX_TAG="${HELIX_RELEASE##*-}" # try to extract version from filename
    HELIX_TAG="${HELIX_TAG%.tar.gz}"
else
    HELIX_TAG=$(gh release view --repo "$HELIX_REPO" --json tagName --jq .tagName 2>/dev/null)
    if [ -z "$HELIX_TAG" ]; then
        echo -e "${RED}Error: Could not find latest release on $HELIX_REPO${NC}"
        exit 1
    fi

    echo "  Latest HelixScreen release: $HELIX_TAG"

    ASSET_NAME=$(gh release view "$HELIX_TAG" --repo "$HELIX_REPO" --json assets --jq \
        '.assets[].name | select(startswith("helixscreen-ad5m") and endswith(".tar.gz"))' 2>/dev/null | head -1)

    if [ -z "$ASSET_NAME" ]; then
        echo -e "${RED}Error: No helixscreen-ad5m*.tar.gz asset found in $HELIX_TAG${NC}"
        gh release view "$HELIX_TAG" --repo "$HELIX_REPO" --json assets --jq '.assets[].name' 2>/dev/null
        exit 1
    fi

    echo "  Downloading $ASSET_NAME..."
    TMP_RELEASE="$WORK_DIR/$ASSET_NAME"
    gh release download "$HELIX_TAG" --repo "$HELIX_REPO" \
        --pattern "$ASSET_NAME" --dir "$WORK_DIR"

    if [ ! -f "$TMP_RELEASE" ]; then
        echo -e "${RED}Error: Download failed${NC}"
        exit 1
    fi
fi

echo "  Extracting..."
tar -xzf "$TMP_RELEASE" -C "$HELIX_DIR" --strip-components=1 2>/dev/null || \
tar xzf "$TMP_RELEASE" -C "$HELIX_DIR" --strip-components=1

if [ ! -f "$HELIX_DIR/bin/helix-screen" ]; then
    echo "  Retrying without --strip-components..."
    rm -rf "$HELIX_DIR"
    mkdir -p "$HELIX_DIR"
    tar -xzf "$TMP_RELEASE" -C "$HELIX_DIR" 2>/dev/null || \
    tar xzf "$TMP_RELEASE" -C "$HELIX_DIR"
    if [ ! -f "$HELIX_DIR/bin/helix-screen" ]; then
        echo -e "${RED}Error: HelixScreen binary not found after extraction${NC}"
        find "$HELIX_DIR" -type f | head -20
        exit 1
    fi
fi

chmod +x "$HELIX_DIR/bin/"* 2>/dev/null || true

# Version from tag (no VERSION file in release tarball)
HELIX_VER="${HELIX_TAG:-unknown}"
echo "  HelixScreen $HELIX_VER ready."

# --- Step 3: Create mod_data directory ---
echo -e "${BLUE}[3/4] Setting up mod_data...${NC}"

mkdir -p "$WORK_DIR/opt/config/mod_data"
echo "  mod_data directory created."

# --- Step 4: Package ---
echo -e "${BLUE}[4/4] Creating image archive...${NC}"

mkdir -p "$OUTPUT_DIR"

RELEASE_TAG="${VERSION}-helixscreen"
ARCHIVES=()

for variant in "Adventurer5M" "Adventurer5MPro"; do
    ARCHIVE_NAME="${variant}-ForgeX-${VERSION}-helixscreen.tgz"
    ARCHIVE_PATH="$OUTPUT_DIR/$ARCHIVE_NAME"

    (cd "$WORK_DIR" && tar -czf "$ARCHIVE_PATH" .)

    SIZE=$(du -h "$ARCHIVE_PATH" | cut -f1)
    echo "  Created: $ARCHIVE_NAME ($SIZE)"
    ARCHIVES+=("$ARCHIVE_PATH")
done

echo ""
echo -e "${GREEN}Build complete!${NC}"
echo "  ForgeX: v${VERSION}"
echo "  HelixScreen: ${HELIX_VER}"
echo ""
ls -lh "$OUTPUT_DIR"/*.tgz 2>/dev/null

# --- Optional: Publish GitHub release ---
if [ "$DO_RELEASE" = true ]; then
    echo ""
    echo -e "${BLUE}Publishing GitHub release: ${RELEASE_TAG}${NC}"

    RELEASE_NOTES="## ForgeX v${VERSION} + HelixScreen ${HELIX_VER}

Flashforge Adventurer 5M (Pro) firmware mod with [HelixScreen](https://github.com/${HELIX_REPO}) as the default touchscreen UI.

### What's included
- **ForgeX v${VERSION}** — Klipper, Moonraker, Fluidd, Mainsail, root SSH
- **HelixScreen ${HELIX_VER}** — Full-featured touchscreen UI (enabled by default)
- GuppyScreen, Feather, Stock, and Headless modes still available

### Flash instructions
1. Download the \`.tgz\` matching your printer model
2. Copy to a FAT32-formatted USB drive (**do NOT extract**)
3. Insert USB, then power on the printer
4. HelixScreen starts automatically after install

### Switching display modes
\`\`\`
SET_MOD PARAM=\"display\" VALUE=\"HELIX\"   # HelixScreen (default)
SET_MOD PARAM=\"display\" VALUE=\"GUPPY\"   # GuppyScreen
SET_MOD PARAM=\"display\" VALUE=\"STOCK\"   # Stock FlashForge
SET_MOD PARAM=\"display\" VALUE=\"FEATHER\" # Lightweight monitor
SET_MOD PARAM=\"display\" VALUE=\"HEADLESS\" # No screen
\`\`\`

### Downloads
| File | Printer |
|------|---------|
| \`Adventurer5M-ForgeX-${VERSION}.tgz\` | AD5M (non-Pro) |
| \`Adventurer5MPro-ForgeX-${VERSION}.tgz\` | AD5M Pro |
"

    gh release create "$RELEASE_TAG" \
        --repo "$FORK_REPO" \
        --title "ForgeX ${VERSION} + HelixScreen ${HELIX_VER}" \
        --notes "$RELEASE_NOTES" \
        "${ARCHIVES[@]}"

    echo ""
    echo -e "${GREEN}Release published!${NC}"
    echo "  https://github.com/${FORK_REPO}/releases/tag/${RELEASE_TAG}"
else
    echo ""
    echo "To publish as a GitHub release, run:"
    echo "  $0 --release"
fi
