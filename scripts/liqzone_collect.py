#!/usr/bin/env python3
"""
清算帯推定 フェーズ1 ステップ1: collect_live

Coinalyze から直近24時間の5分足(OI・OHLCV・清算2系列)を取得し、
data/liqzone/raw/ の日別CSV(UTC)へ上書き追記する。

方針(仕様書より):
  - 確定足のみ保存する (t + 300 <= 現在時刻)
  - 冪等: 同じ範囲を何度実行しても同じ結果。値が変わらない行は fetched_at も据え置く
  - 取得が全部成功してから書く。1つでも失敗したら何も書かず赤で終了
  - src=bv (公開データ) の行は cv で上書きしない
  - 清算は、取得に成功した範囲で返らなかった足を 0 として埋める
  - 数値は取得元の値をそのまま保存 (丸め・単位変換なし)
標準ライブラリのみ。APIキーは出力しない。
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CZ = "https://api.coinalyze.net/v1"
KEY = os.environ.get("COINALYZE_API_KEY", "").strip()
SYM_A = "BTCUSDT_PERP.A"  # Binance
SYM_6 = "BTCUSDT.6"       # Bybit
BAR = 300
WINDOW = 24 * 3600
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "liqzone")

COLS = {
    "oi_5m": ["t", "oi_btc", "oi_hi", "oi_lo", "src", "fetched_at"],
    "ohlcv_5m": ["t", "o", "h", "l", "c", "vol_btc", "taker_buy_vol_btc", "trades", "src", "fetched_at"],
    "liq_5m": ["t", "symbol", "long_btc", "short_btc", "fetched_at"],
}
# 行の同一性キー(t 以外)。liq は t + symbol
DATA_COLS = {
    "oi_5m": ["oi_btc", "oi_hi", "oi_lo", "src"],
    "ohlcv_5m": ["o", "h", "l", "c", "vol_btc", "taker_buy_vol_btc", "trades", "src"],
    "liq_5m": ["long_btc", "short_btc"],
}


def log(s):
    print(s, flush=True)


def get(path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{CZ}/{path}?{q}"
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"api_key": KEY, "User-Agent": "liqzone-collect"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            time.sleep(2)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = min(int(e.headers.get("Retry-After", "10")), 60)
                log(f"  {path}: 429、{wait}秒待って再試行")
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"{path}: HTTP {e.code}")
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            raise RuntimeError(f"{path}: {e}")
    raise RuntimeError(f"{path}: 取得失敗")


def history(data):
    if not data or "history" not in data[0]:
        return []
    return data[0]["history"]


def day_of(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def fmt(v):
    """取得元の値をそのまま文字列化(丸めない)。"""
    return "" if v is None else repr(v) if isinstance(v, float) else str(v)


def path_for(kind, day):
    return os.path.join(ROOT, "raw", kind, f"{day}.csv")


def load(kind, day):
    p = path_for(kind, day)
    rows = {}
    if os.path.exists(p):
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(int(r["t"]), r.get("symbol", ""))] = r
    return rows


def save(kind, day, rows):
    p = path_for(kind, day)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS[kind], lineterminator="\n")
        w.writeheader()
        for k in sorted(rows):
            w.writerow({c: rows[k].get(c, "") for c in COLS[kind]})
    os.replace(tmp, p)


def upsert(kind, new_rows, now_iso):
    """new_rows: list of dict(行)。日別に分けて上書き。変更があった行数を返す。"""
    by_day = {}
    for r in new_rows:
        by_day.setdefault(day_of(int(r["t"])), []).append(r)
    changed = 0
    for day, rs in sorted(by_day.items()):
        cur = load(kind, day)
        dirty = False
        for r in rs:
            key = (int(r["t"]), r.get("symbol", ""))
            old = cur.get(key)
            if old is not None:
                if old.get("src") == "bv" and r.get("src") == "cv":
                    continue  # 公開データの行は cv で上書きしない
                if all(str(old.get(c, "")) == str(r.get(c, "")) for c in DATA_COLS[kind]):
                    continue  # 同値なら据え置き(fetched_at も変えない)
            r["fetched_at"] = now_iso
            cur[key] = {c: ("" if r.get(c) is None else str(r[c])) for c in COLS[kind]}
            dirty = True
            changed += 1
        if dirty:
            save(kind, day, cur)
    return changed


def main():
    if not KEY:
        log("COINALYZE_API_KEY が未設定")
        return 1
    now = int(time.time())
    now_iso = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    frm = ((now - WINDOW) // BAR) * BAR
    last_ok = (now // BAR) * BAR - BAR  # 確定している最後の足の開始時刻
    ok = lambda t: frm <= t <= last_ok and t + BAR <= now
    grid = list(range(frm, last_ok + 1, BAR))
    log(f"窓: {day_of(frm)} 〜 現在 / 確定足 {len(grid)} 本分 / 実行 {now_iso}")

    base = {"interval": "5min", "from": frm, "to": now}
    oi = history(get("open-interest-history", {**base, "symbols": SYM_A, "convert_to_usd": "false"}))
    ohlcv = history(get("ohlcv-history", {**base, "symbols": SYM_A}))
    liq = {
        s: history(get("liquidation-history", {**base, "symbols": s, "convert_to_usd": "false"}))
        for s in (SYM_A, SYM_6)
    }

    oi_rows = [
        {"t": r["t"], "oi_btc": fmt(r["c"]), "oi_hi": fmt(r.get("h")), "oi_lo": fmt(r.get("l")), "src": "cv"}
        for r in oi if ok(r["t"])
    ]
    ohlcv_rows = [
        {"t": r["t"], "o": fmt(r["o"]), "h": fmt(r["h"]), "l": fmt(r["l"]), "c": fmt(r["c"]),
         "vol_btc": fmt(r.get("v")), "taker_buy_vol_btc": fmt(r.get("bv")), "trades": fmt(r.get("tx")), "src": "cv"}
        for r in ohlcv if ok(r["t"])
    ]
    liq_rows = []
    for s, hist in liq.items():
        got = {r["t"]: r for r in hist if ok(r["t"])}
        for t in grid:  # 取得成功の範囲で返らなかった足は 0
            r = got.get(t)
            liq_rows.append({"t": t, "symbol": s,
                             "long_btc": fmt(r.get("l", 0)) if r else "0",
                             "short_btc": fmt(r.get("s", 0)) if r else "0"})

    # 簡易の健全性: OI/OHLCV が極端に少なければ失敗扱い(何も書かない)
    need = int(len(grid) * 0.9)
    if len(oi_rows) < need or len(ohlcv_rows) < need:
        raise RuntimeError(f"取得本数が少ない: oi={len(oi_rows)} ohlcv={len(ohlcv_rows)} 期待≈{len(grid)}")

    n_oi = upsert("oi_5m", oi_rows, now_iso)
    n_ohlcv = upsert("ohlcv_5m", ohlcv_rows, now_iso)
    n_liq = upsert("liq_5m", liq_rows, now_iso)
    nz = {s: sum(1 for r in liq_rows if r["symbol"] == s and (r["long_btc"] != "0" or r["short_btc"] != "0"))
          for s in (SYM_A, SYM_6)}
    log(f"OI 取得{len(oi_rows)}/更新{n_oi}、OHLCV 取得{len(ohlcv_rows)}/更新{n_ohlcv}、清算 更新{n_liq}(非ゼロ A={nz[SYM_A]} 6={nz[SYM_6]})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"失敗: {e}")
        sys.exit(1)
