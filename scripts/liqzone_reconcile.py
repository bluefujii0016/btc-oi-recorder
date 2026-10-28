#!/usr/bin/env python3
"""
清算帯推定 フェーズ1 ステップ3: reconcile_daily

直近7日(UTC、前日まで)のうち data.binance.vision に公開済みで、まだ公開データ(src=bv)で
確定していない日について、OI(metrics)と5分足ローソクを取得して src=bv で差し替える。
差し替え前に、同じ時刻の cv(Coinalyze) と bv の差を測り quality_log.jsonl に1行で残す。
未公開(404)の日は飛ばす(公開は約1〜2日遅れ)。冪等。
標準ライブラリのみ。
"""
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_collect as lc
import liqzone_backfill as bf

LOOKBACK_DAYS = 7


def log(s):
    print(s, flush=True)


def dist(vals):
    if not vals:
        return {"n": 0}
    v = sorted(vals)
    return {"n": len(v), "median": statistics.median(v), "p95": v[min(len(v) - 1, int(len(v) * 0.95))], "max": v[-1]}


def rel(a, b):
    a, b = float(a), float(b)
    return abs(a - b) / b if b else 0.0


def main():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).date()
    days_report, skipped, failed = [], [], False
    for back in range(LOOKBACK_DAYS, 0, -1):
        d = today - timedelta(days=back)
        ds = d.isoformat()
        oi_done = bf.day_complete("oi_5m", d)
        px_done = bf.day_complete("ohlcv_5m", d)
        if oi_done and px_done:
            continue
        rep = {"day": ds}
        try:
            if not oi_done:
                b = bf.http(f"{bf.BV}/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip")
                if b is None:
                    skipped.append(f"{ds} metrics")
                else:
                    new = [{"t": bf.epoch(r[0]), "oi_btc": r[2], "oi_hi": "", "oi_lo": "", "src": "bv"} for r in bf.zip_rows(b)]
                    cur = lc.load("oi_5m", ds)
                    diffs = [rel(cur[(r["t"], "")]["oi_btc"], r["oi_btc"]) for r in new
                             if (r["t"], "") in cur and cur[(r["t"], "")].get("src") == "cv"]
                    rep["oi"] = {"replaced_cv": len(diffs), "added": sum(1 for r in new if (r["t"], "") not in cur), "diff": dist(diffs)}
                    lc.upsert("oi_5m", new, now_iso)
            if not px_done:
                b = bf.http(f"{bf.BV}/klines/BTCUSDT/5m/BTCUSDT-5m-{d}.zip")
                if b is None:
                    skipped.append(f"{ds} klines")
                else:
                    new = [{"t": bf.to_sec(r[0]), "o": r[1], "h": r[2], "l": r[3], "c": r[4],
                            "vol_btc": r[5], "taker_buy_vol_btc": r[9], "trades": r[8], "src": "bv"} for r in bf.zip_rows(b)]
                    cur = lc.load("ohlcv_5m", ds)
                    diffs = [rel(cur[(r["t"], "")]["c"], r["c"]) for r in new
                             if (r["t"], "") in cur and cur[(r["t"], "")].get("src") == "cv"]
                    rep["px"] = {"replaced_cv": len(diffs), "added": sum(1 for r in new if (r["t"], "") not in cur), "diff": dist(diffs)}
                    lc.upsert("ohlcv_5m", new, now_iso)
        except Exception as e:
            log(f"  {ds}: 失敗 {e}")
            rep["error"] = str(e)
            failed = True
        if len(rep) > 1:
            days_report.append(rep)
        time.sleep(0.2)

    log(f"差し替え: {[r['day'] for r in days_report]} / 未公開: {skipped}")
    for r in days_report:
        log(f"  {json.dumps(r, ensure_ascii=False)}")
    qdir = os.path.join(lc.ROOT, "quality")
    os.makedirs(qdir, exist_ok=True)
    with open(os.path.join(qdir, "quality_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso, "job": "reconcile_daily", "days": days_report, "unpublished": skipped},
                           ensure_ascii=False) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"失敗: {e}")
        sys.exit(1)
