# Building ForgeX + HelixScreen Images

## Prerequisites

- [GitHub CLI](https://cli.github.com/) (`gh`) — authenticated
- `rsync`
- ~200MB free disk space

## Quick Start

```bash
# Build only (outputs to dist/)
./build-image.sh

# Build and publish as GitHub release
./build-image.sh --release
```

That's it. The script automatically downloads the latest HelixScreen AD5M release from GitHub, bundles it with ForgeX, and creates the flashable .tgz images.

## Output

Two files in `dist/`:
- `Adventurer5M-ForgeX-<version>-helixscreen.tgz` — AD5M (non-Pro)
- `Adventurer5MPro-ForgeX-<version>-helixscreen.tgz` — AD5M Pro

These are identical contents with different filenames. The stock FlashForge firmware checks the filename to match the printer model (`auto_run.sh` globs `Adventurer5MPro-*.tgz`), so both are required.

## Options

| Flag | Description |
|------|-------------|
| `--release` | Build + publish to GitHub releases on `prestonbrown/ff5m` |
| `--helix-release <path>` | Use a local HelixScreen tarball instead of downloading |
| `--helix-repo <owner/repo>` | Override HelixScreen GitHub repo (default: `prestonbrown/helixscreen`) |
| `--fork-repo <owner/repo>` | Override release target repo (default: `prestonbrown/ff5m`) |
| `--output-dir <path>` | Output directory (default: `./dist`) |

## Using a Local HelixScreen Build

If you want to test with a local build instead of the latest release:

```bash
# Build HelixScreen for AD5M first (in the helixscreen repo)
make ad5m-docker
scripts/package.sh ad5m

# Then use it
./build-image.sh --helix-release ~/Code/Printing/helixscreen/dist/helixscreen-ad5m-v0.9.20.tar.gz
```

## How the Image Works

The .tgz contains:
```
opt/
├── config/
│   ├── mod/          ← ForgeX mod files (this repo)
│   └── mod_data/     ← Runtime state directory (empty at install)
└── helixscreen/      ← HelixScreen binary + assets
    ├── bin/
    ├── config/
    ├── ui_xml/
    └── assets/
```

The stock FlashForge firmware extracts this on boot when it finds the .tgz on USB. ForgeX's `start.sh` checks the `display` mod parameter and starts HelixScreen (or GuppyScreen/Stock/etc.) accordingly.

## Flashing

1. Copy the `.tgz` matching your printer to a FAT32 USB drive (do NOT extract)
2. Insert USB before powering on
3. Printer installs automatically on boot
4. HelixScreen is the default — no configuration needed

## Switching Display Modes

After flashing, you can switch between display modes via Klipper console:

```
SET_MOD PARAM="display" VALUE="HELIX"     # HelixScreen (default)
SET_MOD PARAM="display" VALUE="GUPPY"     # GuppyScreen
SET_MOD PARAM="display" VALUE="STOCK"     # Stock FlashForge
SET_MOD PARAM="display" VALUE="FEATHER"   # Lightweight monitor
SET_MOD PARAM="display" VALUE="HEADLESS"  # No screen
```
