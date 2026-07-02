#!/usr/bin/env python3
"""
Binance公開APIからBTC永久先物のデータを取得し、data/oi_history.json に追記する。
- 価格: markPrice（premiumIndex）
- Funding Rate: lastFundingRate（premiumIndex）を%換算
- OI: sumOpenInterestValue（openInterestHist、USDT建て）を十億ドル(B)換算

出力形式は OI_Recorder.html の記録形式と完全互換:
  { "ts": ISO8601, "price": "107250", "oi": "12.34", "funding": "0.0102" }
新しい順に並び、最大 MAX_ENTRIES 件を保持する。

APIキー不要（公開エンドポイントのみ）・外部ライブラリ不要（標準ライブラリのみ）。
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SYMBOL      = "BTCUSDT"
API_PREMIUM = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}"
API_OI_HIST = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={SYMBOL}&period=5m&limit=1"

DATA_FILE   = Path(__file__).resolve().parent.parent / "data" / "oi_history.json"
MAX_ENTRIES = 200          # 保持する最大件数（約33日分 @ 4時間おき）
MIN_INTERVAL_MIN = 30      # 直前の記録からこの分数以内なら重複とみなしてスキップ


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "oi-recorder-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def main() -> int:
    # ── 取得 ──────────────────────────────────────────────
    premium = get_json(API_PREMIUM)
    oi_hist = get_json(API_OI_HIST)

    price_f   = float(premium["markPrice"])
    funding_f = float(premium["lastFundingRate"]) * 100.0   # 小数 → %
    oi_b      = float(oi_hist[0]["sumOpenInterestValue"]) / 1e9  # USDT → 十億ドル

    now = datetime.now(timezone.utc)
    entry = {
        "ts":      now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "price":   f"{price_f:.0f}",
        "oi":      f"{oi_b:.2f}",
        "funding": f"{funding_f:.4f}",
        "source":  "binance-auto",   # 手動記録と区別するためのフラグ（Recorder側では無視される）
    }

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
