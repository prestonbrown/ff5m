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
