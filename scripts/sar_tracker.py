#!/usr/bin/env python3
"""
sar_tracker.py (v2)

Coinalyze の /ohlcv-history から BTCUSDT_PERP.A の15分足OHLCVを取得し、
Wilder式 Parabolic SAR (AF初期値0.02 / 刻み0.02 / 上限0.20) を計算する。

v1からの変更点(重要):
  v1は毎回「直近3日分をゼロから再計算」していたため、取得ウィンドウが
  15分ずつスライドするたびに計算の起点(トレンド初期仮定)が変わり、
  転換点ちょうどの判定が実行のたびにブレる不具合があった
  (前回は"継続"と判定した足を、今回は"転換点"と判定するなど)。

  v2では sar_state.json に SAR計算の状態そのもの
  (sar値・EP・AF・トレンド方向・直近2本の高安値) を保存し、
  次回実行時はその続きから1本ずつ計算を進める「継続計算」方式に変更。
  これにより過去に確定した足の判定が実行のたびに変わることがなくなる。

  初回実行時のみ、直近3日分をまとめて計算して状態を「起動」させる
  (bootstrap)。2回目以降は前回の状態 + 新規に確定した足だけを処理する。

転換バーを新規検知したら:
  1. Discord Webhookに通知を送信
  2. sar_log.jsonl に1レコード追記(BTC_pattern_observer等での後日検証用)
  3. sar_state.json を更新(次回実行時の重複通知防止・継続計算用)

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

SYMBOL = "BTCUSDT_PERP.A"           # Binance BTCUSDT Perp (SAR/OHLCV計算はこちらに統一)
INTERVAL = "15min"                  # Coinalyze側の interval 表記
BOOTSTRAP_LOOKBACK_SECONDS = 60 * 60 * 24 * 3  # 初回起動時のみ: 直近3日分を取得して状態を作る

# 清算データはBinanceの公開清算フィードが2021年以降「1秒あたり1件」に
# 間引かれているため、実態をより反映しやすいBybitから取得する。
# あくまで別取引所の近似値であり、SAR計算(Binance基準)とは性質が異なる点に注意。
LIQUIDATION_SYMBOL = "BTCUSDT.6"    # Bybit BTCUSDT Perp (USDT建て)

AF_START = 0.02
AF_STEP = 0.02
AF_MAX = 0.20

STATE_PATH = "data/sar_state.json"
LOG_PATH = "data/sar_log.jsonl"

OHLCV_URL = "https://api.coinalyze.net/v1/ohlcv-history"
LIQUIDATION_URL = "https://api.coinalyze.net/v1/liquidation-history"


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------
def fetch_ohlcv(from_ts=None):
    now = int(time.time())
    if from_ts is None:
        from_ts = now - BOOTSTRAP_LOOKBACK_SECONDS

    params = {
        "symbols": SYMBOL,
        "interval": INTERVAL,
        "from": from_ts,
        "to": now,
    }
    headers = {"api_key": COINALYZE_API_KEY}
    resp = requests.get(OHLCV_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data or "history" not in data[0]:
        raise RuntimeError(f"Coinalyzeレスポンス形式が想定と異なります: {data}")

    bars = data[0]["history"]
    bars = sorted(bars, key=lambda b: b["t"])
    return bars


def fetch_liquidations(from_ts):
    """
    /liquidation-history から、from_ts より後の清算実績を取得。
    Bybit(BTCUSDT.6)を情報源とする(Binanceは清算フィードが間引かれているため)。
    レスポンスはBTC建て数量(BASE_ASSET)で返るため、呼び出し側で価格を掛けて
    USD換算する(Coinalyze側のUSD自動変換パラメータは存在が確認できなかった
    ため使用しない)。
    戻り値: {t: {"long_btc": ロング清算量BTC, "short_btc": ショート清算量BTC}}
    取得に失敗した場合はNoneを返す(取得成功・対象時刻の記録なし、の場合は{})
    """
    now = int(time.time())
    params = {
        "symbols": LIQUIDATION_SYMBOL,
        "interval": INTERVAL,
        "from": from_ts,
        "to": now,
    }
    headers = {"api_key": COINALYZE_API_KEY}
    try:
        resp = requests.get(LIQUIDATION_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data or "history" not in data[0]:
            return {}
        return {h["t"]: {"long_btc": h.get("l", 0), "short_btc": h.get("s", 0)} for h in data[0]["history"]}
    except Exception as e:
        print(f"清算データ取得に失敗(処理は継続): {e}", file=sys.stderr)
        return None  # 取得失敗(Noneは"不明"、{}は"取得成功・対象時刻の清算実績なし")


# ---------------------------------------------------------------------------
# Wilder式 Parabolic SAR: 初回起動用(ゼロから系列全体を計算)
# ---------------------------------------------------------------------------
def bootstrap_psar(bars, af_start=AF_START, af_step=AF_STEP, af_max=AF_MAX):
    """
    初回実行専用。bars全体からSAR系列を計算し、
    最終バー時点の「継続計算に必要な状態」を返す。
    """
    n = len(bars)
    if n < 3:
        raise RuntimeError("SAR計算には最低3本以上のバーが必要です")

    high = [b["h"] for b in bars]
    low = [b["l"] for b in bars]
    close = [b["c"] for b in bars]
    ts = [b["t"] for b in bars]

    bull = close[1] >= close[0]
    af = af_start
    if bull:
        sar = low[0]
        ep = high[1]
    else:
        sar = high[0]
        ep = low[1]

    dots = 1  # 系列先頭を仮の1点目として数える(起動時のみの近似値)

    for i in range(1, n):
        prev_sar = sar
        reversed_flag = False
        sar = prev_sar + af * (ep - prev_sar)

        if bull:
            sar = min(sar, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < sar:
                bull = False
                reversed_flag = True
                sar = ep
                ep = low[i]
                af = af_start
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > sar:
                bull = True
                reversed_flag = True
                sar = ep
                ep = high[i]
                af = af_start
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

        dots = 1 if reversed_flag else dots + 1

    prev1 = {"t": ts[-1], "h": high[-1], "l": low[-1]}
    prev2 = {"t": ts[-2], "h": high[-2], "l": low[-2]}

    state = {
        "bull": bull,
        "af": af,
        "ep": ep,
        "sar": sar,
        "prev1": prev1,
        "prev2": prev2,
        "dots_since_flip": dots,
        "last_processed_t": ts[-1],
        "last_notified_flip_t": None,
    }

    last_record = {
        "t": ts[-1],
        "close": close[-1],
        "sar": sar,
        "af": af,
        "ep": ep,
        "trend": "up" if bull else "down",
        "reversed": False,  # 起動直後は転換判定を行わない(誤通知防止)
        "dots_since_flip": dots,
    }

    return state, last_record


# ---------------------------------------------------------------------------
# Wilder式 Parabolic SAR: 2回目以降(継続計算)
# ---------------------------------------------------------------------------
def step_psar(state, bar, af_start=AF_START, af_step=AF_STEP, af_max=AF_MAX):
    """
    永続化された状態(state)を1本分だけ前進させる。
    bar: {'t':.., 'h':.., 'l':.., 'c':..}
    """
    bull = state["bull"]
    af = state["af"]
    ep = state["ep"]
    prev_sar = state["sar"]
    prev1 = state["prev1"]
    prev2 = state["prev2"]

    reversed_flag = False
    sar = prev_sar + af * (ep - prev_sar)

    if bull:
        sar = min(sar, prev1["l"], prev2["l"])
        if bar["l"] < sar:
            bull = False
            reversed_flag = True
            sar = ep
            ep = bar["l"]
            af = af_start
        else:
            if bar["h"] > ep:
                ep = bar["h"]
                af = min(af + af_step, af_max)
    else:
        sar = max(sar, prev1["h"], prev2["h"])
        if bar["h"] > sar:
            bull = True
            reversed_flag = True
            sar = ep
            ep = bar["h"]
            af = af_start
        else:
            if bar["l"] < ep:
                ep = bar["l"]
                af = min(af + af_step, af_max)

    new_dots = 1 if reversed_flag else state["dots_since_flip"] + 1

    new_state = {
        "bull": bull,
        "af": af,
        "ep": ep,
        "sar": sar,
        "prev1": {"t": bar["t"], "h": bar["h"], "l": bar["l"]},
        "prev2": prev1,
        "dots_since_flip": new_dots,
        "last_processed_t": bar["t"],
        "last_notified_flip_t": state.get("last_notified_flip_t"),
    }

    record = {
        "t": bar["t"],
        "close": bar["c"],
        "sar": sar,
        "af": af,
        "ep": ep,
        "trend": "up" if bull else "down",
        "reversed": reversed_flag,
        "dots_since_flip": new_dots,
    }

    return new_state, record


# ---------------------------------------------------------------------------
# 状態管理
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# ログ蓄積
# ---------------------------------------------------------------------------
def append_log(record):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def backfill_liquidations(liq_map):
    """
    清算データはBybit側の集計反映に数分〜数十分のタイムラグがあり、
    足確定直後に取得すると0のまま記録されてしまうことがある。
    このため、直近の複数バーの清算データを毎回広めに再取得し、
    過去に0のまま記録されていたログ行があれば、確定した値で
    自動的に上書き修正する(自己修復)。
    liq_map: {t: {"long_btc":.., "short_btc":..}} (fetch_liquidationsの戻り値)
    """
    if liq_map is None or not os.path.exists(LOG_PATH):
        return 0

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]

    patched = 0
    new_lines = []
    for line in lines:
        rec = json.loads(line)
        t = rec.get("t")
        if t in liq_map:
            new_long = liq_map[t].get("long_btc", 0) * rec.get("close", 0)
            new_short = liq_map[t].get("short_btc", 0) * rec.get("close", 0)
            old_long = rec.get("liq_long_bybit_approx") or 0
            old_short = rec.get("liq_short_bybit_approx") or 0
            # 既存が0で、再取得した値がそれより大きい場合のみ上書き(後退はさせない)
            if new_long > old_long or new_short > old_short:
                rec["liq_long_bybit_approx"] = max(new_long, old_long)
                rec["liq_short_bybit_approx"] = max(new_short, old_short)
                rec["liq_backfilled"] = True
                patched += 1
        new_lines.append(json.dumps(rec, ensure_ascii=False))

    if patched > 0:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")

    return patched


# ---------------------------------------------------------------------------
# Discord通知
# ---------------------------------------------------------------------------
def notify_discord(record):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL未設定のため通知をスキップします", file=sys.stderr)
        return

    direction_jp = "上昇" if record["trend"] == "up" else "下落"
    dt = datetime.fromtimestamp(record["t"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    liq_line = ""
    liq_long = record.get("liq_long_bybit_approx")
    liq_short = record.get("liq_short_bybit_approx")
    if liq_long is not None or liq_short is not None:
        liq_line = (
            f"清算(Bybit近似・ロング/ショート、速報値): "
            f"${liq_long or 0:,.0f} / ${liq_short or 0:,.0f}\n"
        )
    content = (
        f"**SAR転換検知(1点目)**\n"
        f"方向: {direction_jp}\n"
        f"時刻: {dt}\n"
        f"価格: {record['close']:.1f}\n"
        f"SAR値: {record['sar']:.1f}\n"
        f"AF: {record['af']:.2f}\n"
        f"{liq_line}"
        f"✅ SAR:Binance OHLCVから継続計算(Wilder式 0.02/0.02/0.20) / 清算:Bybit近似値"
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

    state = load_state()

    # -------------------------------------------------------------
    # 初回起動(state.jsonがまだ存在しない、または旧形式の場合)
    # -------------------------------------------------------------
    if state is None or "prev1" not in state:
        bars = fetch_ohlcv(from_ts=None)
        state, last_record = bootstrap_psar(bars)

        liq_map = fetch_liquidations(from_ts=last_record["t"] - 1)
        if liq_map is None:
            liq_long_usd = liq_short_usd = None  # 取得失敗: 不明
        else:
            liq = liq_map.get(last_record["t"], {"long_btc": 0, "short_btc": 0})  # 取得成功・データなし=0件
            liq_long_usd = liq.get("long_btc", 0) * last_record["close"]
            liq_short_usd = liq.get("short_btc", 0) * last_record["close"]

        record = {
            "t": last_record["t"],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "interval": INTERVAL,
            "close": last_record["close"],
            "sar": last_record["sar"],
            "af": last_record["af"],
            "ep": last_record["ep"],
            "trend": last_record["trend"],
            "dots_since_flip": last_record["dots_since_flip"],
            "liq_long_bybit_approx": liq_long_usd,
            "liq_short_bybit_approx": liq_short_usd,
        }
        append_log(record)
        save_state(state)
        print("初回起動(bootstrap)完了。次回実行から継続計算に入ります。")
        return

    # -------------------------------------------------------------
    # 2回目以降: 前回処理済み時刻より後の足だけを取得して継続計算
    # -------------------------------------------------------------
    last_processed_t = state["last_processed_t"]
    bars = fetch_ohlcv(from_ts=last_processed_t)
    new_bars = [b for b in bars if b["t"] > last_processed_t]

    if not new_bars:
        print("新規バーなし(前回実行から進捗なし)")
        return

    # 清算データはBybit側の集計反映にタイムラグがあるため、
    # 直近2時間分を広めに取得し、後段でバックフィル(自己修復)に使う
    LIQ_BACKFILL_LOOKBACK = 60 * 60 * 2
    liq_map = fetch_liquidations(from_ts=last_processed_t - LIQ_BACKFILL_LOOKBACK)

    notified_any = False

    for bar in new_bars:
        state, record = step_psar(state, bar)
        record["recorded_at"] = datetime.now(timezone.utc).isoformat()
        record["interval"] = INTERVAL

        if liq_map is None:
            record["liq_long_bybit_approx"] = None
            record["liq_short_bybit_approx"] = None
        else:
            liq = liq_map.get(bar["t"], {"long_btc": 0, "short_btc": 0})
            record["liq_long_bybit_approx"] = liq.get("long_btc", 0) * bar["c"]
            record["liq_short_bybit_approx"] = liq.get("short_btc", 0) * bar["c"]

        append_log({k: v for k, v in record.items() if k != "reversed"})

        is_new_flip = record["reversed"] and (state.get("last_notified_flip_t") != bar["t"])
        if is_new_flip:
            notify_discord(record)
            state["last_notified_flip_t"] = bar["t"]
            notified_any = True
            print(f"=> 転換1点目を検知し、Discordに通知しました (t={bar['t']})", file=sys.stderr)

    # 過去ログの清算データを自己修復(タイムラグで0のまま残っていたものを補正)
    if liq_map is not None:
        patched = backfill_liquidations(liq_map)
        if patched:
            print(f"清算データのバックフィル: {patched}件を補正しました")


    save_state(state)
    print(f"新規バー{len(new_bars)}件を処理しました" + ("(通知あり)" if notified_any else "(通知なし)"))


if __name__ == "__main__":
    main()
