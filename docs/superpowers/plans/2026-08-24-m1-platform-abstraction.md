# M1: Platform Abstraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Forge-X's hardcoded single-board assumptions with a platform descriptor, changing no behaviour on AD5M and adding no AD5X code.

**Architecture:** A platform descriptor block in `.shell/common.sh` defines named variables for every board-specific value (data partition, mount point, log directory, Klipper tree, stock UI process names, display name). All other scripts consume those variables instead of literals. AD5M remains the only platform defined, so every variable resolves to exactly the literal it replaced.

**Tech Stack:** POSIX shell and bash (BusyBox ash on target). No new dependencies, no CI changes, no test framework.

**Spec:** `docs/devel/plans/2026-08-24-ad5x-platform-design.md` (section 7, and milestone M1 in section 8)

**Depends on:** `docs/superpowers/plans/2026-08-24-m0-test-harness.md`. M0 must land first: every test here sources `test/lib/assert.sh` and is discovered by `test/run.sh`. M0 is a separate upstream PR with no AD5X content, deliberately, so this refactor arrives with a way to demonstrate that AD5M did not regress.

## Global Constraints

- **Zero behaviour change on AD5M.** Every variable must expand to the exact literal it replaced. This is the property the test in Task 1 enforces and every later task re-runs.
- **No AD5X code in M1.** No second platform, no `uname -m` branch, no conditionals on board type. M1 creates the seam only. Introducing AD5X values here would make the change unreviewable for a maintainer who owns only AD5M.
- **Minimise novel surface.** No new dependencies beyond what M0 established. The test added here is a plain shell script using M0's assertion library, runnable with `sh test/run.sh`.
- **Respect each file's shebang.** Files declaring `#!/bin/sh` must stay POSIX-clean; only files declaring `#!/bin/bash` may use bashisms. Verify with the matching interpreter, not always `bash -n`.
- **Do not touch `docs/`, `.github/ISSUE_TEMPLATE/`, or `.bin/src/`.** Those contain AD5M references that are correct today (release filenames, issue templates, program descriptions). Changing them is scope creep and dilutes the diff.
- **`Adventurer5M*.json` is NOT a portability problem.** FlashForge ships that same filename on the AD5X (verified on hardware 2026-08-24, symlinked at `/opt/config/Adventurer5M.json`). Leave those globs alone.

---

## File Structure

**Created:**
- `test/platform_vars_test.sh` - value-preservation test. Sources `common.sh` and asserts every platform variable equals the historical AD5M literal. Uses the M0 harness, so `sh test/run.sh` picks it up automatically. This is the guard rail for the whole milestone.

**Modified:**
- `.shell/common.sh` - gains the platform descriptor block. Single source of truth.
- `.shell/S00init`, `.shell/S55boot`, `.shell/S98zssh`, `.shell/S99root`, `.shell/boot/boot.sh`, `.shell/boot/wifi_connect.sh`, `.shell/boot/wifi_reconnect.sh`, `.shell/boot/init_swap.sh`, `.shell/commands/zbackup.sh`, `.root/forge-x` - log directory literals.
- `.shell/common.sh`, `.root/S35tslib`, `.root/forge-x` - partition and mount literals.
- `.shell/S00init`, `.shell/commands/zstart_klipper.sh`, `.shell/commands/ztune_klipper.sh`, `.shell/fix_config.sh`, `.shell/uninstall.sh` - Klipper tree literals.
- `.shell/commands/zdisplay.sh` - stock UI process names.
- `.shell/motd.sh` - display name.

---

### Task 1: Platform descriptor and its guard rail

This task creates the seam and the test that keeps every later task honest. Nothing consumes the variables yet.

