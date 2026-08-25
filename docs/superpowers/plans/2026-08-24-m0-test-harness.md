# M0: Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Forge-X its first test infrastructure: a dependency-free shell assertion harness, static analysis, config consistency gates, and CI to run them.

**Architecture:** A ~60-line pure-POSIX assertion library plus a runner that discovers `test/*_test.sh`. Tests need no packages, so they run on a dev machine, in CI, and on the printer itself. Static analysis is shellcheck. Python tests for klippy plugins use pytest with a stubbed Klipper config, which runs on dev and CI only.

**Tech Stack:** POSIX shell (no bats, no dependencies for the shell suite), shellcheck, pytest for the Python plugins, GitHub Actions.

**Spec:** `docs/devel/plans/2026-08-24-ad5x-platform-design.md`. This milestone is not in the spec's original numbering; it was added because the spec's upstreaming constraint (section 1) requires that each increment be independently mergeable, and a platform refactor is not reviewable without a way to show the existing platform did not regress.

## Global Constraints

- **The shell suite takes no dependencies.** Not bats, not a package. A maintainer must be able to run it with `sh test/run.sh` on a fresh checkout, and it must be runnable on the printer over SSH where BusyBox ash is the only shell.
- **No test may require a printer.** Everything here is static: file contents, config symmetry, variable expansion. Hardware tests are a separate concern.
- **No existing behaviour changes.** This milestone adds files only. The one exception is `.github/workflows/`, which gains a workflow alongside the existing `stale.yml`.
- **Respect each file's shebang.** Target shells are BusyBox ash. Files declaring `#!/bin/sh` must stay POSIX-clean; only `#!/bin/bash` files may use bashisms.
- **Pre-existing failures are not ours to fix.** Some upstream scripts already fail static analysis. The gates must record a baseline and fail only on *new* problems, or the first CI run is red and the PR is unmergeable.

---

## File Structure

**Created:**
- `test/lib/assert.sh` - assertion primitives and counters. Sourced by every test. One responsibility: report pass/fail and track a failure count.
- `test/run.sh` - discovers and runs `test/*_test.sh`, aggregates exit codes, prints a summary. One responsibility: orchestration.
- `test/syntax_test.sh` - every script parses under its declared interpreter.
- `test/display_modes_test.sh` - config symmetry: each display mode excludes every other.
- `test/shellcheck_baseline.txt` - recorded list of pre-existing shellcheck findings.
- `test/shellcheck_test.sh` - fails only on findings absent from the baseline.
- `test/python/conftest.py` - stub Klipper `config` and `printer` objects.
- `test/python/test_mod_params.py` - unit tests for the `mod_params` klippy plugin.
- `.github/workflows/test.yml` - runs the shell suite, shellcheck, and pytest.
- `docs/TESTING.md` - how to run and extend the suite.

**Modified:** none.

---

### Task 1: Assertion harness and runner

**Files:**
- Create: `test/lib/assert.sh`
- Create: `test/run.sh`
- Create: `test/harness_test.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: shell functions sourced by every later test:
  `assert_eq <name> <want> <got>`, `assert_ne <name> <unwanted> <got>`,
  `assert_empty <name> <value>`, `assert_contains <name> <haystack> <needle>`,
  `assert_file <path>`, `fail <message>`, and `finish` which prints the summary
  and exits non-zero if anything failed. Tests set no variables themselves;
  `assert.sh` owns the `_T_FAILED` and `_T_RUN` counters.

- [ ] **Step 1: Write a test for the harness itself**

The harness is the one thing nothing else can verify, so it gets a self-test that exercises both outcomes.

Create `test/harness_test.sh`:

```sh
#!/bin/sh
# Self-test for the assertion harness.
# Verifies that passing assertions pass and that failing ones are counted.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

assert_eq       "eq matches"        "abc"       "abc"
assert_ne       "ne differs"        "abc"       "xyz"
assert_empty    "empty is empty"    ""
assert_contains "contains finds"    "hay needle hay" "needle"
assert_file     "this file exists"  "$SCRIPT_DIR/harness_test.sh"

