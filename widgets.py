#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""widgets.py · 触摸控件 + 弹窗 + 字体（mobile_gui 拆出的 UI 原语）

小屏触屏终端专用：
- Btn：按钮（按下高亮、拖出取消、松手生效）
- VScroll：Canvas 滚动容器（拖动 / 抬起点击 / 长按 / 惯性）
- Modal / Confirm / Text / NumPad / Sheet：弹窗
- Fonts：CJK 字体探测 + HiDPI 字号校准

配色集中在 BG/PANEL/UP/DOWN 等常量（涨跌色与 A 股惯例：红涨绿跌）。
"""
import tkinter as tk
import tkinter.font as tkfont

# ---------------- 配色（暗色，A股红涨绿跌）----------------
BG = "#0b0f14"
PANEL = "#151b23"
PANEL2 = "#111720"
ROW_ALT = "#131a22"
FG = "#e6edf3"
DIM = "#7d8b9a"
LINE = "#222c37"
ACCENT = "#3d8bfd"
ACCENT_DK = "#12325e"
UP = "#ff5252"
DOWN = "#26c281"
WARN = "#e3b341"
SEL = "#22303f"

CJK_FONTS = ("Noto Sans CJK SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
             "Source Han Sans SC", "Microsoft YaHei", "PingFang SC",
             "Droid Sans Fallback", "DejaVu Sans")
MONO_FONTS = ("DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Consolas",
              "WenQuanYi Zen Hei Mono", "Courier New")

# 长按触发阈值（ms）；惯性滚动阈值（像素）
LONG_PRESS_MS = 550
SCROLL_THRESHOLD_PX = 5
KINETIC_DECAY = 0.86
KINETIC_MIN_V = 1.5
KINETIC_TICK_MS = 30


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ================= 字体 =================
class Fonts:
    def __init__(self, root, scale):
        fam = set(tkfont.families(root))
        self.cjk = next((f for f in CJK_FONTS if f in fam), "TkDefaultFont")
        self.mono = next((f for f in MONO_FONTS if f in fam), self.cjk)
        self.s = scale
        self.corr = self._calibrate(root, self.cjk)
        self._cache = {}

    @staticmethod
    def _calibrate(root, family):
        """实测"请求像素字号 → 实际渲染高度"的比例，兼容 HiDPI 缩放。"""
        try:
            cv = tk.Canvas(root, width=8, height=8)
            item = cv.create_text(0, 0, text="汉Ag", anchor="nw",
                                  font=(family, -100))
            box = cv.bbox(item)
            cv.destroy()
            if box and box[3] > box[1]:
                return max(0.5, (box[3] - box[1]) / 100.0)
        except Exception:
            pass
        return 1.4

    def _sz(self, n):
        px = n * 1.35 * self.s / self.corr
        return -max(4, int(round(px)))

    def f(self, n, bold=False):
        key = ("f", n, bold)
        r = self._cache.get(key)
        if r is None:
            r = ((self.cjk, self._sz(n), "bold") if bold
                 else (self.cjk, self._sz(n)))
            self._cache[key] = r
        return r

    def m(self, n, bold=False):
        key = ("m", n, bold)
        r = self._cache.get(key)
        if r is None:
            r = ((self.mono, self._sz(n), "bold") if bold
                 else (self.mono, self._sz(n)))
            self._cache[key] = r
        return r


# ================= 触摸控件 =================
class Btn(tk.Label):
    """轻量按钮：按下高亮、拖出（出框）取消、松手在框内才回调。

    触摸要点：
    - 用 <ButtonPress-1> 而不是 <Button-1>，按下立即出反馈
    - 松手时检查 e.x/e.y 仍在控件内才触发回调（拖出=取消）
    - <Leave> 仅清背景，不会触发回调
    """

    def __init__(self, master, text="", command=None, *, bg=PANEL, fg=FG,
                 font=None, padx=6, pady=3, **kw):
        super().__init__(master, text=text, bg=bg, fg=fg, font=font,
                         padx=padx, pady=pady, **kw)
        self._command = command
        self._bg = bg
        self._armed = False
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        # Leave/Enter 仅用于清掉武装态的视觉残留，不参与回调判定
        self.bind("<Leave>", self._on_leave)
        self.bind("<Enter>", self._on_enter)

    def _press(self, _e):
        self._armed = True
        if self._command:
            self.configure(bg=SEL)

    def _on_enter(self, _e):
        # 按住期间滑回按钮 → 恢复高亮；只是 hover 不做提示（触屏防闪）
        if self._armed:
            self.configure(bg=SEL)

    def _on_leave(self, _e):
        # 仅恢复背景；是否命中由松手坐标决定（电阻屏抖动 ±几像素）
        self.configure(bg=self._bg)

    def _release(self, e):
        # 触摸容差：电阻屏松开时坐标会抖 ±几像素，出框一点也算点中
        slop = 8
        inside = (-slop <= e.x <= self.winfo_width() + slop
                  and -slop <= e.y <= self.winfo_height() + slop)
        self._armed = False
        self.configure(bg=self._bg)
        # 行内滚动后（VScroll 标记 _vscrolled），即使坐标仍在容差内也不触发
        if self._command and inside and not getattr(self, "_vscrolled", False):
            self._command()

    def set_text(self, text):
        self.configure(text=text)

    def set_bg(self, bg):
        self._bg = bg
        self.configure(bg=bg)


class VScroll(tk.Frame):
    """Canvas 版滚动容器：触摸拖动 / 抬起点击 / 长按 / 惯性 / 滚轮。

    bind_row 行级回调：
    - on_tap(w)：按下未移动就抬起时触发
    - on_long(w)：按下超过 LONG_PRESS_MS（且期间未超过 SCROLL_THRESHOLD_PX）
    - 移动超过 SCROLL_THRESHOLD_PX 则视作滚动：取消长按 + 触发拖动
    """

    def __init__(self, master, bg=BG):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<MouseWheel>", lambda e: self.scroll_px(-e.delta / 2))
        self.canvas.bind("<Button-4>", lambda e: self.scroll_px(-24))
        self.canvas.bind("<Button-5>", lambda e: self.scroll_px(24))
        self._py = 0
        self._moved = False
        self._job = None
        self._v = 0.0
        self._kin = None
        # 背景空白处也能拖动（无子控件的区域）
        self.bind_row(self.canvas, recursive=False)

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()

    def scroll_px(self, dy):
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        total = bbox[3] - bbox[1]
        vis = self.canvas.winfo_height()
        if total <= vis:
            return
        top = _clamp(self.canvas.canvasy(0) - dy, 0, total - vis)
        self.canvas.yview_moveto(top / total)

    def bind_row(self, widget, on_tap=None, on_long=None, recursive=True):
        widgets = [widget]
        if recursive:
            stack = list(widget.winfo_children())
            while stack:
                w = stack.pop()
                widgets.append(w)
                stack.extend(w.winfo_children())
        for w in widgets:
            w._tap_cb = on_tap
            w._long_cb = on_long
            w.bind("<ButtonPress-1>", self._row_press, add="+")
            w.bind("<B1-Motion>", self._row_motion, add="+")
            w.bind("<ButtonRelease-1>", self._row_release, add="+")

    def bind_area(self, widget, recursive=True):
        """纯展示内容（聊天气泡/详情行/提示标签）：只加拖动+惯性，无点击回调。"""
        self.bind_row(widget, on_tap=None, on_long=None, recursive=recursive)

    def _row_press(self, e):
        self._cancel_job()
        self._cancel_kinetic()
        self._py = e.y_root
        self._moved = False
        self._v = 0.0
        w = e.widget
        w._vscrolled = False  # 与 Btn 协作：滚动时不误触发按钮
        if getattr(w, "_long_cb", None):
            self._job = w.after(LONG_PRESS_MS, lambda: self._fire_long(w))

    def _fire_long(self, w):
        self._moved = True
        self._cancel_job()
        cb = getattr(w, "_long_cb", None)
        try:
            if cb and w.winfo_exists():
                cb(w)
        except Exception:
            pass

    def _row_motion(self, e):
        dy = e.y_root - self._py
        if abs(dy) >= SCROLL_THRESHOLD_PX:
            self._cancel_job()
            self._moved = True
            e.widget._vscrolled = True
            self.scroll_px(dy)
            self._v = KINETIC_DECAY * self._v + (1 - KINETIC_DECAY) * dy
            self._py = e.y_root

    def _row_release(self, e):
        self._cancel_job()
        w = e.widget
        cb = getattr(w, "_tap_cb", None)
        if cb and not self._moved:
            try:
                if w.winfo_exists():
                    cb(w)
            except Exception:
                pass
        elif self._moved and abs(self._v) >= 4:
            self._kinetic_step()

    def _cancel_kinetic(self):
        if self._kin is not None:
            try:
                self.after_cancel(self._kin)
            except Exception:
                pass
            self._kin = None

    def _kinetic_step(self):
        self._kin = None
        self._v *= KINETIC_DECAY
        if abs(self._v) < KINETIC_MIN_V:
            self._v = 0.0
            return
        self.scroll_px(self._v)
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        total = bbox[3] - bbox[1]
        vis = self.canvas.winfo_height()
        top = self.canvas.canvasy(0) - self._v
        if top <= 0 or top >= total - vis:
            self._v = 0.0
            return
        self._kin = self.after(KINETIC_TICK_MS, self._kinetic_step)

    def _cancel_job(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None


# ================= 弹窗 =================
class Modal(tk.Toplevel):
    """无边框弹窗。80ms 后再 grab，避免 fbdev / 轻量 WM 上"not viewable"。

    touch 行为：esc 关闭（接 USB 键盘时）；松手事件不会落空。
    """

    def __init__(self, app, w, h, title=""):
        super().__init__(app.root)
        self.app = app
        self.overrideredirect(True)
        self.configure(bg=LINE)
        w, h = int(w), int(h)
        x = app.root.winfo_rootx() + max(0, (app.W - w) // 2)
        y = app.root.winfo_rooty() + max(0, (app.H - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.transient(app.root)
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        if title:
            tk.Label(self.body, text=title, bg=PANEL, fg=FG,
                     font=app.fnt.f(10, True)).pack(fill="x", ipady=3)
        self.bind("<Escape>", lambda e: self.close())
        self.after(80, self._grab_when_viewable)

    def _grab_when_viewable(self, tries=0):
        try:
            if self.winfo_viewable():
                try:
                    self.grab_set()
                except tk.TclError:
                    pass
                return
        except tk.TclError:
            return
        if tries < 25:
            self.after(120, lambda: self._grab_when_viewable(tries + 1))

    def _stop_keyboard(self):
        kb = getattr(self, "_kb", None)
        if kb:
            try:
                kb.terminate()
            except Exception:
                pass
            self._kb = None

    def close(self):
        self._stop_keyboard()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class ConfirmDialog(Modal):
    def __init__(self, app, text, on_ok, ok_text="确定"):
        super().__init__(app, app.W - 30, 120, "请确认")
        tk.Label(self.body, text=text, bg=BG, fg=FG, wraplength=app.W - 46,
                 justify="left", font=app.fnt.f(10)).pack(
                     fill="both", expand=True, padx=8, pady=6)
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill="x", pady=4)
        Btn(bar, "取消", self.close, font=app.fnt.f(10),
            bg=PANEL2, padx=10, pady=5).pack(side="left", expand=True, padx=6)
        Btn(bar, ok_text, lambda: (self.close(), on_ok()),
            font=app.fnt.f(10, True), bg=ACCENT, fg="#08101c",
            padx=10, pady=5).pack(side="right", expand=True, padx=6)


class TextDialog(Modal):
    """文本输入框：小屏自带触摸键盘（数字/字母/常用符号）。"""

    KB_ROWS = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm.-_")

    def __init__(self, app, title, initial, on_ok, show=None, hint=""):
        kb = app.W <= 480
        self._row_h = int(24 * app.scale)
        self._bot_h = int(28 * app.scale)
        kb_h = 0 if not kb else (len(self.KB_ROWS) * self._row_h + self._bot_h)
        super().__init__(app, min(app.W - 16, 330),
                         110 + kb_h, title)
        self.app = app
        self.on_ok = on_ok
        if hint:
            tk.Label(self.body, text=hint, bg=BG, fg=DIM,
                     font=app.fnt.f(8), wraplength=app.W - 44).pack(
                         fill="x", padx=8, pady=(3, 0))
        self.var = tk.StringVar(value=initial or "")
        ent = tk.Entry(self.body, textvariable=self.var, show=show or "",
                       bg=PANEL2, fg=FG, insertbackground=FG,
                       font=app.fnt.f(11), relief="flat",
                       highlightthickness=0)
        ent.pack(fill="x", padx=8, pady=5, ipady=3)
        ent.focus_set()
        if kb:
            self._build_kb()
        else:
            bar = tk.Frame(self.body, bg=BG)
            bar.pack(fill="x", pady=4)
            Btn(bar, "取消", self.close, font=app.fnt.f(10),
                bg=PANEL2, padx=10, pady=5).pack(side="left", expand=True,
                                                 padx=6)
            Btn(bar, "保存", self._ok,
                font=app.fnt.f(10, True), bg=ACCENT, fg="#08101c",
                padx=10, pady=5).pack(side="right", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self._ok())
        self.after(200, lambda: ent.focus_force())

    def _ok(self):
        self.on_ok(self.var.get())
        self.close()

    def _build_kb(self):
        self._shift = False
        self._letters = {}
        holder = tk.Frame(self.body, bg=BG)
        holder.pack(fill="both", expand=True, padx=4, pady=(0, 3))
        for chars in self.KB_ROWS:
            row = tk.Frame(holder, bg=BG, height=self._row_h)
            row.pack(fill="x")
            row.pack_propagate(False)
            for ch in chars:
                b = Btn(row, ch, lambda c=ch: self._key(c),
                        font=self.app.fnt.f(9), bg=PANEL2)
                b.pack(side="left", fill="both", expand=True, padx=1, pady=1)
                if ch.isalpha():
                    self._letters[ch] = b
        bot = tk.Frame(holder, bg=BG, height=self._bot_h)
        bot.pack(fill="x")
        bot.pack_propagate(False)
        specs = (("取消", self.close), ("⇧", self._toggle_shift),
                 ("空格", lambda: self._key(" ")),
                 ("⌫", lambda: self.var.set(self.var.get()[:-1])),
                 ("保存", self._ok))
        for label, cmd in specs:
            bg = ACCENT if label == "保存" else PANEL2
            fg = "#08101c" if label == "保存" else FG
            Btn(bot, label, cmd, font=self.app.fnt.f(10, label == "保存"),
                bg=bg, fg=fg).pack(side="left", fill="both", expand=True,
                                   padx=1, pady=1)

    def _key(self, ch):
        if self._shift and ch.isalpha():
            ch = ch.upper()
        self.var.set(self.var.get() + ch)

    def _toggle_shift(self):
        self._shift = not self._shift
        for ch, b in self._letters.items():
            b.set_text(ch.upper() if self._shift else ch)


class NumPadDialog(Modal):
    def __init__(self, app, title, initial, on_ok, hint=""):
        super().__init__(app, app.W - 24, 236, title)
        if hint:
            tk.Label(self.body, text=hint, bg=BG, fg=DIM,
                     font=app.fnt.f(8)).pack(fill="x", padx=8, pady=(3, 0))
        self.var = tk.StringVar(value=initial or "")
        tk.Label(self.body, textvariable=self.var, bg=PANEL2, fg=FG,
                 font=app.fnt.m(14, True), anchor="e").pack(
                     fill="x", padx=8, pady=4, ipady=3)
        grid = tk.Frame(self.body, bg=BG)
        grid.pack(fill="both", expand=True, padx=6)
        keys = (("1", "2", "3", "4"), ("5", "6", "7", "8"),
                ("9", "0", "⌫", "确定"))
        for r, row in enumerate(keys):
            grid.rowconfigure(r, weight=1)
            for c, k in enumerate(row):
                grid.columnconfigure(c, weight=1, uniform="k")
                if k == "确定":
                    cmd = lambda: (on_ok(self.var.get()), self.close())
                    b = Btn(grid, k, cmd, font=app.fnt.f(10, True),
                            bg=ACCENT, fg="#08101c")
                elif k == "⌫":
                    cmd = lambda: self.var.set(self.var.get()[:-1])
                    b = Btn(grid, k, cmd, font=app.fnt.f(12), bg=PANEL2)
                else:
                    cmd = lambda kk=k: self.var.set(self.var.get() + kk)
                    b = Btn(grid, k, cmd, font=app.fnt.f(13), bg=PANEL)
                b.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)


class SheetDialog(Modal):
    def __init__(self, app, title, items):
        super().__init__(app, app.W - 40, 40 + 34 * len(items) + 40, title)
        for label, cb in items:
            Btn(self.body, label, lambda f=cb: (self.close(), f()),
                font=app.fnt.f(10), bg=PANEL).pack(
                    fill="x", padx=8, pady=2, ipady=5)
        Btn(self.body, "取消", self.close, font=app.fnt.f(10),
            bg=PANEL2).pack(fill="x", padx=8, pady=(6, 2), ipady=5)