**Files:**
- Modify: `.shell/common.sh:9` (insert descriptor block above `MOD=`)
- Create: `test/platform_vars_test.sh`
- Create: `test/descriptor_usage_test.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: shell variables sourced from `.shell/common.sh`, all strings:
  `PLATFORM` (`ad5m`), `PLATFORM_NAME` (`AD5M`), `DATA_PART` (`/dev/mmcblk0p7`),
  `ROOT_PART` (`/dev/mmcblk0p6`), `DATA_MNT` (`/data`), `LOG_DIR` (`/data/logFiles`),
  `KLIPPER_DIR` (`/opt/klipper`), `STOCK_UI_PROCS` (`ffstartup-arm firmwareExe`).
  Every later task consumes these and adds none.

- [ ] **Step 1: Write the failing test**

Create `test/platform_vars_test.sh`:

```sh
#!/bin/sh
# Platform descriptor value-preservation test.
#
# Every variable in common.sh's platform descriptor must expand to the exact
# literal it replaced, so that abstracting them changes no behaviour on AD5M.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

# common.sh mutates PATH and unsets LD_* on source; harmless here.
# shellcheck disable=SC1090,SC1091
. "$REPO_DIR/.shell/common.sh"

assert_eq "PLATFORM"       "ad5m"                      "$PLATFORM"
assert_eq "PLATFORM_NAME"  "AD5M"                      "$PLATFORM_NAME"
assert_eq "DATA_PART"      "/dev/mmcblk0p7"            "$DATA_PART"
assert_eq "ROOT_PART"      "/dev/mmcblk0p6"            "$ROOT_PART"
assert_eq "DATA_MNT"       "/data"                     "$DATA_MNT"
assert_eq "LOG_DIR"        "/data/logFiles"            "$LOG_DIR"
assert_eq "KLIPPER_DIR"    "/opt/klipper"              "$KLIPPER_DIR"
assert_eq "STOCK_UI_PROCS" "ffstartup-arm firmwareExe" "$STOCK_UI_PROCS"

# MOD must still resolve exactly as before the refactor.
assert_eq "MOD"            "/data/.mod/.forge-x"       "$MOD"

finish
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sh test/platform_vars_test.sh`
Expected: FAIL. Every `check` except `MOD` reports an empty `got`, because the descriptor does not exist yet.

- [ ] **Step 3: Add the descriptor block to common.sh**

In `.shell/common.sh`, insert immediately after the licence header (before the existing `MOD=` line):

```bash
## Platform descriptor
##
## Every board-specific value used anywhere in the mod is named here. Scripts
## consume these variables rather than hardcoding literals, so that supporting
## a second board is a matter of defining a second descriptor rather than
## editing every script.
##
## AD5M is currently the only supported platform, so each value below is the
## literal it replaced. test/platform_vars_test.sh enforces that.

PLATFORM=ad5m
PLATFORM_NAME=AD5M

ROOT_PART=/dev/mmcblk0p6
DATA_PART=/dev/mmcblk0p7
DATA_MNT=/data

LOG_DIR=$DATA_MNT/logFiles
KLIPPER_DIR=/opt/klipper

## Stock FlashForge UI processes that must be stopped for an alternative
## screen to own the framebuffer. Space-separated, iterated with `for`.
STOCK_UI_PROCS="ffstartup-arm firmwareExe"
```

Then change the existing `MOD` line from:

```bash
MOD=/data/.mod/.forge-x
```

to:

```bash
MOD=$DATA_MNT/.mod/.forge-x
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `sh test/platform_vars_test.sh`
Expected: PASS, with every line reporting `ok`. In particular `MOD = /data/.mod/.forge-x`, proving the rewrite preserved the value.

- [ ] **Step 5: Write the gate that catches the real failure mode**

`platform_vars_test.sh` pins the values, but it cannot catch what will actually
go wrong during the sweeps in Tasks 3 and 4: a file that uses `$LOG_DIR` or
`$KLIPPER_DIR` **without sourcing `common.sh`**. An unset variable expands to the
empty string, so `"$LOG_DIR/boot.log"` silently becomes `/boot.log`. Nothing
errors; logs just go to the wrong place, and on a read-only root they vanish.

