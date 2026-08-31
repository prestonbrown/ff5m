# HelixScreen AD5X payload - build provenance

This tree is the HelixScreen runtime payload for the AD5X rig, synced to
/opt/config/mod/.bin/helixscreen by sync.sh and launched by
.shell/helixscreen.sh from the bootstrap (in place of the boot status card).

It is a build artifact, not source. Regenerate it after changing HelixScreen;
do not hand-edit files here.

- Source:   https://github.com/prestonbrown/helixscreen
- Built from feature/ad5x-ifs-ui HEAD b430ac179 (checked out on the local
  temp branch ad5x-ifs-ui-pkg in .worktrees/ad5x-ifs-ui-pkg, not pushed):
    b430ac179 fix(sound): detect macro-only M300 buzzers and honor the
              speaker override (cherry-picked from helix-sound 64253dd5b;
              the AD5X buzzer is a host GPIO behind [gcode_macro M300], not
              an output_pin, so without this no sound backend installs)
    d6a2257ec feat(ad5x): draw the IFS as it is - selector under the spools,
              hub on the toolhead (two-box topology, no bypass, DB style
              pins - carried here as assets/config/printer_database.json)
    69dabfb71 feat(mock): HELIX_MOCK_AMS=ifs-module drives the real IFS
              backend, and below it the branch's merged main lineage
- Version:  0.99.118
- Built:    2026-08-30, `make ad5x-docker ENABLE_REMOTE_CONTROL=yes`
            (MIPS32r5, mipsel buildroot glibc, fbdev backend, stripped)

Layout follows `make install DESTDIR=...` plus the release-ad5x packaging
rules (mk/cross.mk): dev panels removed from ui_xml/, XML minified
(scripts/minify_xml_tree.py), font sources dropped (fonts are compiled into
the binaries; only the runtime CJK .bin packs ship), assets/sounds dropped
(the AD5X has tone-mode sound only, no tracker), the ad5x preset installed
as config/settings.json.

Two files are rig-specific on top of the release layout:

- config/helixscreen.env   - remote control ON (HELIX_REMOTE_CONTROL=1, socket
                             /tmp/helixscreen-control.sock), log level, and
                             HELIX_CONFIG_DIR pinned to durable storage under
                             /opt/config/mod_data so redeploys never reset the
                             UI's state.
- platform/hooks.sh        - adapted from assets/config/platform/hooks-ad5x.sh
                             (the Z-Mod AD5X hook): cache and log paths point
                             at /opt/config/mod_data, matching this payload.

sha256 (build/ad5x/bin, at build time):

    f01a38b2f6bf4ec9974c1faa1c536bc95cd350c8d1f3a3a3bdc5f5528486073b  helix-screen
    da675622e49a2ea5e378a9f6de6947f0a541e1dc769c6953059401019652117a  helix-splash
    00220a5744fad46f311d5a182958d48c74978aac4f88d2ff4b7847de308fb8fe  helix-watchdog

The binary carries the ctl server (`strings -a bin/helix-screen | grep -c
list_callbacks` is 2, not 0), which the release targets refuse - this is a
dev rig build on purpose.

To regenerate after a HelixScreen change:

    cd <helixscreen worktree at the commit you want>
    make ad5x-docker ENABLE_REMOTE_CONTROL=yes
    make PLATFORM_TARGET=ad5x DESTDIR=/tmp/helix-stage install
    # then the release-parity steps above; see .shell/helixscreen.sh and
    # git history of this directory for the exact sequence

Runtime contract (why it runs inside the chroot): the binary is dynamically
linked against the buildroot sysroot's glibc 2.40 / libstdc++ (GCC 13.3) and
loads via /lib/ld-linux-mipsn8.so.1 - the stock host rootfs has none of
those. The Forge-X chroot ($MOD, our Buildroot 2025.02.4 rootfs) provides
them; init_buildroot bind-mounts /opt/config into the chroot at the same
path, and init_chroot shares /dev and /tmp, which is what gives the UI the
framebuffer, the touchscreen evdev nodes, and a host-reachable ctl socket.

Sound note: this build carries the macro-only M300 speaker detection, so a
printer that defines [gcode_macro M300] (Forge-X wraps its native TONE) gets
the M300 sound backend installed. The rig's mod_params variables.sound must
be 1 or Forge-X's TONE wrapper drops every tone. No startup chime on buzzer
machines - the chime fires before discovery installs the backend; judge
sound by interaction feedback, not the boot chime.
