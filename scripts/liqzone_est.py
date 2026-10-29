#!/usr/bin/env python3
"""
清算帯推定 フェーズ2: 推定ロジック(探索期間のみ)

入力: data/liqzone/raw/ の OI(5分)と OHLCV(5分)。出力は呼び出し側で扱う。
考え方(OI増減 × 価格 × レバレッジ段階):
  1. 足ごとのOI増加分 dOI(BTC) を「新規建玉」とみなし、テイカー買い比率でロング/ショートに按分する
  2. 建玉価格(足の典型価格)から、各レバレッジ段階の清算価格を求め、価格の格子(ビン)に積む
       ロング: 建値 × (1 - 1/L + mmr)    ショート: 建値 × (1 + 1/L - mmr)
  3. OI減少は、既存の積み上げを OI比で一律に縮める。さらに時間減衰(半減期)を掛ける
  4. 価格が清算価格を通過した(足の高値/安値が届いた)ビンは消す
  5. 1時間ごとに、現在値の上下 R% 以内で強いピークを上下 K個ずつ「清算帯」として取り出す

期間のガード:
  このモジュールのデータ読み込みは、探索期間(2024-10-01〜2025-09-30 UTC)だけを返す。
  確認期間・最終確認期間は、事前登録が終わるまで読み込まない(data/history/README.md の運用ルールと同じ)。
"""
import glob
import hashlib
import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "liqzone")
BAR = 300
EXPLORE_START = 1727740800   # 2024-10-01 00:00 UTC
EXPLORE_END = 1759276800     # 2025-10-01 00:00 UTC(この値は含まない)

DEFAULT = {
    "bin_usd": 25.0,
    "tiers": {"10": 0.20, "25": 0.30, "50": 0.30, "100": 0.20},   # レバレッジ段階ごとの重み(仮置き)
    "mmr": 0.005,                  # 維持証拠金率(単純化)
    "halflife_days": 14.0,         # 積み上げの時間減衰の半減期
    "split": "taker",              # 新規建玉のロング/ショート按分: テイカー買い比率
    "smooth_bins": 4,              # ピーク検出の平滑化(±ビン数)
    "range_pct": 5.0,              # 現在値からこの範囲内の帯だけ対象
    "top_k": 5,                    # 上下それぞれ取り出す本数
    "min_strength_frac": 0.003,    # ピークの強さがOIのこの割合以上(イベント数が数百件になる水準。損益では選んでいない)
    "sep_bins": 20,                # 帯同士の最小間隔(ビン数)
    "refresh_bars": 12,            # 帯の更新間隔(5分足×12=1時間)
    "touch_delta_pct": 0.10,       # 帯からこの距離に達したら「触れた」とみなす
    "cooldown_bars": 288,          # 同じ帯の再イベントを抑える期間
    "warmup_days": 30,             # 開始からこの日数はイベントを数えない(積み上げが溜まるまで)
}


def params_id(p):
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:8]


