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

# 価格とSAR値の距離がこの割合(%)以内に近づいたら「接近通知」を送る(絶対距離判定)。
# n=48件のログ分析(2026-09-08時点)により0.1%に調整済み。
# 閾値0.1%: 転換直前バーの56%を捕捉、全バー中の該当率10.6%。
# 通知精度(誤報の少なさ)を優先し、中央値(0.085%)に近い値を採用。
APPROACH_THRESHOLD_PCT = 0.1

# 1本(15分)あたりの距離の縮小幅がこの値(ポイント)以上なら、
# 絶対距離が閾値に届いていなくても「急接近」として通知する(速度判定)。
# 静かに徐々に近づくケースは上記の絶対距離判定で捕捉できるが、
# 遠い位置から1本で急激に距離を詰めて転換するケース(2026-09-09に実例あり、
# 縮小幅0.48pt)は絶対距離判定だけでは捕捉できなかったため追加。
# n=657本のログ分析(2026-09-09時点)による95%タイル(0.285pt)を採用。
# なお、加速の前兆が全くないまま1本で転換ラインを飛び越えるケース
# (同日に別途確認済み)は、この速度判定でも原理的に捕捉できない。
VELOCITY_THRESHOLD_PT = 0.285

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
# ローソク足パターン判定(ログ記録専用・Discord通知には出さない)
#
# 「チャートパターン言語化リファレンス.md」の第3章(ローソク足パターン)を
# 機械的に判定する。定義があいまいな項目(足数の指定がない
# Bearish Breakaway等)は誤判定を避けるため実装せず対象外とした。
#
# 前提・注意点:
#  - BTCは24時間取引のため株式のような「窓(ギャップ)」は本来存在しないが、
#    ここでは「1本前の終値と当該足の始値の差」をギャップの代理として扱う。
#    GAP_THRESHOLD_PCT未満の差は「ギャップなし」とみなす(閾値は仮設定、
#    検証で調整の余地あり)
#  - 「高値圏/安値圏」の判定は、直近LOOKBACK本の中でその足が
#    最高値/最安値を付けているかで代用する(仮の定義)
#  - トレンド方向の文脈(上昇中/下降中)は、SAR側で計算済みのtrendを流用する
# ---------------------------------------------------------------------------
GAP_THRESHOLD_PCT = 0.02  # 始値と前足終値の差が、この割合(%)以上ならギャップありとみなす
ZONE_LOOKBACK = 8         # 高値圏/安値圏判定に使う遡り本数
RECENT_BARS_MAXLEN = ZONE_LOOKBACK + 4  # パターン判定に必要な直近バー保持数(4本パターン+高値圏判定の余裕分)


def _is_bullish(bar):
    return bar["c"] > bar["o"]


def _is_bearish(bar):
    return bar["c"] < bar["o"]


def _body(bar):
    return abs(bar["c"] - bar["o"])


def _range(bar):
    return bar["h"] - bar["l"]


def _is_small_body(bar, ratio=0.3):
    r = _range(bar)
    return r > 0 and _body(bar) / r <= ratio


