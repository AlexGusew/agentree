"""Small pure formatting helpers shared by the model layer."""
from __future__ import annotations

import os
import posixpath


def age(seconds: float | int | None) -> str:
    """Compact age: '<1m', '2m', '5h', '3d'. None -> '?'."""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return "<1m"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def home_dir() -> str:
    return os.path.expanduser("~")


def norm(path: str) -> str:
    """Normalise an absolute path without touching the filesystem."""
    if not path:
        return path
    p = posixpath.normpath(path)
    if p.startswith("//"):
        p = "/" + p.lstrip("/")
    return p


def is_within(path: str, root: str) -> bool:
    """Component-wise prefix test: /a/b is within /a/b and /a, not within /a/bc."""
    p, r = norm(path), norm(root)
    if r == "/":
        return p.startswith("/")
    return p == r or p.startswith(r + "/")


def home_short(path: str | None, home: str | None = None) -> str:
    """Replace a leading home directory with '~' (component-wise)."""
    if not path:
        return ""
    h = norm(home if home is not None else home_dir())
    p = norm(path)
    if h and h != "/" and is_within(p, h):
        return "~" + p[len(h):]
    return p


def tokens(n: int) -> str:
    """12345 -> '12.3k', 200000 -> '200k', 1500000 -> '1.5M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = n / 1000
        unit = "k"
    else:
        v = n / 1_000_000
        unit = "M"
    s = f"{v:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return s + unit
