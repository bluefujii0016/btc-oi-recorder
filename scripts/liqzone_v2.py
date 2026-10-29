#!/usr/bin/env python3
"""
清算帯推定 フェーズ2 定義v2(探索期間のみ)。計画は data/liqzone/README.md の記録を参照。

  帯   : 直近24時間に積まれた建玉の清算価格だけ(CoinGlass の24時間表示相当)。
         各足のOI増加分を、テイカー買い比率でロング/ショートに按分し、10/25/50/100倍の清算価格へ。
         その後のOI減少で一律に縮め、価格が通過したものは除く(足ごとの高値/安値で判定)。
  黄色 : 平滑化した強さが、現在値±5%の範囲の最大値の θ 倍以上のピーク(上下それぞれ最大3本)
  触れた: 帯の価格の0.10%以内に高値/安値が届いた足。同じ帯は24時間に1回
  入る : 触れた足を含む15分足の次の足から最大8本のうち、足の幅が直前14本の平均幅の k 倍以下で、
         直前の足より小さい最初の足の終値(上の帯→ショート、下の帯→ロング)
  出る : 損切り=触れてからエントリーまでの極値の外側0.05%、利確=リスクの m 倍、4時間で時間切れ
結果の損益はリスク(R)単位。手数料(往復)は価格の割合で引き、Rに換算して差し引く。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_est as E

WIN = 288                 # 24時間(5分足)
TIERS = [(10.0, 0.20), (25.0, 0.30), (50.0, 0.30), (100.0, 0.20)]
MMR = 0.005
BIN = 25.0
NB = int(200000 / BIN)
SMOOTH = 4
RANGE = 0.05
TOP_K = 3
SEP = 20
REFRESH = 12
DELTA = 0.0010
COOLDOWN = 288
BUF = 0.0005
RISK_MIN, RISK_MAX = 0.15, 1.5
MAX_CANDLES = 8
TIME_STOP = 48
FEE = 0.05


class Prep:
    def __init__(self, df):
        self.df = df
        self.h, self.l, self.c = (df[k].to_numpy() for k in ("h", "l", "c"))
        self.t = df.index.to_numpy()
        oi, vol, tbv = df["oi"].to_numpy(), df["vol_btc"].to_numpy(), df["taker_buy_vol_btc"].to_numpy()
        self.oi = oi
        self.d = np.r_[0.0, np.diff(oi)]
        self.entry = (self.h + self.l + self.c) / 3.0
        with np.errstate(divide="ignore", invalid="ignore"):
            sh = np.where(vol > 0, tbv / vol, 0.5)
        self.share_l = np.clip(sh, 0.05, 0.95)
        f = np.ones(len(oi))
        f[1:] = np.minimum(1.0, oi[1:] / np.where(oi[:-1] > 0, oi[:-1], np.inf))
        self.CL = np.cumsum(np.log(np.maximum(f, 1e-9)))
        n = len(df)
        # 15分足(格子は 00:00 起点で3本ずつ)
        m = n // 3
        self.m15 = m
        self.h15 = self.h[: m * 3].reshape(m, 3).max(axis=1)
        self.l15 = self.l[: m * 3].reshape(m, 3).min(axis=1)
        self.rng15 = self.h15 - self.l15
        self.c15 = self.c[2: m * 3: 3]


def smoothed_map(P, i):
    """時刻 i(5分足の添字)時点の、直近24時間の清算ビン(ロング・ショート)。平滑化済みを返す。"""
    a = max(1, i - WIN + 1)
    sl = slice(a, i + 1)
    d = P.d[sl]
    pos = d > 0
    if not pos.any():
        z = np.zeros(NB)
        return z, z
    j = np.arange(a, i + 1)[pos]
    S = np.exp(P.CL[i] - P.CL[j])                     # その後のOI減少による縮み
    suf_min = np.minimum.accumulate(P.l[a: i + 1][::-1])[::-1][pos]   # j 以降の最安値
    suf_max = np.maximum.accumulate(P.h[a: i + 1][::-1])[::-1][pos]   # j 以降の最高値
    e = P.entry[j]
    sh = P.share_l[j]
    base = d[pos] * S
    tw = sum(w for _, w in TIERS)
    longs, shorts = np.zeros(NB), np.zeros(NB)
    for L, w in TIERS:
        pl = e * (1 - 1 / L + MMR)
        ps = e * (1 + 1 / L - MMR)
        okl = pl < suf_min          # ロング: 以降の安値が届いていない(未清算)
        oks = ps > suf_max
        np.add.at(longs, (pl[okl] / BIN).astype(int), (base * w / tw * sh)[okl])
        np.add.at(shorts, (ps[oks] / BIN).astype(int), (base * w / tw * (1 - sh))[oks])
    k = np.ones(2 * SMOOTH + 1)
    return np.convolve(longs, k, mode="same"), np.convolve(shorts, k, mode="same")


def peaks(arr, lo, hi, thr):
    if hi <= lo:
        return []
    seg = arr[lo:hi].copy()
    out = []
    for _ in range(TOP_K):
        i = int(np.argmax(seg))
        if seg[i] < thr or seg[i] <= 0:
            break
        out.append((lo + i, float(seg[i])))
        seg[max(0, i - SEP): min(len(seg), i + SEP + 1)] = 0
    return out


def find_events(P, theta, shift_pct=0.0, start=2 * 288):
    """触れたイベントを返す。shift_pct はプラセボ(帯を上にずらす)。"""
    n = len(P.c)
    zones, cool, ev = [], {}, []
    for i in range(start, n):
        if i % REFRESH == 0:
            cur = P.c[i]
            sl_, ss_ = smoothed_map(P, i)
            lo_u, hi_u = int(cur / BIN) + 1, int(cur * (1 + RANGE) / BIN)
            lo_d, hi_d = int(cur * (1 - RANGE) / BIN), int(cur / BIN)
            gmax = max(ss_[lo_u:hi_u].max() if hi_u > lo_u else 0, sl_[lo_d:hi_d].max() if hi_d > lo_d else 0)
            zones = []
            if gmax > 0:
                thr = theta * gmax
                zones += [("U", (b + 0.5) * BIN, s / gmax) for b, s in peaks(ss_, lo_u, hi_u, thr)]
                zones += [("L", (b + 0.5) * BIN, s / gmax) for b, s in peaks(sl_, lo_d, hi_d, thr)]
        for side, price, rel in zones:
            pz = price * (1 + shift_pct / 100.0)
            key = (side, round(np.log(pz) / 0.0025))
            if i - cool.get(key, -10**9) < COOLDOWN:
                continue
            if (side == "U" and P.h[i] >= pz * (1 - DELTA) and P.l[i] < pz) or \
               (side == "L" and P.l[i] <= pz * (1 + DELTA) and P.h[i] > pz):
                cool[key] = i
                ev.append({"i": i, "t": int(P.t[i]), "side": side, "zone": pz, "rel": rel})
    return pd.DataFrame(ev)


def random_events(P, n_ev, seed=0, start=2 * 288):
    """比較用: ランダムな時刻・向きの「触れた」(帯の情報なし)。"""
    rng = np.random.default_rng(seed)
    idx = np.sort(rng.integers(start, len(P.c) - 400, size=n_ev))
    side = rng.choice(["U", "L"], size=n_ev)
    return pd.DataFrame({"i": idx, "t": P.t[idx], "side": side, "zone": P.c[idx], "rel": np.nan})


def entries(P, ev, k):
    """各イベントについて、15分足の縮小条件で入る足を探す。入れなければ除外。
    戻り値: i(触れた足)、e(エントリーの5分足添字)、side、entry、sl、risk%。"""
    out = []
    skipped = {"no_shrink": 0, "risk": 0}
    for r in ev.itertuples():
        i, side = r.i, r.side
        q = i // 3                                   # 触れた足を含む15分足
        if q < 15:
            continue
        atr = P.rng15[q - 14: q].mean()
        found = None
        for qq in range(q + 1, min(q + 1 + MAX_CANDLES, P.m15)):
            if P.rng15[qq] <= k * atr and P.rng15[qq] < P.rng15[qq - 1]:
                found = qq
                break
        if found is None:
            skipped["no_shrink"] += 1
            continue
        e = 3 * found + 2
        entry = P.c[e]
        if side == "U":
            ext = P.h[i: e + 1].max()
            sl = ext * (1 + BUF)
            risk = sl - entry
        else:
            ext = P.l[i: e + 1].min()
            sl = ext * (1 - BUF)
            risk = entry - sl
        rp = risk / entry * 100
        if risk <= 0 or not (RISK_MIN <= rp <= RISK_MAX):
            skipped["risk"] += 1
            continue
        out.append({"i": i, "e": e, "side": side, "entry": entry, "sl": sl, "risk": risk, "risk_pct": rp})
    return pd.DataFrame(out), skipped


def simulate(P, en, m):
    """OCO(損切り/利確 m×R)と時間切れの結果を R単位(手数料控除後)で返す。同じ足で両方届けばSL。"""
    n = len(P.c)
    res = np.empty(len(en))
    for x, r in enumerate(en.itertuples()):
        e, entry, sl, risk = r.e, r.entry, r.sl, r.risk
        short = r.side == "U"
        tp = entry - m * risk if short else entry + m * risk
        out = None
        end = min(e + TIME_STOP, n - 1)
        for j in range(e + 1, end + 1):
            if short:
                if P.h[j] >= sl:
                    out = -1.0
                    break
                if P.l[j] <= tp:
                    out = float(m)
                    break
            else:
                if P.l[j] <= sl:
                    out = -1.0
                    break
                if P.h[j] >= tp:
                    out = float(m)
                    break
        if out is None:
            out = ((entry - P.c[end]) if short else (P.c[end] - entry)) / risk
        res[x] = out - (FEE / 100.0 * entry) / risk
    return res