Create `test/descriptor_usage_test.sh`:

```sh
#!/bin/sh
# Any script using a platform descriptor variable must source common.sh.
#
# An unset shell variable expands to the empty string rather than failing, so a
# missing source turns "$LOG_DIR/boot.log" into "/boot.log" with no error at all.
# This gate is the reason the descriptor refactor is safe to do mechanically.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

DESCRIPTOR_VARS="PLATFORM PLATFORM_NAME ROOT_PART DATA_PART DATA_MNT LOG_DIR KLIPPER_DIR STOCK_UI_PROCS"

for f in $(git ls-files '.shell/*' '.root/*'); do
    [ -f "$f" ] || continue
    # common.sh defines them; it does not need to source itself.
    [ "$f" = ".shell/common.sh" ] && continue

    uses=""
    for v in $DESCRIPTOR_VARS; do
        if grep -q "\$$v\|\${$v}" "$f" 2>/dev/null; then
            uses="$uses $v"
        fi
    done

    [ -z "$uses" ] && continue

    if grep -q 'common\.sh' "$f"; then
        _t_pass "sources common.sh:$f (uses$uses)"
    else
        _t_fail "sources common.sh: $f" \
                "uses$uses but never sources common.sh; those expand to empty"
    fi
done

finish
```

- [ ] **Step 6: Verify the gate is green now and can actually fail**

Run: `sh test/descriptor_usage_test.sh; echo "exit=$?"`

Expected: `exit=0`. At this point only `common.sh` defines the variables and
nothing consumes them yet, so the loop finds no files and the suite is trivially
green. That is correct for now and it becomes load-bearing in Tasks 3 and 4.

Prove it fires before relying on it:

```bash
printf '#!/bin/sh\necho "$LOG_DIR/x"\n' > .shell/zz_unsourced.sh
git add -N .shell/zz_unsourced.sh
sh test/descriptor_usage_test.sh; echo "exit=$?"
```

Expected: `NOT OK sources common.sh: .shell/zz_unsourced.sh` and `exit=1`.

Clean up and confirm green:

```bash
git rm -f --cached .shell/zz_unsourced.sh && rm -f .shell/zz_unsourced.sh
sh test/descriptor_usage_test.sh; echo "exit=$?"
```

- [ ] **Step 7: Syntax-check the modified file with its own interpreter**

Run: `head -1 .shell/common.sh` to confirm the shebang, then `bash -n .shell/common.sh`
Expected: no output, exit 0.

- [ ] **Step 8: Commit**

```bash
git add test/platform_vars_test.sh test/descriptor_usage_test.sh .shell/common.sh
git commit -m "Add platform descriptor to common.sh

Names every board-specific value in one place so that scripts consume
variables rather than literals. AD5M remains the only platform and every
value is the literal it replaced, enforced by test/platform_vars_test.sh."
```

---

### Task 2: Partition and mount abstraction

**Files:**
- Modify: `.shell/common.sh:49-61` (`mount_data_partition`)
- Modify: `.root/S35tslib:41`
- Modify: `.root/forge-x:32`

**Interfaces:**
- Consumes: `DATA_PART`, `DATA_MNT`, `ROOT_PART` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Replace the literals in `mount_data_partition`**

In `.shell/common.sh`, the function currently reads:

```bash
mount_data_partition() {
    # mount data - this would otherwise be mounted later by Flashforge's firmware
    if ! mount | grep -q /dev/mmcblk0p7; then
        echo "// Mounting /data partition..."
        fsck -y /dev/mmcblk0p7 || true
        mount /dev/mmcblk0p7 /data;
    fi
```

Change those four lines to use the variables:

```bash
mount_data_partition() {
    # mount data - this would otherwise be mounted later by Flashforge's firmware
    if ! mount | grep -q "$DATA_PART"; then
        echo "// Mounting $DATA_MNT partition..."
        fsck -y "$DATA_PART" || true
        mount "$DATA_PART" "$DATA_MNT";
    fi
```

