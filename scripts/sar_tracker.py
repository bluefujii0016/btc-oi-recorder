#!/usr/bin/env python3
"""
sar_tracker.py

Coinalyze の /ohlcv-history から BTCUSDT_PERP.A の15分足OHLCVを取得し、
Wilder式 Parabolic SAR (AF初期値0.02 / 刻み0.02 / 上限0.20) を
決定論的に再計算する。

転換後1点目を検知したら:
  1. Discord Webhookに通知を送信
  2. sar_log.jsonl に1レコード追記(BTC_pattern_observer等での後日検証用)
  3. sar_state.json を更新(次回実行時の重複通知防止)

既存のOI Recorderパイプラインと同様、Binance BTCUSDT Perpのみを対象とし、
クロス取引所の混在は行わない。
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
COINALYZE_API_KEY = os.environ.get("COINALYZE_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

SYMBOL = "BTCUSDT_PERP.A"          # Binance BTCUSDT Perp (既存パイプラインと同一スコープ)
INTERVAL = "15min"                  # Coinalyze側の interval 表記
LOOKBACK_SECONDS = 60 * 60 * 24 * 3  # 直近3日分(15分足で約288本)を取得してSARを再計算
                                     # トレンドが長期化している場合は要調整

AF_START = 0.02
AF_STEP = 0.02
AF_MAX = 0.20

STATE_PATH = "data/sar_state.json"
LOG_PATH = "data/sar_log.jsonl"

OHLCV_URL = "https://api.coinalyze.net/v1/ohlcv-history"


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------
def fetch_ohlcv():
    now = int(time.time())
    params = {
        "symbols": SYMBOL,
        "interval": INTERVAL,
        "from": now - LOOKBACK_SECONDS,
        "to": now,
    }
    headers = {"api_key": COINALYZE_API_KEY}
    resp = requests.get(OHLCV_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data or "history" not in data[0]:
        raise RuntimeError(f"Coinalyzeレスポンス形式が想定と異なります: {data}")

    bars = data[0]["history"]
    # 念のため時刻昇順を保証
    bars = sorted(bars, key=lambda b: b["t"])
    return bars


# ---------------------------------------------------------------------------
# Wilder式 Parabolic SAR 計算
# ---------------------------------------------------------------------------
def compute_psar(bars, af_start=AF_START, af_step=AF_STEP, af_max=AF_MAX):
    """
    bars: [{'t':unix_ts, 'o':..,'h':..,'l':..,'c':..}, ...] 昇順
    戻り値: 各バーに対応する dict のリスト
      {t, close, sar, trend('up'/'down'), af, ep, reversed(bool)}
    """
    n = len(bars)
    if n < 3:
        raise RuntimeError("SAR計算には最低3本以上のバーが必要です")

    high = [b["h"] for b in bars]
    low = [b["l"] for b in bars]
    close = [b["c"] for b in bars]
    ts = [b["t"] for b in bars]

    # 初期トレンドは最初の2本の close 比較で仮決め
    bull = close[1] >= close[0]
    af = af_start
    if bull:
        sar = low[0]
        ep = high[1]
    else:
        sar = high[0]
        ep = low[1]

    results = []
    results.append({
        "t": ts[0], "close": close[0], "sar": sar,
        "trend": "up" if bull else "down", "af": af, "ep": ep, "reversed": False
    })

    for i in range(1, n):
        prev_sar = sar
        reversed_flag = False

        # 通常のSAR前進計算
        sar = prev_sar + af * (ep - prev_sar)

        if bull:
            # 上昇トレンド: SARは安値を上回ってはいけない(直近2本の安値でクリップ)
            sar = min(sar, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < sar:
                # 反転: 下降トレンドへ
                bull = False
                reversed_flag = True
                sar = ep          # 直前トレンドのEPを新SARに
                ep = low[i]
                af = af_start
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:
            # 下降トレンド: SARは高値を下回ってはいけない(直近2本の高値でクリップ)
            sar = max(sar, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > sar:
                # 反転: 上昇トレンドへ
                bull = True
                reversed_flag = True
                sar = ep
                ep = high[i]
                af = af_start
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

        results.append({
            "t": ts[i], "close": close[i], "sar": sar,
            "trend": "up" if bull else "down", "af": af, "ep": ep,
            "reversed": reversed_flag
        })

    return results


def dots_since_flip(results):
    """末尾バーが直近の転換から何点目かを数える(転換した足自体が1点目)"""
    count = 0
    for r in reversed(results):
        count += 1
        if r["reversed"]:
            return count
    return count  # 系列内に転換が一度もない場合


# ---------------------------------------------------------------------------
# 状態管理(重複通知防止)
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_notified_flip_t": None, "last_processed_t": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# ログ蓄積(後日のパターン検証用)
# ---------------------------------------------------------------------------
def append_log(record):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Discord通知
# ---------------------------------------------------------------------------
def notify_discord(record):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL未設定のため通知をスキップします", file=sys.stderr)
        return

    direction_jp = "上昇" if record["trend"] == "up" else "下落"
    dt = datetime.fromtimestamp(record["t"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    content = (
        f"**SAR転換検知(1点目)**\n"
        f"方向: {direction_jp}\n"
        f"時刻: {dt}\n"
        f"価格: {record['close']:.1f}\n"
        f"SAR値: {record['sar']:.1f}\n"
        f"AF: {record['af']:.2f}\n"
        f"✅ Coinalyze OHLCVから決定論的に計算(Wilder式 0.02/0.02/0.20)"
    )

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def main():
    if not COINALYZE_API_KEY:
        print("COINALYZE_API_KEY が未設定です", file=sys.stderr)
        sys.exit(1)

    bars = fetch_ohlcv()
    results = compute_psar(bars)

    state = load_state()
    last_processed_t = state.get("last_processed_t")

    # 前回実行以降に新しく確定した足だけを対象にする
    # (last_processed_t が None なら初回実行なので最新1本のみ扱う)
    if last_processed_t is None:
        new_bars = [(len(results) - 1, results[-1])]
    else:
        new_bars = [(i, r) for i, r in enumerate(results) if r["t"] > last_processed_t]

    if not new_bars:
        print("新規バーなし(前回実行から進捗なし)")
        return

    notified_any = False

    for idx, bar in new_bars:
        record = {
            "t": bar["t"],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "interval": INTERVAL,
            "close": bar["close"],
            "sar": bar["sar"],
            "af": bar["af"],
            "ep": bar["ep"],
            "trend": bar["trend"],
            "dots_since_flip": dots_since_flip(results[: idx + 1]),
        }

        append_log(record)

        # 転換バー かつ 未通知の場合のみ通知
        is_new_flip = bar["reversed"] and (state.get("last_notified_flip_t") != bar["t"])
        if is_new_flip:
            notify_discord(record)
            state["last_notified_flip_t"] = bar["t"]
            notified_any = True
            print(f"=> 転換1点目を検知し、Discordに通知しました (t={bar['t']})", file=sys.stderr)

    # 処理済みの最新バー時刻を必ず更新(実行の抜けを次回検知するための基準点)
    state["last_processed_t"] = results[-1]["t"]
    save_state(state)

    print(f"新規バー{len(new_bars)}件を処理しました" + ("(通知あり)" if notified_any else "(通知なし)"))


if __name__ == "__main__":
    main()
