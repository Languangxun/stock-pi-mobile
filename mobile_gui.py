#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock-pi-mobile · 2.4寸触摸屏股票终端（移动端 GUI）

定位：随身盯盘终端，代替手机。
只做界面：行情/预测/荐股/AI 全部复用 stock_predict.py（与 stock_gui.py 同源生成物）。
适配 240x320 竖屏 / 320x240 横屏（自动检测，也可 --size 强制预览）。

模块拆分（2024 重构）：
  mobile_gui.py   入口 + App（页面组装 / 生命周期 / 任务泵）
  widgets.py      触摸控件 Btn / VScroll / 弹窗 / 字体
  helpers.py      后端加载 / 格式化 / WiFi(nmcli)

用法：
  python3 mobile_gui.py --check                 # 只检查后端/数据，不开界面
  python3 mobile_gui.py                         # 小屏自动全屏；桌面默认 240x320 预览窗
  python3 mobile_gui.py --size 320x240          # 桌面横屏预览
  python3 mobile_gui.py --fullscreen            # 真机全屏（Pi 启动脚本用）
  python3 mobile_gui.py --backend ~/stock_predict   # 指定后端目录

后端查找顺序：--backend > 环境变量 STOCK_BACKEND > 本目录 > 常见路径。
后端目录需含 stock_predict.py（stock_cache.db / stock_gui.ini 同目录）。
"""
import argparse
import configparser
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor

from helpers import (find_backend, load_backend, clamp, fmt_price, fmt_pct,
                     trunc, human_size, market_state, wifi_available,
                     wifi_status, wifi_known, wifi_scan, wifi_connect,
                     wifi_disconnect, wifi_set_static, wifi_set_dhcp,
                     mem_avail_mb)
from widgets import (BG, PANEL, PANEL2, ROW_ALT, FG, DIM, LINE, ACCENT,
                     ACCENT_DK, UP, DOWN, WARN, SEL, Fonts, Btn, VScroll,
                     Modal, ConfirmDialog, TextDialog, NumPadDialog,
                     SheetDialog)

APP = "stock-pi-mobile"
VERSION = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(HERE, "stock_mobile.ini")

TIER_LIST = ("稳健", "均衡", "激进")
UNIVERSE_LIST = ("all", "main", "etf", "all_etf")
UNIVERSE_NAME = {"all": "全A", "main": "沪深主板", "etf": "仅ETF",
                 "all_etf": "全A含ETF"}
MODEL_LIST = ("deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro",
              "deepseek-v4.1-flash", "kimi-k3")


class App:
    REFRESH_MS = 30000

    def __init__(self, args):
        self.args = args
        self.cfg = configparser.ConfigParser(interpolation=None)
        try:
            self.cfg.read(INI_PATH, encoding="utf-8")
        except Exception:
            pass
        backend_path = find_backend(args.backend,
                                    self._ini("backend", "dir", ""))
        if not backend_path:
            raise SystemExit(
                "未找到 stock_predict.py。请把后端目录放到本目录，或用 "
                "--backend DIR / 环境变量 STOCK_BACKEND 指定。")
        self.backend_path = backend_path
        self.sp = load_backend(backend_path)
        self._sync_ai_env()

        self.root = tk.Tk()
        self.root.title(APP)
        self._setup_root()
        self.fnt = Fonts(self.root, self.scale)
        self.updown = self._ini("ui", "updown", "red_up")
        self.watchlist = self._load_watchlist()
        self.names = {}
        self.quotes = {}
        self.qrows = {}
        self.idx_cells = {}
        self.tokens = {}
        self.jobs = {}
        self.q = queue.Queue()
        self.detail_res = None
        self.detail_code = ""
        self.picks_data = {}
        self.picks_tier = self._ini("picks", "tier", "均衡")
        if self.picks_tier not in TIER_LIST:
            self.picks_tier = "均衡"
        self.ai_msgs = self._load_ai_session()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._pump)
        self.root.after(120, self._clock)
        self._show("watch")
        self._after_start()

    # ---------- 配置 ----------
    def _ini(self, section, key, default=""):
        try:
            return self.cfg.get(section, key, fallback=default).strip()
        except Exception:
            return default

    def _save_ini(self):
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(INI_PATH, encoding="utf-8")
        except Exception:
            pass
        for sec in ("backend", "ui", "watchlist", "picks", "deepseek",
                    "wifi"):
            if not cp.has_section(sec):
                cp.add_section(sec)
        cp.set("backend", "dir", self._ini("backend", "dir", "") or
               os.path.dirname(self.backend_path))
        cp.set("ui", "updown", self.updown)
        cp.set("ui", "tab", self.screen_name)
        cp.set("watchlist", "codes", ",".join(self.watchlist))
        cp.set("picks", "tier", self.picks_tier)
        for k in ("capital", "risk_pref", "universe"):
            cp.set("picks", k, self._ini("picks", k, self._picks_default(k)))
        cp.set("deepseek", "api_key", self._ini("deepseek", "api_key", ""))
        cp.set("deepseek", "model", self._ini("deepseek", "model", ""))
        cp.set("deepseek", "base_url", self._ini("deepseek", "base_url", ""))
        for k in ("ssid", "password", "static_ip", "gateway"):
            cp.set("wifi", k, self._ini("wifi", k, self._wifi_default(k)))
        try:
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
        except OSError:
            pass

    def _picks_default(self, key):
        return {"capital": "100000", "risk_pref": "均衡",
                "universe": "all"}.get(key, "")

    def _wifi_default(self, key):
        return {"ssid": "", "password": "", "static_ip": "",
                "gateway": ""}.get(key, "")

    def _sync_ai_env(self):
        key = self._ini("deepseek", "api_key", "")
        if key:
            self.sp.ENV_API_KEY = key
        m = self._ini("deepseek", "model", "")
        if m:
            self.sp.AI_MODEL = m
        b = self._ini("deepseek", "base_url", "")
        if b:
            self.sp.AI_BASE_URL = b.rstrip("/")

    def _load_watchlist(self):
        codes = [c.strip() for c in
                 self._ini("watchlist", "codes", "").split(",") if c.strip()]
        if codes:
            return codes
        try:
            codes = [c.strip() for c in self.sp._ai_ini_get(
                "watchlist", "codes", "").split(",") if c.strip()]
        except Exception:
            codes = []
        return codes or ["sz000725"]

    def _load_ai_session(self):
        try:
            d = self.sp.ai_session_load("mobile", self.sp.AI_MODEL)
            msgs = (d or {}).get("msgs") or []
            return [m for m in msgs if isinstance(m, dict)]
        except Exception:
            return []

    # ---------- 窗口与布局 ----------
    def _setup_root(self):
        self.root.configure(bg=BG)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        full = False
        if self.args.size:
            w, h = (int(x) for x in self.args.size.lower().split("x"))
        elif self.args.fullscreen or sw <= 700 or sh <= 500:
            w, h, full = sw, sh, True
        else:
            w, h = 240, 320
        self.W, self.H = w, h
        self.portrait = h >= w
        self.scale = clamp(min(w, h) / 240.0, 0.72, 1.3)
        s = self.scale
        self.status_h = max(17, int(19 * s))
        self.nav_h = max(36, int((48 if self.portrait else 38) * s))
        self.row_h = max(30, int((44 if self.portrait else 34) * s))
        self.tool_h = max(24, int(28 * s))
        self.ind_cell_h = max(30, int((36 if self.portrait else 42) * s))
        if full:
            self.root.attributes("-fullscreen", True)
        else:
            self.root.geometry(f"{w}x{h}")
            self.root.resizable(False, False)
        if self.args.nocursor:
            self.root.configure(cursor="none")

    def _build(self):
        self.status = tk.Frame(self.root, bg=PANEL, height=self.status_h)
        self.status.pack(fill="x")
        self.status.pack_propagate(False)
        self.lbl_clock = tk.Label(self.status, text="--:--", bg=PANEL,
                                  fg=DIM, font=self.fnt.m(8))
        self.lbl_clock.pack(side="left", padx=4)
        self.lbl_state = tk.Label(self.status, text="--", bg=PANEL,
                                  fg=DIM, font=self.fnt.f(8))
        self.lbl_state.pack(side="right", padx=4)
        self.lbl_status = tk.Label(self.status, text="", bg=PANEL, fg=WARN,
                                   font=self.fnt.f(8), anchor="center")
        self.lbl_status.pack(side="left", fill="x", expand=True)

        self.nav = tk.Frame(self.root, bg=PANEL, height=self.nav_h)
        self.nav.pack(fill="x", side="bottom")
        self.nav.pack_propagate(False)
        self.nav_btns = {}
        for key, label in (("watch", "自选"), ("picks", "荐股"),
                           ("ai", "AI"), ("settings", "设置")):
            b = Btn(self.nav, label, lambda k=key: self._show(k),
                    font=self.fnt.f(10, True), bg=PANEL, fg=DIM)
            b.pack(side="left", fill="both", expand=True)
            self.nav_btns[key] = b

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill="both", expand=True)
        self.screens = {}
        for name, builder in (("watch", self._build_watch),
                              ("picks", self._build_picks),
                              ("ai", self._build_ai),
                              ("settings", self._build_settings)):
            f = tk.Frame(self.content, bg=BG)
            f.place(x=0, y=0, relwidth=1, relheight=1)
            builder(f)
            self.screens[name] = f

        self.detail = tk.Frame(self.root, bg=BG)
        self._build_detail(self.detail)

        self.wifi_page = tk.Frame(self.root, bg=BG)
        self._build_wifi(self.wifi_page)

        self.toast_lbl = tk.Label(self.root, text="", bg=ACCENT_DK, fg=FG,
                                  font=self.fnt.f(9), padx=8, pady=3)

    def _show(self, name):
        self.screen_name = name
        for pg in (getattr(self, "detail", None),
                   getattr(self, "wifi_page", None)):
            if pg is not None and pg.winfo_ismapped():
                pg.place_forget()
        for k, f in self.screens.items():
            if k == name:
                f.lift()
            else:
                f.lower()
        for k, b in self.nav_btns.items():
            b.set_bg(SEL if k == name else PANEL)
            b.configure(fg=FG if k == name else DIM)
        if name == "watch":
            self._auto_quotes()
        elif name == "picks":
            self.refresh_picks()
        elif name == "settings":
            self._load_data_status()

    # ---------- 任务/事件 ----------
    def submit(self, key, fn, on_done=None, on_error=None):
        tok = object()
        self.tokens[key] = tok

        def work():
            def prog(msg):
                self.q.put(("prog", key, msg, tok))
            try:
                r = fn(prog)
            except Exception as e:
                self.q.put(("err", key, e, tok))
            else:
                self.q.put(("ok", key, r, tok))

        self.jobs[key] = (on_done, on_error)
        threading.Thread(target=work, daemon=True,
                         name=f"task-{key}").start()

    def _pump(self):
        got = False
        try:
            while True:
                kind, key, val, tok = self.q.get_nowait()
                got = True
                if kind == "prog":
                    if self.tokens.get(key) is tok:
                        self.set_status(str(val))
                    continue
                if self.tokens.get(key) is not tok:
                    continue
                done, err = self.jobs.pop(key, (None, None))
                if kind == "ok":
                    self.set_status("")
                    if done:
                        done(val)
                else:
                    self.set_status("")
                    if err:
                        err(val)
                    else:
                        self.toast(f"失败：{trunc(str(val), 24)}")
        except queue.Empty:
            pass
        # 空闲时降频，减少定时器唤醒（小内存设备省电/省 CPU）
        busy = bool(self.jobs)
        delay = 40 if (got and busy) else (80 if busy else 200)
        self.root.after(delay, self._pump)

    def set_status(self, text):
        self.lbl_status.configure(text=trunc(text, int(self.W / (6.4 * self.scale))))

    def toast(self, text, ms=2000):
        self.toast_lbl.configure(text=trunc(text, 26))
        self.toast_lbl.place(relx=0.5, y=self.status_h + 6, anchor="n")
        self.toast_lbl.lift()
        if getattr(self, "_toast_job", None):
            try:
                self.root.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.root.after(ms, self.toast_lbl.place_forget)

    def _clock(self):
        st, color = market_state()
        self.lbl_clock.configure(text=time.strftime("%H:%M"))
        self.lbl_state.configure(text=st, fg=color)
        self.root.after(10000, self._clock)

    def _on_close(self):
        self._save_ini()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ================= 自选屏 =================
    def _build_watch(self, scr):
        self.idx_frame = tk.Frame(scr, bg=BG)
        self.idx_frame.pack(fill="x")
        cells = list(self.sp.INDEX_CODES) + [(None, "自选均涨")]
        if self.portrait:
            cols, rows = 3, 2
        else:
            cols, rows = 6, 1
        for i, (code, name) in enumerate(cells):
            r, c = divmod(i, cols)
            cell = tk.Frame(self.idx_frame, bg=PANEL if (r + c) % 2 == 0
                            else PANEL2, height=self.ind_cell_h,
                            highlightbackground=BG, highlightthickness=1)
            cell.grid(row=r, column=c, sticky="nsew")
            cell.pack_propagate(False)
            self.idx_frame.grid_columnconfigure(c, weight=1, uniform="ic")
            self.idx_frame.grid_rowconfigure(r, weight=1, uniform="ir")
            lbl_n = tk.Label(cell, text=name, bg=cell["bg"], fg=DIM,
                             font=self.fnt.f(7), anchor="n")
            lbl_p = tk.Label(cell, text="--", bg=cell["bg"], fg=FG,
                             font=self.fnt.m(8, True), anchor="n")
            lbl_c = tk.Label(cell, text="--", bg=cell["bg"], fg=DIM,
                             font=self.fnt.m(7), anchor="n")
            lbl_n.place(x=0, y=0, relwidth=1)
            if self.portrait:
                lbl_p.place(x=4, y=self.ind_cell_h // 2 - 4)
                lbl_c.place(relx=1.0, x=-4, y=self.ind_cell_h // 2 - 4,
                            anchor="ne")
            else:
                lbl_p.place(x=3, y=12)
                lbl_c.place(relx=1.0, x=-3, y=25, anchor="ne")
            self.idx_cells[code or "avg"] = (lbl_p, lbl_c)

        bar = tk.Frame(scr, bg=BG, height=self.tool_h)
        bar.pack(fill="x", pady=1)
        bar.pack_propagate(False)
        Btn(bar, "＋ 加自选", self.add_watch_dialog, font=self.fnt.f(10, True),
            bg=ACCENT, fg="#08101c", padx=8).pack(
                side="left", fill="y", padx=(3, 2))
        Btn(bar, "刷新行情", lambda: self.refresh_quotes(True),
            font=self.fnt.f(9), bg=PANEL).pack(
                side="left", fill="y", padx=2)
        self.lbl_watch_n = tk.Label(bar, text="", bg=BG, fg=DIM,
                                    font=self.fnt.f(8))
        self.lbl_watch_n.pack(side="right", padx=4)

        self.watch_scroll = VScroll(scr, bg=BG)
        self.watch_scroll.pack(fill="both", expand=True)

    def _render_watch(self):
        vs = self.watch_scroll
        vs.clear()
        self.qrows = {}
        if not self.watchlist:
            lbl = tk.Label(vs.inner, text="点【＋ 加自选】添加股票代码",
                           bg=BG, fg=DIM, font=self.fnt.f(10))
            lbl.pack(pady=20)
            vs.bind_area(lbl)
        for i, code in enumerate(self.watchlist):
            name = self.names.get(code, "") or code[-6:]
            bg = ROW_ALT if i % 2 else PANEL
            row = tk.Frame(vs.inner, bg=bg, height=self.row_h)
            row.pack(fill="x", pady=1)
            row.pack_propagate(False)
            top = tk.Frame(row, bg=bg, height=self.row_h // 2)
            top.pack(fill="x")
            top.pack_propagate(False)
            bot = tk.Frame(row, bg=bg)
            bot.pack(fill="both", expand=True)
            tk.Label(top, text=trunc(name, 7), bg=bg, fg=FG,
                     font=self.fnt.f(10, True), anchor="w").pack(
                         side="left", padx=4)
            pl = tk.Label(top, text="--", bg=bg, fg=FG,
                          font=self.fnt.m(11, True), anchor="e")
            pl.pack(side="right", padx=4)
            tk.Label(bot, text=code, bg=bg, fg=DIM,
                     font=self.fnt.m(8), anchor="w").pack(
                         side="left", padx=4)
            hl = tk.Label(bot, text="--", bg=bg, fg=DIM,
                          font=self.fnt.m(9), anchor="e")
            hl.pack(side="right", padx=4)
            vs.bind_row(row, on_tap=lambda w, c=code: self.open_detail(c),
                        on_long=lambda w, c=code: self._watch_actions(c))
            self.qrows[code] = (pl, hl)
        self.lbl_watch_n.configure(text=f"{len(self.watchlist)}只")
        self._apply_quotes()

    def _watch_actions(self, code):
        name = self.names.get(code, code[-6:])

        def rm():
            if code in self.watchlist:
                self.watchlist.remove(code)
                self._save_ini()
                self._render_watch()

        def top():
            if code in self.watchlist:
                self.watchlist.remove(code)
                self.watchlist.insert(0, code)
                self._save_ini()
                self._render_watch()

        SheetDialog(self, f"{name} {code}",
                    [("置顶", top), ("删除自选", rm)])

    def add_watch_dialog(self):
        def ok(text):
            code = (text or "").strip()
            if not code:
                return
            try:
                full = self.sp.normalize_code(code)
            except Exception as e:
                self.toast(f"代码有误：{e}")
                return
            if full not in self.watchlist:
                self.watchlist.append(full)
                self._save_ini()
                self._load_names()
                self._render_watch()
                self.refresh_quotes()
            self.toast(f"已加入 {full}")

        NumPadDialog(self, "添加自选", "", ok, hint="输入 6 位股票/ETF 代码")

    # ---------- 行情 ----------
    def _load_names(self):
        codes = list(self.watchlist)

        def work(_p):
            out = {}
            try:
                with self.sp.db_conn() as conn:
                    ph = ",".join("?" for _ in codes)
                    for c, n in conn.execute(
                            f"select code,name from stocks where code in ({ph})",
                            codes):
                        out[c] = n or ""
            except Exception:
                pass
            return out

        self.submit("names", work, on_done=self._on_names,
                    on_error=lambda e: None)

    def _on_names(self, out):
        if out:
            self.names.update(out)
            self._render_watch()

    def refresh_quotes(self, show=False):
        codes = list(self.watchlist)
        idx = [c for c, _ in self.sp.INDEX_CODES]
        if not codes and not idx:
            return
        if show:
            self.set_status("拉取行情…")

        def work(_p):
            out = {"quotes": {}, "idx": {}}
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = {c: ex.submit(self.sp.fetch_quote, c)
                        for c in codes + idx}
                for c, f in futs.items():
                    try:
                        out["quotes" if c in codes else "idx"][c] = f.result(
                            timeout=10)
                    except Exception:
                        pass
            return out

        self.submit("quotes", work, on_done=self._on_quotes,
                    on_error=lambda e: show and self.toast(
                        f"行情失败：{trunc(str(e), 18)}"))

    def _on_quotes(self, out):
        self.quotes.update(out.get("quotes") or {})
        self.idx_quotes = out.get("idx") or {}
        self._apply_quotes()
        if self.screen_name == "picks":
            pass
        self._apply_index()

    def _apply_quotes(self):
        chgs = []
        for code, (pl, hl) in self.qrows.items():
            q = self.quotes.get(code)
            if not q:
                continue
            chg = ((q["price"] / q["prev_close"] - 1) * 100
                   if q.get("prev_close") else None)
            if chg is not None:
                chgs.append(chg)
            pl.configure(text=fmt_price(q["price"]),
                         fg=self._color_of(chg))
            hl.configure(text=fmt_pct(chg) if chg is not None else "--",
                         fg=self._color_of(chg))
        if chgs:
            self._set_avg(sum(chgs) / len(chgs))
        q = self.quotes.get(self.detail_code)
        if q and self.detail_code:
            self._detail_header(q, self.detail_res)

    def _set_avg(self, avg):
        cell = self.idx_cells.get("avg")
        if cell:
            cell[0].configure(text=f"{avg:+.2f}%", fg=self._color_of(avg))
            cell[1].configure(text="")

    def _apply_index(self):
        for code, _name in self.sp.INDEX_CODES:
            q = getattr(self, "idx_quotes", {}).get(code)
            cell = self.idx_cells.get(code)
            if not q or not cell:
                continue
            chg = ((q["price"] / q["prev_close"] - 1) * 100
                   if q.get("prev_close") else None)
            cell[0].configure(text=fmt_price(q["price"]),
                              fg=self._color_of(chg))
            cell[1].configure(text=fmt_pct(chg) if chg is not None else "--",
                              fg=self._color_of(chg))

    def _color_of(self, v):
        if v is None:
            return DIM
        up = UP if self.updown == "red_up" else DOWN
        dn = DOWN if self.updown == "red_up" else UP
        return up if v >= 0 else dn

    def _auto_quotes(self):
        if getattr(self, "_auto_job", None):
            try:
                self.root.after_cancel(self._auto_job)
            except Exception:
                pass
        self.refresh_quotes()
        self._auto_job = self.root.after(self.REFRESH_MS, self._auto_quotes)

    # ================= 详情屏 =================
    def _build_detail(self, scr):
        head = tk.Frame(scr, bg=PANEL, height=self.status_h + 8)
        head.pack(fill="x")
        head.pack_propagate(False)
        Btn(head, "‹", self.close_detail, font=self.fnt.f(15, True),
            bg=PANEL, padx=6).pack(side="left", fill="y")
        self.d_lbl_name = tk.Label(head, text="--", bg=PANEL, fg=FG,
                                   font=self.fnt.f(11, True), anchor="w")
        self.d_lbl_name.pack(side="left", padx=2)
        self.d_lbl_price = tk.Label(head, text="--", bg=PANEL, fg=FG,
                                    font=self.fnt.m(12, True), anchor="e")
        self.d_lbl_price.pack(side="right", padx=5)
        self.d_lbl_chg = tk.Label(head, text="", bg=PANEL, fg=DIM,
                                  font=self.fnt.m(9), anchor="e")
        self.d_lbl_chg.pack(side="right")

        self.d_canvas = tk.Canvas(scr, bg=PANEL2, highlightthickness=0, bd=0,
                                  height=self._chart_h())
        self.d_canvas.pack(fill="x", padx=3, pady=(2, 1))
        self.d_canvas.bind("<Configure>", lambda e: self._paint_chart())

        bar = tk.Frame(scr, bg=BG, height=self.tool_h)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.d_btn_full = Btn(bar, "完整分析", lambda: self._detail_analyze(False),
                              font=self.fnt.f(9, True), bg=ACCENT,
                              fg="#08101c")
        self.d_btn_full.pack(side="left", fill="y", padx=(3, 2))
        Btn(bar, "AI问", self.detail_ai, font=self.fnt.f(9),
            bg=PANEL).pack(side="left", fill="y", padx=2)
        self.d_btn_watch = Btn(bar, "加自选", self.detail_toggle_watch,
                               font=self.fnt.f(9), bg=PANEL)
        self.d_btn_watch.pack(side="right", fill="y", padx=3)

        self.d_scroll = VScroll(scr, bg=BG)
        self.d_scroll.pack(fill="both", expand=True)

    def _chart_h(self):
        return max(56, int((96 if self.portrait else 70) * self.scale))

    def open_detail(self, code, name=""):
        self.detail_code = code
        self.detail_res = None
        self.d_lbl_name.configure(text=f"{name or self.names.get(code, code)} {code}")
        self.d_scroll.clear()
        self.detail.place(x=0, y=0, relwidth=1, relheight=1)
        self.detail.lift()
        q = self.quotes.get(code)
        if q:
            self._detail_header(q, None)
        # K线取数放后台：缓存过期时 get_daily 会联网，不能卡住触摸
        self.submit("detail_rows", lambda _p: self._detail_cached_rows(code),
                    on_done=lambda rows, c=code: self._on_detail_rows(c, rows),
                    on_error=lambda e: None)
        self._detail_analyze(True)

    def close_detail(self):
        self.tokens.pop("analyze", None)
        self.tokens.pop("detail_rows", None)
        self.detail.place_forget()

    def _on_detail_rows(self, code, rows):
        if code != self.detail_code:
            return
        self._draw_chart(rows, self.detail_res)

    def _detail_cached_rows(self, code):
        try:
            rows = self.sp.get_daily(code, min_bars=30, tail=70)
            return rows or []
        except Exception:
            return []

    def _detail_analyze(self, quick):
        code = self.detail_code
        if not code:
            return
        self.set_status("快速分析中…" if quick else "完整分析中(首次较慢)…")

        def work(prog):
            return self.sp.analyze(code, progress=prog, quick=quick)

        self.submit("analyze", work, on_done=lambda r, c=code: self._on_analyze(c, r),
                    on_error=lambda e, c=code: self._on_analyze_err(c, e))

    def _on_analyze(self, code, res):
        if code != self.detail_code:
            return
        self.detail_res = res
        self._detail_header(res.get("quote") or {}, res)
        self._draw_chart(res.get("disp_rows") or [], res)
        self._render_detail(res)

    def _on_analyze_err(self, code, err):
        if code != self.detail_code:
            return
        self.d_scroll.clear()
        tk.Label(self.d_scroll.inner,
                 text=f"分析失败：\n{trunc(str(err), 120)}\n\n"
                      "盘中需联网；可稍后点【完整分析】重试。",
                 bg=BG, fg=WARN, font=self.fnt.f(9), justify="left",
                 wraplength=self.W - 16).pack(padx=6, pady=10)
        self.toast("分析失败，检查网络")

    def _detail_header(self, q, res):
        if not q:
            return
        price = q.get("price")
        prev = q.get("prev_close")
        chg = ((price / prev - 1) * 100) if (price and prev) else None
        self.d_lbl_price.configure(text=fmt_price(price),
                                   fg=self._color_of(chg))
        self.d_lbl_chg.configure(text=fmt_pct(chg), fg=self._color_of(chg))
        if res:
            self.d_lbl_name.configure(
                text=trunc(f"{q.get('name', '')} {self.detail_code}", 16))
        in_w = self.detail_code in self.watchlist
        self.d_btn_watch.set_text("删自选" if in_w else "加自选")
        self.d_btn_watch.set_bg(PANEL2 if in_w else PANEL)

    def _render_detail(self, res):
        vs = self.d_scroll
        vs.clear()
        q = res.get("quote") or {}
        prev = res.get("prev_close")
        chg = ((q.get("price", 0) / prev - 1) * 100) if prev else None
        act = res.get("action") or {}
        tp = res.get("t_pred") or {}
        p10, p50, p90 = (tp.get("cl") or {}).get(10), \
            (tp.get("cl") or {}).get(50), (tp.get("cl") or {}).get(90)
        up = tp.get("up_prob")
        lines = []
        if act.get("verdict"):
            lines.append(("建议", f"{act['verdict']} ({act.get('score', 0):+d})",
                          UP if act.get("score", 0) > 0 else
                          (DOWN if act.get("score", 0) < 0 else WARN)))
        if p50 is not None:
            lines.append(("T+1预测",
                          f"{p50:.2f}  区间 {p10:.2f}~{p90:.2f}  "
                          f"上行概率 {(up * 100):.0f}%", FG))
        regime = res.get("cur_regime") or "--"
        vr = res.get("vr_now")
        lines.append(("量能", f"{regime}" + (f" 量比{vr:.2f}" if vr else ""), FG))
        ic = res.get("idx_chg_today")
        lines.append(("大盘", fmt_pct(ic) if ic is not None else "--",
                      self._color_of(ic)))
        sc = res.get("sector_chg_today")
        lines.append(("板块", f"{res.get('sector_name') or '--'} "
                              f"{fmt_pct(sc) if sc is not None else ''}",
                      self._color_of(sc)))
        if res.get("band_note"):
            lines.append(("策略", res["band_note"], DIM))
        sigs = res.get("signals") or []
        for i, day, typ, txt in sigs[-3:]:
            lines.append(("信号" if i == sigs[-1][0] else "",
                          f"{day} [{'买' if typ == 'BUY' else '卖'}] {txt}",
                          UP if typ == "BUY" else DOWN))
        bt = res.get("bt_stats")
        if bt and bt.get("winrate") is not None:
            lines.append(("回测",
                          f"{bt['trades']}笔 胜率{bt['winrate'] * 100:.0f}% "
                          f"年化{bt['ann'] * 100:+.0f}% "
                          f"回撤{bt['mdd'] * 100:.0f}%", DIM))
        items = act.get("items") or []
        if items:
            lines.append(("多维", " ".join(
                f"{lab}{'+' if s > 0 else ('-' if s < 0 else '')}"
                for lab, s, _ in items), DIM))
        for label, value, color in lines:
            row = tk.Frame(vs.inner, bg=PANEL)
            row.pack(fill="x", pady=1)
            if label:
                tk.Label(row, text=label, bg=PANEL, fg=DIM,
                         font=self.fnt.f(8), width=5, anchor="w").pack(
                             side="left", padx=(4, 0))
            tk.Label(row, text=value, bg=PANEL, fg=color,
                     font=self.fnt.f(9), justify="left", anchor="w",
                     wraplength=self.W - (52 if label else 14)).pack(
                         side="left", fill="x", expand=True, padx=4, pady=2)
            vs.bind_area(row)  # 详情行也能拖动滚动
        if res.get("quick"):
            lbl = tk.Label(vs.inner, text="※ 快速预览；点【完整分析】补全样本池",
                           bg=BG, fg=WARN, font=self.fnt.f(8))
            lbl.pack(anchor="w", padx=6, pady=4)
            vs.bind_area(lbl)

    def _draw_chart(self, rows, res=None):
        self._chart_rows = rows or []
        self._chart_res = res
        self._paint_chart()

    def _paint_chart(self):
        cv = self.d_canvas
        rows = getattr(self, "_chart_rows", [])
        res = getattr(self, "_chart_res", None)
        cv.delete("all")
        w = cv.winfo_width()
        h = cv.winfo_height()
        if w < 20:
            w = self.W - 6
        if h < 20:
            h = self._chart_h()
        if not rows or len(rows) < 5:
            cv.create_text(w // 2, h // 2, text="K线数据不足", fill=DIM,
                           font=self.fnt.f(9))
            return
        n = min(len(rows), max(18, int(w / (2.6 * self.scale))))
        data = rows[-n:]
        pred = (res or {}).get("t_pred") or {}
        cl = pred.get("cl") or {}
        p10, p50, p90 = cl.get(10), cl.get(50), cl.get(90)
        pad_r = 32 if p50 is not None else 4
        plot_w = max(30, w - pad_r - 5)
        bw = plot_w / n
        lo = min(r["low"] for r in data)
        hi = max(r["high"] for r in data)
        if p10 and p90:
            lo, hi = min(lo, p10), max(hi, p90)
        if hi <= lo:
            hi = lo + 1

        def xv(i):
            return 4 + (i + 0.5) * bw

        def yv(v):
            return h - 4 - (v - lo) * (h - 9) / (hi - lo)

        for f in (0.25, 0.5, 0.75):
            cv.create_line(3, int(h * f), w - pad_r, int(h * f),
                           fill=LINE, dash=(2, 3))
        for i, r in enumerate(data):
            c = UP if r["close"] >= r["open"] else DOWN
            if self.updown == "green_up":
                c = DOWN if r["close"] >= r["open"] else UP
            cv.create_line(xv(i), yv(r["high"]), xv(i), yv(r["low"]),
                           fill=c, width=max(1, int(bw * 0.72)))
        closes = [r["close"] for r in rows]
        # MA20 用滑动窗口一次算完（原实现每个点都重算 sum，O(n*20)）
        ma20 = [None] * len(closes)
        s = 0.0
        for j, c in enumerate(closes):
            s += c
            if j >= 20:
                s -= closes[j - 20]
            if j >= 4:
                ma20[j] = s / (min(j, 19) + 1)
        pts = []
        base = len(rows) - n
        for j in range(base, len(rows)):
            v = ma20[j]
            if v is not None:
                pts += [xv(j - base), yv(v)]
        if len(pts) >= 4:
            cv.create_line(*pts, fill="#ffa94d", width=1)
        last = data[-1]["close"]
        ly = yv(last)
        cv.create_line(3, ly, w - pad_r, ly, fill="#4a5866", dash=(1, 3))
        if p50 is not None:
            x0, x1 = w - pad_r + 2, w - 3
            cv.create_rectangle(x0, yv(p90), x1, yv(p10), outline=ACCENT,
                                fill=ACCENT, stipple="gray25")
            cv.create_line(w - pad_r - 1, ly, x1, yv(p50), fill=FG, dash=(1, 2))
            cv.create_text((x0 + x1) // 2, max(6, yv(p50) - 6),
                           text=f"{p50:.2f}", fill=FG, font=self.fnt.m(7))
        cv.create_text(4, 1, text=f"{hi:.2f}", anchor="nw", fill=DIM,
                       font=self.fnt.m(7))
        cv.create_text(4, h - 1, text=f"{lo:.2f}", anchor="sw", fill=DIM,
                       font=self.fnt.m(7))

    def detail_toggle_watch(self):
        code = self.detail_code
        if not code:
            return
        if code in self.watchlist:
            self.watchlist.remove(code)
            self.toast("已移出自选")
        else:
            self.watchlist.append(code)
            self.toast("已加入自选")
        self._save_ini()
        self._load_names()
        self._render_watch()
        self._detail_header(self.quotes.get(code) or {}, self.detail_res)

    def detail_ai(self):
        if not self.sp.get_ai_key() and not self._ini("deepseek", "api_key", ""):
            self.toast("先到设置里填 API Key")
            self._show("settings")
            return
        self.close_detail()
        self._show("ai")
        name = self.names.get(self.detail_code, self.detail_code)
        self.ai_preset(f"分析 {name} {self.detail_code} 现在能不能买？"
                       "给出方向、价位与仓位。")

    # ================= WiFi 屏 =================
    def _build_wifi(self, scr):
        head = tk.Frame(scr, bg=PANEL, height=self.status_h + 8)
        head.pack(fill="x")
        head.pack_propagate(False)
        Btn(head, "‹", self.close_wifi, font=self.fnt.f(15, True),
            bg=PANEL, padx=6).pack(side="left", fill="y")
        tk.Label(head, text="WiFi / 网络", bg=PANEL, fg=FG,
                 font=self.fnt.f(10, True)).pack(side="left", padx=2)
        self.w_lbl_state = tk.Label(head, text="--", bg=PANEL, fg=DIM,
                                    font=self.fnt.f(8), anchor="e")
        self.w_lbl_state.pack(side="right", padx=4)

        bar = tk.Frame(scr, bg=BG, height=self.tool_h)
        bar.pack(fill="x", pady=1)
        bar.pack_propagate(False)
        Btn(bar, "扫描", lambda: self._wifi_refresh(True),
            font=self.fnt.f(9, True), bg=ACCENT, fg="#08101c").pack(
                side="left", fill="y", padx=(3, 2))
        Btn(bar, "一键连预设", self._wifi_quick, font=self.fnt.f(9),
            bg=PANEL).pack(side="left", fill="y", padx=2)
        Btn(bar, "断开", self._wifi_disconnect, font=self.fnt.f(9),
            bg=PANEL).pack(side="right", fill="y", padx=3)

        foot = tk.Frame(scr, bg=BG)
        foot.pack(side="bottom", fill="x", pady=1)
        Btn(foot, "手动连接", self._wifi_manual, font=self.fnt.f(9),
            bg=PANEL).pack(side="left", fill="x", expand=True, padx=1)
        Btn(foot, "设静态IP", self._wifi_static, font=self.fnt.f(9),
            bg=PANEL).pack(side="left", fill="x", expand=True, padx=1)
        Btn(foot, "恢复DHCP", self._wifi_dhcp, font=self.fnt.f(9),
            bg=PANEL).pack(side="left", fill="x", expand=True, padx=1)

        self.w_scroll = VScroll(scr, bg=BG)
        self.w_scroll.pack(fill="both", expand=True)

    def _wifi_auto(self):
        """启动时若未连网且预设 SSID 可见，后台自动连接（静默失败）。"""
        if not wifi_available():
            return
        ssid = self._ini("wifi", "ssid", "")
        pw = self._ini("wifi", "password", "")
        if not ssid:
            return

        def work(_p):
            st = wifi_status()
            if st.get("ssid"):
                return st
            try:
                nets = wifi_scan()
            except Exception:
                return st
            if any(n[0] == ssid for n in nets):
                try:
                    wifi_connect(ssid, pw)
                except Exception:
                    pass
                return wifi_status()
            return st

        self.submit("wifi_auto", work,
                    on_done=lambda st: setattr(self, "_wifi_st", st))

    def open_wifi(self):
        self.wifi_page.place(x=0, y=0, relwidth=1, relheight=1)
        self.wifi_page.lift()
        self._wifi_refresh()

    def close_wifi(self):
        self.tokens.pop("wifi_scan", None)
        self.wifi_page.place_forget()
        self._refresh_settings_view()

    def _wifi_refresh(self, scan=False):
        if not wifi_available():
            self._wifi_msg("未检测到 nmcli：WiFi 功能仅支持 NetworkManager"
                           "（树莓派/Ubuntu）；本机可用网线或系统网络设置。")
            return
        self.set_status("扫描附近 WiFi…" if scan else "读取网络状态…")

        def work(_p):
            return wifi_status(), wifi_scan()

        self.submit("wifi_scan", work,
                    on_done=lambda r: self._render_wifi(*r),
                    on_error=self._wifi_err)

    def _wifi_err(self, e):
        self._wifi_msg(f"WiFi 操作失败：{trunc(str(e), 60)}")

    def _wifi_msg(self, text):
        self.w_lbl_state.configure(text="不可用", fg=WARN)
        vs = self.w_scroll
        vs.clear()
        lbl = tk.Label(vs.inner, text=text, bg=BG, fg=WARN, font=self.fnt.f(9),
                       wraplength=self.W - 16, justify="left")
        lbl.pack(padx=6, pady=10, anchor="w")
        vs.bind_area(lbl)

    def _render_wifi(self, st, nets):
        self._wifi_st = st
        ssid, ip = st.get("ssid") or "", st.get("ip") or ""
        self.w_lbl_state.configure(
            text=f"{ssid or '未连接'} · {ip or '无IP'}"
                 + (f" · 信号{st['signal']}%" if st.get("signal") else ""),
            fg=FG if ssid else DIM)
        vs = self.w_scroll
        vs.clear()
        known = wifi_known()
        preset = self._ini("wifi", "ssid", "lan")
        if not nets:
            lbl = tk.Label(vs.inner, text="未扫描到网络", bg=BG, fg=DIM,
                           font=self.fnt.f(9))
            lbl.pack(pady=10)
            vs.bind_area(lbl)
            return
        for net_ssid, sig, sec in nets:
            bg = PANEL2 if net_ssid == ssid else PANEL
            row = tk.Frame(vs.inner, bg=bg, height=self.row_h - 4)
            row.pack(fill="x", pady=1)
            row.pack_propagate(False)
            top = tk.Frame(row, bg=bg, height=(self.row_h - 4) // 2)
            top.pack(fill="x")
            top.pack_propagate(False)
            bot = tk.Frame(row, bg=bg)
            bot.pack(fill="both", expand=True)
            tk.Label(top, text=trunc(net_ssid, 10), bg=bg,
                     fg=ACCENT if net_ssid == ssid else FG,
                     font=self.fnt.f(10, True), anchor="w").pack(
                         side="left", padx=4)
            tk.Label(top, text=f"{sig}%", bg=bg, fg=DIM,
                     font=self.fnt.m(8), anchor="e").pack(
                         side="right", padx=4)
            tags = []
            if net_ssid == ssid:
                tags.append("已连接")
            if net_ssid in known:
                tags.append("已保存")
            if preset and net_ssid.lower() == preset.lower():
                tags.append("预设")
            if sec:
                tags.append("加密" if sec != "--" else "开放")
            tk.Label(bot, text=" · ".join(tags) or "开放", bg=bg, fg=DIM,
                     font=self.fnt.f(7), anchor="w").pack(
                         side="left", padx=4)
            vs.bind_row(row, on_tap=lambda w, s=net_ssid, k=(net_ssid in known),
                        locked=sec not in ("", "--"): self._wifi_tap(s, locked, k))

    def _wifi_tap(self, ssid, locked, known):
        if known or not locked:
            ConfirmDialog(self, f"连接到 {ssid}？",
                          lambda: self._wifi_do(ssid, ""), "连接")
        else:
            TextDialog(self, f"WiFi 密码", "", lambda pw: self._wifi_do(ssid, pw),
                       show="*", hint=f"输入 {trunc(ssid, 12)} 的密码")

    def _wifi_do(self, ssid, password):
        self.set_status(f"连接 {trunc(ssid, 10)} …")

        def work(_p):
            try:
                wifi_connect(ssid, password)
            except Exception:
                # SSID 大小写不一致时，按扫描结果兜底重试
                alt = ""
                try:
                    for n in wifi_scan():
                        if n[0].lower() == ssid.lower():
                            alt = n[0]
                            break
                except Exception:
                    pass
                if not alt or alt == ssid:
                    raise
                wifi_connect(alt, password)
            return wifi_status()

        self.submit("wifi_conn", work,
                    on_done=lambda st: (self.toast(f"已连接 {st.get('ssid', ssid)}"),
                                        self._render_wifi(st, wifi_scan())),
                    on_error=lambda e: self.toast(f"连接失败：{trunc(str(e), 20)}"))

    def _wifi_quick(self):
        ssid = self._ini("wifi", "ssid", "")
        pw = self._ini("wifi", "password", "")
        if not ssid:
            self.toast("未配置预设 WiFi：点设置里的【预设】")
            return
        ConfirmDialog(self, f"连接预设 WiFi「{ssid}」？",
                      lambda: self._wifi_do(ssid, pw), "连接")

    def _wifi_set_preset(self):
        def ask_pw(ssid):
            ssid = (ssid or "").strip()
            if not ssid:
                return

            def save(pw):
                cp = configparser.ConfigParser(interpolation=None)
                cp.read(INI_PATH, encoding="utf-8")
                if not cp.has_section("wifi"):
                    cp.add_section("wifi")
                cp.set("wifi", "ssid", ssid)
                cp.set("wifi", "password", (pw or "").strip())
                with open(INI_PATH, "w", encoding="utf-8") as f:
                    cp.write(f)
                self.cfg.read(INI_PATH, encoding="utf-8")
                self._refresh_settings_view()
                self.toast(f"预设已保存：{ssid}")

            TextDialog(self, "预设 WiFi 密码",
                       self._ini("wifi", "password", ""), save, show="*",
                       hint=f"{ssid} 的密码（WPA 至少 8 位）")

        TextDialog(self, "预设 WiFi 名称", self._ini("wifi", "ssid", ""),
                   ask_pw, hint="要一键连接的 WiFi 名称（SSID）")

    def _wifi_disconnect(self):
        ConfirmDialog(self, "断开当前 WiFi？", self._wifi_do_disconnect,
                      "断开")

    def _wifi_do_disconnect(self):
        self.set_status("断开中…")

        def work(_p):
            wifi_disconnect()
            return wifi_status()

        self.submit("wifi_conn", work,
                    on_done=lambda st: (self.toast("已断开"),
                                        self._render_wifi(st, [])),
                    on_error=lambda e: self.toast(f"断开失败：{trunc(str(e), 18)}"))

    def _wifi_manual(self):
        def ask_pw(ssid):
            if not ssid.strip():
                return
            TextDialog(self, "WiFi 密码", "", lambda pw: self._wifi_do(
                ssid.strip(), pw), show="*",
                hint=f"输入 {trunc(ssid.strip(), 12)} 的密码（开放网络留空）")
        TextDialog(self, "手动连接 WiFi", "", ask_pw,
                   hint="输入 WiFi 名称（SSID）")

    def _wifi_static(self):
        cur = self._ini("wifi", "static_ip", self._wifi_default("static_ip"))

        def ok(ip):
            ip = (ip or "").strip()
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                self.toast("IP 格式不对")
                return
            gw = self._ini("wifi", "gateway", "") or \
                ".".join(ip.split(".")[:3]) + ".1"
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(INI_PATH, encoding="utf-8")
            if not cp.has_section("wifi"):
                cp.add_section("wifi")
            cp.set("wifi", "static_ip", ip)
            cp.set("wifi", "gateway", gw)
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
            self.cfg.read(INI_PATH, encoding="utf-8")
            ConfirmDialog(
                self, f"把当前 WiFi 设为静态IP {ip}（网关 {gw}）？\n"
                      "应用时网络会短暂中断；失败可能需在系统里改回 DHCP。",
                lambda: self._wifi_apply_static(ip, gw), "应用")

        NumPadDialog(self, "静态IP", cur, ok, hint="如 192.168.3.42")

    def _wifi_apply_static(self, ip, gw):
        self.set_status(f"应用静态IP {ip} …")

        def work(_p):
            wifi_set_static(ip, gw, gw)
            return wifi_status()

        self.submit("wifi_conn", work,
                    on_done=lambda st: (self.toast(f"静态IP {ip} 已应用"),
                                        self._render_wifi(st, [])),
                    on_error=lambda e: self.toast(f"设置失败：{trunc(str(e), 18)}"))

    def _wifi_dhcp(self):
        ConfirmDialog(self, "把当前 WiFi 改回自动获取 IP（DHCP）？",
                      self._wifi_do_dhcp, "恢复")

    def _wifi_do_dhcp(self):
        self.set_status("恢复 DHCP…")

        def work(_p):
            wifi_set_dhcp()
            return wifi_status()

        self.submit("wifi_conn", work,
                    on_done=lambda st: (self.toast("已恢复 DHCP"),
                                        self._render_wifi(st, [])),
                    on_error=lambda e: self.toast(f"恢复失败：{trunc(str(e), 18)}"))

    # ================= 荐股屏 =================
    def _build_picks(self, scr):
        head = tk.Frame(scr, bg=BG, height=self.tool_h)
        head.pack(fill="x", pady=1)
        head.pack_propagate(False)
        tk.Label(head, text="今日荐股", bg=BG, fg=FG,
                 font=self.fnt.f(10, True)).pack(side="left", padx=4)
        Btn(head, "刷新", lambda: self.refresh_picks(True), font=self.fnt.f(9),
            bg=PANEL).pack(side="right", fill="y", padx=3)
        self.lbl_picks_date = tk.Label(head, text="", bg=BG, fg=DIM,
                                       font=self.fnt.f(8))
        self.lbl_picks_date.pack(side="right", padx=2)

        seg = tk.Frame(scr, bg=BG, height=self.tool_h)
        seg.pack(fill="x", pady=1)
        seg.pack_propagate(False)
        self.tier_btns = {}
        for t in TIER_LIST:
            b = Btn(seg, t, lambda tt=t: self._switch_tier(tt),
                    font=self.fnt.f(10, True), bg=PANEL, fg=DIM)
            b.pack(side="left", fill="both", expand=True, padx=1)
            self.tier_btns[t] = b

        gate = tk.Frame(scr, bg=PANEL2)
        gate.pack(fill="x")
        self.lbl_gate = tk.Label(gate, text="", bg=PANEL2, fg=DIM,
                                 font=self.fnt.f(8), anchor="w", padx=4)
        self.lbl_gate.pack(side="left", fill="x", expand=True)
        self.lbl_picks_note = tk.Label(gate, text="", bg=PANEL2, fg=DIM,
                                       font=self.fnt.f(8))
        self.lbl_picks_note.pack(side="right", padx=2)
        self.btn_ai_tier = Btn(gate, "AI选档", lambda: self._ai_choose_tier(),
                               font=self.fnt.f(8), bg=PANEL)
        self.btn_ai_tier.pack(side="right", padx=2, pady=1)

        self.picks_scroll = VScroll(scr, bg=BG)
        self.picks_scroll.pack(fill="both", expand=True)

    def _switch_tier(self, tier):
        self.picks_tier = tier
        self._save_ini()
        self._update_tier_btns()
        self.refresh_picks()

    def _update_tier_btns(self):
        for t, b in self.tier_btns.items():
            on = t == self.picks_tier
            b.set_bg(ACCENT if on else PANEL)
            b.configure(fg="#08101c" if on else DIM)

    def _picks_snapshot(self):
        """读取本目录/后端目录里的荐股快照（make_picks.py 生成）。"""
        for d in (HERE, os.path.dirname(self.backend_path)):
            p = os.path.join(d, "picks_cache.json")
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    return None
        return None

    def _snapshot_res(self, snap, tier):
        d = (snap.get("tiers") or {}).get(tier)
        if not d:
            return None
        return {"signal_date": d.get("signal_date")
                or snap.get("generated", "--"),
                "capital": snap.get("capital") or 0,
                "tiers": snap["tiers"]}

    def refresh_picks(self, force=False):
        self._update_tier_btns()
        tier = self.picks_tier
        cached = self.picks_data.get(tier)
        if cached and not force and time.time() - cached[0] < 600:
            self._render_picks(cached[1])
            return
        cap = 100000.0
        try:
            cap = float(self._ini("picks", "capital", "100000") or 100000)
        except ValueError:
            pass
        uni = self._ini("picks", "universe", "all")
        if uni not in UNIVERSE_LIST:
            uni = "all"
        avail = mem_avail_mb()
        local_ok = self.sp.np is not None and (avail is None or avail >= 450)
        snap = self._picks_snapshot()
        # 快照优先（小内存终端的常规路径）；本机算力够且手动刷新才本地重算
        if not (force and local_ok):
            res = self._snapshot_res(snap or {}, tier)
            if res:
                self._render_picks(res, note=f"快照 {snap.get('generated', '')}")
                return
        if not local_ok:
            why = ("缺少 numpy" if self.sp.np is None
                   else f"可用内存仅 {avail}MB")
            self._picks_error(
                f"{why}，且无 picks_cache.json 快照：在电脑上运行 "
                "make_picks.py 后把快照拷到本目录", title="荐股不可用")
            return
        self.set_status(f"{tier}档 计算中(首次较慢)…")

        def work(_p):
            return self.sp.tier_latest_picks(capital=cap, tiers=(tier,),
                                             universe=uni)

        self.submit("picks", work, on_done=lambda r, t=tier: self._on_picks(t, r),
                    on_error=self._picks_error)

    def _on_picks(self, tier, res):
        if tier != self.picks_tier:
            return
        self.picks_data[tier] = (time.time(), res)
        self._render_picks(res)

    def _picks_error(self, err, title="三档引擎不可用"):
        vs = self.picks_scroll
        vs.clear()
        msg = str(err)
        self.lbl_gate.configure(text=title, fg=WARN)
        lbl = tk.Label(vs.inner, text=trunc(msg, 90), bg=BG, fg=WARN,
                       font=self.fnt.f(9), wraplength=self.W - 16,
                       justify="left")
        lbl.pack(padx=6, pady=8, anchor="w")
        vs.bind_area(lbl)
        if self.sp.np is not None:
            b = Btn(vs.inner, "本地扫描荐股（慢）", self._picks_fallback,
                    font=self.fnt.f(9), bg=PANEL)
            b.pack(padx=6, pady=4, anchor="w")
            vs.bind_area(b)

    def _picks_fallback(self):
        def work(prog):
            return self.sp.daily_picks(progress=prog, top_n=20)

        def done(picks):
            vs = self.picks_scroll
            vs.clear()
            self.lbl_gate.configure(text="本地扫描（无三档）", fg=DIM)
            if not picks:
                lbl = tk.Label(vs.inner, text="今日无入围标的", bg=BG, fg=DIM,
                               font=self.fnt.f(9))
                lbl.pack(pady=10)
                vs.bind_area(lbl)
                return
            for code, name, close, chg, score, reasons, band in picks:
                self._pick_row(vs, code, name, close, chg, score,
                               f"{band} {reasons}")

        self.submit("picks", work, on_done=done, on_error=self._picks_error)

    def _render_picks(self, res, note=""):
        vs = self.picks_scroll
        vs.clear()
        d = (res.get("tiers") or {}).get(self.picks_tier) or {}
        gate = d.get("gate_on", True)
        cfg = d.get("cfg") or {}
        self.lbl_picks_date.configure(
            text=f"信号日 {res.get('signal_date', '--')}")
        if gate:
            self.lbl_gate.configure(
                text=f"闸门开·可持仓（{cfg.get('gate', '')}MA"
                     f"{cfg.get('ma', '')}）", fg=UP)
        else:
            self.lbl_gate.configure(text="闸门关·建议空仓持现金", fg=WARN)
        picks = d.get("picks") or []
        prefix = f"{note} · " if note else ""
        self.lbl_picks_note.configure(
            text=f"{prefix}top{cfg.get('top', '')} {len(picks)}只")
        if not picks:
            tk.Label(vs.inner, text="当前档无持仓建议", bg=BG, fg=DIM,
                     font=self.fnt.f(9)).pack(pady=10)
            return
        for x in picks:
            self._pick_row(vs, x.get("code", ""), x.get("name", ""),
                           x.get("price"), None, x.get("score"),
                           f"{x.get('lots', 0)}手 {x.get('cost', 0):,.0f}元 "
                           f"β{x.get('beta60') or 0:.2f}")

    def _pick_row(self, vs, code, name, price, chg, score, note):
        row = tk.Frame(vs.inner, bg=PANEL, height=self.row_h + 6)
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)
        top = tk.Frame(row, bg=PANEL, height=(self.row_h + 6) // 2)
        top.pack(fill="x")
        top.pack_propagate(False)
        bot = tk.Frame(row, bg=PANEL)
        bot.pack(fill="both", expand=True)
        tk.Label(top, text=trunc(name or code[-6:], 6), bg=PANEL, fg=FG,
                 font=self.fnt.f(10, True), anchor="w").pack(
                     side="left", padx=4)
        pl = tk.Label(top, text=fmt_price(price), bg=PANEL, fg=FG,
                      font=self.fnt.m(11, True), anchor="e")
        pl.pack(side="right", padx=4)
        if chg is not None:
            pl.configure(fg=self._color_of(chg))
        left = f"{code}" + (f" ·{score:.2f}" if score is not None else "")
        tk.Label(bot, text=trunc(left, 14), bg=PANEL, fg=ACCENT if score is
                 not None else DIM, font=self.fnt.m(7), anchor="w").pack(
                     side="left", padx=4)
        tk.Label(bot, text=note, bg=PANEL, fg=DIM, font=self.fnt.f(7),
                 anchor="e").pack(side="right", padx=4)
        vs.bind_row(row, on_tap=lambda w, c=code: self.open_detail(c, name))

    def _ai_choose_tier(self):
        if not self.sp.get_ai_key():
            self.toast("先到设置里填 API Key")
            return
        pref = self._ini("picks", "risk_pref", "均衡")
        self.set_status("AI 选档中…")

        def work(_p):
            return self.sp.ai_choose_tier(pref=pref)

        def done(r):
            tier, reason = r
            self.toast(f"AI选档：{tier}（{trunc(reason, 14)}）")
            if tier in TIER_LIST:
                self._switch_tier(tier)

        self.submit("ai_tier", work, on_done=done,
                    on_error=lambda e: self.toast(f"AI选档失败：{trunc(str(e), 14)}"))

    # ================= AI 屏 =================
    def _build_ai(self, scr):
        presets = ("大盘速览", "自选点评", "荐股解读", "风险提示")
        ibar = tk.Frame(scr, bg=BG, height=self.tool_h)
        ibar.pack(side="bottom", fill="x", pady=2)
        ibar.pack_propagate(False)
        pbar = tk.Frame(scr, bg=BG)
        pbar.pack(side="bottom", fill="x")
        self.ai_scroll = VScroll(scr, bg=BG)
        self.ai_scroll.pack(fill="both", expand=True)
        for p in presets:
            Btn(pbar, p, lambda pp=p: self.ai_preset(pp), font=self.fnt.f(9),
                bg=PANEL).pack(side="left", fill="x", expand=True,
                               padx=1, pady=1)
        self.ai_var = tk.StringVar()
        ent = tk.Entry(ibar, textvariable=self.ai_var, bg=PANEL2, fg=FG,
                       insertbackground=FG, font=self.fnt.f(10), relief="flat",
                       highlightthickness=0)
        ent.pack(side="left", fill="both", expand=True, padx=(3, 2), pady=2)
        ent.bind("<Return>", lambda e: self.ai_send())
        self.ai_send_btn = Btn(ibar, "发送", self.ai_send,
                               font=self.fnt.f(10, True), bg=ACCENT,
                               fg="#08101c")
        self.ai_send_btn.pack(side="right", fill="y", padx=(0, 3), pady=2)

    def _render_chat(self):
        """增量渲染：只追加新消息（原实现每来一条回复就全量重建，长会话会卡）。"""
        vs = self.ai_scroll
        done_n = getattr(self, "_chat_rendered", 0)
        if len(self.ai_msgs) < done_n:
            vs.clear()
            self._chat_placeholder = None
            done_n = 0
        if not self.ai_msgs:
            if not done_n and getattr(self, "_chat_placeholder", None) is None:
                self._chat_placeholder = tk.Label(
                    vs.inner,
                    text="点下方预设问题，或输入后回车。\n"
                         "AI 已带大盘/自选/荐股上下文。",
                    bg=BG, fg=DIM, font=self.fnt.f(9), justify="left",
                    wraplength=self.W - 20)
                self._chat_placeholder.pack(padx=8, pady=12, anchor="w")
                vs.bind_area(self._chat_placeholder)
            return
        ph = getattr(self, "_chat_placeholder", None)
        if ph is not None:
            ph.destroy()
            self._chat_placeholder = None
        for m in self.ai_msgs[done_n:]:
            user = m.get("role") == "user"
            bg = ACCENT_DK if user else PANEL
            txt = m.get("content", "")
            if user and "\n\n问题：" in txt:
                txt = txt.split("\n\n问题：")[-1]
            bubble = tk.Frame(vs.inner, bg=bg)
            bubble.pack(fill="x", pady=2,
                        padx=(int(self.W * 0.18), 4) if user else (4, int(self.W * 0.12)))
            tk.Label(bubble, text=txt, bg=bg, fg=FG, font=self.fnt.f(9),
                     justify="left", anchor="w", wraplength=int(self.W * 0.72)).pack(
                         fill="x", padx=5, pady=3)
            vs.bind_area(bubble)  # 气泡上也能拖动滚动
        self._chat_rendered = len(self.ai_msgs)
        vs.inner.update_idletasks()
        vs.canvas.yview_moveto(1.0)

    def _ai_context(self):
        parts = []
        try:
            parts.append(self.sp.ai_market_brief())
        except Exception:
            pass
        if self.quotes:
            rows = []
            for c in self.watchlist[:6]:
                q = self.quotes.get(c)
                if q and q.get("prev_close"):
                    chg = (q["price"] / q["prev_close"] - 1) * 100
                    rows.append(f"{q.get('name', c)}({c}) {q['price']:.2f} "
                                f"{chg:+.1f}%")
            if rows:
                parts.append("我的自选：" + "；".join(rows))
        if self.detail_res:
            act = self.detail_res.get("action") or {}
            if act.get("verdict"):
                parts.append(f"当前查看 {self.detail_code}：{act['verdict']}")
        return ("以下是实时数据上下文（只读，不用复述）：\n"
                + "\n".join(parts))

    def ai_preset(self, text):
        self.ai_var.set(text)
        self.ai_send()

    def ai_send(self):
        if self.ai_send_btn._command is None:
            return
        text = (self.ai_var.get() or "").strip()
        if not text:
            return
        key = self.sp.get_ai_key()
        if not key:
            self.toast("先到设置里填 API Key")
            self._show("settings")
            return
        first = not self.ai_msgs
        base_msgs = list(self.ai_msgs)
        self.ai_send_btn.configure(text="思考…")
        self.ai_send_btn._command = None
        self.set_status("AI 思考中…")

        def work(_p):
            # 上下文构建（含DB查询/行情拼接）也放后台，避免卡住触摸
            prompt = (self._ai_context() + "\n\n问题：" + text) if first else text
            msgs = base_msgs + [{"role": "user", "content": prompt}]
            reply = self.sp._deepseek_chat(
                key, msgs, model=self.sp.AI_MODEL, timeout=120,
                session=self.sp._ai_session_id("mobile", self.sp.AI_MODEL))
            return msgs, reply

        def done(result):
            msgs, reply = result
            self.ai_msgs = msgs + [{"role": "assistant", "content": reply}]
            try:
                self.sp.ai_session_save("mobile", self.ai_msgs, "",
                                        self.sp.AI_MODEL)
            except Exception:
                pass
            self.ai_var.set("")
            self._render_chat()
            self._ai_enable()

        def err(e):
            self.toast(f"AI失败：{trunc(str(e), 20)}")
            self.ai_var.set(text)
            self._ai_enable()

        self.submit("ai", work, on_done=done, on_error=err)

    def _ai_enable(self):
        self.ai_send_btn.configure(text="发送")
        self.ai_send_btn._command = self.ai_send

    # ================= 设置屏 =================
    def _build_settings(self, scr):
        self.set_scroll = VScroll(scr, bg=BG)
        self.set_scroll.pack(fill="both", expand=True)
        self.set_labels = {}
        vs = self.set_scroll.inner

        def section(title):
            lbl = tk.Label(vs, text=title, bg=BG, fg=ACCENT,
                           font=self.fnt.f(9, True), anchor="w")
            lbl.pack(fill="x", padx=5, pady=(6, 1))
            self.set_scroll.bind_area(lbl)

        def row(label, key, command=None, hint=""):
            f = tk.Frame(vs, bg=PANEL, height=self.row_h)
            f.pack(fill="x", pady=1)
            f.pack_propagate(False)
            tk.Label(f, text=label, bg=PANEL, fg=FG, font=self.fnt.f(9),
                     anchor="w", width=6).pack(side="left", padx=5)
            if command:
                tk.Label(f, text="›", bg=PANEL, fg=DIM,
                         font=self.fnt.f(11)).pack(side="right", padx=(0, 4))
            v = tk.Label(f, text="--", bg=PANEL, fg=DIM, font=self.fnt.f(9),
                         anchor="e")
            v.pack(side="right", padx=6)
            if command:
                # 触摸：拖动=滚动，抬起未移动才算点击
                self.set_scroll.bind_row(f, on_tap=lambda w, cmd=command: cmd())
            else:
                self.set_scroll.bind_area(f)
            if hint:
                v.configure(text=hint)
            self.set_labels[key] = v
            return v

        section("AI 接口")
        row("API Key", "key", self._set_key)
        row("模型", "model", self._set_model)
        row("接口", "base", self._set_base)
        row("测试", "test", self._test_ai)

        section("荐股")
        row("资金", "capital", self._set_capital)
        row("偏好", "risk_pref", lambda: self._cycle("risk_pref", TIER_LIST))
        row("标的池", "universe",
            lambda: self._cycle("universe", UNIVERSE_LIST, UNIVERSE_NAME))
        row("默认档", "tier", lambda: self._cycle("tier", TIER_LIST))
        row("AI选档", "ai_tier", self._ai_choose_tier)

        section("网络")
        row("WiFi", "wifi", self.open_wifi)
        row("本机IP", "ip", self.open_wifi)
        row("预设", "wifi_preset", self._wifi_set_preset)
        row("静态IP", "static_ip", self._wifi_static)

        section("数据")
        row("状态", "data", self._load_data_status)
        row("刷新代码表", "refresh_codes", self._act_refresh_codes)
        row("补自选K线", "prefetch", self._act_prefetch)
        row("清空AI会话", "clear_ai", self._clear_ai)

        section("显示")
        row("涨跌色", "updown", self._toggle_updown)

        section("关于")
        about = tk.Label(vs, text=f"{APP} v{VERSION}\n后端：{self.backend_path}\n"
                                  "仅统计参考，不构成投资建议。",
                         bg=BG, fg=DIM, font=self.fnt.f(8), justify="left",
                         anchor="w", wraplength=self.W - 16)
        about.pack(fill="x", padx=6, pady=6)
        self.set_scroll.bind_area(about)
        self._refresh_settings_view()
        self._load_data_status()

    def _refresh_settings_view(self):
        key = self._ini("deepseek", "api_key", "")
        eff = key or self.sp.get_ai_key()
        if not eff:
            txt, color = "未设置", WARN
        elif key:
            txt, color = "已设置 ****" + key[-4:], FG
        else:
            txt, color = "沿用后端配置", FG
        self.set_labels["key"].configure(text=txt, fg=color)
        self.set_labels["model"].configure(text=self.sp.AI_MODEL)
        base_short = re.sub(r"^https?://", "", self.sp.AI_BASE_URL or "")
        self.set_labels["base"].configure(text=trunc(base_short, 26))
        self.set_labels["capital"].configure(
            text=f"{self._ini('picks', 'capital', '100000')}元")
        self.set_labels["risk_pref"].configure(
            text=self._ini("picks", "risk_pref", "均衡"))
        uni = self._ini("picks", "universe", "all")
        self.set_labels["universe"].configure(
            text=UNIVERSE_NAME.get(uni, uni))
        self.set_labels["tier"].configure(text=self.picks_tier)
        self.set_labels["updown"].configure(
            text="红涨绿跌" if self.updown == "red_up" else "绿涨红跌")
        st = getattr(self, "_wifi_st", None)
        if st is not None:
            self.set_labels["wifi"].configure(
                text=st.get("ssid") or "未连接",
                fg=FG if st.get("ssid") else DIM)
            self.set_labels["ip"].configure(text=st.get("ip") or "--")
        self.set_labels["wifi_preset"].configure(
            text=self._ini("wifi", "ssid", "") or "未设置")
        self.set_labels["static_ip"].configure(
            text=self._ini("wifi", "static_ip", "") or "未设置")

    def _load_data_status(self):
        def work(_p):
            out = {}
            try:
                with self.sp.db_conn() as conn:
                    out["stocks"] = conn.execute(
                        "select count(*) from stocks").fetchone()[0]
                    out["latest"] = conn.execute(
                        "select max(date) from daily_bars").fetchone()[0]
            except Exception as e:
                out["err"] = str(e)
            try:
                out["size"] = human_size(os.path.getsize(self.sp.DB_PATH))
            except Exception:
                out["size"] = "--"
            try:
                age = self.sp.stocks_age()
                out["age"] = f"{age / 86400:.0f}天前"
            except Exception:
                out["age"] = "--"
            if wifi_available():
                try:
                    out["wifi"] = wifi_status()
                except Exception:
                    pass
            return out

        def done(d):
            if d.get("wifi") is not None:
                self._wifi_st = d["wifi"]
                self._refresh_settings_view()
            v = self.set_labels.get("data")
            if not v:
                return
            if d.get("err"):
                v.configure(text="读取失败", fg=WARN)
                return
            v.configure(text=f"{d['stocks']}只·{d['latest']}·{d['size']}",
                        fg=FG)

        self.submit("data_status", work, on_done=done, on_error=lambda e: None)

    def _set_key(self):
        cur = self._ini("deepseek", "api_key", "")

        def ok(text):
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(INI_PATH, encoding="utf-8")
            if not cp.has_section("deepseek"):
                cp.add_section("deepseek")
            cp.set("deepseek", "api_key", text.strip())
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
            self.cfg.read(INI_PATH, encoding="utf-8")
            self._sync_ai_env()
            self._refresh_settings_view()
            self.toast("API Key 已保存")

        TextDialog(self, "API Key", cur, ok, show="*",
                   hint="DeepSeek 等 OpenAI 兼容接口的 Key")

    def _set_model(self):
        def ok(text):
            text = text.strip()
            if not text:
                return
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(INI_PATH, encoding="utf-8")
            if not cp.has_section("deepseek"):
                cp.add_section("deepseek")
            cp.set("deepseek", "model", text)
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
            self.cfg.read(INI_PATH, encoding="utf-8")
            self._sync_ai_env()
            self._refresh_settings_view()

        TextDialog(self, "模型", self.sp.AI_MODEL, ok,
                   hint="如 " + " / ".join(MODEL_LIST[:3]))

    def _set_base(self):
        def ok(text):
            text = text.strip()
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(INI_PATH, encoding="utf-8")
            if not cp.has_section("deepseek"):
                cp.add_section("deepseek")
            cp.set("deepseek", "base_url", text)
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
            self.cfg.read(INI_PATH, encoding="utf-8")
            self._sync_ai_env()
            self._refresh_settings_view()

        TextDialog(self, "接口地址", self.sp.AI_BASE_URL, ok,
                   hint="OpenAI 兼容 base_url")

    def _test_ai(self):
        key = self.sp.get_ai_key()
        if not key:
            self.toast("未配置 API Key")
            return
        self.set_status("测试接口…")

        def work(_p):
            return self.sp.fetch_ai_models(key, self.sp.AI_BASE_URL, timeout=15)

        self.submit("test_ai", work,
                    on_done=lambda ms: self.toast(
                        f"接口正常，{len(ms)} 个模型"),
                    on_error=lambda e: self.toast(f"接口失败：{trunc(str(e), 16)}"))

    def _set_capital(self):
        cur = self._ini("picks", "capital", "100000")

        def ok(text):
            text = re.sub(r"\D", "", text or "")
            if not text:
                return
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(INI_PATH, encoding="utf-8")
            if not cp.has_section("picks"):
                cp.add_section("picks")
            cp.set("picks", "capital", text)
            with open(INI_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
            self.cfg.read(INI_PATH, encoding="utf-8")
            self._refresh_settings_view()

        NumPadDialog(self, "荐股资金 (元)", cur, ok)

    def _cycle(self, key, values, names=None):
        section = "picks"
        cur = self._ini(section, key, values[0])
        idx = values.index(cur) if cur in values else 0
        nxt = values[(idx + 1) % len(values)]
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(INI_PATH, encoding="utf-8")
        if not cp.has_section(section):
            cp.add_section(section)
        cp.set(section, key, nxt)
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
        self.cfg.read(INI_PATH, encoding="utf-8")
        if key == "tier":
            self.picks_tier = nxt
        if key == "risk_pref":
            self.picks_data.clear()
        self._refresh_settings_view()
        self.toast(f"{key} → {names.get(nxt, nxt) if names else nxt}")

    def _toggle_updown(self):
        self.updown = "green_up" if self.updown == "red_up" else "red_up"
        self._save_ini()
        self._refresh_settings_view()
        self._render_watch()
        self._apply_index()
        if self.detail_res:
            self._draw_chart(self.detail_res.get("disp_rows") or [],
                             self.detail_res)

    def _clear_ai(self):
        def ok():
            try:
                with self.sp.db_conn(commit=True) as conn:
                    conn.execute("delete from meta where key like 'ai:mobile:%'")
            except Exception:
                pass
            self.ai_msgs = []
            self._render_chat()
            self.toast("AI 会话已清空")

        ConfirmDialog(self, "清空本机 AI 对话记录？", ok, "清空")

    def _act_refresh_codes(self):
        self.set_status("刷新全市场代码表(约1分钟)…")

        def done(r):
            self.toast("代码表已刷新")
            self._load_data_status()
            self._load_names()

        self.submit("refresh_codes",
                    lambda p: self.sp.refresh_all_codes(progress=p),
                    on_done=done,
                    on_error=lambda e: self.toast(f"刷新失败：{trunc(str(e), 16)}"))

    def _act_prefetch(self):
        codes = list(self.watchlist)
        if not codes:
            return
        self.set_status("补齐自选K线…")

        def done(r):
            self.toast("自选K线已补齐")
            self.refresh_quotes(True)
            self._load_data_status()

        self.submit("prefetch",
                    lambda p: self.sp.prefetch(codes, workers=4, progress=p),
                    on_done=done,
                    on_error=lambda e: self.toast(f"补K线失败：{trunc(str(e), 14)}"))

    # ================= 启动与收尾 =================
    def _after_start(self):
        if not os.path.exists(INI_PATH):
            self._save_ini()
        self._load_names()
        self._render_watch()
        self._render_chat()
        self.refresh_quotes()
        if not getattr(self.args, "no_wifi_auto", False):
            self._wifi_auto()
        tab = self.args.tab or self._ini("ui", "tab", "watch")
        if tab in self.screens and tab != "watch":
            self._show(tab)

    def run(self):
        if self.args.screenshot:
            delay = max(500, int(self.args.shot_delay))
            self.root.after(delay, self._take_screenshot)
        self.root.mainloop()

    def _take_screenshot(self):
        ok = self._grab(self.args.screenshot)
        if ok:
            print(f"[shot] 已保存 {self.args.screenshot}")
        self.root.after(200, self.root.destroy)

    def _grab(self, path, widget=None):
        ok = False
        try:
            import gi
            os.environ.setdefault("GDK_BACKEND", "x11")
            gi.require_version("Gdk", "3.0")
            gi.require_version("GdkX11", "3.0")
            from gi.repository import Gdk, GdkX11
            dpy = Gdk.Display.get_default()
            wobj = widget if widget is not None else self.root
            gwin = GdkX11.X11Window.foreign_new_for_display(
                dpy, wobj.winfo_id())
            w, h = wobj.winfo_width(), wobj.winfo_height()
            pb = Gdk.pixbuf_get_from_window(gwin, 0, 0, w, h)
            if pb.get_width() > w or pb.get_height() > h:
                pb = pb.new_subpixbuf(0, 0, min(w, pb.get_width()),
                                      min(h, pb.get_height()))
            pb.savev(path, "png", [], [])
            ok = True
        except Exception as e:
            print(f"[shot] 失败: {e}")
        return ok


def check_backend(args):
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(INI_PATH, encoding="utf-8")
    except Exception:
        pass
    ini_dir = cfg.get("backend", "dir", fallback="")
    p = find_backend(args.backend, ini_dir)
    if not p:
        print("未找到 stock_predict.py")
        return 1
    sp = load_backend(p)
    print(f"后端: {p}")
    print(f"数据库: {sp.DB_PATH}")
    try:
        with sp.db_conn() as conn:
            n = conn.execute("select count(*) from stocks").fetchone()[0]
            latest = conn.execute("select max(date) from daily_bars").fetchone()[0]
        print(f"股票 {n} 只 · 最新日K {latest}")
    except Exception as e:
        print(f"数据库读取失败: {e}")
    print(f"numpy: {'OK ' + sp.np.__version__ if sp.np is not None else '缺失（三档引擎不可用）'}")
    print(f"AI Key: {'已配置' if sp.get_ai_key() else '未配置'}")
    if wifi_available():
        st = wifi_status()
        print(f"WiFi: {st.get('ssid') or '未连接'} · IP {st.get('ip') or '--'}")
    else:
        print("WiFi: 未检测到 nmcli（不支持无线管理）")
    return 0


def main():
    ap = argparse.ArgumentParser(description=f"{APP} v{VERSION}")
    ap.add_argument("--size", help="窗口尺寸，如 240x320 / 320x240（桌面预览）")
    ap.add_argument("--fullscreen", action="store_true", help="全屏（真机）")
    ap.add_argument("--backend", default="", help="stock_predict.py 所在目录")
    ap.add_argument("--tab", default="", choices=["", "watch", "picks", "ai",
                                                  "settings"], help="启动页")
    ap.add_argument("--nocursor", action="store_true", help="隐藏鼠标指针")
    ap.add_argument("--no-wifi-auto", action="store_true",
                    help="启动时不自动连接预设 WiFi")
    ap.add_argument("--check", action="store_true", help="只检查后端/数据")
    ap.add_argument("--screenshot", default="", help="启动后截图到路径并退出")
    ap.add_argument("--shot-delay", type=int, default=2500, help="截图延迟(ms)")
    args = ap.parse_args()
    if args.check:
        raise SystemExit(check_backend(args))
    app = App(args)
    app.run()


if __name__ == "__main__":
    main()