# Verify failures are actually counted, without failing this suite.
# Run a nested shell so the failure lands in its counter, not ours.
_nested=$(
    . "$SCRIPT_DIR/lib/assert.sh"
    assert_eq "deliberate mismatch" "a" "b" >/dev/null 2>&1
    echo "$_T_FAILED"
)
assert_eq "failures are counted" "1" "$_nested"

finish
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sh test/harness_test.sh`
Expected: FAIL, with an error like `lib/assert.sh: No such file or directory`, because the harness does not exist yet.

- [ ] **Step 3: Write the assertion library**

Create `test/lib/assert.sh`:

```sh
#!/bin/sh
# Assertion primitives for the Forge-X shell test suite.
#
# Deliberately dependency-free POSIX shell: the suite must run on a fresh
# checkout with no packages installed, and on the printer itself, where
# BusyBox ash is the only shell available.
#
# Source this, call assertions, then call finish.

_T_RUN=0
_T_FAILED=0

_t_pass() {
    _T_RUN=$((_T_RUN + 1))
    echo "ok     $1"
}

_t_fail() {
    _T_RUN=$((_T_RUN + 1))
    _T_FAILED=$((_T_FAILED + 1))
    echo "NOT OK $1"
    [ -n "$2" ] && echo "       $2"
}

fail() {
    _t_fail "$1" "$2"
}

assert_eq() {
    if [ "$3" = "$2" ]; then
        _t_pass "$1"
    else
        _t_fail "$1" "want '$2', got '$3'"
    fi
}

assert_ne() {
    if [ "$3" != "$2" ]; then
        _t_pass "$1"
    else
        _t_fail "$1" "expected something other than '$2'"
    fi
}

assert_empty() {
    if [ -z "$2" ]; then
        _t_pass "$1"
    else
        _t_fail "$1" "expected empty, got '$2'"
    fi
}

assert_contains() {
    case "$2" in
        *"$3"*) _t_pass "$1" ;;
        *)      _t_fail "$1" "'$2' does not contain '$3'" ;;
    esac
}

assert_file() {
    if [ -f "$2" ]; then
        _t_pass "$1"
    else
        _t_fail "$1" "no such file: $2"
    fi
}

finish() {
    echo "--- $_T_RUN assertions, $_T_FAILED failed"
    [ "$_T_FAILED" -eq 0 ] || exit 1
    exit 0
}
```

- [ ] **Step 4: Run the self-test to verify it passes**

Run: `sh test/harness_test.sh`
Expected: six `ok` lines, then `--- 6 assertions, 0 failed`, exit 0.

Verify the exit code explicitly: `sh test/harness_test.sh; echo "exit=$?"` must print `exit=0`.

- [ ] **Step 5: Write the runner**

Create `test/run.sh`:

```sh
#!/bin/sh
# Run the Forge-X shell test suite.
#
# Discovers every test/*_test.sh, runs each in its own shell so one test's
# variables and exit cannot affect another, and reports a summary.
#
# Usage: sh test/run.sh

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

suites=0
failed=0

