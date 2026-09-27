#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_picks.py · 在算力足够的机器上生成荐股快照（小屏终端直接读取）

小内存设备（Pi Zero/armv6）跑不动三档引擎，本脚本用同一套 stock_predict
后端在开发机/服务器上算好，写成 picks_cache.json，拷到终端同目录即可秒开。

用法：
  python3 make_picks.py                        # 默认资金10万/全A/三档
  python3 make_picks.py --capital 50000 --universe all_etf
  python3 make_picks.py --out /tmp/picks_cache.json --backend ~/stock_predict
"""
import argparse
import configparser
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(HERE, "stock_mobile.ini")
TIERS = ("稳健", "均衡", "激进")
UNIVERSES = ("all", "main", "etf", "all_etf")


def find_backend(explicit=""):
    ini_dir = ""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(INI_PATH, encoding="utf-8")
        ini_dir = cfg.get("backend", "dir", fallback="")
    except Exception:
        pass
    cands = [explicit, os.environ.get("STOCK_BACKEND", ""), ini_dir, HERE,
             os.path.expanduser("~/stock_predict"),
             os.path.expanduser("~/ai-quant/scripts/cli")]
    for d in cands:
        if d and os.path.isfile(os.path.join(os.path.expanduser(d),
                                             "stock_predict.py")):
            return os.path.join(os.path.expanduser(d), "stock_predict.py")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="")
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--universe", default="all", choices=UNIVERSES)
    ap.add_argument("--out", default=os.path.join(HERE, "picks_cache.json"))
    args = ap.parse_args()
    path = find_backend(args.backend)
    if not path:
        raise SystemExit("未找到 stock_predict.py（--backend 指定后端目录）")
    spec = importlib.util.spec_from_file_location("stock_backend", path)
    sp = importlib.util.module_from_spec(spec)
    sys.modules["stock_backend"] = sp
    spec.loader.exec_module(sp)
    if sp.np is None:
        raise SystemExit("本机没有 numpy，无法运行三档引擎")
    tiers = {}
    for tier in TIERS:
        t0 = time.time()
        r = sp.tier_latest_picks(capital=args.capital, tiers=(tier,),
                                 universe=args.universe)
        cfg = r["tiers"][tier]
        cfg["signal_date"] = r["signal_date"]
        tiers[tier] = cfg
        print(f"{tier}: {len(cfg['picks'])}只 闸门"
              f"{'开' if cfg['gate_on'] else '关'} ({time.time()-t0:.0f}s)")
    out = {"generated": time.strftime("%Y-%m-%d %H:%M"),
           "ts": time.time(), "capital": args.capital,
           "universe": args.universe, "tiers": tiers}
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, args.out)
    print(f"写入 {args.out}（{os.path.getsize(args.out)/1024:.0f} KB）")


if __name__ == "__main__":
    main()
