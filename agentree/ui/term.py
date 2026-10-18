"""Raw terminal handling and key decoding.

Key names:
  printable chars   -> the char itself ("j", "W", " ", "/")
  specials          -> "enter" "esc" "tab" "backtab" "backspace" "delete" "insert"
                       "up" "down" "left" "right" "home" "end" "pgup" "pgdn"
  with ctrl         -> "ctrl+n", "ctrl+enter", "ctrl+up", ...
  bracketed paste   -> Key("paste", text)
  mouse (SGR)       -> "click" / "wheelup" / "wheeldown" with x, y (0-based)
Alt/Super combos are dropped.
"""
from __future__ import annotations

import codecs
import os
import termios
import tty
from dataclasses import dataclass

ESC_TIMEOUT = 0.025


@dataclass(frozen=True, slots=True)
class Key:
    name: str
    text: str = ""
    x: int = 0
    y: int = 0

    def __repr__(self) -> str:  # compact in test failures
        return f"Key({self.name!r})" if not self.text or self.text == self.name else f"Key({self.name!r},{self.text!r})"


def char(c: str) -> Key:
    return Key(c, c)


_SS3 = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
_CSI_LETTER = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
               "E": None, "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
_TILDE = {1: "home", 7: "home", 4: "end", 8: "end", 5: "pgup", 6: "pgdn", 3: "delete", 2: "insert"}
_CODE = {13: "enter", 9: "tab", 127: "backspace", 8: "backspace", 27: "esc"}
_PASTE_END = "\x1b[201~"
_PASTE_START = object()


def _mods(m: int) -> tuple[bool, bool, bool]:
    """(shift, alt_or_super, ctrl) from an xterm/kitty modifier parameter (1 = none)."""
    b = max(0, m - 1)
    return bool(b & 1), bool(b & (2 | 8)), bool(b & 4)


def _named(name: str | None, m: int) -> Key | None:
    if name is None:
        return None
    shift, alt, ctrl = _mods(m)
    if alt:
        return None
    if name == "tab" and shift and not ctrl:
        return Key("backtab")
    return Key(f"ctrl+{name}" if ctrl else name)


def _from_code(code: int, m: int) -> Key | None:
    """Kitty CSI-u / modifyOtherKeys: unicode codepoint + modifiers."""
    shift, alt, ctrl = _mods(m)
    if alt:
        return None
    if code in _CODE:
        return _named(_CODE[code], m)
    if code < 32 or 57344 <= code <= 63743:  # controls / kitty private-use functional keys
        return None
    try:
        c = chr(code)
    except ValueError:
        return None
    if ctrl:
        return Key("ctrl+" + c.lower())
    if shift and c.isalpha():
        c = c.upper()
    return char(c)


def _ints(params: str) -> list[list[int]]:
    """'13;5:1' -> [[13], [5, 1]] (empty fields -> [])."""
    out = []
    for f in params.split(";"):
        sub = []
        for p in f.split(":"):
            sub.append(int(p) if p.isdigit() else 0)
        out.append(sub)
    return out


def _csi(params: str, final: str):
    if params.startswith("<") and final in "Mm":
        try:
            b, x, y = (int(v) for v in params[1:].split(";"))
        except ValueError:
            return None
        if final == "m" or b & 32:  # release / motion
            return None
        if b & 64:
            return Key("wheelup" if (b & 3) == 0 else "wheeldown", x=x - 1, y=y - 1)
        if (b & 3) == 0:
            return Key("click", x=x - 1, y=y - 1)
        return None
    if params[:1] in ("?", ">", "=", "<"):
        return None  # terminal responses (DA, kitty flag query, ...)
    p = _ints(params) if params else [[0]]
    first = p[0][0] if p[0] else 0
    m = p[1][0] if len(p) > 1 and p[1] and p[1][0] else 1
    if final in _CSI_LETTER:
        return _named(_CSI_LETTER[final], m)
    if final == "Z":
        return Key("backtab")
    if final == "~":
        if first == 200:
            return _PASTE_START
        if first == 27 and len(p) > 2 and p[2]:  # xterm modifyOtherKeys
            return _from_code(p[2][0], m)
        return _named(_TILDE.get(first), m)
    if final == "u":
        if len(p) > 1 and len(p[1]) > 1 and p[1][1] == 3:  # key release event
            return None
        return _from_code(first, m)
    return None


