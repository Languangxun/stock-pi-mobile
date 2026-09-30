#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""helpers.py · 后端加载 / 通用格式化 / WiFi 管理（mobile_gui 拆出的非 UI 层）

- find_backend / load_backend：零复制复用 stock_predict.py
- fmt_price / fmt_pct / trunc / human_size / market_state：界面文案
- WiFi：基于 nmcli（树莓派 / NetworkManager），全部走同步函数，由 UI 层丢线程池
"""
import configparser
import importlib.util
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(HERE, "stock_mobile.ini")

BG = "#0b0f14"
DIM = "#7d8b9a"
UP = "#ff5252"
WARN = "#e3b341"


# ================= 后端加载（算法复用，零复制） =================
def find_backend(explicit="", ini_dir=""):
    cands = []
    for d in (explicit, os.environ.get("STOCK_BACKEND", ""), ini_dir, HERE,
              os.path.expanduser("~/stock_predict"),
              os.path.expanduser("~/ai-quant/scripts/cli")):
        if d:
            cands.append(os.path.expanduser(d))
    for d in cands:
        p = os.path.join(d, "stock_predict.py")
        if os.path.isfile(p):
            return p
    return None


def load_backend(path):
    spec = importlib.util.spec_from_file_location("stock_backend", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stock_backend"] = mod
    spec.loader.exec_module(mod)
    return mod


# ================= 通用小工具 =================
def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def fmt_price(v):
    if v is None:
        return "--"
    return f"{v:.2f}" if v < 1000 else f"{v:.0f}"


def fmt_pct(v):
    if v is None:
        return "--"
    return f"{v:+.2f}%"


def trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024.0


def market_state():
    t = time.localtime()
    if t.tm_wday >= 5:
        return "休市", DIM
    hm = t.tm_hour * 100 + t.tm_min
    if 925 <= hm <= 1135 or 1255 <= hm <= 1505:
        return "交易中", UP
    if 1135 < hm < 1255:
        return "午间休市", WARN
    if hm < 925:
        return "未开盘", DIM
    return "已收盘", DIM


# ================= WiFi / 网络（nmcli，树莓派/NetworkManager） =================
def _run(cmd, timeout=25):
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def wifi_available():
    return bool(shutil.which("nmcli"))


def wifi_device():
    rc, out, _ = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"], 8)
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return ""


def local_ips():
    rc, out, _ = _run(["hostname", "-I"], 4)
    return out.split()


def mem_avail_mb():
    """MemAvailable（MB）；读不到返回 None。"""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def wifi_status():
    st = {"ssid": "", "signal": "", "ip": "", "dev": wifi_device()}
    if not st["dev"]:
        return st
    rc, out, _ = _run(["nmcli", "-t", "-f", "active,ssid,signal",
                       "device", "wifi"], 8)
    for line in out.splitlines():
        if line.startswith("yes:"):
            p = line.split(":")
            st["ssid"] = p[1] if len(p) > 1 else ""
            st["signal"] = p[2] if len(p) > 2 else ""
            break
    if not st["ssid"]:
        # 冷启动时缓存列表可能为空，退回活动连接的名称
        st["ssid"] = _wifi_active_conn()
    ips = local_ips()
    st["ip"] = ips[0] if ips else ""
    return st


def wifi_known():
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                      8)
    return {line.rsplit(":", 1)[0] for line in out.splitlines()
            if line.endswith(":802-11-wireless")}


def wifi_scan():
    if not wifi_available():
        raise RuntimeError("未检测到 nmcli")
    _run(["nmcli", "device", "wifi", "rescan"], 15)
    rc, out, err = _run(["nmcli", "-t", "-f", "ssid,signal,security",
                         "device", "wifi", "list"], 30)
    if rc != 0 and not out:
        raise RuntimeError((err or "扫描失败").splitlines()[-1][:60])
    best = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        try:
            sig = int(parts[-2] or 0)
        except ValueError:
            continue
        ssid = ":".join(parts[:-2]).replace("\\:", ":")
        if not ssid:
            continue
        sec = parts[-1] or ""
        if ssid not in best or sig > best[ssid][0]:
            best[ssid] = (sig, sec)
    return sorted(((s, v[0], v[1]) for s, v in best.items()),
                  key=lambda x: -x[1])


def wifi_connect(ssid, password="", timeout=50):
    cmd = ["nmcli", "--wait", "35", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out, err = _run(cmd, timeout)
    if rc != 0:
        raise RuntimeError(((err or out or "连接失败").splitlines() or [""])[-1][:70])


def wifi_disconnect():
    dev = wifi_device()
    if not dev:
        raise RuntimeError("未找到无线网卡")
    rc, out, err = _run(["nmcli", "device", "disconnect", dev], 20)
    if rc != 0:
        raise RuntimeError((err or "断开失败").splitlines()[-1][:60])


def _wifi_active_conn():
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection",
                       "show", "--active"])
    for line in out.splitlines():
        if line.endswith(":802-11-wireless"):
            return line.rsplit(":", 1)[0]
    return ""


def wifi_set_static(ip, gateway="", dns=""):
    name = _wifi_active_conn()
    if not name:
        raise RuntimeError("当前没有已连接的 WiFi")
    args = ["nmcli", "connection", "modify", name, "ipv4.method", "manual",
            "ipv4.addresses", f"{ip}/24"]
    if gateway:
        args += ["ipv4.gateway", gateway]
    if dns:
        args += ["ipv4.dns", dns]
    rc, out, err = _run(args, 20)
    if rc != 0:
        raise RuntimeError((err or "设置失败").splitlines()[-1][:60])
    rc, out, err = _run(["nmcli", "connection", "up", name], 45)
    if rc != 0:
        raise RuntimeError((err or "应用失败").splitlines()[-1][:60])


def wifi_set_dhcp():
    name = _wifi_active_conn()
    if not name:
        raise RuntimeError("当前没有已连接的 WiFi")
    rc, out, err = _run(["nmcli", "connection", "modify", name,
                         "ipv4.method", "auto", "ipv4.addresses", "",
                         "ipv4.gateway", ""], 20)
    if rc != 0:
        raise RuntimeError((err or "设置失败").splitlines()[-1][:60])
    rc, out, err = _run(["nmcli", "connection", "up", name], 45)
    if rc != 0:
        raise RuntimeError((err or "应用失败").splitlines()[-1][:60])