Also update the second occurrence further down the same function (the `if ! mount | grep -q /dev/mmcblk0p7; then` at line 61) to `if ! mount | grep -q "$DATA_PART"; then`.

Leave the commented-out block (lines 56-59) alone. It is dead code and editing it adds diff noise.

- [ ] **Step 2: Replace the root partition literals**

In `.root/S35tslib`, change:

```sh
        mount /dev/mmcblk0p6 /tmp/parent_root
```

to:

```sh
        mount "$ROOT_PART" /tmp/parent_root
```

In `.root/forge-x`, change:

```sh
    mount /dev/mmcblk0p6 /_root
```

to:

```sh
    mount "$ROOT_PART" /_root
```

- [ ] **Step 3: Verify both files source common.sh**

Run: `grep -n 'common.sh' .root/S35tslib .root/forge-x`
Expected: each file shows a line sourcing `common.sh`.

If either does **not** source it, add this near the top of that file, after its shebang and licence header:

```sh
source /opt/config/mod/.shell/common.sh
```

matching however the sibling scripts in `.root/` do it. Do not invent a different mechanism.

- [ ] **Step 4: Confirm no partition literals remain outside the descriptor**

Run: `git grep -nE 'mmcblk0p[0-9]' -- .shell .root .py | grep -v 'common.sh'`
Expected: no output. Only `common.sh` may name a partition device.

- [ ] **Step 5: Syntax-check and re-run the guard rail**

Run each of these and confirm exit 0:

```bash
bash -n .shell/common.sh
sh -n .root/S35tslib || bash -n .root/S35tslib
sh -n .root/forge-x  || bash -n .root/forge-x
sh test/platform_vars_test.sh
```

Expected: no syntax errors, and `PASS` from the test.

- [ ] **Step 6: Commit**

```bash
git add .shell/common.sh .root/S35tslib .root/forge-x
git commit -m "Use platform descriptor for partition and mount paths

The data and root partition devices and the data mount point now come from
the descriptor rather than being spelled out at each mount site."
```

---

### Task 3: Log directory abstraction

The largest mechanical sweep. `/data/logFiles` becomes `$LOG_DIR` everywhere.

**Files:**
- Modify: `.shell/S00init:68-70,159,263`, `.shell/S55boot:71`, `.shell/S98zssh:57,81,169`, `.shell/S99root:108`, `.shell/boot/boot.sh:56`, `.shell/boot/wifi_connect.sh:45`, `.shell/boot/wifi_reconnect.sh:27`, `.shell/commands/zbackup.sh:46-59`, `.root/forge-x:77-79`

**Interfaces:**
- Consumes: `LOG_DIR` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Enumerate every site before changing anything**

Run: `git grep -n '/data/logFiles' -- .shell .root .py`

Record the count. Every one of these becomes `$LOG_DIR`, quoted. Work through the list file by file rather than with a blind tree-wide `sed`, because several sites sit inside double-quoted strings and a few inside single-quoted ones where `$LOG_DIR` would not expand.

- [ ] **Step 2: Replace them, minding the quoting**

The rule for each site:

- Bare or double-quoted use: `/data/logFiles/boot.log` becomes `"$LOG_DIR/boot.log"`.
- Already inside a double-quoted string: `"/data/logFiles/ssh.log"` becomes `"$LOG_DIR/ssh.log"`.
- Inside single quotes: rewrite the quoting so the variable expands. A single-quoted `'/data/logFiles/x'` must become `"$LOG_DIR/x"`, not `'$LOG_DIR/x'`, which would pass the literal text `$LOG_DIR` to the command.
- Glob suffixes are preserved: `/data/logFiles/boot.log*` becomes `"$LOG_DIR"/boot.log*`. Note the closing quote before the glob, so the `*` still expands.

Worked example, `.shell/S00init:68-70`:

