"""Modal widgets drawn over the dimmed frame: confirm, prompt, agent chooser, help and a generic form.

Every modal has `handle(key) -> str | None` (returns "ok" / "cancel" / None) and
`draw(canvas, theme)`. They hold their own state and never call actions themselves;
the app reads the result and acts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .screen import Canvas
from .term import Key
from .text import cut, tail, truncate, width, wrap
from .theme import BOLD, SPINNER, STRIKE, Theme

BG = "modal"


# --------------------------------------------------------------------------- text input

class TextInput:
    """Single-line editor with a cursor. Keys: chars, ←/→, home/end, ^a/^e, ⌫, del, ^u, ^w, ^k, paste."""

    def __init__(self, value: str = "") -> None:
        self.value = value
        self.pos = len(value)

    def set(self, value: str) -> None:
        self.value, self.pos = value, len(value)

    def handle(self, key: Key) -> bool:
        """Apply an editing key; True if it was consumed."""
        n, v, p = key.name, self.value, self.pos
        if n == "paste":
            t = " ".join(key.text.splitlines())
            self.value, self.pos = v[:p] + t + v[p:], p + len(t)
        elif len(n) == 1 and key.text:
            self.value, self.pos = v[:p] + n + v[p:], p + 1
        elif n == "backspace":
            if p:
                self.value, self.pos = v[:p - 1] + v[p:], p - 1
        elif n == "delete":
            self.value = v[:p] + v[p + 1:]
        elif n == "left":
            self.pos = max(0, p - 1)
        elif n == "right":
            self.pos = min(len(v), p + 1)
        elif n in ("home", "ctrl+a"):
            self.pos = 0
        elif n in ("end", "ctrl+e"):
            self.pos = len(v)
        elif n == "ctrl+u":
            self.value, self.pos = v[p:], 0
        elif n == "ctrl+k":
            self.value = v[:p]
        elif n == "ctrl+w":
            i = p
            while i and v[i - 1] == " ":
                i -= 1
            while i and v[i - 1] != " ":
                i -= 1
            self.value, self.pos = v[:i] + v[p:], i
        else:
            return False
        return True

    def draw(self, cv: Canvas, th: Theme, x: int, y: int, w: int, *, focused: bool = True,
             placeholder: str = "") -> None:
        """Input well `w` cells wide with a block cursor when focused."""
        well = th.st("text", "input")
        cv.fill(x, y, w, 1, well)
        if not self.value and not focused:
            cv.put(x + 1, y, truncate(placeholder, w - 2), th.st("dim", "input"))
            return
        inner = w - 2
        before, after = self.value[:self.pos], self.value[self.pos:]
        if width(before) >= inner:
            before = tail(before, inner - 1)
        cx = cv.put(x + 1, y, before, well)
        if focused:
            ch = after[:1] or " "
            cv.put(cx, y, ch, th.st("ground", "text"))
            cv.put(cx + width(ch), y, cut(after[1:], max(0, x + w - 1 - cx - width(ch))), well)
        else:
            cv.put(cx, y, cut(after, max(0, x + w - 1 - cx)), well)
        if not self.value and placeholder and focused:
            cv.put(x + 2, y, truncate(placeholder, w - 3), th.st("dim", "input"))


# --------------------------------------------------------------------------- box helpers

def draw_box(cv: Canvas, th: Theme, x: int, y: int, w: int, h: int, title: str = "",
             tone: str = "text") -> None:
    """Rounded box with a soft drop shadow and an inline title."""
    shadow = lambda s: (s[0], "#0b0d11", 0)  # noqa: E731
    cv.restyle(x + 2, y + h, w, shadow)
    for yy in range(y + 1, y + h + 1):
        cv.restyle(x + w, yy, 2, shadow)
    cv.fill(x, y, w, h, th.st("text", BG))
    edge = th.st("rule", BG)
    cv.put(x, y, "╭" + "─" * (w - 2) + "╮", edge)
    cv.put(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", edge)
    for yy in range(y + 1, y + h - 1):
        cv.put(x, yy, "│", edge)
        cv.put(x + w - 1, yy, "│", edge)
    if title:
        t = truncate(title, w - 6)
        cv.put(x + 2, y, " ", edge)
        tx = cv.put(x + 3, y, t, th.st(tone, BG, BOLD))
        cv.put(tx, y, " ", edge)


def centre(cv: Canvas, w: int, h: int) -> tuple[int, int, int, int]:
    w = max(10, min(w, cv.w - 4))
    h = max(3, min(h, cv.h - 2))
    return (cv.w - w) // 2, max(0, (cv.h - h) // 2 - 1), w, h


def hint_spans(th: Theme, items: Sequence[tuple[str, str]], strong: str | None = None):
    out = []
    for i, (k, label) in enumerate(items):
        if i:
            out.append(("   ", th.st("dim", BG)))
        role = "accent" if k == strong else "text"
        out.append((k, th.st(role, BG, BOLD)))
        out.append((" " + label, th.st("dim", BG)))
    return out


class Modal:
    title = ""

    def handle(self, key: Key) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def draw(self, cv: Canvas, th: Theme) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------- confirm

class ConfirmModal(Modal):
    """Shows what will happen; optional typed confirmation. y/↵ confirm, n/esc cancel."""

    def __init__(self, title: str, lines: Sequence[str] = (), require_text: str | None = None,
                 danger: bool = True, payload=None) -> None:
        self.title = title
        self.lines = tuple(lines)
        self.require = require_text
        self.danger = danger
        self.payload = payload
        self.input = TextInput()
        self.error = ""

    @classmethod
    def from_plan(cls, plan) -> "ConfirmModal":
        c = plan.confirm
        lines = c.lines or tuple("$ " + " ".join(s) for s in plan.steps)
        return cls(c.title, lines, c.require_text, c.danger, payload=plan)

    @property
    def typed(self) -> str | None:
        return self.input.value if self.require else None

    def handle(self, key: Key) -> str | None:
        n = key.name
        if n == "esc":
            return "cancel"
        if self.require is None:
            if n in ("y", "Y", "enter"):
                return "ok"
            if n in ("n", "N", "q"):
                return "cancel"
            return None
        if n in ("enter", "ctrl+enter"):
            if self.input.value == self.require:
                return "ok"
            self.error = "does not match" if self.input.value else "type it to confirm"
            return None
        if self.input.handle(key):
            self.error = ""
        return None

    def draw(self, cv: Canvas, th: Theme) -> None:
        w = min(76, max(44, cv.w * 2 // 3))
        w = min(w, cv.w - 4)
        iw = w - 6
        body: list[tuple[str, tuple]] = []
        for ln in self.lines:
            role = "text" if ln.startswith("$ ") else "dim"
            for part in wrap(ln, iw):
                body.append((part, th.st(role, BG)))
        h = 5 + len(body) + (4 if self.require else 0)
        x, y, w, h = centre(cv, w, h)
        tone = "red" if self.danger else "accent"
        draw_box(cv, th, x, y, w, h, ("! " if self.danger else "") + self.title, tone)
        yy = y + 2
        max_body = h - 5 - (4 if self.require else 0)
        max_body = max(1, max_body)
        for i, (t, s) in enumerate(body[:max_body]):
            if i == max_body - 1 and len(body) > max_body:
                t = truncate(t + " …", iw)
            cv.put(x + 3, yy, t, s)
            yy += 1
        if self.require:
            yy += 1
            px = cv.put(x + 3, yy, "type ", th.st("dim", BG))
            px = cv.put(px, yy, truncate(self.require, max(4, x + w - 3 - px - 12)), th.st("text", BG, BOLD))
            cv.put(px, yy, " to confirm", th.st("dim", BG))
            yy += 1
            self.input.draw(cv, th, x + 3, yy, iw)
            ok = self.input.value == self.require
            mark = ("✓ matches", "green") if ok else ((f"✗ {self.error}", "red") if self.error else ("", "dim"))
            yy += 1
            if mark[0]:
                cv.put(x + 3, yy, mark[0], th.st(mark[1], BG))
            yy += 1
            hints = [("↵", "confirm"), ("esc", "cancel")]
        else:
            hints = [("y", "confirm"), ("n", "cancel")]
        cv.spans(x + 3, y + h - 2, hint_spans(th, hints, strong=hints[0][0]), x + w - 2)


# --------------------------------------------------------------------------- prompt

class PromptModal(Modal):
    def __init__(self, title: str, label: str, value: str = "", ok_label: str = "save",
                 payload=None) -> None:
        self.title, self.label, self.ok_label = title, label, ok_label
        self.input = TextInput(value)
        self.payload = payload
        self.error = ""

    @property
    def value(self) -> str:
        return self.input.value

    def handle(self, key: Key) -> str | None:
        n = key.name
        if n == "esc":
            return "cancel"
        if n in ("enter", "ctrl+enter"):
            if not self.input.value.strip():
                self.error = "cannot be empty"
                return None
            return "ok"
        if self.input.handle(key):
            self.error = ""
        return None

    def draw(self, cv: Canvas, th: Theme) -> None:
        x, y, w, h = centre(cv, min(60, cv.w - 4), 8)
        draw_box(cv, th, x, y, w, h, self.title, "accent")
        lx = cv.put(x + 3, y + 2, self.label, th.st("dim", BG))
        self.input.draw(cv, th, lx + 2, y + 2, x + w - 3 - lx - 2)
        if self.error:
            cv.put(lx + 2, y + 3, "✗ " + self.error, th.st("red", BG))
        cv.spans(x + 3, y + h - 2, hint_spans(th, [("↵", self.ok_label), ("esc", "cancel")], "↵"), x + w - 2)


# --------------------------------------------------------------------------- agent chooser

class AgentChooserModal(Modal):
    """Pick which agent to start (`o` when more than one is installed). ↑/↓/j/k move,
    ↵ launches, esc/q cancel. `choices` are AgentChoice-like (id, label, available);
    only the available ones are listed, in the order given."""

    title = "Agent here"

    def __init__(self, choices: Sequence, default_provider: str | None = None, *, payload=None) -> None:
        self.choices = tuple(c for c in choices if c.available)
        self.payload = payload
        ids = [c.id for c in self.choices]
        self.index = ids.index(default_provider) if default_provider in ids else 0

    @property
    def provider(self) -> str | None:
        return self.choices[self.index].id if self.choices else None

    def handle(self, key: Key) -> str | None:
        n = key.name
        if n in ("esc", "q"):
            return "cancel"
        if n in ("enter", "ctrl+enter"):
            return "ok" if self.choices else "cancel"
        if n in ("up", "k"):
            self.index = max(0, self.index - 1)
        elif n in ("down", "j"):
            self.index = min(max(0, len(self.choices) - 1), self.index + 1)
        elif n == "home":
            self.index = 0
        elif n == "end":
            self.index = max(0, len(self.choices) - 1)
        return None

    def draw(self, cv: Canvas, th: Theme) -> None:
        lw = max((width(c.label) for c in self.choices), default=0)
        x, y, w, h = centre(cv, min(max(44, lw + 10), 60, cv.w - 4), len(self.choices) + 5)
        draw_box(cv, th, x, y, w, h, self.title, "accent")
        room = max(1, h - 5)
        top = min(max(0, self.index - room + 1), max(0, len(self.choices) - room))
        for i, c in enumerate(self.choices[top:top + room], start=top):
            yy = y + 2 + i - top
            if i == self.index:
                cv.fill(x + 1, yy, w - 2, 1, th.st("text", "selected"))
                cv.put(x + 1, yy, "▍", th.st("accent", "selected"))
                cv.put(x + 3, yy, truncate(c.label, w - 6), th.st("text", "selected", BOLD))
            else:
                cv.put(x + 3, yy, truncate(c.label, w - 6), th.st("text", BG))
        hints = [("↑ ↓", "choose"), ("↵", "launch"), ("esc", "cancel")]
        cv.spans(x + 3, y + h - 2, hint_spans(th, hints, "↵"), x + w - 2)


# --------------------------------------------------------------------------- help

HELP: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("MOVE", (("j k ↑ ↓", "move"), ("h l ← →", "fold, or go to parent/child"), ("␣", "toggle fold"),
              ("Home End", "first, last"), ("PgUp PgDn", "page up, down"))),
    ("FILTER", (("/", "filter by text"), ("W", "cycle worktree filter"),
                ("A P D", "agents, panes, detached"), ("esc", "clear filter, then close"))),
    ("ACT", (("↵", "jump to it"), ("o", "agent here"), ("t", "open a tab for it"),
             ("g", "lazygit"), ("x", "remove…"), ("r", "rename session"), ("^n", "spawn a workspace"))),
    ("APP", (("R", "refresh now"), ("?", "this help"), ("q", "close"))),
)


class HelpModal(Modal):
    title = "Keys"

    def handle(self, key: Key) -> str | None:
        if key.name in ("esc", "?", "q", "enter", "ctrl+c"):
            return "cancel"
        return None

    def draw(self, cv: Canvas, th: Theme) -> None:
        kw = max(width(k) for _, items in HELP for k, _ in items)
        dw = max(width(d) for _, items in HELP for _, d in items)
        col = kw + 2 + dw
        two = cv.w - 4 >= col * 2 + 10
        if two:
            left, right = HELP[:2], HELP[2:]
            lh = sum(len(i) + 2 for _, i in left)
            rh = sum(len(i) + 2 for _, i in right)
            w, h = col * 2 + 10, max(lh, rh) + 3
        else:
            left, right = HELP, ()
            w, h = col + 6, sum(len(i) + 2 for _, i in HELP) + 3
        x, y, w, h = centre(cv, w, h)
        draw_box(cv, th, x, y, w, h, "Keys", "accent")

        def column(sections, cx: int) -> None:
            yy = y + 2
            for name, items in sections:
                if yy >= y + h - 2:
                    return
                cv.put(cx, yy, name, th.st("dim", BG, BOLD))
                yy += 1
                for k, d in items:
                    if yy >= y + h - 2:
                        return
                    cv.put(cx, yy, k, th.st("accent", BG, BOLD))
                    cv.put(cx + kw + 2, yy, truncate(d, max(0, x + w - 3 - cx - kw - 2)), th.st("text", BG))
                    yy += 1
                yy += 1

        column(left, x + 3)
        if right:
            column(right, x + 3 + col + 4)
        cv.spans(x + 3, y + h - 1, [(" esc close ", th.st("dim", BG))])


# --------------------------------------------------------------------------- generic form

@dataclass
class TextField:
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    hint: Callable[[dict], str] | None = None      # e.g. derived worktree path preview


@dataclass
class ChoiceField:
    name: str
    label: str
    options: tuple[str, ...]                       # values
    index: int = 0
    labels: tuple[str, ...] | None = None          # display text per option (default: the value)
    empty: str = "none"                            # shown when there are no options

    @property
    def value(self) -> str:
        return self.options[self.index] if self.options else ""

    @property
    def display(self) -> str:
        if not self.options:
            return self.empty
        if self.labels and self.index < len(self.labels):
            return self.labels[self.index]
        return self.value


@dataclass
class CheckField:
    name: str
    label: str
    checked: bool = False


Field = TextField | ChoiceField | CheckField
STEP_ICON = {"pending": ("·", "dim"), "running": ("●", "accent"), "ok": ("✓", "green"),
             "failed": ("✗", "red"), "skipped": ("–", "dim"), "rolled_back": ("↺", "accent")}

Line = list  # list of (text, role, flags)


@dataclass
class Step:
    label: str
    state: str = "pending"        # pending | running | ok | failed | skipped | rolled_back
    detail: str = ""


class FormModal(Modal):
    """Text, choice and checkbox fields + read-only preview + progress view.

    tab/↓/↵ next field, shift-tab/↑ previous, ←/→/␣ change a choice, ␣ toggles a checkbox,
    ^↵ (or ^s) submits (only while `submit_enabled`), esc cancels. When `progress` is set the
    form is read-only: the fields are replaced by `summary()` and the step list is shown.
    `preview(values)` returns lines: a str, or a (text, role) pair.
    """

    def __init__(self, title: str, fields: Sequence[Field], *,
                 preview: Callable[[dict], Sequence] | None = None,
                 submit_label: str = "create", preview_title: str = "COMMANDS") -> None:
        self.title = title
        self.fields = list(fields)
        self.preview = preview
        self.submit_label = submit_label
        self.preview_title = preview_title
        self.focus = 0
        self.inputs = {f.name: TextInput(f.value) for f in self.fields if isinstance(f, TextField)}
        self.progress: list[Step] | None = None
        self.error = ""
        self.done = False
        self.submit_enabled = True
        self.tick = 0                 # spinner frame, set by the app before each render
        self.footer: tuple[str, str] | None = None   # (text, role) line above the hints

    # ------------------------------------------------------------------ values
    def values(self) -> dict:
        out: dict = {}
        for f in self.fields:
            if isinstance(f, TextField):
                out[f.name] = self.inputs[f.name].value
            elif isinstance(f, ChoiceField):
                out[f.name] = f.value
            else:
                out[f.name] = f.checked
        return out

    def field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)

    # ------------------------------------------------------------------ keys
    def handle(self, key: Key) -> str | None:
        n = key.name
        if self.progress is not None:
            if n in ("esc", "enter") and self.done:
                return "cancel"
            return None
        if n == "esc":
            return "cancel"
        if n in ("ctrl+enter", "ctrl+s"):
            if not self.submit_enabled:
                self.error = self.error or "fix the problems above first"
                return None
            return "ok"
        if not self.fields:
            return None
        f = self.fields[self.focus]
        if n in ("tab", "down") or (n == "enter" and not isinstance(f, CheckField)):
            self.focus = (self.focus + 1) % len(self.fields)
            return None
        if n in ("backtab", "up"):
            self.focus = (self.focus - 1) % len(self.fields)
            return None
        if isinstance(f, TextField):
            if self.inputs[f.name].handle(key):
                self.error = ""
        elif isinstance(f, ChoiceField) and f.options:
            if n in ("right", " ", "l"):
                f.index = (f.index + 1) % len(f.options)
            elif n in ("left", "h"):
                f.index = (f.index - 1) % len(f.options)
        elif isinstance(f, CheckField) and n in (" ", "x", "enter"):
            f.checked = not f.checked
        return None

    # ------------------------------------------------------------------ content (overridable)
    def preview_lines(self) -> list[tuple[str, str]]:
        out = []
        for ln in (self.preview(self.values()) if self.preview else ()):
            out.append(ln if isinstance(ln, tuple) else (ln, "text"))
        return out

    def summary(self) -> list[tuple[str, str]]:
        """Shown instead of the fields while running."""
        return [(f"{f.label}  {self.inputs[f.name].value}", "text")
                for f in self.fields if isinstance(f, TextField) and self.inputs[f.name].value]

    def hints(self) -> list[tuple[str, str]]:
        if self.progress is not None:
            return [("↵", "close"), ("esc", "close")] if self.done else [("esc", "cancel")]
        return [("^↵", self.submit_label), ("tab", "next"), ("␣", "change"), ("esc", "cancel")]

    def step_lines(self, w: int) -> list[Line]:
        out: list[Line] = []
        for st in self.progress or ():
            icon, role = STEP_ICON.get(st.state, ("?", "dim"))
            if st.state == "running":
                icon = SPINNER[self.tick % len(SPINNER)]
            text_role = "dim" if st.state in ("pending", "skipped") else "text"
            out.append([(icon + " ", role, BOLD), (truncate(st.label, w - 2), text_role, 0)])
            if st.detail:
                drole = "red" if st.state == "failed" else ("accent" if st.state == "rolled_back" else "dim")
                out.append([("  " + truncate(st.detail, w - 2), drole, 0)])
        return out

    # ------------------------------------------------------------------ draw
    def draw(self, cv: Canvas, th: Theme) -> None:
        w = min(96, cv.w - 4)
        iw = w - 6
        lw = max((width(f.label) for f in self.fields if not isinstance(f, CheckField)), default=6)
        vals = self.values()
        running = self.progress is not None
        # top block: fields (edit) or summary (running)
        top: list = []
        if running:
            for text, role in self.summary():
                top.append(("spans", [(truncate(text, iw), role, 0)]))
        else:
            for i, f in enumerate(self.fields):
                top.append(("field", i))
                if isinstance(f, TextField) and f.hint:
                    top.append(("hint", f.hint(vals)))
        # lower block: preview (edit) or steps (running)
        if running:
            lower = self.step_lines(iw)
            title = "PROGRESS"
        else:
            lower = []
            for t, r in self.preview_lines():
                parts = wrap(t, iw - 2) if width(t) > iw else [t]
                if len(parts) > 3:
                    parts = parts[:2] + [truncate(" ".join(parts[2:]), iw - 2)]
                lower.append([(parts[0], r, 0)])
                lower += [[("  " + p, r, 0)] for p in parts[1:]]
            title = self.preview_title
        foot = ("✗ " + self.error, "red") if self.error and not running else self.footer
        fixed = 2 + len(top) + (1 if top else 0) + (1 if foot else 0) + 3
        x, y, w, h = centre(cv, w, fixed + (1 + len(lower) if lower else 0))
        room = max(0, h - fixed - 1)
        if lower and len(lower) > room:
            if running:
                lower = lower[len(lower) - room:]          # keep the newest steps in view
            else:
                lower = lower[:max(0, room - 1)] + [[("…", "dim", 0)]]
        draw_box(cv, th, x, y, w, h, self.title, "accent")
        vx = x + 3 + lw + 2
        vw = x + w - 3 - vx
        yy = y + 2
        limit = y + h - 3 - (1 if foot else 0)
        for kind, arg in top:
            if yy > limit:
                break
            if kind == "spans":
                cv.spans(x + 3, yy, [(t, th.st(r, BG, fl)) for t, r, fl in arg], x + w - 3)
            elif kind == "hint":
                cv.put(vx + 1, yy, truncate("→ " + arg, vw - 1), th.st("dim", BG))
            else:
                self._draw_field(cv, th, arg, x, yy, vx, vw)
            yy += 1
        if lower and yy + 2 <= limit:
            if top:
                yy += 1
            hx = cv.put(x + 3, yy, title, th.st("dim", BG, BOLD))
            cv.put(hx + 1, yy, "─" * max(0, x + w - 4 - hx), th.st("border", BG))
            yy += 1
            for line in lower:
                if yy > limit:
                    break
                cv.spans(x + 3, yy, [(t, th.st(r, BG, fl)) for t, r, fl in line], x + w - 3)
                yy += 1
        if foot:
            cv.put(x + 3, y + h - 3, truncate(foot[0], iw), th.st(foot[1], BG, BOLD))
        hints = self.hints()
        spans = hint_spans(th, hints, strong=hints[0][0])
        if not running and not self.submit_enabled:
            spans[0] = (spans[0][0], th.st("dim", BG, STRIKE))
            spans[1] = (spans[1][0], th.st("dim", BG, STRIKE))
        cv.spans(x + 3, y + h - 2, spans, x + w - 2)

    def _draw_field(self, cv: Canvas, th: Theme, i: int, x: int, yy: int, vx: int, vw: int) -> None:
        f = self.fields[i]
        focused = i == self.focus
        mark = th.st("accent", BG, BOLD) if focused else th.st("dim", BG)
        cv.put(x + 1, yy, "▍" if focused else " ", th.st("accent", BG))
        if isinstance(f, TextField):
            cv.put(x + 3, yy, f.label, mark)
            self.inputs[f.name].draw(cv, th, vx, yy, vw, focused=focused, placeholder=f.placeholder)
        elif isinstance(f, ChoiceField):
            cv.put(x + 3, yy, f.label, mark)
            arrow = th.st("accent" if focused else "rule", BG)
            px = cv.put(vx, yy, "‹ ", arrow)
            role = "text" if f.options else "dim"
            px = cv.put(px, yy, truncate(f.display, vw - 10), th.st(role, BG, BOLD if focused else 0))
            px = cv.put(px, yy, " ›", arrow)
            if len(f.options) > 1:
                cv.put(px + 2, yy, f"{f.index + 1}/{len(f.options)}", th.st("dim", BG))
        else:
            box = "[x]" if f.checked else "[ ]"
            bx = cv.put(vx, yy, box, th.st("accent" if f.checked else "rule", BG, BOLD))
            cv.put(bx + 1, yy, truncate(f.label, vw - 4),
                   th.st("text" if focused or f.checked else "dim", BG, BOLD if focused else 0))
