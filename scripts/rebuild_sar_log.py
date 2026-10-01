#!/usr/bin/env python3
"""
sar_log.jsonl 再構築スクリプト(手元PCで実行する)

Binance公式の過去データ(data.binance.vision)から BTCUSDT無期限の確定15分足を取得し、
修正版 sar_tracker.py と同じ関数(bootstrap_psar / step_psar)でSAR・Nを計算し直す。
清算データ(Bybit近似)は既存の sar_log.jsonl から足の時刻(t)で結び付ける。

  入力 : 同じフォルダの sar_tracker.py(修正版)
         GitHub上の sar_log.jsonl(自動ダウンロード。既存ログには一切書き込まない)
  出力 : sar_log_rebuilt.jsonl(同じフォルダに新規作成)

使い方:  python rebuild_sar_log.py
"""
import io, json, sys, time, zipfile, csv
from datetime import datetime, timedelta, timezone
import requests

import sar_tracker as S   # 修正版と同じSAR計算ロジックをそのまま使う

OLD_LOG_URL = "https://raw.githubusercontent.com/bluefujii0016/btc-oi-recorder/refs/heads/main/data/sar_log.jsonl"
DAILY_URL = "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/15m/BTCUSDT-15m-{d}.zip"
REST_URL = "https://fapi.binance.com/fapi/v1/klines"
BAR = 900
WARMUP_DAYS = 3          # 出力開始前にSARを安定させるための助走期間
OUT_PATH = "sar_log_rebuilt.jsonl"


def load_old_log():
    r = requests.get(OLD_LOG_URL, timeout=60)
    r.raise_for_status()
    rows = {}
    for line in r.text.splitlines():
        if line.strip():
            rec = json.loads(line)
            rows[rec["t"]] = rec          # 同じtは後の行を採用
    return rows


def parse_kline_rows(rows):
    bars = []
    for row in rows:
        if not row or not str(row[0]).strip().isdigit():
            continue                       # ヘッダ行を飛ばす
        bars.append({"t": int(row[0]) // 1000, "o": float(row[1]), "h": float(row[2]),
                     "l": float(row[3]), "c": float(row[4]), "v": float(row[5])})
    return bars


def fetch_daily(day):
    url = DAILY_URL.format(d=day.strftime("%Y-%m-%d"))
    r = requests.get(url, timeout=60)
    if r.status_code == 404:
        return None                        # まだ公開されていない日
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open(z.namelist()[0]) as f:
            return parse_kline_rows(csv.reader(io.TextIOWrapper(f, "utf-8")))


def fetch_rest(start_t):
    bars, t = [], start_t
    while True:
        r = requests.get(REST_URL, params={"symbol": "BTCUSDT", "interval": "15m",
                                           "startTime": t * 1000, "limit": 1500}, timeout=30)
        r.raise_for_status()
        chunk = parse_kline_rows(r.json())
        if not chunk:
            break
        bars += chunk
        if len(chunk) < 1500:
            break
        t = chunk[-1]["t"] + BAR
    return bars


def fetch_bars(start_t):
    day = datetime.fromtimestamp(start_t, tz=timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    bars = []
    while day < today:
        got = fetch_daily(day)
        if got is None:
            break
        bars += got
        print(f"  {day} : {len(got)}本")
        day += timedelta(days=1)
    rest_from = (bars[-1]["t"] + BAR) if bars else start_t
    rest = fetch_rest(rest_from)
    print(f"  直近分(REST API) : {len(rest)}本")
    bars += rest
    now = int(time.time())
    uniq = {b["t"]: b for b in bars if b["t"] + BAR <= now}   # 確定足のみ
    return [uniq[t] for t in sorted(uniq)]


def main():
    print("既存ログを取得中...")
    old = load_old_log()
    first_t = min(old)
    print(f"  既存ログ: {len(old)}行  開始 {datetime.fromtimestamp(first_t, tz=timezone.utc)}")

    print("Binanceの確定15分足を取得中...")
    bars = fetch_bars(first_t - WARMUP_DAYS * 86400)
    gaps = sum(1 for a, b in zip(bars, bars[1:]) if b["t"] - a["t"] != BAR)
    print(f"  合計 {len(bars)}本  欠損箇所 {gaps}")

    warm = [b for b in bars if b["t"] < first_t]
    body = [b for b in bars if b["t"] >= first_t]
    state, _ = S.bootstrap_psar(warm)
    close_by_t = {b["t"]: b["c"] for b in bars}

    out, flips_new = [], 0
    for b in body:
        state, rec = S.step_psar(state, b)
        flips_new += rec["reversed"]
        row = {k: rec[k] for k in ["t", "open", "high", "low", "close", "sar", "af", "ep",
                                   "trend", "dots_since_flip"]}
        for h in S.TREND_WINDOWS_HOURS:
            ref = close_by_t.get(b["t"] - h * 3600)
            row[f"trend_{h}h"] = None if ref is None else ("up" if b["c"] > ref else "down")
        o = old.get(b["t"])
        row["liq_long_bybit_approx"] = o.get("liq_long_bybit_approx") if o else None
        row["liq_short_bybit_approx"] = o.get("liq_short_bybit_approx") if o else None
        if rec.get("candlestick_patterns"):
            row["candlestick_patterns"] = rec["candlestick_patterns"]
        row["bar_confirmed"] = True
        row["source"] = "binance_vision_rebuild"
        out.append(row)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for row in out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 旧ログとの比較(参考)
    common = [r for r in out if r["t"] in old]
    flips_old = sum(1 for t in old if old[t].get("dots_since_flip") == 1)
    same_trend = sum(1 for r in common if old[r["t"]].get("trend") == r["trend"])
    same_n = sum(1 for r in common if old[r["t"]].get("dots_since_flip") == r["dots_since_flip"])
    print("\n=== 完了 ===")
    print(f"出力: {OUT_PATH}  {len(out)}行")
    print(f"転換回数  旧ログ {flips_old}回 / 再構築 {flips_new}回")
    if common:
        print(f"同じ時刻の足でトレンド方向が一致: {same_trend/len(common)*100:.1f}%  "
              f"Nが一致: {same_n/len(common)*100:.1f}%  (比較 {len(common)}本)")


if __name__ == "__main__":
    main()
