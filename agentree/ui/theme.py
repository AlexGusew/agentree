"""Palette, styles and SGR encoding (truecolor with xterm-256 fallback).

A style is a plain tuple (fg_hex, bg_hex, flags) so it hashes fast and compares cheaply.
"""
from __future__ import annotations

import os
import re

BOLD, UNDERLINE, ITALIC, STRIKE = 1, 2, 4, 8
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

GROUND = "#14171d"
BAR = "#171b22"
BORDER = "#262c36"
SELECTED = "#222936"
TEXT = "#d5d9e0"
DIM = "#8d96a5"
ACCENT = "#e5b454"
TEAL = "#5ec4b6"
GREEN = "#9ccc65"
RED = "#e88b8b"
BLUE = "#7fb2e5"

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def truecolor_supported(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (round(r), round(g), round(b))


def blend(a: str, b: str, t: float) -> str:
    """Mix colour a toward b by t (0 = a, 1 = b)."""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


_CUBE = (0, 95, 135, 175, 215, 255)


def to256(h: str) -> int:
    """Nearest xterm-256 index (colour cube or grey ramp)."""
    r, g, b = _rgb(h)

    def near(v: int) -> int:
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))

    ci = (near(r), near(g), near(b))
    cr, cg, cb = (_CUBE[i] for i in ci)
    cube_d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    avg = (r + g + b) // 3
    gi = max(0, min(23, round((avg - 8) / 10)))
    gv = 8 + gi * 10
    grey_d = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    if grey_d < cube_d:
        return 232 + gi
    return 16 + 36 * ci[0] + 6 * ci[1] + ci[2]


class Theme:
    """Role colours + a style cache. `st(role, bg)` returns a style tuple."""

    def __init__(self, accent: str = ACCENT, truecolor: bool | None = None) -> None:
        accent = accent if isinstance(accent, str) and _HEX.match(accent) else ACCENT
        self.truecolor = truecolor_supported() if truecolor is None else truecolor
        self.colors = {
            "ground": GROUND, "bar": BAR, "border": BORDER, "selected": SELECTED,
            "text": TEXT, "dim": DIM, "accent": accent, "warn": accent,
            "teal": TEAL, "green": GREEN, "red": RED, "blue": BLUE,
        }
        # derived tones (not in the brief's table, all mixed from it)
        self.colors["rule"] = blend(BORDER, DIM, 0.35)          # brackets, scrollbar track
        self.colors["chip"] = blend(BAR, accent, 0.16)           # active filter chip fill
        self.colors["key"] = blend(GROUND, BORDER, 0.55)         # key-cap fill in action chips
        self.colors["modal"] = BAR
        self.colors["danger"] = blend(BAR, RED, 0.14)            # danger modal title fill
        self.colors["input"] = blend(GROUND, BAR, 0.5)           # text input well
        self._sgr: dict[tuple, str] = {}
        self._faded: dict[tuple, tuple] = {}

    def c(self, role: str) -> str:
        return self.colors.get(role) or self.colors["text"]

    def st(self, role: str = "text", bg: str = "ground", flags: int = 0) -> tuple:
        return (self.c(role), self.c(bg), flags)

    def fade(self, style: tuple) -> tuple:
        """Backdrop version of a style (used behind modals)."""
        f = self._faded.get(style)
        if f is None:
            fg, bg, _ = style
            f = (blend(fg, GROUND, 0.62), blend(bg, GROUND, 0.5), 0)
            self._faded[style] = f
        return f

    def sgr(self, style: tuple) -> str:
        s = self._sgr.get(style)
        if s is None:
            fg, bg, flags = style
            parts = ["0"]
            if flags & BOLD:
                parts.append("1")
            if flags & ITALIC:
                parts.append("3")
            if flags & UNDERLINE:
                parts.append("4")
            if flags & STRIKE:
                parts.append("9")
            if self.truecolor:
                parts.append("38;2;%d;%d;%d" % _rgb(fg))
                parts.append("48;2;%d;%d;%d" % _rgb(bg))
            else:
                parts.append("38;5;%d" % to256(fg))
                parts.append("48;5;%d" % to256(bg))
            s = "\x1b[" + ";".join(parts) + "m"
            self._sgr[style] = s
        return s