```bash
    rotate_logs "$LOG_DIR/boot.log"
    rotate_logs "$LOG_DIR/ssh.log"
    rotate_logs "$LOG_DIR/wifi.log"
```

Worked example, `.shell/S00init:159` and `:263`:

```bash
    ln -fns /opt/config/mod_data/log "$LOG_DIR/mod"
    ln -fns "$LOG_DIR" /root/printer_data/logs
```

Worked example, `.shell/commands/zbackup.sh:46-48` (glob suffix):

```bash
    "$LOG_DIR"/boot.log*
    "$LOG_DIR"/skip.log*
    "$LOG_DIR"/ssh.log*
```

- [ ] **Step 3: Confirm every file that now uses `$LOG_DIR` sources common.sh**

Run:

Run: `sh test/descriptor_usage_test.sh`

Expected: `PASS`. Any file reported must get a `common.sh` source line matching how its siblings do it, or it will expand `$LOG_DIR` to the empty string and write logs to `/boot.log`. This is the gate written in Task 1 step 5, now doing the job it exists for.

- [ ] **Step 4: Confirm no literals remain**

Run: `git grep -n '/data/logFiles' -- .shell .root .py`
Expected: no output.

- [ ] **Step 5: Check nothing expands to an empty path**

Run:

```bash
sh test/platform_vars_test.sh
git grep -n '"\$LOG_DIR' -- .shell .root | wc -l
git grep -n "'\\\$LOG_DIR" -- .shell .root
```

Expected: the test prints `PASS`; the second command's count is non-zero; the third prints nothing, confirming no site left `$LOG_DIR` inside single quotes where it cannot expand.

- [ ] **Step 6: Syntax-check every touched file**

Run:

```bash
for f in .shell/S00init .shell/S55boot .shell/S98zssh .shell/S99root \
         .shell/boot/boot.sh .shell/boot/wifi_connect.sh \
         .shell/boot/wifi_reconnect.sh .shell/commands/zbackup.sh .root/forge-x; do
    if head -1 "$f" | grep -q bash; then bash -n "$f" || echo "SYNTAX: $f";
    else sh -n "$f" || echo "SYNTAX: $f"; fi
done
```

Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add .shell .root
git commit -m "Use platform descriptor for the log directory

/data/logFiles is AD5M's stock log location. Routing it through LOG_DIR
means a board with a different layout needs one descriptor value rather
than edits at twenty call sites."
```

---

### Task 4: Klipper tree abstraction

**Files:**
- Modify: `.shell/S00init:248,253,302`, `.shell/commands/zstart_klipper.sh:21`, `.shell/commands/ztune_klipper.sh:5-6`, `.shell/fix_config.sh:16-17`, `.shell/uninstall.sh:13`

**Interfaces:**
- Consumes: `KLIPPER_DIR` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Enumerate the sites**

Run: `git grep -n '/opt/klipper' -- .shell .root .py`

- [ ] **Step 2: Replace them**

Each `/opt/klipper` becomes `$KLIPPER_DIR`, quoted where it forms part of a path argument.

Worked example, `.shell/commands/zstart_klipper.sh:21`:

```bash
KLIPPER_START="$KLIPPER_DIR/start.sh"
```

Worked example, `.shell/commands/ztune_klipper.sh:5-6`:

```bash
MCU_F="$KLIPPER_DIR/klippy/mcu.py"
TOOLHEAD_F="$KLIPPER_DIR/klippy/toolhead.py"
```

Worked example, `.shell/fix_config.sh:16-17`:

```bash
    rm -f "$KLIPPER_DIR/klippy/extras/zmod.py"
    rm -f "$KLIPPER_DIR/klippy/extras/zmod_color.py"
```

Worked example, `.shell/uninstall.sh:13` and `.shell/S00init:302`:

```bash
    local TARGET_DIR="$KLIPPER_DIR/klippy"
