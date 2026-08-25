# Platform Descriptor

Forge-X targets the FlashForge Adventurer 5M (Pro). Board-specific values are
named in one place rather than spelled out across the shell scripts, so that
supporting another board is a matter of defining a second descriptor rather
than editing every script.

AD5M is currently the only platform defined, and every value below is the exact
literal it replaced, so nothing about AD5M behaviour changed when the descriptor
was introduced.

## Where it lives

The descriptor is `.shell/platform.sh`, a file of its own rather than a block
inside `.shell/common.sh`.

`common.sh` is irreducibly bash: it uses arrays, `declare -n`, `[[`, and
`${BASH_SOURCE[0]}`. Both `dash` and BusyBox `ash` abort while sourcing it. Some
consumers of the descriptor declare `#!/bin/sh` and run under BusyBox ash on the
printer, so telling them to source `common.sh` would break them. `platform.sh`
is POSIX-clean and every shell in the tree can read it.

How a script gets the values depends on its shebang:

| Shebang | Sources | Why |
|---|---|---|
| `#!/bin/sh` | `.shell/platform.sh` directly | `common.sh` will not parse under ash |
| `#!/bin/bash` | `.shell/common.sh` (which sources `platform.sh` as its first action) | Gets the descriptor plus everything else `common.sh` provides |

A bash script may source `platform.sh` directly instead, and the gate accepts
either. The two `#!/bin/sh` consumers today are `.root/S35tslib` and
`.shell/commands/zstart_klipper.sh`; `zstart_klipper.sh` is the one that matters,
because sourcing `common.sh` there would abort the script and Klipper would not
start.

Because `platform.sh` is sourced by ash, it is restricted to plain `NAME=value`
assignments. No arrays, no `declare`, no `[[`, no `local`, no command
substitution. Verify a change with the real interpreters, not `bash -n`, which
accepts bashisms happily:

    dash -c '. .shell/platform.sh && echo "$LOG_DIR"'
    busybox ash -c '. .shell/platform.sh && echo "$LOG_DIR"'

## Values

| Variable | AD5M value | Meaning |
|---|---|---|
| `PLATFORM` | `ad5m` | Short identifier, lowercase |
| `PLATFORM_NAME` | `AD5M` | Display name, used in the motd banner |
| `ROOT_PART` | `/dev/mmcblk0p6` | Stock root partition |
| `DATA_PART` | `/dev/mmcblk0p7` | Writable data partition |
| `DATA_MNT` | `/data` | Where `DATA_PART` is mounted |
| `LOG_DIR` | `$DATA_MNT/logFiles` | Stock firmware log directory |
| `KLIPPER_DIR` | `/opt/klipper` | Host-side Klipper tree |
| `STOCK_UI_PROCS` | `ffstartup-arm firmwareExe` | Stock UI processes to stop for an alternative screen |

`LOG_DIR` is defined in terms of `DATA_MNT` rather than being spelled out, so a
board that mounts its data partition elsewhere moves its log directory with it.
That also means the assignment order in `platform.sh` is load-bearing:
`DATA_MNT` must be set before `LOG_DIR`.

`PLATFORM` is defined and pinned by the test but not yet read by any script. It
exists for the descriptor selection that a second board will need. There is no
selection mechanism today: `platform.sh` unconditionally defines AD5M, and there
is no `uname` branch or board detection anywhere. The seam exists; choosing
between descriptors does not.

## Rules

- Any script needing one of these values sources the descriptor and uses the
  variable. Do not reintroduce the literal.
- **Sourcing is not optional and its absence is silent.** An unset shell
  variable expands to the empty string rather than failing, so a missing source
  line turns `"$LOG_DIR/boot.log"` into `/boot.log` with no error at all. On a
  read-only root the log simply vanishes. `test/descriptor_usage_test.sh` exists
  for exactly this failure and will fail the build if a file uses a descriptor
  variable without sourcing `platform.sh` or `common.sh`.
- `STOCK_UI_PROCS` is a space-separated list and must be left **unquoted** when
  iterated with `for`, so that word splitting yields one process per iteration.
  Quoting it would look for a single process literally named
  `ffstartup-arm firmwareExe`.
- Paths *inside* the mod's own chroot stay literal. In `.shell/S00init` the bind
  mount reads `mount --bind "$KLIPPER_DIR" "$MOD"/opt/klipper`: the source is a
  host path the board dictates and is abstracted, the target is a path the mod
  creates and is not. Getting this backwards silently breaks the chroot.
- `Adventurer5M*.json` is not board-specific. FlashForge ships that same
  filename on other printers in the range, verified on AD5X hardware on
  2026-08-24, so those references in `.shell/boot/boot.sh` and
  `.shell/commands/zprint.sh` are left alone.

## Known deferred

These are board-specific but not yet abstracted. They are listed so their
absence reads as a decision rather than an oversight.

- **`Adventurer5M*.tgz`** in `.shell/boot/init_boot_flag.sh`, which detects a
  stock firmware image on a USB stick. Unlike the `.json` config, stock packages
  for other printers in the range do use different names. It belongs with the
  install and recovery work, where what the check should accept is actually
  decided.
- **The `/data` mount generally.** `DATA_MNT` currently reaches only the log
  directory and the mount call in `common.sh`. Around 49 other `/data` literals
  remain across 16 files, including the bind mount and the gcodes symlink in
  `.shell/S00init`, `zbackup.sh`'s path lists, and `zclear.sh`'s `find` sweeps.
  That residue is the real porting surface for a board that mounts its data
  partition elsewhere. Separating "path the board dictates" from "path the mod
  creates" needs case-by-case judgement at each site and is its own change.

## Testing

Two gates cover the descriptor, and `sh test/run.sh` discovers both.

| Test | What it protects |
|---|---|
| `test/platform_vars_test.sh` | Every variable still expands to the literal it replaced |
| `test/descriptor_usage_test.sh` | Every file using a variable actually sources the descriptor |

Run them after any change to `platform.sh` or its consumers:

    sh test/platform_vars_test.sh
    sh test/descriptor_usage_test.sh

Neither needs a printer or any dependency beyond a POSIX shell.