for t in "$SCRIPT_DIR"/*_test.sh; do
    [ -f "$t" ] || continue
    suites=$((suites + 1))
    echo "=== $(basename "$t")"
    if sh "$t"; then
        :
    else
        failed=$((failed + 1))
    fi
    echo
done

echo "======================================"
if [ "$failed" -eq 0 ]; then
    echo "PASS: $suites suite(s)"
    exit 0
fi
echo "FAIL: $failed of $suites suite(s)"
exit 1
```

- [ ] **Step 6: Verify the runner finds and passes the self-test**

Run: `sh test/run.sh; echo "exit=$?"`
Expected: the `harness_test.sh` section runs, then `PASS: 1 suite(s)` and `exit=0`.

- [ ] **Step 7: Commit**

```bash
git add test/lib/assert.sh test/run.sh test/harness_test.sh
git commit -m "Add a dependency-free shell test harness

POSIX assertion primitives and a runner that discovers test/*_test.sh.
No packages required, so the suite runs on a fresh checkout, in CI, and
on the printer over SSH where BusyBox ash is the only shell."
```

---

### Task 2: Syntax gate

Every shipped script must parse under the interpreter its shebang declares. This catches the single most common way a shell change breaks a printer: a bashism in a `#!/bin/sh` file, which parses fine on a dev box where `/bin/sh` is often bash, and fails on BusyBox ash at boot.

**Files:**
- Create: `test/syntax_test.sh`

**Interfaces:**
- Consumes: `assert.sh` from Task 1.
- Produces: nothing.

- [ ] **Step 1: Establish the pre-existing failure baseline**

Before writing the gate, find out which files already fail, so the gate can exempt them rather than turning CI red on arrival.

Run from the repo root:

```bash
for f in $(git ls-files '.shell/*' '.root/*' '.py/*'); do
    head -1 "$f" 2>/dev/null | grep -q '^#!' || continue
    if head -1 "$f" | grep -q bash; then
        bash -n "$f" 2>/dev/null || echo "BASH-FAIL $f"
    elif head -1 "$f" | grep -q 'sh$\|sh '; then
        sh -n "$f" 2>/dev/null || echo "SH-FAIL $f"
    fi
done
```

Record every reported path. From analysis on 2026-08-24, `.root/stop.sh` declares `#!/bin/sh` but contains a bashism and fails; expect at least that one. Do not fix these here. Fixing a pre-existing bug inside a test-infrastructure PR makes the PR harder to review and mixes two concerns.

- [ ] **Step 2: Write the gate**

Create `test/syntax_test.sh`, putting the paths you recorded in step 1 into `KNOWN_BAD`:

```sh
#!/bin/sh
# Every shipped script must parse under the interpreter its shebang declares.
#
# This is the gate that catches a bashism landing in a #!/bin/sh file. Such a
# file parses fine on a dev machine, where /bin/sh is frequently bash, and then
# fails on the printer, where it is BusyBox ash.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

# Scripts that already fail on upstream main. Fixing them is a separate
# concern from adding this gate; the gate exists to stop NEW breakage.
KNOWN_BAD=".root/stop.sh"

is_known_bad() {
    for k in $KNOWN_BAD; do
        [ "$1" = "$k" ] && return 0
    done
    return 1
}

for f in $(git ls-files '.shell/*' '.root/*' '.py/*'); do
    [ -f "$f" ] || continue
    head -1 "$f" 2>/dev/null | grep -q '^#!' || continue
    is_known_bad "$f" && continue

    if head -1 "$f" | grep -q bash; then
        if bash -n "$f" 2>/dev/null; then
            _t_pass "parses (bash): $f"
        else
            _t_fail "parses (bash): $f" "$(bash -n "$f" 2>&1 | head -3)"
        fi
    elif head -1 "$f" | grep -qE 'sh$|sh '; then
        if sh -n "$f" 2>/dev/null; then
            _t_pass "parses (sh): $f"
        else
            _t_fail "parses (sh): $f" "$(sh -n "$f" 2>&1 | head -3)"
        fi
    fi
done

finish
```

- [ ] **Step 3: Run the gate and confirm it is green**

Run: `sh test/syntax_test.sh; echo "exit=$?"`
Expected: a run of `ok parses (...)` lines and `exit=0`.

If it is red, the failing path belongs in `KNOWN_BAD` (it is pre-existing) or you introduced it. Check with `git stash`-free comparison: `git show origin/main:<path> > /tmp/orig && sh -n /tmp/orig`. If the original also fails, add it to `KNOWN_BAD`.

- [ ] **Step 4: Verify the gate actually catches breakage**

A gate that cannot fail is worthless. Prove it detects a real problem:

```bash
printf '#!/bin/sh\nfoo() {\n' > /tmp/broken_test_file.sh
cp /tmp/broken_test_file.sh .shell/zz_deliberately_broken.sh
git add -N .shell/zz_deliberately_broken.sh
sh test/syntax_test.sh; echo "exit=$?"
```

Expected: `NOT OK parses (sh): .shell/zz_deliberately_broken.sh` and `exit=1`.

Then remove it and confirm green again:

```bash
git rm -f --cached .shell/zz_deliberately_broken.sh
rm -f .shell/zz_deliberately_broken.sh
sh test/syntax_test.sh; echo "exit=$?"
```

Expected: `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add test/syntax_test.sh
git commit -m "Add a syntax gate for shipped scripts

Each script is parsed with the interpreter its shebang declares, which
catches a bashism in a #!/bin/sh file. Those parse on a dev box, where
/bin/sh is often bash, and fail on the printer's BusyBox ash at boot.

Scripts already failing on main are listed in KNOWN_BAD so the gate
blocks new breakage rather than arriving red."
```

---

### Task 3: Display mode symmetry gate

This gate encodes a real bug. Each `.cfg/init.display.<mode>.cfg` selects one display config and removes the others with `-[include ...]` lines. Adding a mode means every other mode must learn to exclude it. Miss one and both configs stay included, and since each defines `_PRINT_STATUS` and `reset_screen`, Klipper sees duplicates. Nothing catches this today.

**Files:**
- Create: `test/display_modes_test.sh`

**Interfaces:**
- Consumes: `assert.sh` from Task 1.
- Produces: nothing.

- [ ] **Step 1: Write the gate**

Create `test/display_modes_test.sh`:

```sh
#!/bin/sh
# Display mode configs must be mutually exclusive.
#
# Each .cfg/init.display.<mode>.cfg includes its own config/<mode>.cfg and
# removes every other mode's with a -[include ...] line. Adding a mode without
# teaching the others to exclude it leaves two display configs active at once,
# and both define _PRINT_STATUS and reset_screen.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

# Derive the mode list from the files themselves so a new mode is covered
# automatically rather than needing this test updated.
MODES=""
for f in .cfg/init.display.*.cfg; do
    [ -f "$f" ] || continue
    m=$(basename "$f" .cfg)
    m=${m#init.display.}
    MODES="$MODES $m"
done

assert_ne "at least one display mode found" "" "$MODES"

for mode in $MODES; do
    f=".cfg/init.display.$mode.cfg"

    # It must include its own config.
    if grep -q "^\[include \./mod/config/$mode\.cfg\]" "$f"; then
        _t_pass "$mode includes its own config"
    else
        _t_fail "$mode includes its own config" "no [include ./mod/config/$mode.cfg] in $f"
    fi

    # It must exclude every other mode's config.
    for other in $MODES; do
        [ "$other" = "$mode" ] && continue
        if grep -q -- "^-\[include \./mod/config/$other\.cfg\]" "$f"; then
            _t_pass "$mode excludes $other"
        else
            _t_fail "$mode excludes $other" "missing -[include ./mod/config/$other.cfg] in $f"
        fi
    done
done

finish
```

- [ ] **Step 2: Run it against the current tree**

Run: `sh test/display_modes_test.sh; echo "exit=$?"`

Expected on upstream `main`: **green**, because main has four modes (`stock`, `feather`, `guppy`, `headless`) and they are mutually consistent.

Note: if run on a branch that has added `helix.cfg` without updating the other four, this gate goes red, which is the point. That is the exact defect it exists to catch.

- [ ] **Step 3: Prove it catches the real bug**

Reproduce the defect deliberately and confirm the gate fires:

```bash
cp .cfg/init.display.stock.cfg /tmp/stock.bak
grep -v 'config/guppy.cfg' /tmp/stock.bak > .cfg/init.display.stock.cfg
sh test/display_modes_test.sh; echo "exit=$?"
```

Expected: `NOT OK stock excludes guppy` and `exit=1`.

Restore and confirm green:

```bash
cp /tmp/stock.bak .cfg/init.display.stock.cfg
sh test/display_modes_test.sh; echo "exit=$?"
```

Expected: `exit=0`.

- [ ] **Step 4: Commit**

```bash
git add test/display_modes_test.sh
git commit -m "Add a display mode symmetry gate

Each init.display.<mode>.cfg must include its own config and exclude every
other mode's. Adding a mode without updating the others leaves two display
configs active, and both define _PRINT_STATUS and reset_screen.

The mode list is derived from the files, so a new mode is covered without
editing this test."
```

---

### Task 4: shellcheck with a baseline

**Files:**
- Create: `test/shellcheck_baseline.txt`
- Create: `test/shellcheck_test.sh`

**Interfaces:**
- Consumes: `assert.sh` from Task 1.
- Produces: nothing.

- [ ] **Step 1: Confirm shellcheck is available and record the baseline**

Run: `shellcheck --version`

If absent, install it (`apt-get install shellcheck` on Debian/Ubuntu). It is a dev and CI dependency only, never needed on the printer.

Generate the baseline as stable `file:code` pairs, deliberately excluding line numbers so that unrelated edits shifting lines do not invalidate it:

```bash
cd "$(git rev-parse --show-toplevel)"
for f in $(git ls-files '.shell/*' '.root/*'); do
    head -1 "$f" 2>/dev/null | grep -q '^#!' || continue
    shellcheck -f gcc "$f" 2>/dev/null
done | sed -E 's/^([^:]+):[0-9]+:[0-9]+: [a-z]+: .*\[(SC[0-9]+)\]$/\1:\2/' \
     | sort -u > test/shellcheck_baseline.txt
wc -l test/shellcheck_baseline.txt
```

Expected: a non-empty file. Forge-X has never run shellcheck, so a large baseline is normal and is not a problem to solve in this PR.

- [ ] **Step 2: Write the gate**

Create `test/shellcheck_test.sh`:

```sh
#!/bin/sh
# shellcheck findings gate.
#
# Forge-X has never run shellcheck, so there is a large body of pre-existing
# findings. Failing on all of them would make this unmergeable, so the gate
# compares against a recorded baseline and fails only on findings that are new.
#
# The baseline stores file:code pairs, not line numbers, so that unrelated
# edits which shift lines do not produce spurious failures.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

cd "$REPO_DIR" || exit 1

if ! command -v shellcheck >/dev/null 2>&1; then
    echo "ok     shellcheck not installed, skipping"
    finish
fi

BASELINE="$SCRIPT_DIR/shellcheck_baseline.txt"
assert_file "baseline exists" "$BASELINE"

CURRENT=$(mktemp)
for f in $(git ls-files '.shell/*' '.root/*'); do
    head -1 "$f" 2>/dev/null | grep -q '^#!' || continue
    shellcheck -f gcc "$f" 2>/dev/null
done | sed -E 's/^([^:]+):[0-9]+:[0-9]+: [a-z]+: .*\[(SC[0-9]+)\]$/\1:\2/' \
     | sort -u > "$CURRENT"

NEW=$(comm -13 "$BASELINE" "$CURRENT")
rm -f "$CURRENT"

if [ -z "$NEW" ]; then
    _t_pass "no new shellcheck findings"
else
    _t_fail "no new shellcheck findings" "$(echo "$NEW" | head -10)"
    echo "       (regenerate the baseline only if these are intentional)"
fi

finish
```

- [ ] **Step 3: Verify it is green against the recorded baseline**

Run: `sh test/shellcheck_test.sh; echo "exit=$?"`
Expected: `ok no new shellcheck findings` and `exit=0`.

- [ ] **Step 4: Prove it catches a new finding**

```bash
printf '#!/bin/sh\nfoo=1\necho $foo\ncd /tmp\n' > .shell/zz_sc_probe.sh
git add -N .shell/zz_sc_probe.sh
sh test/shellcheck_test.sh; echo "exit=$?"
```

Expected: `NOT OK no new shellcheck findings`, listing `.shell/zz_sc_probe.sh:SC2164` (unchecked `cd`) or similar, and `exit=1`.

Clean up and confirm green:

```bash
git rm -f --cached .shell/zz_sc_probe.sh
rm -f .shell/zz_sc_probe.sh
sh test/shellcheck_test.sh; echo "exit=$?"
```

Expected: `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add test/shellcheck_baseline.txt test/shellcheck_test.sh
git commit -m "Add shellcheck gate with a recorded baseline

Forge-X has never run shellcheck, so there is a large body of existing
findings. The gate records those as file:code pairs and fails only on new
ones, so it blocks regressions without demanding a tree-wide cleanup first.

Line numbers are deliberately excluded from the baseline so unrelated
edits that shift lines do not cause spurious failures."
```

---

### Task 5: Python tests for klippy plugins

Forge-X ships six klippy plugins and eleven patched Klipper modules, none tested. `mod_params` is the highest-value target: it is the settings authority the whole mod reads through, and its parsing and persistence are pure logic with no hardware dependency.

**Files:**
- Create: `test/python/conftest.py`
- Create: `test/python/test_mod_params.py`

**Interfaces:**
- Consumes: `.py/klipper/plugins/mod_params.py`.
- Produces: pytest fixtures `stub_config` and `declaration_file`, usable by future plugin tests.

- [ ] **Step 1: Write the stub Klipper environment**

Klipper's plugin contract is small enough to stub. A plugin receives a `config` object and calls `config.get_printer()`, `config.get(...)`, and `printer.lookup_object("gcode")`.

Create `test/python/conftest.py`:

```python
"""Stub Klipper objects so klippy plugins can be unit tested off-printer.

A Klipper extra receives a config object, pulls the printer from it, looks up
the gcode object, and registers commands. None of that needs hardware, so a
small stub is enough to exercise a plugin's real logic.
"""

import json
import sys
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[2] / ".py" / "klipper" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))


class StubGCode:
    def __init__(self):
        self.commands = {}
        self.responses = []
        self.scripts = []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def respond_info(self, msg):
        self.responses.append(msg)

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def error(self, msg):
        return RuntimeError(msg)


class StubReactor:
    def monotonic(self):
        return 0.0


class StubPrinter:
    def __init__(self):
        self.objects = {"gcode": StubGCode()}
        self.event_handlers = {}

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def load_object(self, config, name):
        return self.objects.setdefault(name, StubGCodeMacro())

    def get_reactor(self):
        return StubReactor()

    def register_event_handler(self, event, handler):
        self.event_handlers.setdefault(event, []).append(handler)


class StubGCodeMacro:
    def load_template(self, config, name):
        return None


class StubConfig:
    """Mimics Klipper's ConfigWrapper for the subset plugins actually use."""

    def __init__(self, values, printer=None):
        self._values = values
        self._printer = printer or StubPrinter()

    def get_printer(self):
        return self._printer

    def get(self, key, default=object()):
        if key in self._values:
            return self._values[key]
        if isinstance(default, object) and default.__class__ is object:
            raise KeyError("missing required config option: %s" % key)
        return default

    def getint(self, key, default=None, minval=None, maxval=None):
        return int(self._values.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self._values.get(key, default))

    def error(self, msg):
        return RuntimeError(msg)


@pytest.fixture
def declaration_file(tmp_path):
    """A minimal mod_params declaration covering a scalar and an enum."""
    decl = {
        "enums": {
            "DisplayEnum": {
                "type": "int",
                "values": {"STOCK": 0, "GUPPY": 3},
            }
        },
        "parameters": [
            {
                "key": "backlight",
                "type": "int",
                "default": 50,
                "label": "Backlight",
            },
            {
                "key": "display",
                "type": "DisplayEnum",
                "default": "STOCK",
                "label": "Display",
                "options": {"STOCK": "Stock screen", "GUPPY": "Guppy screen"},
            },
        ],
    }
    path = tmp_path / "mod_params.json"
    path.write_text(json.dumps(decl))
    return path


@pytest.fixture
def stub_config(tmp_path, declaration_file):
    variables = tmp_path / "variables.cfg"
    variables.write_text("")
    return StubConfig(
        {
            "declaration": str(declaration_file),
            "filename": str(variables),
        }
    )
```

- [ ] **Step 2: Write the failing tests**

Create `test/python/test_mod_params.py`:

```python
"""Unit tests for the mod_params klippy plugin.

mod_params is the settings authority the rest of the mod reads through, so its
defaults, typing, and persistence are worth pinning down.
"""

import mod_params


def test_defaults_are_applied_when_storage_is_empty(stub_config):
    mgr = mod_params.ModParamManagement(stub_config)
    status = mgr.get_status(None)
    assert status["variables"]["backlight"] == 50


def test_enum_default_resolves_to_its_name(stub_config):
    mgr = mod_params.ModParamManagement(stub_config)
    status = mgr.get_status(None)
    assert status["variables"]["display"] == 0


def test_registers_its_gcode_commands(stub_config):
    mod_params.ModParamManagement(stub_config)
    commands = stub_config.get_printer().lookup_object("gcode").commands
    for name in ("LIST_MOD_PARAMS", "GET_MOD_PARAM", "SET_MOD_PARAM", "SET_MOD"):
        assert name in commands


def test_load_config_returns_a_manager(stub_config):
    assert isinstance(mod_params.load_config(stub_config),
                      mod_params.ModParamManagement)
```

- [ ] **Step 3: Run them to see where the stub is wrong**

Run: `python3 -m pytest test/python/ -v`

Expected on the first run: some tests fail, because the stub almost certainly does not match Klipper's contract exactly on the first attempt. Read each failure and fix `conftest.py`, not the assertions. The assertions describe what `mod_params` genuinely does; the stub is what is approximate.

If a test reveals that `mod_params` needs a config option the stub does not provide, add it to the `stub_config` fixture's dict. If it needs a printer object the stub lacks, add it to `StubPrinter.objects`.

- [ ] **Step 4: Iterate until green**

Run: `python3 -m pytest test/python/ -v`
Expected: 4 passed.

- [ ] **Step 5: Verify the tests can actually fail**

A passing test that cannot fail proves nothing. Mutate the declaration and confirm a test catches it:

```bash
python3 - <<'EOF'
import json, pathlib
# Temporarily change the default so test_defaults_are_applied should fail.
print("Run pytest after editing conftest's declaration default from 50 to 51")
EOF
```

Edit `test/python/conftest.py`, change the `backlight` default from `50` to `51`, then run:

Run: `python3 -m pytest test/python/test_mod_params.py::test_defaults_are_applied_when_storage_is_empty -v`
Expected: FAIL, asserting 51 != 50.

Change it back to `50` and confirm green again.

- [ ] **Step 6: Commit**

```bash
git add test/python/conftest.py test/python/test_mod_params.py
git commit -m "Add Python unit tests for the mod_params plugin

Stubs the small part of Klipper's plugin contract that extras actually use
(config, printer, gcode) so plugins can be tested without hardware.

mod_params is the settings authority the rest of the mod reads through, so
its defaults, enum typing, and command registration are pinned down first.
The stub fixtures are reusable for the other five plugins."
```

---

### Task 6: CI and documentation

**Files:**
- Create: `.github/workflows/test.yml`
- Create: `docs/TESTING.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/test.yml`:

```yaml
name: Tests

on:
  push:
    branches: [main]
  pull_request:

jobs:
  shell:
    name: Shell suite
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install shellcheck
        run: sudo apt-get update && sudo apt-get install -y shellcheck

      - name: Run shell test suite
        run: sh test/run.sh

  python:
    name: Python plugins
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install pytest
        run: pip install pytest

      - name: Run plugin tests
        run: python3 -m pytest test/python/ -v
```

- [ ] **Step 2: Verify the workflow parses**

Run: `python3 -c "import yaml, sys; yaml.safe_load(open('.github/workflows/test.yml')); print('valid yaml')"`
Expected: `valid yaml`.

If PyYAML is unavailable, run `pip install pyyaml` first. This is a local check only and adds no repo dependency.

- [ ] **Step 3: Run the full suite exactly as CI will**

```bash
sh test/run.sh; echo "shell exit=$?"
python3 -m pytest test/python/ -v; echo "python exit=$?"
```

Expected: both `exit=0`. If the shell suite is red here but green individually, a test is leaking state; each test must run in its own shell, which `run.sh` already does, so investigate the specific test rather than the runner.

- [ ] **Step 4: Write the documentation**

Create `docs/TESTING.md`:

```markdown
# Testing

Forge-X has two test suites. Neither needs a printer.

## Shell suite

Dependency-free POSIX shell. Runs on a fresh checkout, in CI, and on the
printer itself over SSH.

    sh test/run.sh

It discovers every `test/*_test.sh`. Current gates:

| Test | What it protects |
|---|---|
| `harness_test.sh` | The assertion library itself |
| `syntax_test.sh` | Every script parses under its declared interpreter |
| `display_modes_test.sh` | Display mode configs stay mutually exclusive |
| `shellcheck_test.sh` | No new shellcheck findings versus the baseline |

### Why the syntax gate matters

On most dev machines `/bin/sh` is bash, so a bashism in a `#!/bin/sh` file
parses cleanly and then fails on the printer, where `/bin/sh` is BusyBox ash.
The gate parses each file with the interpreter its shebang actually names.

### The shellcheck baseline

shellcheck had never been run on this tree, so there is a large body of
pre-existing findings. `test/shellcheck_baseline.txt` records them as
`file:code` pairs and the gate fails only on findings absent from it. Line
numbers are excluded deliberately, so unrelated edits that shift lines do not
cause spurious failures.

To regenerate after an intentional change:

    for f in $(git ls-files '.shell/*' '.root/*'); do
        head -1 "$f" | grep -q '^#!' || continue
        shellcheck -f gcc "$f" 2>/dev/null
    done | sed -E 's/^([^:]+):[0-9]+:[0-9]+: [a-z]+: .*\[(SC[0-9]+)\]$/\1:\2/' \
         | sort -u > test/shellcheck_baseline.txt

## Python suite

For the klippy plugins under `.py/klipper/plugins/`.

    pip install pytest
    python3 -m pytest test/python/ -v

`test/python/conftest.py` stubs the small part of Klipper's plugin contract
that extras actually use: a config object, a printer, and a gcode object. That
is enough to exercise a plugin's real logic without hardware. The
`stub_config` and `declaration_file` fixtures are reusable for the other
plugins.

## Adding a test

Shell: create `test/<name>_test.sh`, source `lib/assert.sh`, call assertions,
end with `finish`. The runner picks it up automatically.

    #!/bin/sh
    SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
    . "$SCRIPT_DIR/lib/assert.sh"

    assert_eq "description" "expected" "$actual"

    finish

Available assertions: `assert_eq`, `assert_ne`, `assert_empty`,
`assert_contains`, `assert_file`, and `fail`.

Python: add a file under `test/python/` and use the existing fixtures.
```

- [ ] **Step 5: Verify the docs match the actual test list**

Run: `ls test/*_test.sh | xargs -n1 basename`

Every file listed must appear in the table in `docs/TESTING.md`, and the table must list nothing that does not exist.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/test.yml docs/TESTING.md
git commit -m "Run the test suites in CI and document them

Adds a Tests workflow running the shell suite with shellcheck available
and the Python plugin tests, plus docs covering how to run and extend
both, why the syntax gate uses each file's declared interpreter, and how
to regenerate the shellcheck baseline."
```

---

## Self-Review

**Spec coverage.** This milestone is not in the spec's original numbering. It exists because the spec's upstreaming constraint (section 1) requires each increment to be independently mergeable, and a platform refactor cannot be shown safe without a way to demonstrate the existing platform did not regress. It also directly serves "reduce, not increase, the maintainer's burden": the harness is usable value independent of AD5X, which makes it the cleanest possible first substantive contribution.

**Placeholder scan.** No TBD or "add appropriate handling" steps. Every file is given in full. Task 5 step 3 deliberately expects failure and tells the implementer to fix the stub rather than the assertions, which is instruction rather than a placeholder.

**Type consistency.** `assert.sh` defines six assertions plus `finish` and the internal `_t_pass` / `_t_fail`. Tasks 2, 3 and 4 use `_t_pass` and `_t_fail` directly for per-file loops where the assertion name is computed; that is intentional and those functions are defined in Task 1. Python fixtures `stub_config` and `declaration_file` are defined in Task 5's `conftest.py` and used by name in the same task's tests.

**Ordering constraint for the executor.** Task 1 must land before all others, since every test sources `lib/assert.sh`. Tasks 2 through 5 are independent of each other. Task 6 must be last, because `docs/TESTING.md` lists the tests that exist and CI runs them.

**Deliberate omission.** No test covers the eleven patched Klipper modules under `.py/klipper/patches/`. Those are full-file replacements of upstream Klipper, and meaningfully testing them needs a real Klipper import environment, which is a much larger undertaking than this milestone. `mod_params` was chosen instead because it is genuinely self-contained.
