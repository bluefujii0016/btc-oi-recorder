#!/usr/bin/env python3
"""
Coinalyze API経由でBinance BTCUSDT永久先物のOI・Funding Rateを取得し、
Coinbase公開APIからBTC価格を取得して data/oi_history.json に追記する。

なぜCoinalyzeか：
  Binance/Bybit等の取引所APIは米国IP（GitHub Actionsのサーバー）を
  地域ブロックする（HTTP 451/403）。Coinalyzeはデータプロバイダーで
  地域制限がなく、Binanceのデータ（シンボル BTCUSDT_PERP.A）を取得できる。

必要な環境変数：
  COINALYZE_API_KEY … Coinalyze無料APIキー（GitHub Secretsに設定）

出力形式は OI_Recorder.html の記録形式と完全互換:
  { "ts": ISO8601, "price": "107250", "oi": "12.34", "funding": "0.0102" }
新しい順に並び、最大 MAX_ENTRIES 件を保持する。
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SYMBOL = "BTCUSDT_PERP.A"   # .A = Binance（Coinalyzeの取引所コード）

API_OI      = f"https://api.coinalyze.net/v1/open-interest?symbols={SYMBOL}&convert_to_usd=true"
API_FUNDING = f"https://api.coinalyze.net/v1/funding-rate?symbols={SYMBOL}"
API_PRICE   = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

DATA_FILE   = Path(__file__).resolve().parent.parent / "data" / "oi_history.json"
MAX_ENTRIES = 200          # 保持する最大件数（約33日分 @ 4時間おき）
MIN_INTERVAL_MIN = 30      # 直前の記録からこの分数以内なら重複とみなしてスキップ


def get_json(url: str, api_key: str | None = None):
    headers = {"User-Agent": "oi-recorder-bot/2.0"}
    if api_key:
        headers["api_key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def main() -> int:
    api_key = os.environ.get("COINALYZE_API_KEY", "").strip()
    if not api_key:
        print("ERROR: 環境変数 COINALYZE_API_KEY が設定されていません。", file=sys.stderr)
        print("GitHubリポジトリの Settings > Secrets and variables > Actions で", file=sys.stderr)
        print("COINALYZE_API_KEY という名前のSecretを追加してください。", file=sys.stderr)
        return 1

    # ── 取得 ──────────────────────────────────────────────
    oi_res      = get_json(API_OI, api_key)
    funding_res = get_json(API_FUNDING, api_key)
    price_res   = get_json(API_PRICE)

    price_f = float(price_res["data"]["amount"])

    # OI: convert_to_usd=true でUSD建て想定。万一BTC建てで返ってきた場合の
    # 保険として、値が小さすぎる（1000万未満＝BTC枚数とみなせる）ときは価格を掛ける
    oi_raw = float(oi_res[0]["value"])
    oi_usd = oi_raw if oi_raw >= 1e7 else oi_raw * price_f
    oi_b   = oi_usd / 1e9

    # Funding: %表記想定（例 0.0100）。万一小数表記（0.000100）で返ってきた
    # 場合の保険として、絶対値が0.005未満なら100倍して%に揃える
    fr_raw = float(funding_res[0]["value"])
    funding_pct = fr_raw * 100.0 if abs(fr_raw) < 0.005 else fr_raw

    now = datetime.now(timezone.utc)
    entry = {
        "ts":      now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "price":   f"{price_f:.0f}",
        "oi":      f"{oi_b:.2f}",
        "funding": f"{funding_pct:.4f}",
        "source":  "coinalyze-binance-auto",
    }

    # 検証用に生値もログに出す（初回運用時のCoinGlassとの突き合わせ用）
    print(f"raw: oi_value={oi_raw} funding_value={fr_raw} price={price_f}")

    # ── 読み込み・重複ガード ──────────────────────────────
    history = []
    if DATA_FILE.exists():
        try:
            history = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARN: data file was corrupt; starting fresh", file=sys.stderr)

    if history:
        last_ts = datetime.fromisoformat(history[0]["ts"].replace("Z", "+00:00"))
        elapsed_min = (now - last_ts).total_seconds() / 60
        if elapsed_min < MIN_INTERVAL_MIN:
            print(f"SKIP: last entry was {elapsed_min:.0f} min ago (< {MIN_INTERVAL_MIN} min)")
            return 0

    # ── 追記・保存（新しい順・件数上限） ──────────────────
    history.insert(0, entry)
    history = history[:MAX_ENTRIES]

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"OK: {entry['ts']} | price ${entry['price']} | OI {entry['oi']}B | funding {entry['funding']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
