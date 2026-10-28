#!/usr/bin/env python3
"""
清算帯推定 フェーズ1 ステップ0: 接続と継ぎ目の確認(読み取り専用)

目的(仕様書「実装順序」ステップ0の受け入れ基準):
  1. GitHub Actions(米国IP)から Binance fapi が拒否されること、および
     data.binance.vision に届くこと、前日分がいつ公開されるかを確認する
  2. Coinalyze の5分足(OI・OHLCV・清算)の形式・単位・形成中の足の扱いを確認する
  3. Coinalyze と Binance公開データを同じ日で突き合わせ、時刻の対応と差を測る

ファイルは一切保存しない。結果は標準出力と GITHUB_STEP_SUMMARY に出す。
APIキーは出力しない(ヘッダーでのみ送る)。標準ライブラリのみ使用。
"""
import csv
import io
import json
import os
import statistics
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

SYMBOL_A = "BTCUSDT_PERP.A"   # Binance BTCUSDT Perp
SYMBOL_6 = "BTCUSDT.6"        # Bybit BTCUSDT Perp
BV = "https://data.binance.vision/data/futures/um/daily"
CZ = "https://api.coinalyze.net/v1"
KEY = os.environ.get("COINALYZE_API_KEY", "").strip()
BAR = 300

lines = []


def p(s=""):
    print(s, flush=True)
    lines.append(s)


def http(url, method="GET", headers=None, timeout=60):
    h = {"User-Agent": "liqzone-probe"}
    h.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read() if method == "GET" else b""
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"")[:300]
    except Exception as e:  # 接続失敗など
        return None, {}, str(e).encode()


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def cz_get(path, params):
    """Coinalyze GET。429 は Retry-After 秒待って1回だけ再試行。失敗は None。"""
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{CZ}/{path}?{q}"
    for attempt in range(2):
        st, h, b = http(url, headers={"api_key": KEY})
        time.sleep(2)
        if st == 429 and attempt == 0:
            wait = int(h.get("Retry-After", "10"))
            p(f"  - {path}: 429、{wait}秒待って再試行")
            time.sleep(min(wait, 60))
            continue
        if st != 200:
            p(f"  - {path}: HTTP {st} {b[:150]!r}")
            return None
        return json.loads(b)
    return None


def history(data):
    if not data or "history" not in data[0]:
        return []
    return data[0]["history"]


def zip_csv(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0][:1].isdigit():
        rows = rows[1:]
    return rows


