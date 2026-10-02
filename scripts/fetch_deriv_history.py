"""
デリバティブ指標の過去データ取得(手動実行用)
  1) OI・L/S比率・テイカー比率(5分刻み) : data.binance.vision  futures/um/daily/metrics
  2) Funding Rate(8時間ごと)            : data.binance.vision  futures/um/monthly/fundingRate
  3) 清算(日足のみ)                       : Coinalyze /liquidation-history  interval=daily
出力先: data/history/
注意: fapi.binance.com は米国IP(GitHub Actionsのランナー)から使えないため使わない
"""
import csv, gzip, hashlib, io, json, os, sys, time, urllib.request, zipfile
from datetime import date, datetime, timedelta, timezone

SYMBOL = "BTCUSDT"
START = date(2024, 10, 1)          # sar_history と同じ開始日
END = date(2026, 9, 30)            # この日まで(含む)
OUT = "data/history"
BV = "https://data.binance.vision/data/futures/um"

def get(url, tries=4, headers=None):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "btc-oi-recorder"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            print(f"  HTTP {e.code} {url} (retry {i+1})", flush=True)
        except Exception as e:
            print(f"  {e} {url} (retry {i+1})", flush=True)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"取得失敗: {url}")

def read_zip_csv(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0][:1].isdigit():   # ヘッダー行あり
        return rows[0], rows[1:]
    return None, rows

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def to_epoch(s):
    """'2024-10-01 00:05:00' 形式(UTC)→ UNIX秒"""
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())

def fetch_metrics():
    path = f"{OUT}/deriv_metrics_{SYMBOL}_5m.csv.gz"
    missing, n = [], 0
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "datetime_utc", "sum_open_interest", "sum_open_interest_value",
                    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                    "count_long_short_ratio", "sum_taker_long_short_vol_ratio"])
        d = START
        while d <= END:
            ds = d.isoformat()
            raw = get(f"{BV}/daily/metrics/{SYMBOL}/{SYMBOL}-metrics-{ds}.zip")
            if raw is None:
                missing.append(ds)
            else:
                header, rows = read_zip_csv(raw)
                for r in rows:
                    # 列: create_time, symbol, 以降6列
                    w.writerow([to_epoch(r[0]), r[0]] + r[2:8]); n += 1
            if d.day == 1:
                print(f"metrics {ds} ... 累計{n}行", flush=True)
            d += timedelta(days=1)
    return path, n, missing

def fetch_funding():
    path = f"{OUT}/funding_{SYMBOL}.csv.gz"
    missing, n = [], 0
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "datetime_utc", "funding_interval_hours", "last_funding_rate"])
        y, m = START.year, START.month
        while (y, m) <= (END.year, END.month):
            ym = f"{y}-{m:02d}"
            raw = get(f"{BV}/monthly/fundingRate/{SYMBOL}/{SYMBOL}-fundingRate-{ym}.zip")
            if raw is None:
                missing.append(ym)
            else:
                header, rows = read_zip_csv(raw)
                for r in rows:
                    ms = int(r[0]); t = ms // 1000
                    w.writerow([t, datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), r[1], r[2]]); n += 1
            print(f"funding {ym} ... 累計{n}行", flush=True)
            m += 1
            if m == 13: y, m = y + 1, 1
    return path, n, missing

def fetch_liq_daily():
    key = os.environ.get("COINALYZE_API_KEY")
    if not key:
        print("COINALYZE_API_KEY が未設定のため清算はスキップ", flush=True)
        return None, 0, ["no_api_key"]
    path = f"{OUT}/liquidation_daily_{SYMBOL}.csv"
    frm = int(datetime(START.year, START.month, START.day, tzinfo=timezone.utc).timestamp())
    to = int(datetime(END.year, END.month, END.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    url = (f"https://api.coinalyze.net/v1/liquidation-history?symbols=BTCUSDT_PERP.A"
           f"&interval=daily&from={frm}&to={to}&convert_to_usd=true")
    data = json.loads(get(url, headers={"api_key": key, "User-Agent": "btc-oi-recorder"}))
    hist = data[0]["history"] if data else []
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "date_utc", "long_liq_usd", "short_liq_usd"])
        for h in hist:
            w.writerow([h["t"], datetime.fromtimestamp(h["t"], timezone.utc).strftime("%Y-%m-%d"), h["l"], h["s"]])
    return path, len(hist), []

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    manifest = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "range": [START.isoformat(), END.isoformat()], "files": {}}
    for name, fn in [("metrics", fetch_metrics), ("funding", fetch_funding), ("liquidation_daily", fetch_liq_daily)]:
        print(f"=== {name} ===", flush=True)
        path, n, missing = fn()
        info = {"rows": n, "missing": missing}
        if path:
            info.update(path=path, sha256=sha256(path))
        manifest["files"][name] = info
        print(json.dumps(info, ensure_ascii=False), flush=True)
    with open(f"{OUT}/deriv_manifest.json", "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("完了", flush=True)
