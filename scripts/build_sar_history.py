#!/usr/bin/env python3
"""
長期検証用データセット作成スクリプト(手元PCで実行)

Binance公式の過去データ(data.binance.vision)から BTCUSDT無期限の確定15分足を取得し、
稼働中の sar_tracker.py と同じ関数(bootstrap_psar / step_psar)で SAR・N・ローソク足パターンを計算する。

  入力 : 同じフォルダの sar_tracker.py(修正版)
         同じフォルダの sar_log_rebuilt.jsonl(あれば、計算結果の照合に使う。無くても動く)
  出力 : sar_history_BTCUSDT_15m.csv.gz

使い方:  python build_sar_history.py
         開始日を変える場合:  python build_sar_history.py 2024-10-01
"""
import csv, gzip, io, json, os, sys, time, zipfile
from datetime import date, datetime, timedelta, timezone
import requests

import sar_tracker as S

START = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2024, 10, 1)
WARMUP_DAYS = 3
BAR = 900
OUT = "sar_history_BTCUSDT_15m.csv.gz"
BASE = "https://data.binance.vision/data/futures/um"
MONTHLY = BASE + "/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-{y:04d}-{m:02d}.zip"
DAILY = BASE + "/daily/klines/BTCUSDT/15m/BTCUSDT-15m-{d}.zip"
REST = "https://fapi.binance.com/fapi/v1/klines"


def parse(rows):
    out = []
    for r in rows:
        if not r or not str(r[0]).strip().isdigit():
            continue                                   # ヘッダ行を飛ばす
        out.append({"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                    "c": float(r[4]), "v": float(r[5]), "qv": float(r[7]), "n": int(r[8]),
                    "tbv": float(r[9]), "tbqv": float(r[10])})
    return out


def get_zip(url):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z, z.open(z.namelist()[0]) as f:
                return parse(csv.reader(io.TextIOWrapper(f, "utf-8")))
        except requests.RequestException as e:
            print(f"    再試行 {attempt + 1}/3: {e}")
            time.sleep(3)
    raise RuntimeError(f"取得に失敗しました: {url}")


def get_rest(start_t):
    bars, t = [], start_t
    while True:
        r = requests.get(REST, params={"symbol": "BTCUSDT", "interval": "15m",
                                       "startTime": t * 1000, "limit": 1500}, timeout=30)
        r.raise_for_status()
        chunk = parse(r.json())
        if not chunk:
            break
        bars += chunk
        if len(chunk) < 1500:
            break
        t = chunk[-1]["t"] + BAR
    return bars


def fetch_all(first_day):
    today = datetime.now(timezone.utc).date()
    bars, day = [], first_day
    while day < today:
        if day.day == 1 and (day.replace(day=28) + timedelta(days=4)).replace(day=1) <= today:
            got = get_zip(MONTHLY.format(y=day.year, m=day.month))      # 月単位(速い)
            if got is not None:
                print(f"  {day:%Y-%m} : {len(got)}本")
                bars += got
                day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
                continue
        got = get_zip(DAILY.format(d=day.isoformat()))                   # 日単位
        if got is None:
            break                                                        # まだ公開されていない日
        print(f"  {day} : {len(got)}本")
        bars += got
        day += timedelta(days=1)
    rest_from = bars[-1]["t"] + BAR if bars else int(datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc).timestamp())
    rest = get_rest(rest_from)
    print(f"  直近分(REST API) : {len(rest)}本")
    bars += rest
    now = int(time.time())
    uniq = {b["t"]: b for b in bars if b["t"] + BAR <= now}               # 確定足のみ
    return [uniq[t] for t in sorted(uniq)]


def main():
    first_day = START - timedelta(days=WARMUP_DAYS)
    print(f"Binanceの確定15分足を取得中({first_day} 〜 現在)...")
    bars = fetch_all(first_day)
    start_t = int(datetime(START.year, START.month, START.day, tzinfo=timezone.utc).timestamp())
    gaps = [(a["t"], b["t"]) for a, b in zip(bars, bars[1:]) if b["t"] - a["t"] != BAR]
    print(f"  合計 {len(bars)}本  欠損箇所 {len(gaps)}")
    for a, b in gaps[:10]:
        print(f"    欠損: {datetime.fromtimestamp(a, tz=timezone.utc):%Y-%m-%d %H:%M} の次が "
              f"{datetime.fromtimestamp(b, tz=timezone.utc):%Y-%m-%d %H:%M}")

    warm = [b for b in bars if b["t"] < start_t]
    body = [b for b in bars if b["t"] >= start_t]
    state, _ = S.bootstrap_psar(warm)
    close_by_t = {b["t"]: b["c"] for b in bars}

    cols = ["t", "datetime_utc", "open", "high", "low", "close", "volume", "quote_volume", "trades",
            "taker_buy_volume", "taker_buy_quote_volume", "sar", "af", "ep", "trend", "dots_since_flip",
            "reversed", "trend_8h", "trend_12h", "trend_24h", "candlestick_patterns"]
    rows, flips = [], 0
    for b in body:
        state, rec = S.step_psar(state, b)
        flips += rec["reversed"]
        tr = {}
        for h in S.TREND_WINDOWS_HOURS:
            ref = close_by_t.get(b["t"] - h * 3600)
            tr[h] = "" if ref is None else ("up" if b["c"] > ref else "down")
        rows.append([b["t"], datetime.fromtimestamp(b["t"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                     b["o"], b["h"], b["l"], b["c"], b["v"], b["qv"], b["n"], b["tbv"], b["tbqv"],
                     round(rec["sar"], 2), round(rec["af"], 2), rec["ep"], rec["trend"], rec["dots_since_flip"],
                     int(rec["reversed"]), tr[8], tr[12], tr[24], "|".join(rec.get("candlestick_patterns") or [])])

    with gzip.open(OUT, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    print("\n=== 完了 ===")
    print(f"出力: {OUT}  {len(rows)}行  {rows[0][1]} 〜 {rows[-1][1]} UTC  転換 {flips}回")
    print(f"ファイルサイズ: {os.path.getsize(OUT) / 1024 / 1024:.1f} MB")

    # 再構築ログとの照合(同じ足でトレンド・Nが一致するか)
    if os.path.exists("sar_log_rebuilt.jsonl"):
        rb = {}
        for line in open("sar_log_rebuilt.jsonl", encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                rb[r["t"]] = r
        common = [r for r in rows if r[0] in rb]
        if common:
            same = sum(1 for r in common if rb[r[0]]["trend"] == r[14] and rb[r[0]]["dots_since_flip"] == r[15])
            print(f"再構築ログとの照合: トレンド・Nとも一致 {same}/{len(common)}本 ({same / len(common) * 100:.1f}%)")


if __name__ == "__main__":
    main()