def to_epoch(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def stats(vals):
    if not vals:
        return "n=0"
    vals = sorted(vals)
    p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
    return f"n={len(vals)} 中央値={statistics.median(vals):.3e} p95={p95:.3e} 最大={vals[-1]:.3e}"


def main():
    now = int(time.time())
    today = datetime.now(timezone.utc).date()
    p(f"# 清算帯推定 ステップ0 確認結果")
    p(f"実行時刻(UTC): {utc(now)}")
    p()

    # 1) fapi が拒否されるか
    p("## 1. Binance fapi への直接接続(Actions から)")
    st, _, body = http("https://fapi.binance.com/fapi/v1/ping")
    p(f"- fapi.binance.com /fapi/v1/ping: HTTP {st}")
    p()

    # 2) data.binance.vision の到達性と公開時刻
    p("## 2. data.binance.vision の到達性と公開時刻")
    p("| 種別 | 対象日(UTC) | HTTP | Last-Modified | サイズ |")
    p("| --- | --- | --- | --- | --- |")
    urls = {}
    avail = {}
    for kind, tmpl in (
        ("metrics", BV + "/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip"),
        ("klines5m", BV + "/klines/BTCUSDT/5m/BTCUSDT-5m-{d}.zip"),
    ):
        for back in range(0, 4):
            d = today - timedelta(days=back)
            url = tmpl.format(d=d.isoformat())
            st, h, _ = http(url, method="HEAD")
            p(f"| {kind} | {d} | {st} | {h.get('Last-Modified', '')} | {h.get('Content-Length', '')} |")
            urls[(kind, d)] = url
            avail[(kind, d)] = (st == 200)
    p()

    # 3) Coinalyze
    p("## 3. Coinalyze 5分足の形式")
    if not KEY:
        p("- COINALYZE_API_KEY が未設定のためスキップ")
        finish()
        return

    day = None
    for back in (1, 2, 3):
        d = today - timedelta(days=back)
        if avail.get(("metrics", d)) and avail.get(("klines5m", d)):
            day = d
            break
    if day is None:
        day = today - timedelta(days=1)
        p(f"- 突き合わせ可能な公開データが見つからない。対象日は仮に {day}")
    frm = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    to = frm + 86400
    # 形成中の足の確認のため、末尾の直近分も別に取る
    recent_from = now - 3 * 3600

    oi_btc = history(cz_get("open-interest-history", {"symbols": SYMBOL_A, "interval": "5min", "from": frm, "to": to, "convert_to_usd": "false"}))
    oi_usd = history(cz_get("open-interest-history", {"symbols": SYMBOL_A, "interval": "5min", "from": frm, "to": to, "convert_to_usd": "true"}))
    ohlcv = history(cz_get("ohlcv-history", {"symbols": SYMBOL_A, "interval": "5min", "from": frm, "to": to}))
    recent = history(cz_get("open-interest-history", {"symbols": SYMBOL_A, "interval": "5min", "from": recent_from, "to": now, "convert_to_usd": "false"}))

    p(f"- 対象日: {day}(UTC)。期待される5分足は288本")
    if oi_btc:
        cs = [r["c"] for r in oi_btc]
        p(f"- OI(convert_to_usd=false): {len(oi_btc)}本、{utc(oi_btc[0]['t'])} 〜 {utc(oi_btc[-1]['t'])}、c の中央値 {statistics.median(cs):,.1f}、フィールド {sorted(oi_btc[0].keys())}")
    if oi_usd and oi_btc:
        ratio = statistics.median(r["c"] for r in oi_usd) / statistics.median(r["c"] for r in oi_btc)
        p(f"- OI(convert_to_usd=true) / false の中央値比: {ratio:,.0f}(価格の水準に近ければ false は BTC建て)")
    if ohlcv:
        p(f"- OHLCV: {len(ohlcv)}本、{utc(ohlcv[0]['t'])} 〜 {utc(ohlcv[-1]['t'])}、フィールド {sorted(ohlcv[0].keys())}")
    if recent:
        last = recent[-1]
        forming = last["t"] + BAR > now
        p(f"- 直近3時間の末尾の足: t={utc(last['t'])}、現在時刻 {utc(now)}、形成中={forming}(t+300 > 現在)")
        p(f"  - 末尾3本の c: {[r['c'] for r in recent[-3:]]}")

    # 清算
    for sym in (SYMBOL_A, SYMBOL_6):
        liq = history(cz_get("liquidation-history", {"symbols": sym, "interval": "5min", "from": frm, "to": to}))
        nz = [r for r in liq if (r.get("l", 0) or r.get("s", 0))]
        p(f"- 清算 {sym}: {len(liq)}本(期待288)、うち非ゼロ {len(nz)}本、l合計 {sum(r.get('l', 0) for r in liq):,.2f}、s合計 {sum(r.get('s', 0) for r in liq):,.2f}(単位は要確認)")
    p()

    # 4) 継ぎ目
    p("## 4. 継ぎ目の検証(同じ日の Coinalyze と Binance公開データ)")
    if not (avail.get(("metrics", day)) and avail.get(("klines5m", day))):
        p(f"- {day} の公開データが未公開のため、突き合わせはスキップ")
        finish()
        return

    st, _, raw = http(urls[("metrics", day)])
    bv_oi = {}
    if st == 200:
        for r in zip_csv(raw):
            bv_oi[to_epoch(r[0])] = float(r[2])  # sum_open_interest (BTC)
    st, _, raw = http(urls[("klines5m", day)])
    bv_k = {}
    bv_vol = {}
    if st == 200:
        for r in zip_csv(raw):
            t = int(r[0]) // 1000
            bv_k[t] = float(r[4])
            bv_vol[t] = float(r[5])
    p(f"- 公開データ: metrics {len(bv_oi)}行、klines {len(bv_k)}行")

    cz_oi = {r["t"]: r["c"] for r in oi_btc}
    p("")
    p("### OI(BTC建て): Coinalyze の足 t の c と、Binance metrics の時刻 t+k の値の相対差")
    p("| k(秒) | 比較結果 |")
    p("| --- | --- |")
    best = None
    for k in (-300, 0, 300, 600):
        diffs = [abs(c - bv_oi[t + k]) / bv_oi[t + k] for t, c in cz_oi.items() if (t + k) in bv_oi and bv_oi[t + k]]
        p(f"| {k} | {stats(diffs)} |")
        if diffs and (best is None or statistics.median(diffs) < best[1]):
            best = (k, statistics.median(diffs))
    if best:
        p(f"- 差が最小になるずれ: k={best[0]}秒(中央値 {best[1]:.3e})")

    cz_close = {r["t"]: r["c"] for r in ohlcv}
    p("")
    p("### 価格: Coinalyze の足 t の終値と、Binance 5分足(開始時刻 t+k)の終値の相対差")
    p("| k(秒) | 比較結果 |")
    p("| --- | --- |")
    for k in (-300, 0, 300):
        diffs = [abs(c - bv_k[t + k]) / bv_k[t + k] for t, c in cz_close.items() if (t + k) in bv_k and bv_k[t + k]]
        p(f"| {k} | {stats(diffs)} |")
    cz_vol = {r["t"]: r["v"] for r in ohlcv}
    ratios = [cz_vol[t] / bv_vol[t] for t in cz_vol if t in bv_vol and bv_vol[t]]
    p(f"- 出来高の比(Coinalyze / Binance、同一 t): {stats(ratios)}(1に近ければ同じ単位)")
    p("")
    finish()


def finish():
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