```

Worked example, `.shell/S00init:248,253` (note the bind mount target keeps its `/opt/klipper` shape *inside* `$MOD`, because that is a path within the mod's own chroot, not a host path):

```bash
    mkdir -p "$MOD"/opt/klipper
    mount --bind "$KLIPPER_DIR" "$MOD"/opt/klipper
```

Only the **source** of the bind mount is a host path and becomes `$KLIPPER_DIR`. The target inside `$MOD` stays literal. Getting this backwards silently breaks the chroot.

- [ ] **Step 3: Confirm every touched file sources common.sh**

Run:

Run: `sh test/descriptor_usage_test.sh`

Expected: `PASS`.

- [ ] **Step 4: Confirm only the intended literals remain**

Run: `git grep -n '/opt/klipper' -- .shell .root .py`
Expected: only the two `"$MOD"/opt/klipper` bind-mount *target* lines in `.shell/S00init`. Nothing else.

- [ ] **Step 5: Syntax-check and re-run the guard rail**

Run:

```bash
for f in .shell/S00init .shell/commands/zstart_klipper.sh \
         .shell/commands/ztune_klipper.sh .shell/fix_config.sh .shell/uninstall.sh; do
    if head -1 "$f" | grep -q bash; then bash -n "$f" || echo "SYNTAX: $f";
    else sh -n "$f" || echo "SYNTAX: $f"; fi
done
sh test/platform_vars_test.sh
```

Expected: no syntax output, and `PASS`.

- [ ] **Step 6: Commit**

```bash
git add .shell
git commit -m "Use platform descriptor for the Klipper tree path

Only the host-side path is abstracted. The bind-mount target inside the
mod chroot stays literal, since it is a path we create rather than one
the board dictates."
```

---

### Task 5: Stock UI processes and display name

**Files:**
- Modify: `.shell/commands/zdisplay.sh:135-136`
- Modify: `.shell/motd.sh:30`

**Interfaces:**
- Consumes: `STOCK_UI_PROCS`, `PLATFORM_NAME` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Replace the hardcoded process names**

`.shell/commands/zdisplay.sh` currently reads:

```sh
    killall "ffstartup-arm" &> /dev/null
    killall "firmwareExe" &> /dev/null
```

Replace both lines with a loop over the descriptor:

```sh
    for _ui_proc in $STOCK_UI_PROCS; do
        killall "$_ui_proc" &> /dev/null
    done
```

`$STOCK_UI_PROCS` is deliberately unquoted in the `for`, because word-splitting is how the space-separated list becomes separate iterations. Quoting it would try to kill one process literally named `ffstartup-arm firmwareExe`.

- [ ] **Step 2: Replace the display name in the motd**

`.shell/motd.sh:30` currently reads:

```bash
FF_TEXT=$(centered "\033[1;33m⚡ \033[36mAD5M v${FIRMWARE_VERSION}" 35 $_OFFSET)
```

Change the literal `AD5M` to the variable, leaving the escape codes and width argument untouched:

```bash
FF_TEXT=$(centered "\033[1;33m⚡ \033[36m${PLATFORM_NAME} v${FIRMWARE_VERSION}" 35 $_OFFSET)
```

The `35` is a field width used for centring. `AD5M` and `${PLATFORM_NAME}` are the same four characters today, so the banner is unchanged.

- [ ] **Step 3: Verify the motd renders identically**

Run: `FIRMWARE_VERSION=1.4.1 bash .shell/motd.sh`
Expected: the banner prints and reads `AD5M v1.4.1`, visually identical to before the change. If `motd.sh` requires additional environment or sourcing to run standalone, run it as `bash -n .shell/motd.sh` instead and confirm the substitution by eye with `grep -n 'PLATFORM_NAME' .shell/motd.sh`.

- [ ] **Step 4: Confirm the literals are gone from functional code**

Run: `git grep -nE 'ffstartup-arm|firmwareExe' -- .shell .root | grep -v 'zbackup.sh' | grep -v '^\S*:[0-9]*: *#'`

Expected: no output. `zbackup.sh` is excluded deliberately: its `firmwareExe.log` and `ffstartup-arm.log` entries are stock **log filenames**, not process names, and are a separate concern from stopping a UI. Comment lines in `boot.sh` mentioning `firmwareExe` explain stock behaviour and stay as prose.

- [ ] **Step 5: Syntax-check and re-run the guard rail**

Run:

```bash
bash -n .shell/motd.sh
sh -n .shell/commands/zdisplay.sh || bash -n .shell/commands/zdisplay.sh
sh test/platform_vars_test.sh
```

Expected: no syntax output, and `PASS`.

- [ ] **Step 6: Commit**

```bash
git add .shell/commands/zdisplay.sh .shell/motd.sh
git commit -m "Use platform descriptor for stock UI processes and display name

