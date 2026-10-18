"""Cell canvas, plain-text dump and a line-diffing ANSI renderer.

A Canvas is the composed frame: per row, a list of chars and a list of styles. A wide char
occupies its cell and puts "" in the next one (skipped on output).
"""
from __future__ import annotations

from .text import clean, cw


class Canvas:
    __slots__ = ("w", "h", "chars", "styles", "hits", "info")

    def __init__(self, w: int, h: int, style: tuple) -> None:
        self.w, self.h = w, h
        self.chars = [[" "] * w for _ in range(h)]
        self.styles = [[style] * w for _ in range(h)]
        self.hits: list[tuple[int, int, int, str, str]] = []  # (y, x0, x1, kind, key) for mouse
        self.info: dict = {}

    # ------------------------------------------------------------------ primitives
    def _fix(self, y: int, x: int) -> None:
        """Before overwriting cell x, break any wide char that straddles it."""
        row = self.chars[y]
        if row[x] == "" and x > 0:
            row[x - 1] = " "
        elif x + 1 < self.w and row[x + 1] == "" and row[x] != "":
            row[x + 1] = " "

    def fill(self, x: int, y: int, w: int, h: int, style: tuple, ch: str = " ") -> None:
        x0, x1 = max(0, x), min(self.w, x + w)
        if x1 <= x0:
            return
        for yy in range(max(0, y), min(self.h, y + h)):
            self._fix(yy, x0)
            self._fix(yy, x1 - 1)
            self.chars[yy][x0:x1] = [ch] * (x1 - x0)
            self.styles[yy][x0:x1] = [style] * (x1 - x0)

    def put(self, x: int, y: int, text: str, style: tuple, limit: int | None = None) -> int:
        """Write `text` at (x, y), clipped at `limit` (exclusive, default canvas width).
        Returns the x after the last written cell."""
        if not (0 <= y < self.h) or not text:
            return x
        end = self.w if limit is None else min(limit, self.w)
        chars, styles = self.chars[y], self.styles[y]
        text = clean(text)
        for ch in text:
            w = cw(ch)
            if w == 0:
                if x > 0 and x - 1 < end:
                    chars[x - 1] += ch
                continue
            if x + w > end:
                break
            if x >= 0:
                self._fix(y, x)
                chars[x] = ch
                styles[x] = style
                if w == 2:
                    self._fix(y, x + 1)
                    chars[x + 1] = ""
                    styles[x + 1] = style
            x += w
        return x

    def spans(self, x: int, y: int, spans, limit: int | None = None) -> int:
        for t, st in spans:
            x = self.put(x, y, t, st, limit)
        return x

    def restyle(self, x: int, y: int, w: int, fn) -> None:
        """Apply fn(style) -> style to a horizontal run (e.g. selection background)."""
        if not (0 <= y < self.h):
            return
        row = self.styles[y]
        for i in range(max(0, x), min(self.w, x + w)):
            row[i] = fn(row[i])

    def fade_all(self, fade) -> None:
        for row in self.styles:
            row[:] = [fade(s) for s in row]


def plain(cv: Canvas) -> list[str]:
    """Frame as plain text lines (trailing spaces stripped)."""
    return ["".join(row).rstrip() for row in cv.chars]


def line_ansi(chars: list[str], styles: list[tuple], sgr) -> str:
    out: list[str] = []
    last = None
    for ch, st in zip(chars, styles):
        if ch == "":
            continue
        if st is not last:
            if st != last:
                out.append(sgr(st))
            last = st
        out.append(ch)
    return "".join(out)


class Renderer:
    """Turns canvases into minimal ANSI updates by diffing whole lines."""

    def __init__(self, sgr) -> None:
        self.sgr = sgr
        self.prev: list[str] = []
        self.size: tuple[int, int] | None = None

    def invalidate(self) -> None:
        self.prev = []
        self.size = None

    def render(self, cv: Canvas) -> str:
        full = self.size != (cv.w, cv.h)
        out: list[str] = []
        if full:
            out.append("\x1b[0m\x1b[2J")
            self.prev = [""] * cv.h
            self.size = (cv.w, cv.h)
        for y in range(cv.h):
            line = line_ansi(cv.chars[y], cv.styles[y], self.sgr)
            if full or line != self.prev[y]:
                out.append("\x1b[%d;1H" % (y + 1))
                out.append(line)
                self.prev[y] = line
        if len(out) == 0:
            return ""
        # synchronized output: terminals that know it paint the frame atomically
        return "\x1b[?2026h" + "".join(out) + "\x1b[0m\x1b[?2026l"
