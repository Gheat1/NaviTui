"""Album-art-derived live theming — tint the chrome with the cover's color.

When the current song's art loads we pull one vibrant color out of it and
nudge the kit's accent roles (`blue`/`lav`/`mauve`) toward it, so borders,
the progress bar and the now-playing line quietly take on the album's hue.
The tint is a thin layer over `ricekit.palette`: roles are still read at
render time, so a theme switch (which rebuilds the palette) wins — the app
just re-applies whatever tint is active afterwards.

Everything degrades off:

- no truecolor (`system`/ANSI theme) → `anim.can_blend()` is False, so we
  never touch the palette and the terminal's own colors stay untouched
- no art / extraction failure → the tint clears and the theme is untinted
- Pillow missing → extraction returns None; the feature is simply inert

Extraction runs off the already-cached local art file and is cheap (a
32px thumbnail), but callers still keep it off the UI's critical path.
"""

from __future__ import annotations

from pathlib import Path

from ricekit import palette

from navitui import anim

# roles nudged toward the album color, and how far (0..1) each moves. The
# progress bar reads blue+lav; borders/markers read blue; accents read mauve.
_TINTED_ROLES = {"blue": 0.55, "lav": 0.5, "mauve": 0.45}

# the currently-applied tint (a hex string) and the untinted base colors we
# blended away from, so a re-apply after a theme swap starts from clean roles.
_tint: str | None = None
_base: dict[str, str] = {}


def extract_vibrant(path: Path, thumb: int = 32) -> str | None:
    """Pull one vibrant dominant color out of the cover as `#rrggbb`.

    Downscales hard (a ~32px thumbnail is plenty for a single color) and
    scores pixels by saturation × mid-weighted lightness so we favour a
    lively hue over the near-black/near-white that dominates most covers.
    Any failure — Pillow absent, unreadable file — returns None.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((thumb, thumb))
            pixels = list(im.getdata())
    except Exception:
        return None
    if not pixels:
        return None

    best_score = -1.0
    best = None
    fallback = None  # brightest pixel, if nothing scores as vibrant
    fallback_lum = -1.0
    for r, g, b in pixels:
        mx, mn = max(r, g, b), min(r, g, b)
        lum = mx + mn  # 0..510, cheap "lightness"
        sat = 0 if mx == 0 else (mx - mn) / mx
        # mid-lightness weight: peaks around 50% grey, falls off at the ends
        light = 1.0 - abs((lum / 510.0) - 0.5) * 2.0
        score = sat * (0.35 + 0.65 * light)
        if score > best_score:
            best_score, best = score, (r, g, b)
        if lum > fallback_lum:
            fallback_lum, fallback = lum, (r, g, b)

    r, g, b = best if best_score > 0.12 else (fallback or (0, 0, 0))
    return f"#{r:02x}{g:02x}{b:02x}"


def _apply() -> None:
    """Blend the current tint onto the current (untinted) palette roles.

    Reads the live role values as the base, so it re-derives correctly
    after a theme swap rebuilt the palette. No-op when blending is off.
    """
    global _base
    if _tint is None or not anim.can_blend():
        return
    _base = {role: getattr(palette, role) for role in _TINTED_ROLES}
    for role, amount in _TINTED_ROLES.items():
        setattr(palette, role, anim.blend(_base[role], _tint, amount))


def _restore() -> None:
    for role, value in _base.items():
        setattr(palette, role, value)
    _base.clear()


def set_tint(color: str | None) -> None:
    """Switch to `color` (a `#rrggbb` hex), or clear the tint with None.

    Idempotent per color. Safe under any theme: under `system`/ANSI it just
    records the intent and touches nothing (there's nothing to blend)."""
    global _tint
    if color == _tint:
        return
    _restore()
    _tint = color
    _apply()


def reapply() -> None:
    """Re-assert the active tint after a theme change rebuilt the palette.

    Call this from `on_kit_theme_changed`: the base roles are already fresh
    (the palette was just rebuilt), so we blend straight onto them."""
    _base.clear()
    _apply()
