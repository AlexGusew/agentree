"""Pure frame composition: AppState + (width, height) -> Canvas.

Nothing here touches the terminal, the clock or the filesystem, so every frame can be
tested as plain text with `plain(compose(state, size))`.

Frame layout (top to bottom):
  0      header bar   ▍agentree  sessions · worktrees · agents          ↻ stale [/ filter … ]
  1      filter bar   ⎇ worktrees [all][with tab][dirty][hide] │ [◆ agents][▪ panes][○ detached]
  2      rule         ──────────────────────┬──────────────────
  3..h-3 body         tree (≈60%)           │ detail pane
  h-2    rule         ──────────────────────┴──────────────────
  h-1    key bar      ↵ attach  ␣ fold  W worktree filter …                 esc close
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from agentree.model.detail import detail_for
from agentree.model.tree import build_view, resolve_selection, row_for
from agentree.types import Detail, Row, Snapshot, UiState, View

from .screen import Canvas, plain
from .text import tail, truncate, truncate_spans, width, wrap_path, wrap_spans
from .theme import BOLD, SPINNER, STRIKE, Theme

__all__ = ["AppState", "Status", "compose", "make_state", "rebuild", "plain", "MIN_DETAIL_W"]

MIN_DETAIL_W = 90          # below this many columns the detail pane is hidden
TREE_SHARE = 0.6
MODE_LABELS = (("all", "all"), ("with_tab", "with tab"), ("dirty", "dirty"), ("hide", "hide"))
CARET = {"open": "▾", "closed": "▸", None: " "}
ACTION_KEY = {"enter": "↵"}
PATH_FIELDS = {"path", "cwd"}         # wrapped at "/" instead of cut


@dataclass
class Status:
    text: str
    tone: str = "info"            # "info" | "ok" | "error" | "busy"
    until: float = 0.0            # monotonic deadline; 0 = until replaced


@dataclass
class AppState:
    snapshot: Snapshot
    ui: UiState
    view: View
    theme: Theme
    now: float
    home: str | None = None
    mode: str = "normal"          # "normal" | "filter"
    modal: Any = None             # widgets.Modal or None
    status: Status | None = None          # transient message (wins over activity)
    activity: Status | None = None        # "working… <what>" while an action runs
    tick: int = 0                         # spinner frame
    refreshing: bool = False
    scroll: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def selected_row(self) -> Row | None:
        r = row_for(self.view, self.ui.selected)
        return r if r is not None and r.selectable else None


def rebuild(st: AppState, now: float | None = None) -> None:
    """Recompute the view after the snapshot, ui or clock changed; keep the selection by key."""
    if now is not None:
        st.now = now
    st.view = build_view(st.snapshot, st.ui, st.now, home=st.home)
    sel = resolve_selection(st.view, st.ui.selected)
    if sel != st.ui.selected:
        st.ui = replace(st.ui, selected=sel)


def make_state(snapshot: Snapshot, ui: UiState, *, now: float, theme: Theme | None = None,
               home: str | None = None) -> AppState:
    st = AppState(snapshot=snapshot, ui=ui, view=View(rows=()), theme=theme or Theme(truecolor=True),
                  now=now, home=home)
    rebuild(st)
    return st


# --------------------------------------------------------------------------- helpers

def _segs(th: Theme, segs, bg: str, extra: int = 0):
    return [(s.text, th.st(s.role, bg, (BOLD if s.bold else 0) | extra)) for s in segs]


def _meta_spans(th: Theme, segs, bg: str):
    out = []
    for i, s in enumerate(segs):
        if i:
            out.append((" ", th.st("text", bg)))
        out.append((s.text, th.st(s.role, bg, BOLD if s.bold else 0)))
    return out


def tree_width(w: int) -> int:
    """Width of the tree column (the divider sits at x == tree_width)."""
    if w < MIN_DETAIL_W:
        return w
    return max(40, round((w - 1) * TREE_SHARE))


# --------------------------------------------------------------------------- header + filter bar

def _header(cv: Canvas, st: AppState) -> None:
    th = st.theme
    w = cv.w
    bar = "bar"
    cv.fill(0, 0, w, 1, th.st("text", bar))
    x = cv.put(1, 0, "▍", th.st("accent", bar))
    x = cv.put(x, 0, "agentree", th.st("text", bar, BOLD))

    # filter box, right aligned
    box_w = 34 if w >= 100 else max(16, min(34, w - 46 if w >= 80 else w - 12))
    bx = w - 1 - box_w
    _filter_box(cv, st, bx, 0, box_w)

    # indicators just left of the box
    ind: list[tuple[str, tuple]] = []
    if st.refreshing:
        ind.append(("↻", th.st("dim", bar)))
    if st.snapshot.stale:
        if ind:
            ind.append((" ", th.st("dim", bar)))
        ind.append(("stale", th.st("warn", bar)))
    iw = sum(width(t) for t, _ in ind)
    ix = bx - 2 - iw
    sub = "  sessions · worktrees · agents"
    if ix - x >= width(sub) + 2:
        cv.put(x, 0, sub, th.st("dim", bar))
    if ind and ix > x + 1:
        cv.spans(ix, 0, ind)


def _filter_box(cv: Canvas, st: AppState, x: int, y: int, w: int) -> None:
    th = st.theme
    editing = st.mode == "filter"
    text = st.ui.text
    lit = editing or bool(text)
    edge = th.st("accent" if lit else "rule", "bar", BOLD if editing else 0)
    well = "ground"
    cv.put(x, y, "[", edge)
    cv.put(x + w - 1, y, "]", edge)
    cv.fill(x + 1, y, w - 2, 1, th.st("text", well))
    ix = cv.put(x + 1, y, "/ ", th.st("accent" if lit else "dim", well))
    room = x + w - 1 - ix
    if text or editing:
        shown = text
        need = width(shown) + (1 if editing else 0)
        if need > room:
            # keep the end of the text visible while typing
            shown = "…" + tail(text, max(0, room - 2 - (1 if editing else 0)))
        ex = cv.put(ix, y, shown, th.st("text", well))
        if editing and ex < x + w - 1:
            cv.put(ex, y, " ", th.st("ground", "text"))   # block cursor
    else:
        cv.put(ix, y, truncate("filter by name, branch, agent", room), th.st("dim", well))
    cv.hits.append((y, x, x + w, "filter", ""))


def _chip(cv: Canvas, th: Theme, x: int, y: int, label: str, active: bool, hit: str) -> int:
    if active:
        edge, body = th.st("accent", "chip", BOLD), th.st("accent", "chip", BOLD)
    else:
        edge, body = th.st("rule", "bar"), th.st("dim", "bar")
    x0 = x
    x = cv.put(x, y, "[", edge)
    x = cv.put(x, y, label, body)
    x = cv.put(x, y, "]", edge)
    cv.hits.append((y, x0, x, "chip", hit))
    return x


def _filter_bar(cv: Canvas, st: AppState) -> None:
    th = st.theme
    ui = st.ui
    y = 1
    cv.fill(0, y, cv.w, 1, th.st("text", "bar"))
    x = cv.put(1, y, "⎇", th.st("teal", "bar"))
    x = cv.put(x, y, " worktrees ", th.st("dim", "bar"))
    for mode, label in MODE_LABELS:
        x = _chip(cv, th, x, y, label, ui.worktree_mode == mode, f"mode:{mode}")
    x = cv.put(x, y, " │ ", th.st("rule", "bar"))
    for name, label, on in (("show_agents", "◆ agents", ui.show_agents),
                            ("show_panes", "▪ panes", ui.show_panes),
                            ("show_detached", "○ detached", ui.show_detached)):
        x = _chip(cv, th, x, y, label, on, f"toggle:{name}")
    # quiet totals on the right when there is room
    snap = st.snapshot
    n_s = len(snap.sessions)
    n_w = sum(len(s.worktrees) for s in snap.sessions) + len(snap.orphans.worktrees)
    n_a = (sum(len(w.agents) for s in snap.sessions for w in s.worktrees)
           + sum(len(s.loose_agents) for s in snap.sessions) + len(snap.orphans.agents))
    summary = f"{n_s} sessions · {n_w} worktrees · {n_a} agents"
    sw = width(summary)
    if cv.w - 2 - sw >= x + 4:
        cv.put(cv.w - 1 - sw, y, summary, th.st("dim", "bar"))


# --------------------------------------------------------------------------- tree

def _tree_row(cv: Canvas, th: Theme, row: Row, y: int, x0: int, cw: int, selected: bool) -> None:
    bg = "selected" if selected else "ground"
    if selected:
        cv.fill(x0, y, cw, 1, th.st("text", bg))
        cv.put(x0, y, "▍", th.st("accent", bg))
    end = x0 + cw - 1                       # one cell of right padding
    if row.kind == "footer":
        text = "".join(s.text for s in row.label)
        room = end - x0 - 6
        while width(text) > room and " · " in text:      # drop trailing hints before cutting words
            text = text.rsplit(" · ", 1)[0]
        x = cv.put(x0 + 1, y, "╌╌ ", th.st("rule", bg), end)
        x = cv.put(x, y, truncate(text, max(0, end - x - 1)), th.st("dim", bg), end)
        x = cv.put(x, y, " ", th.st("rule", bg), end)
        if x < end:
            cv.put(x, y, "╌" * (end - x), th.st("rule", bg), end)
        return
    x = x0 + 1 + 2 * row.depth
    if x >= end:
        return
    x = cv.put(x, y, CARET[row.caret], th.st("dim", bg), end)
    x = cv.put(x, y, " ", th.st("text", bg), end)
    if row.icon is not None:
        x = cv.put(x, y, row.icon.text, th.st(row.icon.role, bg, BOLD if row.icon.bold else 0), end)
        x = cv.put(x, y, " ", th.st("text", bg), end)
    label = _segs(th, row.label, bg)
    meta = _meta_spans(th, row.meta, bg)
    avail = end - x
    lw = sum(width(t) for t, _ in label)
    mw = sum(width(t) for t, _ in meta)
    gap = 2 if meta else 0
    if lw + gap + mw > avail:
        # the label gives way first, down to a readable minimum; then the meta is cut
        lw_new = max(min(lw, 10), avail - gap - mw)
        label = truncate_spans(label, max(0, min(lw, lw_new, avail)))
        lw = sum(width(t) for t, _ in label)
        meta = truncate_spans(meta, max(0, avail - lw - gap))
        mw = sum(width(t) for t, _ in meta)
    cv.spans(x, y, label, end)
    if meta and mw:
        cv.spans(end - mw, y, meta, end)
    cv.hits.append((y, x0, x0 + cw, "row", row.key))


def _scroll(prev: int, sel: int, n: int, h: int) -> int:
    if n <= h or h <= 0:
        return 0
    margin = min(2, max(0, (h - 1) // 4))
    s = prev
    if sel >= 0:
        if sel < s + margin:
            s = sel - margin
        elif sel > s + h - 1 - margin:
            s = sel - (h - 1 - margin)
    return max(0, min(s, n - h))


def _tree(cv: Canvas, st: AppState, x0: int, y0: int, w: int, h: int, bar_x: int | None) -> None:
    th = st.theme
    rows = list(st.view.rows)
    footer = rows.pop() if rows and rows[-1].kind == "footer" else None
    sel_key = st.ui.selected
    sel_idx = next((i for i, r in enumerate(rows) if r.key == sel_key), -1)
    avail = h
    pinned = False
    if footer is not None and len(rows) + 1 > h:
        avail, pinned = h - 1, True
    scroll = _scroll(st.scroll, sel_idx, len(rows), avail)
    cv.info["scroll"] = scroll
    cv.info["tree_rows"] = avail
    cw = w - (1 if bar_x is not None and bar_x < x0 + w else 0)

    if not rows:
        msg = (f"Nothing matches “{st.ui.text.strip()}”" if st.ui.text.strip()
               else "No sessions found")
        cv.put(x0 + 3, y0 + min(1, h - 1), truncate(msg, max(0, cw - 4)), th.st("dim"))
        if st.ui.text.strip() and h > 3:
            cv.put(x0 + 3, y0 + 2, truncate("esc clears the filter", max(0, cw - 4)), th.st("dim"))
    for i in range(avail):
        ri = scroll + i
        if ri >= len(rows):
            break
        r = rows[ri]
        _tree_row(cv, th, r, y0 + i, x0, cw, r.key == sel_key and r.selectable)
    if footer is not None:
        fy = y0 + h - 1 if pinned else y0 + min(len(rows), h - 1)
        _tree_row(cv, th, footer, fy, x0, cw, False)

    # scrollbar (on the divider when the detail pane is shown, else the last column)
    if len(rows) > avail and bar_x is not None and avail > 0:
        thumb = max(1, avail * avail // len(rows))
        top = round(scroll * (avail - thumb) / max(1, len(rows) - avail))
        glyph = "┃" if bar_x >= x0 + w else "▐"
        for i in range(thumb):
            cv.put(bar_x, y0 + top + i, glyph, th.st("dim"))


# --------------------------------------------------------------------------- detail pane

def _chip_spans(th: Theme, d: Detail):
    chips = []
    for a in d.actions:
        k = ACTION_KEY.get(a.key, a.key)
        if a.enabled:
            sp = [("[", th.st("rule")), (k, th.st("accent", "ground", BOLD)), (" " + a.label, th.st("text")),
                  ("]", th.st("rule"))]
        else:
            sp = [("[", th.st("border")), (k, th.st("dim")), (" ", th.st("dim")),
                  (a.label, th.st("dim", "ground", STRIKE)), ("]", th.st("border"))]
        chips.append(sp)
    return chips


def _layout_chips(chips, w: int):
    lines: list[list] = []
    cur: list = []
    cw = 0
    for sp in chips:
        sw = sum(width(t) for t, _ in sp)
        if cur and cw + sw > w:
            lines.append(cur)
            cur, cw = [], 0
        if sw > w:
            sp = truncate_spans(sp, w)
            sw = w
        cur.extend(sp)
        cw += sw
    if cur:
        lines.append(cur)
    return lines


def _detail(cv: Canvas, st: AppState, x: int, y: int, w: int, h: int) -> None:
    th = st.theme
    cx, cw = x + 2, w - 4
    if cw < 12 or h < 3:
        return
    row = st.selected_row
    if row is None:
        cv.put(cx, y + 1, "Nothing selected", th.st("dim"))
        return
    d = detail_for(row, st.snapshot, st.now, home=st.home)
    pad = 1 if h >= 20 else 0
    bottom = y + h - pad
    chip_lines = _layout_chips(_chip_spans(th, d), cw)
    chips_y = bottom - len(chip_lines)
    yy = y + pad

    # kind heading with a hairline that runs to the edge
    kind = d.kind.upper()
    hx = cv.put(cx, yy, truncate(kind, cw), th.st("dim", "ground", BOLD))
    if cx + cw - hx > 2:
        cv.put(hx + 1, yy, "─" * (cx + cw - hx - 1), th.st("border"))
    yy += 1
    # title: up to two lines
    tl = wrap_spans(_segs(th, d.title, "ground"), cw) if d.title else []
    if len(tl) > 2:
        rest = [sp for i, line in enumerate(tl[1:]) for sp in ([(" ", th.st())] if i else []) + line]
        tl = [tl[0], truncate_spans(rest, cw)]
    for line in tl:
        cv.spans(cx, yy, line, cx + cw)
        yy += 1
    yy += 1
    if d.fields:
        lw = min(14, max(width(f.label) for f in d.fields))
        vx, vw = cx + lw + 2, cw - lw - 2
        limit = chips_y - (1 if chip_lines else 0)
        for f in d.fields:
            if yy >= limit:
                break
            cv.put(cx, yy, truncate(f.label, lw), th.st("dim"))
            spans = _segs(th, f.value, "ground")
            if f.wrap:
                lines = wrap_spans(spans, vw)
                room = limit - yy
                if len(lines) > room:
                    lines = lines[:room]
                    if lines:
                        lines[-1] = truncate_spans(lines[-1] + [(" …", th.st("dim"))], vw)
                for line in lines:
                    cv.spans(vx, yy, line, vx + vw)
                    yy += 1
            else:
                if f.label in PATH_FIELDS and len(spans) == 1:
                    t, s = spans[0]
                    for part in wrap_path(t, vw, max(1, min(3, limit - yy))):
                        cv.put(vx, yy, part, s, vx + vw)
                        yy += 1
                    continue
                cv.spans(vx, yy, truncate_spans(spans, vw), vx + vw)
                yy += 1
    for i, line in enumerate(chip_lines):
        if chips_y + i >= yy:
            cv.spans(cx, chips_y + i, line, cx + cw)


# --------------------------------------------------------------------------- key bar

def _hints(st: AppState) -> tuple[list[tuple[str, str]], tuple[str, str]]:
    if st.mode == "filter":
        return [("type", "to filter"), ("⌫", "delete"), ("^u", "clear"), ("↵", "keep")], ("esc", "clear")
    row = st.selected_row
    jump = "attach" if row is not None and row.kind == "session" else "jump"
    left = [("↵", jump), ("␣", "fold"), ("W", "worktree filter"), ("A", "agents"), ("P", "panes"),
            ("D", "detached"), ("^n", "spawn"), ("/", "filter"), ("?", "help")]
    if st.activity is not None:                 # an action is running: esc/q only warn
        right = ("q q", "force quit")
    else:
        right = ("esc", "clear filter") if st.ui.text else ("esc", "close")
    return left, right


def _keybar(cv: Canvas, st: AppState, y: int) -> None:
    th = st.theme
    w = cv.w
    cv.fill(0, y, w, 1, th.st("text", "bar"))
    left, (rk, rl) = _hints(st)
    right = [(rk, th.st("text", "bar", BOLD)), (" " + rl, th.st("dim", "bar"))]
    rw = width(rk) + 1 + width(rl)
    rx = w - 1 - rw
    status = st.status or st.activity
    if status is not None:
        icon, role = {"ok": ("✓", "green"), "error": ("✗", "red"), "warn": ("!", "accent"),
                      "busy": (SPINNER[st.tick % len(SPINNER)], "accent")}.get(status.tone, ("›", "accent"))
        role_text = "red" if status.tone == "error" else "text"
        x = cv.put(1, y, icon + " ", th.st(role, "bar", BOLD))
        cv.put(x, y, truncate(status.text, max(0, rx - x - 2)), th.st(role_text, "bar"))
    else:
        x = 1
        for i, (k, label) in enumerate(left):
            piece = width(k) + 1 + width(label) + (2 if i else 0)
            if x + piece > rx - 2:
                break
            if i:
                x = cv.put(x, y, "  ", th.st("dim", "bar"))
            x = cv.put(x, y, k, th.st("text", "bar", BOLD))
            x = cv.put(x, y, " " + label, th.st("dim", "bar"))
    if rx > 1:
        cv.spans(rx, y, right)


# --------------------------------------------------------------------------- frame

def compose(st: AppState, size: tuple[int, int]) -> Canvas:
    w, h = size
    w, h = max(1, w), max(1, h)
    th = st.theme
    cv = Canvas(w, h, th.st("text", "ground"))
    if w < 40 or h < 8:
        msg = "agentree needs at least 40×8"
        cv.put(max(0, (w - width(msg)) // 2), h // 2, truncate(msg, w), th.st("dim"))
        cv.info["scroll"] = st.scroll
        return cv
    _header(cv, st)
    _filter_bar(cv, st)
    tw = tree_width(w)
    show_detail = tw < w
    rule = th.st("border")
    cv.put(0, 2, "─" * w, rule)
    cv.put(0, h - 2, "─" * w, rule)
    y0, bh = 3, h - 5
    if show_detail:
        cv.put(tw, 2, "┬", rule)
        cv.put(tw, h - 2, "┴", rule)
        for yy in range(y0, y0 + bh):
            cv.put(tw, yy, "│", rule)
    pad = 1 if bh >= 24 else 0
    _tree(cv, st, 0, y0 + pad, tw, bh - pad, tw if show_detail else w - 1)
    if show_detail:
        _detail(cv, st, tw + 1, y0, w - tw - 1, bh)
    _keybar(cv, st, h - 1)
    cv.info["detail"] = show_detail
    cv.info["tree_y0"] = y0 + pad
    if st.modal is not None:
        cv.fade_all(th.fade)
        cv.hits.clear()
        st.modal.draw(cv, th)
    return cv