class KeyDecoder:
    """Incremental decoder. feed() bytes as they arrive; when `pending` stays true for
    ESC_TIMEOUT with no new input, call flush() (turns a buffered lone ESC into "esc")."""

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self.buf = ""
        self._paste: list[str] | None = None

    @property
    def pending(self) -> bool:
        return bool(self.buf) and self._paste is None

    def feed(self, data: bytes) -> list[Key]:
        self.buf += self._utf8.decode(data)
        return self._parse(final=False)

    def flush(self) -> list[Key]:
        return self._parse(final=True)

    def _parse(self, final: bool) -> list[Key]:
        out: list[Key] = []
        b = self.buf
        i, n = 0, len(b)
        while i < n:
            if self._paste is not None:
                j = b.find(_PASTE_END, i)
                if j < 0:
                    # keep a possible partial terminator for the next read
                    k = b.rfind("\x1b", max(i, n - len(_PASTE_END) + 1))
                    cut = k if k >= 0 else n
                    self._paste.append(b[i:cut])
                    i = cut
                    break
                self._paste.append(b[i:j])
                out.append(Key("paste", "".join(self._paste)))
                self._paste = None
                i = j + len(_PASTE_END)
                continue
            c = b[i]
            if c == "\x1b":
                if i + 1 >= n:
                    if final:
                        out.append(Key("esc"))
                        i += 1
                        continue
                    break
                nx = b[i + 1]
                if nx == "[":
                    j = i + 2
                    while j < n and 0x20 <= ord(b[j]) <= 0x3F:
                        j += 1
                    if j >= n:
                        if final:  # stray "ESC [" -> alt+[ : drop
                            i = n
                            continue
                        break
                    fin = b[j]
                    params = b[i + 2:j]
                    i = j + 1
                    if not 0x40 <= ord(fin) <= 0x7E:
                        continue  # malformed: drop the sequence
                    k = _csi(params, fin)
                    if k is _PASTE_START:
                        self._paste = []
                    elif k is not None:
                        out.append(k)
                    continue
                if nx == "O":
                    if i + 2 >= n:
                        if final:
                            i = n
                            continue
                        break
                    name = _SS3.get(b[i + 2])
                    if name:
                        out.append(Key(name))
                    i += 3
                    continue
                if nx == "\x1b":
                    out.append(Key("esc"))
                    i += 1
                    continue
                i += 2  # alt + char: ignored
                continue
            i += 1
            if c in "\r\n":
                out.append(Key("enter"))
            elif c == "\t":
                out.append(Key("tab"))
            elif c in "\x7f\x08":
                out.append(Key("backspace"))
            elif c < " ":
                if c != "\x00":
                    out.append(Key("ctrl+" + chr(ord(c) + 96)))
            elif "\x80" <= c < "\xa0":
                pass
            else:
                out.append(char(c))
        self.buf = b[i:]
        return out


# --------------------------------------------------------------------------- terminal

ENTER_SEQ = (
    "\x1b[?1049h"   # alternate screen
    "\x1b[?25l"     # hide cursor
    "\x1b[?7l"      # no autowrap (writing the last column never scrolls)
    "\x1b[?2004h"   # bracketed paste
    "\x1b[>1u"      # kitty keyboard protocol: disambiguate escape codes
    "\x1b[?1000h\x1b[?1006h"  # mouse clicks + wheel, SGR encoding
)
LEAVE_SEQ = (
    "\x1b[?1006l\x1b[?1000l"
    "\x1b[<u"
    "\x1b[?2004l"
    "\x1b[?7h"
    "\x1b[0m"
    "\x1b[?25h"
    "\x1b[?1049l"
)


def fileno(obj, default) -> int:
    if obj is None:
        obj = default
    return obj if isinstance(obj, int) else obj.fileno()


class Terminal:
    def __init__(self, in_fd: int, out_fd: int, mouse: bool = True) -> None:
        self.in_fd, self.out_fd = in_fd, out_fd
        self.mouse = mouse
        self._saved = None
        self.active = False

    def size(self) -> tuple[int, int]:
        for fd in (self.out_fd, self.in_fd):
            try:
                s = os.get_terminal_size(fd)
                if s.columns > 0 and s.lines > 0:
                    return s.columns, s.lines
            except OSError:
                pass
        return 80, 24

    def enter(self) -> None:
        self._saved = termios.tcgetattr(self.in_fd)
        tty.setraw(self.in_fd, termios.TCSANOW)
        self.active = True
        seq = ENTER_SEQ if self.mouse else ENTER_SEQ.replace("\x1b[?1000h\x1b[?1006h", "")
        self.write(seq)

    def leave(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            self.write(LEAVE_SEQ)
        except OSError:
            pass
        if self._saved is not None:
            try:
                termios.tcsetattr(self.in_fd, termios.TCSADRAIN, self._saved)
            except (OSError, termios.error):
                pass

    def write(self, s: str) -> None:
        data = s.encode("utf-8", "replace")
        while data:
            try:
                n = os.write(self.out_fd, data)
            except InterruptedError:
                continue
            except BlockingIOError:
                import select
                select.select([], [self.out_fd], [], 0.05)
                continue
            data = data[n:]
