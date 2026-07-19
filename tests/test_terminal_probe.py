"""Stdlib-only tests for terminal-protocol detection (terminal_probe.py).

The repo ships no test framework, so this is a self-contained runner:
`python tests/test_terminal_probe.py`. It exercises the pure env-var logic
(kitty/ghostty detection, NAVITUI_ART forcing, respecting an explicit choice)
and the idempotent auto-run, saving and restoring os.environ around each case
so it never leaks state into the process it runs in.
"""

from __future__ import annotations

import os

from navitui import terminal_probe as tp

# keys the probe reads or writes — snapshot and restore these around each case
_KEYS = ("NAVITUI_ART", "TERM_PROGRAM", "TERM")


def _with_env(**env):
    """Run against a controlled environment: keys given are set, the rest of
    the probe's keys are cleared. Returns a restore() callable."""
    saved = {k: os.environ.get(k) for k in _KEYS}

    def restore() -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    for k in _KEYS:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    return restore


def check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(f"FAILED: {name}")
    print(f"  ok: {name}")


def test_kitty_detection() -> None:
    for term_program in ("ghostty", "kitty", "Ghostty", "KITTY"):
        restore = _with_env(TERM_PROGRAM=term_program)
        try:
            check(f"TERM_PROGRAM={term_program} is kitty-compatible", tp.is_kitty_compatible())
        finally:
            restore()

    restore = _with_env(TERM="xterm-kitty")
    try:
        check("TERM=xterm-kitty is kitty-compatible", tp.is_kitty_compatible())
    finally:
        restore()

    for other in ("iTerm.app", "Apple_Terminal", "vscode"):
        restore = _with_env(TERM_PROGRAM=other)
        try:
            check(f"TERM_PROGRAM={other} is NOT kitty-compatible", not tp.is_kitty_compatible())
        finally:
            restore()

    restore = _with_env()  # nothing set
    try:
        check("bare env is NOT kitty-compatible", not tp.is_kitty_compatible())
    finally:
        restore()


def test_force_protocol_on_kitty() -> None:
    restore = _with_env(TERM_PROGRAM="ghostty")
    try:
        tp._force_protocol_for_terminal()
        check("ghostty forces NAVITUI_ART=tgp", os.environ.get("NAVITUI_ART") == "tgp")
    finally:
        restore()

    restore = _with_env(TERM="xterm-kitty")
    try:
        tp._force_protocol_for_terminal()
        check("xterm-kitty forces NAVITUI_ART=tgp", os.environ.get("NAVITUI_ART") == "tgp")
    finally:
        restore()


def test_respects_explicit_choice() -> None:
    restore = _with_env(TERM_PROGRAM="ghostty", NAVITUI_ART="sixel")
    try:
        tp._force_protocol_for_terminal()
        check("explicit NAVITUI_ART is not overridden", os.environ.get("NAVITUI_ART") == "sixel")
    finally:
        restore()


def test_no_force_on_plain_terminal() -> None:
    restore = _with_env(TERM_PROGRAM="Apple_Terminal")
    try:
        tp._force_protocol_for_terminal()
        check("plain terminal leaves NAVITUI_ART unset", "NAVITUI_ART" not in os.environ)
    finally:
        restore()


def test_current_protocol_default() -> None:
    restore = _with_env()
    try:
        check("current_protocol defaults to 'auto'", tp.current_protocol() == "auto")
    finally:
        restore()

    restore = _with_env(NAVITUI_ART="SIXEL")
    try:
        check("current_protocol lowercases", tp.current_protocol() == "sixel")
    finally:
        restore()


def test_probe_idempotent_and_ran_on_import() -> None:
    # importing the module already ran probe() once
    check("probe auto-ran on import (_PROBED is True)", tp._PROBED is True)
    # calling again is a no-op and must not raise
    tp.probe()
    check("probe() is idempotent", tp._PROBED is True)


def main() -> None:
    print("test_terminal_probe:")
    test_kitty_detection()
    test_force_protocol_on_kitty()
    test_respects_explicit_choice()
    test_no_force_on_plain_terminal()
    test_current_protocol_default()
    test_probe_idempotent_and_ran_on_import()
    print("all terminal_probe tests passed")


if __name__ == "__main__":
    main()