def load_explore():
    """探索期間の5分足(OI・OHLCV)を、欠けのない格子に揃えて返す。"""
    def read(kind):
        fs = []
        for f in sorted(glob.glob(os.path.join(ROOT, "raw", kind, "*.csv"))):
            d = datetime.strptime(os.path.basename(f)[:-4], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            if EXPLORE_START <= d < EXPLORE_END:
                fs.append(f)
        df = pd.concat([pd.read_csv(f) for f in fs], ignore_index=True)
        df = df[(df.t >= EXPLORE_START) & (df.t < EXPLORE_END)]
        return df.drop_duplicates("t").set_index("t").sort_index()

    oi, px = read("oi_5m"), read("ohlcv_5m")
    grid = pd.RangeIndex(EXPLORE_START, EXPLORE_END, BAR)
    df = px[["o", "h", "l", "c", "vol_btc", "taker_buy_vol_btc"]].reindex(grid)
    df["oi"] = oi["oi_btc"].reindex(grid)
    # 値が0の足は取得元の不備とみなして欠損扱いにする(OI 25本、出来高 21本。実際の値ではない)
    n_zero_oi = int((df["oi"] == 0).sum())
    n_zero_vol = int((df["vol_btc"] == 0).sum())
    df.loc[df["oi"] == 0, "oi"] = np.nan
    df.loc[df["vol_btc"] == 0, ["vol_btc", "taker_buy_vol_btc"]] = np.nan
    n_missing_px = int(df["c"].isna().sum())
    n_missing_oi = int(df["oi"].isna().sum())
    df = df.ffill()   # 欠けた足は直前の値で埋める
    assert df.index.max() < EXPLORE_END
    return df, {"missing_px": n_missing_px, "missing_oi_incl_zero": n_missing_oi, "zero_oi": n_zero_oi, "zero_vol": n_zero_vol}


def _peaks(arr, lo, hi, p, strength_min):
    """arr(平滑化済み)の [lo, hi) の範囲から、強さの順に top_k 本(間隔 sep_bins 以上)。"""
    if hi <= lo:
        return []
    seg = arr[lo:hi].copy()
    out = []
    for _ in range(p["top_k"]):
        i = int(np.argmax(seg))
        if seg[i] < strength_min:
            break
        out.append((lo + i, float(seg[i])))
        a, b = max(0, i - p["sep_bins"]), min(len(seg), i + p["sep_bins"] + 1)
        seg[a:b] = 0
    return out


def run(df, p=None, shift_pct=0.0):
    """推定を実行し、「触れた」イベントの一覧を返す。

    shift_pct: プラセボ用。帯の価格を一律にこの割合だけ上にずらして検知する(通常は0)。
    イベント: touch した足 j、帯の側('U'=上の帯=ショート清算の集中、'L'=下の帯=ロング清算の集中)、
             帯の価格、強さ、OI。経路の評価は別関数で行う。
    """
    p = p or DEFAULT
    binw = p["bin_usd"]
    nb = int(200000 / binw)
    longs, shorts = np.zeros(nb), np.zeros(nb)
    tiers = [(float(k), w) for k, w in p["tiers"].items()]
    tw = sum(w for _, w in tiers)
    decay = 0.5 ** (1.0 / (p["halflife_days"] * 288))

    o, h, l, c = (df[k].to_numpy() for k in ("o", "h", "l", "c"))
    vol, tbv, oi = df["vol_btc"].to_numpy(), df["taker_buy_vol_btc"].to_numpy(), df["oi"].to_numpy()
    t = df.index.to_numpy()
    n = len(df)
    warm = int(p["warmup_days"] * 288)
    sm_w = np.ones(2 * p["smooth_bins"] + 1)
    zones, cool, events = [], {}, []
    delta = p["touch_delta_pct"] / 100.0
    mmr = p["mmr"]

    for i in range(1, n):
        # 3) OI減少は一律縮小、時間減衰
        d = oi[i] - oi[i - 1]
        longs *= decay
        shorts *= decay
        if d < 0 and oi[i - 1] > 0:
            r = oi[i] / oi[i - 1]
            longs *= r
            shorts *= r
        # 1)(2) 新規建玉を清算価格のビンに積む
        if d > 0:
            share_l = tbv[i] / vol[i] if vol[i] > 0 else 0.5
            share_l = min(max(share_l, 0.05), 0.95)
            entry = (h[i] + l[i] + c[i]) / 3.0
            for L, w in tiers:
                amt = d * w / tw
                pl = entry * (1 - 1 / L + mmr)
                ps = entry * (1 + 1 / L - mmr)
                if 0 < pl < nb * binw:
                    longs[int(pl / binw)] += amt * share_l
                if 0 < ps < nb * binw:
                    shorts[int(ps / binw)] += amt * (1 - share_l)
        # 4) 通過したビンを消す(ロング清算価格 >= 安値、ショート清算価格 <= 高値)
        longs[int(l[i] / binw):] = 0
        shorts[: int(h[i] / binw) + 1] = 0

        # 5) 帯の更新
        if i % p["refresh_bars"] == 0:
            zones = []
            cur = c[i]
            smin = p["min_strength_frac"] * oi[i]
            sl = np.convolve(longs, sm_w, mode="same")
            ss = np.convolve(shorts, sm_w, mode="same")
            lo_u, hi_u = int(cur / binw) + 1, int(cur * (1 + p["range_pct"] / 100) / binw)
            lo_d, hi_d = int(cur * (1 - p["range_pct"] / 100) / binw), int(cur / binw)
            for b, s in _peaks(ss, lo_u, hi_u, p, smin):
                zones.append(("U", (b + 0.5) * binw, s))
            for b, s in _peaks(sl, lo_d, hi_d, p, smin):
                zones.append(("L", (b + 0.5) * binw, s))

        # 触れた判定(帯の更新直後の足から。ウォームアップ中は数えない)
        if i >= warm and zones:
            for side, price, s in zones:
                pz = price * (1 + shift_pct / 100.0)
                key = (side, round(np.log(pz) / 0.0025))
                if i - cool.get(key, -10**9) < p["cooldown_bars"]:
                    continue
                if side == "U" and h[i] >= pz * (1 - delta) and l[i] < pz:
                    hit = True
                elif side == "L" and l[i] <= pz * (1 + delta) and h[i] > pz:
                    hit = True
                else:
                    hit = False
                if hit:
                    cool[key] = i
                    events.append({"i": i, "t": int(t[i]), "side": side, "zone": pz,
                                   "strength": s, "strength_frac": s / oi[i], "oi": oi[i]})
    return pd.DataFrame(events)


def evaluate(df, ev, sl_pct, tp_pct, horizon=48, delta_pct=0.10, fee_rt_pct=0.05):
    """各イベントを、指値(帯から delta 手前)で入り、SL/TPのOCOと時間切れで出た場合の損益(%)を返す。
    ルール: 約定は触れた足。SLは触れた足から判定。TPは次の足から。同じ足でSLとTPの両方に届いたらSLを先に採用。
    時間切れは horizon 本目の終値で成行決済。損益は建値に対する%、手数料(往復)を控除済み。"""
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    n = len(df)
    out = np.empty(len(ev))
    dl = delta_pct / 100.0
    for k, (i, side, z) in enumerate(zip(ev["i"].to_numpy(), ev["side"].to_numpy(), ev["zone"].to_numpy())):
        if side == "U":   # 上の帯: 手前で売り(ショート)
            e = z * (1 - dl)
            sl, tp = e * (1 + sl_pct / 100), e * (1 - tp_pct / 100)
            res = None
            if h[i] >= sl:
                res = -sl_pct
            else:
                for j in range(i + 1, min(i + 1 + horizon, n)):
                    if h[j] >= sl:
                        res = -sl_pct
                        break
                    if l[j] <= tp:
                        res = tp_pct
                        break
                if res is None:
                    jj = min(i + horizon, n - 1)
                    res = (e - c[jj]) / e * 100
        else:             # 下の帯: 手前で買い(ロング)
            e = z * (1 + dl)
            sl, tp = e * (1 - sl_pct / 100), e * (1 + tp_pct / 100)
            res = None
            if l[i] <= sl:
                res = -sl_pct
            else:
                for j in range(i + 1, min(i + 1 + horizon, n)):
                    if l[j] <= sl:
                        res = -sl_pct
                        break
                    if h[j] >= tp:
                        res = tp_pct
                        break
                if res is None:
                    jj = min(i + horizon, n - 1)
                    res = (c[jj] - e) / e * 100
        out[k] = res - fee_rt_pct
    return out