def detect_candlestick_patterns(recent_bars, trend):
    """
    recent_bars: 直近の足を古い順に並べたリスト(各要素は o/h/l/c を持つdict)。
                 最後の要素が「今回判定対象の足」。
    trend: 現在のSARトレンド("up"/"down") — 文脈判定の代用に使う
    戻り値: 検出されたパターン名のリスト(なければ空リスト)
    """
    patterns = []
    n = len(recent_bars)
    if n < 1:
        return patterns
    cur = recent_bars[-1]

    # --- 2本パターン ---
    if n >= 2:
        prev1 = recent_bars[-2]
        gap_up = (cur["o"] - prev1["c"]) / prev1["c"] * 100 >= GAP_THRESHOLD_PCT
        gap_down = (prev1["c"] - cur["o"]) / prev1["c"] * 100 >= GAP_THRESHOLD_PCT

        # Two Black Gapping: 下降トレンド中、窓を開けて陰線、さらに安値を更新する陰線
        if trend == "down" and _is_bearish(prev1) and _is_bearish(cur) and gap_down and cur["l"] < prev1["l"]:
            patterns.append("Two Black Gapping")

        # Matching Low: 下降トレンド中の安値圏で、終値がほぼ同水準の陰線が2本連続(下げ止まりの兆候)
        if trend == "down" and _is_bearish(prev1) and _is_bearish(cur):
            close_diff_pct = abs(cur["c"] - prev1["c"]) / prev1["c"] * 100
            if close_diff_pct <= GAP_THRESHOLD_PCT:
                patterns.append("Matching Low")

        # Inverted Hammer(天井圏、簡易確認): 上昇トレンド中、上ヒゲが長く実体が小さい足が、
        # 直近ZONE_LOOKBACK本の中で最高値を付けており、翌足が陰線で確認
        upper_wick = prev1["h"] - max(prev1["o"], prev1["c"])
        lower_wick = min(prev1["o"], prev1["c"]) - prev1["l"]
        zone_window = recent_bars[max(0, n-1-ZONE_LOOKBACK):n-1]  # prev1を含まない、それ以前の本数
        is_at_high = (not zone_window) or prev1["h"] >= max(b["h"] for b in zone_window)
        if (
            trend == "up"
            and _is_small_body(prev1)
            and upper_wick > _body(prev1) * 2
            and lower_wick < _body(prev1)
            and is_at_high
            and _is_bearish(cur)
        ):
            patterns.append("Inverted Hammer(天井圏)")

    # --- 3本パターン ---
    if n >= 3:
        b1, b2, b3 = recent_bars[-3], recent_bars[-2], recent_bars[-1]

        gap_up_2 = (b2["o"] - b1["c"]) / b1["c"] * 100 >= GAP_THRESHOLD_PCT
        gap_up_3 = (b3["o"] - b2["c"]) / b2["c"] * 100 >= GAP_THRESHOLD_PCT if _is_bullish(b1) else False

        # Evening Star: 上昇トレンド中、陽線 → 窓を開けた小さい実体 → 陽線の実体を大きく飲み込む陰線
        if (
            trend == "up"
            and _is_bullish(b1)
            and not _is_small_body(b1, ratio=0.6)
            and (b2["o"] - b1["c"]) / b1["c"] * 100 >= GAP_THRESHOLD_PCT
            and _is_small_body(b2)
            and _is_bearish(b3)
            and b3["c"] < (b1["o"] + b1["c"]) / 2
        ):
            patterns.append("Evening Star")

        # Bullish Abandoned Baby: 下降トレンド中、下降陰線 → 窓開け小実体 → 窓開け陽線
        gap1_down = (b1["c"] - b2["o"]) / b1["c"] * 100 >= GAP_THRESHOLD_PCT if b2["o"] < b1["c"] else False
        gap2_up = (b3["o"] - b2["c"]) / b2["c"] * 100 >= GAP_THRESHOLD_PCT if b3["o"] > b2["c"] else False
        if (
            trend == "down"
            and _is_bearish(b1)
            and _is_small_body(b2)
            and max(b2["o"], b2["c"]) < b1["c"]
            and _is_bullish(b3)
            and min(b3["o"], b3["c"]) > b2["c"]
        ):
            patterns.append("Bullish Abandoned Baby")

        # Upside Tasuki Gap: 上昇トレンド中の陽線 → 窓を開けて陽線 → 窓を埋めない小幅な陰線
        if (
            trend == "up"
            and _is_bullish(b1)
            and _is_bullish(b2)
            and (b2["o"] - b1["c"]) / b1["c"] * 100 >= GAP_THRESHOLD_PCT
            and _is_bearish(b3)
            and b3["o"] < b2["c"]  # b2実体内(終値より下)から始まる
            and b3["c"] > b1["c"]  # 窓(b1["c"]〜b2["o"])を埋めきっていない
        ):
            patterns.append("Upside Tasuki Gap")

        # Three Black Crows: 上昇トレンド後(反転文脈)、実体の大きい陰線が3本連続で安値切り下げ
        if (
            trend == "up"
            and _is_bearish(b1) and _is_bearish(b2) and _is_bearish(b3)
            and not _is_small_body(b1) and not _is_small_body(b2) and not _is_small_body(b3)
            and b2["c"] < b1["c"] < b2["o"]
            and b3["c"] < b2["c"] < b3["o"]
        ):
            patterns.append("Three Black Crows")

    # --- 4本パターン ---
    if n >= 4:
        b1, b2, b3, b4 = recent_bars[-4], recent_bars[-3], recent_bars[-2], recent_bars[-1]

        # Bullish Three Line Strike: 下降トレンド中、陰線3本の後に、直前3本を丸ごと飲み込む大陽線
        if (
            trend == "down"
            and _is_bearish(b1) and _is_bearish(b2) and _is_bearish(b3)
            and b2["c"] < b1["c"] and b3["c"] < b2["c"]
            and _is_bullish(b4)
            and b4["o"] <= b3["c"]
            and b4["c"] > b1["o"]
        ):
            patterns.append("Bullish Three Line Strike")

        # Bearish Three Line Strike: 上昇トレンド中、陽線3本の後に、直前3本を丸ごと飲み込む大陰線
        if (
            trend == "up"
            and _is_bullish(b1) and _is_bullish(b2) and _is_bullish(b3)
            and b2["c"] > b1["c"] and b3["c"] > b2["c"]
            and _is_bearish(b4)
            and b4["o"] >= b3["c"]
            and b4["c"] < b1["o"]
        ):
            patterns.append("Bearish Three Line Strike")

    return patterns


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
    open_ = [b["o"] for b in bars]
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

    # パターン判定用に、直近RECENT_BARS_MAXLEN本のOHLCを保持しておく
    recent_bars = [
        {"o": open_[i], "h": high[i], "l": low[i], "c": close[i]}
        for i in range(max(0, n - RECENT_BARS_MAXLEN), n)
    ]

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
        "approach_notified": False,
        "prev_distance_pct": None,
        "recent_bars": recent_bars,
    }

    last_record = {
        "t": ts[-1],
        "close": close[-1],
        "open": open_[-1],
        "high": high[-1],
        "low": low[-1],
        "sar": sar,
        "af": af,
        "ep": ep,
        "trend": "up" if bull else "down",
        "reversed": False,  # 起動直後は転換判定を行わない(誤通知防止)
        "dots_since_flip": dots,
        "candlestick_patterns": detect_candlestick_patterns(recent_bars, "up" if bull else "down"),
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

    # 直近バーのOHLC履歴を更新(パターン判定用)。RECENT_BARS_MAXLEN本を超えたら古い方を捨てる
    recent_bars = list(state.get("recent_bars", []))
    recent_bars.append({"o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"]})
    recent_bars = recent_bars[-RECENT_BARS_MAXLEN:]

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
        "approach_notified": state.get("approach_notified", False),
        "prev_distance_pct": state.get("prev_distance_pct"),
        "recent_bars": recent_bars,
    }

    record = {
        "t": bar["t"],
        "close": bar["c"],
        "open": bar["o"],
        "high": bar["h"],
        "low": bar["l"],
        "sar": sar,
        "af": af,
        "ep": ep,
        "trend": "up" if bull else "down",
        "reversed": reversed_flag,
        "dots_since_flip": new_dots,
        "candlestick_patterns": detect_candlestick_patterns(recent_bars, "up" if bull else "down"),
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
    broken = 0
    new_lines = []
    for line_no, line in enumerate(lines, start=1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            # 手動編集などで構文が壊れた行があっても、その行はそのまま素通しし、
            # 他の行の処理・以降の実行を止めない(1行の破損で全体を止めないため)
            print(f"警告: {line_no}行目のJSON構文が不正なためスキップします: {e}", file=sys.stderr)
            new_lines.append(line.rstrip("\n"))
            broken += 1
            continue

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

    if broken > 0:
        print(f"警告: 構文不正な行が{broken}件見つかりました。手動編集を確認してください。", file=sys.stderr)

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

    range_line = ""
    high, low = record.get("high"), record.get("low")
    if high is not None and low is not None and record.get("close"):
        range_pct = (high - low) / record["close"] * 100
        range_line = f"転換足レンジ: 高値{high:.1f} / 安値{low:.1f} (幅{range_pct:.2f}%)\n"

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
        f"{range_line}"
        f"{liq_line}"
        f"✅ SAR:Binance OHLCVから継続計算(Wilder式 0.02/0.02/0.20) / 清算:Bybit近似値"
    )

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
    resp.raise_for_status()


def notify_approach(record, distance_pct, reason=None, velocity_pt=None):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL未設定のため通知をスキップします", file=sys.stderr)
        return

    trend_jp = "上昇" if record["trend"] == "up" else "下落"
    dt = datetime.fromtimestamp(record["t"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if reason == "速度":
        reason_line = f"検知理由: 急接近(1本で{velocity_pt:.2f}pt縮小、閾値{VELOCITY_THRESHOLD_PT}pt以上)\n"
    else:
        reason_line = f"検知理由: 絶対距離(閾値{APPROACH_THRESHOLD_PCT}%以内)\n"

    content = (
        f"**SAR接近通知(転換の可能性あり)**\n"
        f"現在のトレンド: {trend_jp}\n"
        f"時刻: {dt}\n"
        f"価格: {record['close']:.1f}\n"
        f"SAR値: {record['sar']:.1f}\n"
        f"距離: {distance_pct:.2f}%\n"
        f"{reason_line}"
        f"dots_since_flip: {record['dots_since_flip']}\n"
        f"⚠️ 転換が確定したわけではありません。次の足で転換しない可能性もあります。"
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
            "open": last_record["open"],
            "high": last_record["high"],
            "low": last_record["low"],
            "sar": last_record["sar"],
            "af": last_record["af"],
            "ep": last_record["ep"],
            "trend": last_record["trend"],
            "dots_since_flip": last_record["dots_since_flip"],
            "liq_long_bybit_approx": liq_long_usd,
            "liq_short_bybit_approx": liq_short_usd,
        }
        if last_record.get("candlestick_patterns"):
            record["candlestick_patterns"] = last_record["candlestick_patterns"]
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

        # 接近判定は、この行をログに書く前に確定させておく
        # (後からログだけを見て「この時点で通知が送られたか」が分かるようにするため)
        # ①絶対距離が閾値以内、②1本での縮小幅(速度)が閾値以上、のいずれかで発火
        approach_fired = False
        approach_reason = None
        if not record["reversed"]:
            distance_pct = abs(bar["c"] - record["sar"]) / bar["c"] * 100
            prev_distance_pct = state.get("prev_distance_pct")
            velocity_pt = None
            if prev_distance_pct is not None:
                velocity_pt = prev_distance_pct - distance_pct  # 正の値=縮小

            if not state.get("approach_notified", False):
                if distance_pct <= APPROACH_THRESHOLD_PCT:
                    approach_fired = True
                    approach_reason = "距離"
                elif velocity_pt is not None and velocity_pt >= VELOCITY_THRESHOLD_PT:
                    approach_fired = True
                    approach_reason = "速度"

            if approach_fired:
                record["distance_pct"] = round(distance_pct, 4)
                if velocity_pt is not None:
                    record["velocity_pt"] = round(velocity_pt, 4)
                record["approach_reason"] = approach_reason

            # 次のバーで速度を計算できるよう、今回の距離を保存しておく
            state["prev_distance_pct"] = distance_pct
        else:
            # 転換が起きたら、次のトレンドの立ち上がりであり比較対象が変わるため
            # 速度計算の基準もリセットする
            state["prev_distance_pct"] = None

        # 抑制フラグは「絶対距離が閾値以内に入ったか」だけで立てる。
        # 距離型は定義上必ず閾値以内なので毎回抑制対象になるが、
        # 速度型は「大きく動いたが、まだ距離自体は遠い」場合は抑制せず、
        # 同じトレンド中に再度の急接近があれば重ねて通知できるようにする。
        if approach_fired and not record["reversed"] and distance_pct <= APPROACH_THRESHOLD_PCT:
            state["approach_notified"] = True

        if not record.get("candlestick_patterns"):
            record.pop("candlestick_patterns", None)

        append_log({k: v for k, v in record.items() if k != "reversed"})

        is_new_flip = record["reversed"] and (state.get("last_notified_flip_t") != bar["t"])
        if is_new_flip:
            notify_discord(record)
            state["last_notified_flip_t"] = bar["t"]
            notified_any = True
            print(f"=> 転換1点目を検知し、Discordに通知しました (t={bar['t']})", file=sys.stderr)

        if record["reversed"]:
            # 転換が起きたら、次のトレンドに向けて接近通知のフラグをリセット
            state["approach_notified"] = False
        elif approach_fired:
            notify_approach(record, record["distance_pct"], approach_reason, record.get("velocity_pt"))
            print(
                f"=> SAR接近を検知し、Discordに通知しました "
                f"(t={bar['t']}, 理由={approach_reason}, 距離={record['distance_pct']:.2f}%, "
                f"抑制フラグ={'ON' if state.get('approach_notified') else 'OFF(再発火あり得る)'})",
                file=sys.stderr,
            )

    # 過去ログの清算データを自己修復(タイムラグで0のまま残っていたものを補正)
    if liq_map is not None:
        try:
            patched = backfill_liquidations(liq_map)
            if patched:
                print(f"清算データのバックフィル: {patched}件を補正しました")
        except Exception as e:
            # バックフィル処理自体が失敗しても、今回の新規検知・通知・ログ追記は
            # 既に完了しているため、ここで処理を止めない
            print(f"バックフィル処理に失敗(処理は継続): {e}", file=sys.stderr)


    save_state(state)
    print(f"新規バー{len(new_bars)}件を処理しました" + ("(通知あり)" if notified_any else "(通知なし)"))


if __name__ == "__main__":
    main()