The set of stock processes that must be stopped for an alternative screen
to own the framebuffer is board-specific, so it becomes a descriptor value
iterated at the call site."
```

---

### Task 6: Whole-tree gate and documentation

Closes the milestone: proves no functional literal survived, and records the descriptor so the next person extends it rather than adding a literal back.

**Files:**
- Create: `docs/PLATFORM.md`
- Modify: none

**Interfaces:**
- Consumes: every variable from Task 1.
- Produces: nothing.

- [ ] **Step 1: Run the whole-tree literal sweep**

```bash
echo "--- partitions ---"
git grep -nE 'mmcblk0p[0-9]' -- .shell .root .py | grep -v 'common.sh'
echo "--- log dir ---"
git grep -n '/data/logFiles' -- .shell .root .py
echo "--- klipper ---"
git grep -n '/opt/klipper' -- .shell .root .py | grep -v '"\$MOD"/opt/klipper'
echo "--- stock ui procs ---"
git grep -nE 'killall "(ffstartup-arm|firmwareExe)"' -- .shell .root
echo "--- done ---"
```

Expected: every section empty. Any hit is a missed site and must be fixed before continuing.

- [ ] **Step 2: Syntax-check the whole tree with correct interpreters**

```bash
fail=0
for f in $(git ls-files '.shell/*' '.root/*'); do
    head -1 "$f" | grep -q '^#!' || continue
    if head -1 "$f" | grep -q bash; then
        bash -n "$f" || { echo "SYNTAX: $f"; fail=1; }
    else
        sh -n "$f" || { echo "SYNTAX: $f"; fail=1; }
    fi
done
echo "exit=$fail"
```

Expected: `exit=0` with no `SYNTAX:` lines.

Note: `.root/stop.sh` declares `#!/bin/sh` but contains a bashism and fails `sh -n` on the **unmodified** upstream file. Confirm any such failure is pre-existing by checking out the original and re-running: `git stash list` is not needed, use `git show origin/main:.root/stop.sh > /tmp/orig.sh && sh -n /tmp/orig.sh`. A failure present on the original is not ours to fix in this milestone.

- [ ] **Step 3: Run the guard rail one final time**

Run: `sh test/platform_vars_test.sh`
Expected: `PASS`, every line `ok`.

- [ ] **Step 4: Write the platform documentation**

Create `docs/PLATFORM.md`:

