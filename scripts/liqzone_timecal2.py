#!/usr/bin/env python3
"""x→時刻の較正(第2版): 列ごとの高安を、5分足の高安(その列の時間幅)と照合。s=秒/px、T_right=右端の時刻を格子探索。"""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_bars as B, liqzone_calib as C

def columns(sid):
    y0, k, c = B.geometry(sid)
    plot = B.load(B.png_for(sid), y0); cand, bar = B.masks(plot)
    hi, lo = B.candle_columns(plot, cand, k, c, y0)
    return hi, lo   # index 0 = x=B.X0

def fit(sid, srange=range(100, 181, 2), step=300, back=6*3600, fwd=2*3600):
    t = pd.Timestamp(f"{sid[:4]}-{sid[4:6]}-{sid[6:8]} {sid[9:11]}:{sid[11:13]}", tz='UTC')
    T = int(t.timestamp()); df, last = C.load_window(T + 4*3600 if False else T)
    hi, lo = columns(sid); W = len(hi); ok = ~np.isnan(hi)
    t0 = df.index.values; H = df.h.values; L = df.l.values
    best = []
    # 累積: 区間 [ta, tb) の最大/最小を高速に取るため5分足の配列を使う
    for s in srange:
        for Tr in range(T - back, T + fwd + 1, step):
            xs = np.arange(W)
            tb = Tr - (W - 1 - xs) * s            # 列の右端の時刻
            ta = tb - s
            ia = np.clip(((ta - t0[0]) // 300).astype(int), 0, len(t0) - 1)
            ib = np.clip(((tb - t0[0]) // 300).astype(int), 0, len(t0) - 1)
            m = ok & (ta >= t0[0]) & (tb <= t0[-1] + 300)
            if m.sum() < 300: continue
            dh = np.array([H[a:b+1].max() for a, b in zip(ia[m], ib[m])])
            dl = np.array([L[a:b+1].min() for a, b in zip(ia[m], ib[m])])
            e = np.abs(hi[m] - dh) + np.abs(lo[m] - dl)
            best.append((np.median(e), s, Tr, int(m.sum())))
    best.sort()
    return best[:3], T

if __name__ == '__main__':
    for sid in sys.argv[1:]:
        b, T = fit(sid)
        print(sid, [(round(m), s, round((Tr - T) / 60), n) for m, s, Tr, n in b])
