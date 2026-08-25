# Testing

Forge-X has two test suites. Neither needs a printer.

## Shell suite

Dependency-free POSIX shell. Runs on a fresh checkout, in CI, and on the
printer itself over SSH.

    sh test/run.sh

It discovers every `test/*_test.sh`. Current gates:

| Test | What it protects |
|---|---|
| `display_modes_test.sh` | Display mode configs stay mutually exclusive |
| `harness_test.sh` | The assertion library itself |
| `shellcheck_test.sh` | No new shellcheck findings versus the baseline |
| `syntax_test.sh` | Every script parses under its declared interpreter |

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

shellcheck is a dev and CI dependency only; it is never needed on the printer.
Where it is not installed the gate reports a skip rather than failing.

To regenerate after an intentional change:

    for f in $(git ls-files '.shell/*' '.root/*'); do
        head -1 "$f" | grep -q '^#!' || continue
        shellcheck -f gcc "$f" 2>/dev/null
    done | sed -E 's/^([^:]+):[0-9]+:[0-9]+: [a-z]+: .*\[(SC[0-9]+)\]$/\1:\2/' \
         | LC_ALL=C sort -u > test/shellcheck_baseline.txt

`LC_ALL=C` is required. Without it a baseline generated under a UTF-8 locale and
a list generated under C collate differently, and `comm` silently reports
phantom findings.

## Python suite

For the klippy plugins under `.py/klipper/plugins/`.

    pip install pytest
    python3 -m pytest test/python/ -v

`test/python/conftest.py` stubs the small part of Klipper's plugin contract
that extras actually use: a config object, a printer, and a gcode object. That
is enough to exercise a plugin's real logic without hardware. The
`stub_config` and `declaration_file` fixtures are reusable for the other
plugins.

CI runs this suite on Python 3.8, because that is what klippy runs on the
printer. Code that only parses on a newer interpreter would pass locally and
fail on the machine that matters.

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
