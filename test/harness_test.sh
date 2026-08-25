#!/bin/sh
# Self-test for the assertion harness.
#
# The thing that actually matters here is the negative case: an assertion that
# cannot fail makes every test that uses it worthless. Each assertion is checked
# for both outcomes.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
. "$SCRIPT_DIR/lib/assert.sh"

# Run an assertion in a nested shell and echo how many failures it recorded.
# Nested so the deliberate failures land in that shell's counter, not ours.
failures_from() {
    (
        . "$SCRIPT_DIR/lib/assert.sh"
        "$@" >/dev/null 2>&1
        echo "$_T_FAILED"
    )
}

# Each assertion must PASS on its true case...
assert_eq "eq passes when equal"     "0" "$(failures_from assert_eq       x "abc" "abc")"
assert_eq "ne passes when different" "0" "$(failures_from assert_ne       x "abc" "xyz")"
assert_eq "empty passes when empty"  "0" "$(failures_from assert_empty    x "")"
assert_eq "contains passes on match" "0" "$(failures_from assert_contains x "hay needle" "needle")"
assert_eq "file passes when present" "0" "$(failures_from assert_file     x "$SCRIPT_DIR/harness_test.sh")"

# ...and FAIL on its false case. This half is the point of the file.
assert_eq "eq fails when different"  "1" "$(failures_from assert_eq       x "abc" "xyz")"
assert_eq "ne fails when equal"      "1" "$(failures_from assert_ne       x "abc" "abc")"
assert_eq "empty fails when set"     "1" "$(failures_from assert_empty    x "nonempty")"
assert_eq "contains fails on miss"   "1" "$(failures_from assert_contains x "hay" "needle")"
assert_eq "file fails when absent"   "1" "$(failures_from assert_file     x "/nonexistent/path")"

# finish must exit non-zero when anything failed, or CI reports green on red.
(
    . "$SCRIPT_DIR/lib/assert.sh"
    assert_eq "deliberate" "a" "b" >/dev/null 2>&1
    finish >/dev/null 2>&1
)
assert_eq "finish exits non-zero after a failure" "1" "$?"

finish