```markdown
# Platform Descriptor

Forge-X targets the FlashForge Adventurer 5M (Pro). Board-specific values are
named in one place rather than spelled out across the shell scripts, so that
supporting another board is a matter of defining a second descriptor.

The descriptor lives at the top of `.shell/common.sh`:

| Variable | AD5M value | Meaning |
|---|---|---|
| `PLATFORM` | `ad5m` | Short identifier, lowercase |
| `PLATFORM_NAME` | `AD5M` | Display name, used in the motd banner |
| `ROOT_PART` | `/dev/mmcblk0p6` | Stock root partition |
| `DATA_PART` | `/dev/mmcblk0p7` | Writable data partition |
| `DATA_MNT` | `/data` | Where `DATA_PART` is mounted |
| `LOG_DIR` | `/data/logFiles` | Stock firmware log directory |
| `KLIPPER_DIR` | `/opt/klipper` | Host-side Klipper tree |
| `STOCK_UI_PROCS` | `ffstartup-arm firmwareExe` | Stock UI processes to stop for an alternative screen |

## Rules

- Any script needing one of these values sources `.shell/common.sh` and uses
  the variable. Do not reintroduce the literal.
- `STOCK_UI_PROCS` is a space-separated list and must be left unquoted when
  iterated with `for`, so that word splitting yields one process per iteration.
- Paths *inside* the mod's own chroot (`"$MOD"/opt/klipper`) are ours, not the
  board's, and stay literal.
- `Adventurer5M*.json` is not board-specific. FlashForge ships that filename on
  other printers in the range too, so those globs are left alone.

## Testing

`test/platform_vars_test.sh` asserts that every descriptor variable expands to the
literal it replaced. Run it after any change to the descriptor or its consumers:

    sh test/platform_vars_test.sh

It requires no dependencies and does not need to run on a printer.
```

- [ ] **Step 5: Verify the documentation matches reality**

Run:

```bash
grep -E '^(PLATFORM|PLATFORM_NAME|ROOT_PART|DATA_PART|DATA_MNT|LOG_DIR|KLIPPER_DIR|STOCK_UI_PROCS)=' .shell/common.sh
```

Compare each value against the table in `docs/PLATFORM.md`. They must match exactly. A doc that drifts from the descriptor on the first commit is worse than no doc.

- [ ] **Step 6: Commit**

```bash
git add docs/PLATFORM.md
git commit -m "Document the platform descriptor

Records what each value means and the three rules that are easy to get
wrong: word splitting on STOCK_UI_PROCS, chroot-internal paths staying
literal, and Adventurer5M*.json not being board-specific."
```

---

## Self-Review

**Spec coverage.** Section 7 of the spec asks for the hardcoded `Adventurer5M*` paths, the AD5M motd string, and the implicit single-board assumption to be removed, with no AD5X code. Tasks 1-5 cover the functional literals (partitions, log directory, Klipper tree, stock UI processes, display name) and Task 6 gates the result. One spec item is deliberately **not** implemented: the `Adventurer5M*.json` and `Adventurer5M*.tgz` globs. Hardware verification on 2026-08-24 showed the AD5X ships the same `Adventurer5M.json` filename, so abstracting those would be churn without portability benefit. This is recorded in the Global Constraints and in `docs/PLATFORM.md` rather than left implicit.

**Placeholder scan.** No TBD, TODO, or "handle edge cases" steps. Every code step shows the literal before-and-after text. The two sweeps (Tasks 3 and 4) enumerate their sites with a `git grep` in step 1 and give worked examples for each distinct quoting case rather than a single unrepresentative sample.

**Type consistency.** Eight variables are defined in Task 1 and consumed by name in Tasks 2-5: `DATA_PART` and `DATA_MNT` and `ROOT_PART` (Task 2), `LOG_DIR` (Task 3), `KLIPPER_DIR` (Task 4), `STOCK_UI_PROCS` and `PLATFORM_NAME` (Task 5). `PLATFORM` is defined and documented but not consumed by any task in M1, which is intentional: it exists for the M2+ descriptor selection and is asserted by the test so it cannot silently drift. Every name in the test, the tasks, and `docs/PLATFORM.md` matches.

**Risk noted for the executor.** The single most likely way to break this silently is a file that uses `$LOG_DIR` or `$KLIPPER_DIR` without sourcing `common.sh`. An unset variable expands to the empty string, so `"$LOG_DIR/boot.log"` becomes `/boot.log` and logging quietly writes to the wrong place instead of erroring. Tasks 3 and 4 each carry an explicit source-check step for exactly this reason; do not skip them.
