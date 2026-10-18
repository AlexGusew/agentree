"""Cell-width helpers: width, truncate, wrap. Icons used by agentree are all width 1."""
from __future__ import annotations

import unicodedata

ELLIPSIS = "…"
_cache: dict[str, int] = {}


def cw(ch: str) -> int:
    """Terminal cell width of one code point (0, 1 or 2)."""
    o = ord(ch)
    if 32 <= o < 0x7F:
        return 1
    w = _cache.get(ch)
    if w is None:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            w = 0
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w = 2
        else:
            w = 1
        _cache[ch] = w
    return w


def width(s: str) -> int:
    if s.isascii():
        return len(s)
    return sum(cw(c) for c in s)


def clean(s: str) -> str:
    """Replace control characters (incl. ESC) so untrusted text can never emit escapes."""
    if s.isprintable():
        return s
    return "".join(c if c.isprintable() else (" " if c in "\t\n\r" else "·") for c in s)


def truncate(s: str, w: int, ellipsis: str = ELLIPSIS) -> str:
    """Cut `s` to at most `w` cells, ending with `ellipsis` when cut."""
    if w <= 0:
        return ""
    if width(s) <= w:
        return s
    ew = width(ellipsis)
    if w <= ew:
        return ellipsis[:w]
    out, used = [], 0
    for c in s:
        x = cw(c)
        if used + x > w - ew:
            break
        out.append(c)
        used += x
    return "".join(out).rstrip() + ellipsis


def truncate_left(s: str, w: int, ellipsis: str = ELLIPSIS) -> str:
    """Cut from the left: keeps the end (good for paths)."""
    if w <= 0:
        return ""
    if width(s) <= w:
        return s
    out, used = [], 0
    for c in reversed(s):
        x = cw(c)
        if used + x > w - 1:
            break
        out.append(c)
        used += x
    return ellipsis + "".join(reversed(out))


def tail(s: str, w: int) -> str:
    """Last `w` cells of `s` (no ellipsis) — for scrolled text inputs."""
    out, used = [], 0
    for c in reversed(s):
        x = cw(c)
        if used + x > w:
            break
        out.append(c)
        used += x
    return "".join(reversed(out))


def wrap(s: str, w: int) -> list[str]:
    """Greedy word wrap to `w` cells; long words are hard-split. Always returns >= 1 line."""
    if w <= 0:
        return [""]
    lines: list[str] = []
    for para in s.split("\n"):
        cur, cur_w = "", 0
        for word in para.split(" "):
            ww = width(word)
            if cur_w and cur_w + 1 + ww <= w:
                cur += " " + word
                cur_w += 1 + ww
                continue
            if cur_w or cur:
                lines.append(cur)
                cur, cur_w = "", 0
            while ww > w:  # hard split
                piece, pw = "", 0
                for c in word:
                    x = cw(c)
                    if pw + x > w:
                        break
                    piece += c
                    pw += x
                lines.append(piece)
                word = word[len(piece):]
                ww = width(word)
            cur, cur_w = word, ww
        lines.append(cur)
    return lines or [""]


def wrap_path(s: str, w: int, max_lines: int = 3) -> list[str]:
    """Split a path over lines, breaking after "/" where possible. The last line keeps the
    end of the path (left-truncated) if it still does not fit."""
    if w <= 0:
        return [""]
    lines: list[str] = []
    rest = s
    while width(rest) > w and len(lines) < max_lines - 1:
        head = cut(rest, w)
        k = head.rfind("/")
        if k > w // 3:
            head = head[:k + 1]
        lines.append(head)
        rest = rest[len(head):]
    lines.append(truncate_left(rest, w))
    return lines


# --------------------------------------------------------------------------- styled spans
# A span is (text, style). Lists of spans are laid out left to right.

def spans_width(spans) -> int:
    return sum(width(t) for t, _ in spans)


def truncate_spans(spans, w: int, ellipsis: str = ELLIPSIS):
    """Truncate a span list to `w` cells; the ellipsis takes the style of the cut span."""
    if spans_width(spans) <= w:
        return list(spans)
    if w <= 0:
        return []
    out, used = [], 0
    for t, st in spans:
        tw = width(t)
        if used + tw <= w - 1:
            out.append((t, st))
            used += tw
            continue
        out.append((cut(t, w - 1 - used).rstrip() + ellipsis, st))
        break
    return out


def cut(s: str, w: int) -> str:
    """Longest prefix of `s` that fits in `w` cells (no ellipsis)."""
    if width(s) <= w:
        return s
    out, used = [], 0
    for c in s:
        x = cw(c)
        if used + x > w:
            break
        out.append(c)
        used += x
    return "".join(out)


def wrap_spans(spans, w: int):
    """Word-wrap styled spans into lines of spans."""
    words: list[list[tuple[str, object]]] = []  # each word = list of (text, style) pieces
    cur: list[tuple[str, object]] = []
    for t, st in spans:
        parts = t.split(" ")
        for i, p in enumerate(parts):
            if i > 0:
                words.append(cur)
                cur = []
            if p:
                cur.append((p, st))
    words.append(cur)
    lines: list[list] = []
    line: list = []
    lw = 0
    for word in words:
        ww = spans_width(word)
        if not word:
            continue
        if lw and lw + 1 + ww <= w:
            line.append((" ", word[0][1]))
            line.extend(word)
            lw += 1 + ww
            continue
        if line:
            lines.append(line)
        if ww > w:  # hard split long word
            flat = "".join(t for t, _ in word)
            st = word[0][1]
            chunks = wrap(flat, w)
            for c in chunks[:-1]:
                lines.append([(c, st)])
            line, lw = [(chunks[-1], st)], width(chunks[-1])
        else:
            line, lw = list(word), ww
    if line or not lines:
        lines.append(line)
    return lines
