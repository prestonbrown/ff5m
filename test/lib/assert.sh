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
