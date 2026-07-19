"""Centralized terminal protocol detection.

Two TTY/ANSI workarounds are collected here so no single module has to know
the details:

- ``textual_image`` blocks on import while it probes the terminal for sixel
  and kitty-graphics support. On some terminals (and over ssh/tmux) that probe
  hangs for seconds — or forever — at startup. We pre-import the sixel/tgp
  submodules and force their ``query_terminal_support`` to ``False`` so the
  probe never runs, while leaving ``is_tty=True`` so rendering still works.

- Ghostty and Kitty both speak the kitty graphics protocol, but
  ``textual_image``'s auto-detection doesn't always pick it. We force
  ``NAVITUI_ART=tgp`` on those terminals unless the user set the variable
  explicitly.

``probe()`` runs once at import time so any consumer (``art.py``, ``app.py``,
tests) gets the patching for free without having to call it. Idempotent.
"""

from __future__ import annotations

import os

_PROBED = False


def is_kitty_compatible() -> bool:
    """Return True if the running terminal speaks the kitty graphics protocol
    (Ghostty or Kitty)."""
    term = os.environ.get("TERM_PROGRAM", "").lower()
    if term in ("ghostty", "kitty"):
        return True
    return os.environ.get("TERM") == "xterm-kitty"


def current_protocol() -> str:
    """Return the resolved ``NAVITUI_ART`` value (``auto`` if unset)."""
    return os.environ.get("NAVITUI_ART", "auto").lower()


def _force_protocol_for_terminal() -> None:
    """On Ghostty/Kitty, force ``NAVITUI_ART=tgp`` unless the user set it."""
    if "NAVITUI_ART" in os.environ:
        return
    if is_kitty_compatible():
        os.environ["NAVITUI_ART"] = "tgp"


def _disable_textual_image_tty_queries() -> None:
    """Pre-import sixel/tgp and force their blocking TTY probes to ``False``.

    We pre-import the submodules so the patch lands before
    ``textual_image.widget`` reads them at import time. Wrapped defensively:
    a missing package or a changed module layout must never break startup —
    ``art.py`` falls back to its own try/except.
    """
    try:
        import textual_image.renderable.sixel as _sixel
        import textual_image.renderable.tgp as _tgp

        _sixel.query_terminal_support = lambda: False
        _tgp.query_terminal_support = lambda: False
    except Exception:
        pass


def probe() -> None:
    """Run terminal detection and patching. Idempotent and safe to re-call."""
    global _PROBED
    if _PROBED:
        return
    _PROBED = True
    _force_protocol_for_terminal()
    _disable_textual_image_tty_queries()


# Auto-run on import so callers don't have to remember to call probe().
probe()
