#!/usr/bin/env python3
"""
清算帯推定 フェーズ1 ステップ2: backfill_once(手動・1回のみ。再実行しても結果は同じ)

  1) 既存の data/history/deriv_metrics_BTCUSDT_5m.csv.gz を時刻で整列・重複除去し、
     data/liqzone/raw/oi_5m/ へ複製する (src=bv)。既存ファイルは変更しない。
  2) 2026-10-01 以降の日次 metrics を data.binance.vision から取得し oi_5m へ (src=bv)。
  3) 5分足ローソクを 2024-10-01 から data.binance.vision の日次ファイルで取得し ohlcv_5m へ (src=bv)。
     すでに288本そろっている日は取得を省略する(中断後の再開用)。
公開前(404)の日は飛ばす。結果と欠損数は data/liqzone/quality/quality_log.jsonl に1行追記する。
標準ライブラリのみ。fapi は使わない(米国IPで451)。
"""
import csv
import glob
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_collect as lc  # upsert / load / path_for を再利用

START = date(2024, 10, 1)
OLD_END = date(2026, 9, 30)
BV = "https://data.binance.vision/data/futures/um/daily"
OLD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "history", "deriv_metrics_BTCUSDT_5m.csv.gz")
BAR = 300


def log(s):
    print(s, flush=True)


def http(url):
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "liqzone-backfill"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            log(f"  HTTP {e.code} {url} (retry {i + 1})")
        except Exception as e:
            log(f"  {e} {url} (retry {i + 1})")
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"取得失敗: {url}")


def zip_rows(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    rows = list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())))
    if rows and not rows[0][0][:1].isdigit():
        rows = rows[1:]
    return rows


def epoch(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def to_sec(v):
    v = int(v)
    if v > 10**14:      # マイクロ秒
        return v // 10**6
    if v > 10**11:      # ミリ秒
        return v // 1000
    return v


def day_complete(kind, day):
    rows = lc.load(kind, day.isoformat())
    return len(rows) >= 288 and all(r.get("src") == "bv" for r in rows.values())


def main():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    yday = datetime.now(timezone.utc).date() - timedelta(days=1)

    # 1) 既存ファイルの整列・重複除去・複製
    with gzip.open(OLD, "rt", newline="") as f:
        raw = list(csv.DictReader(f))
    by_t = {}
    for r in raw:
        by_t[int(r["t"])] = r["sum_open_interest"]
    back = sum(1 for a, b in zip(raw, raw[1:]) if int(b["t"]) < int(a["t"]))
    log(f"既存ファイル: {len(raw)}行 → 重複除去後 {len(by_t)}行(時刻の逆行 {back}件)")
    rows = [{"t": t, "oi_btc": v, "oi_hi": "", "oi_lo": "", "src": "bv"} for t, v in sorted(by_t.items())]
    n = lc.upsert("oi_5m", rows, now_iso)
    log(f"oi_5m へ複製: 更新 {n}行")

    # 2) 2026-10-01 以降の metrics
    d = OLD_END + timedelta(days=1)
    last_metrics = None
    while d <= yday:
        if not day_complete("oi_5m", d):
            b = http(f"{BV}/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip")
            if b is None:
                log(f"  metrics {d}: 未公開(404)")
            else:
                rs = [{"t": epoch(r[0]), "oi_btc": r[2], "oi_hi": "", "oi_lo": "", "src": "bv"} for r in zip_rows(b)]
                lc.upsert("oi_5m", rs, now_iso)
                last_metrics = d
        d += timedelta(days=1)
        time.sleep(0.1)

    # 3) 5分足ローソク
    d, got, skipped, missing_days = START, 0, 0, []
    while d <= yday:
        if day_complete("ohlcv_5m", d):
            skipped += 1
        else:
            b = http(f"{BV}/klines/BTCUSDT/5m/BTCUSDT-5m-{d}.zip")
            if b is None:
                missing_days.append(d.isoformat())
            else:
                rs = [{"t": to_sec(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                       "vol_btc": r[5], "taker_buy_vol_btc": r[9], "trades": r[8], "src": "bv"}
                      for r in zip_rows(b)]
                lc.upsert("ohlcv_5m", rs, now_iso)
                got += 1
        d += timedelta(days=1)
        time.sleep(0.1)
    log(f"ローソク: 取得 {got}日 / 既存のため省略 {skipped}日 / 未公開・なし {missing_days}")

    # 結果の検査: 5分の格子に対する欠損
    summary = {"ts": now_iso, "job": "backfill_once"}
    for kind in ("oi_5m", "ohlcv_5m"):
        ts = sorted({int(k[0]) for f in glob.glob(lc.path_for(kind, "*")) for k in lc.load(kind, os.path.basename(f)[:-4])})
        if not ts:
            summary[kind] = {"rows": 0}
            continue
        miss = sum((b - a) // BAR - 1 for a, b in zip(ts, ts[1:]) if b - a != BAR)
        summary[kind] = {"rows": len(ts), "from": ts[0], "to": ts[-1], "missing_in_range": miss}
        log(f"{kind}: {len(ts)}本 {datetime.fromtimestamp(ts[0], timezone.utc):%Y-%m-%d %H:%M} 〜 "
            f"{datetime.fromtimestamp(ts[-1], timezone.utc):%Y-%m-%d %H:%M} 欠損 {miss}")
    qdir = os.path.join(lc.ROOT, "quality")
    os.makedirs(qdir, exist_ok=True)
    with open(os.path.join(qdir, "quality_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"失敗: {e}")
        sys.exit(1)
