"""事前登録 候補E・F(2026-10-15) 評価用スクリプト
使い方: python eval_20261015_E_F.py <period>   period = explore | confirm | final
データ: data/history/sar_history_BTCUSDT_15m.csv.gz
"""
import sys, numpy as np, pandas as pd

PERIODS = {  # (開始t, 終了t, 前半/後半の境界t)
    'explore': (1727740800, 1759276800, 1743465600),  # 2024-10-01 / 2025-10-01 / 2025-04-01
    'confirm': (1759276800, 1777593600, 1768435200),  # 2025-10-01 / 2026-05-01 / 2026-01-15
    'final':   (1777593600, 1788307200, 1782950400),  # 2026-05-01 / 2026-09-02 / 2026-07-02
}
FEE = 0.05  # 往復手数料(%)

def load(path, t1):
    df = pd.read_csv(path)
    df = df[df.t < t1].reset_index(drop=True)  # 期間終了以降のデータは使わない
    return df

def sweeps(H, L, C, k):
    """左右k本のフラクタルで確定した直近スイング高値/安値を、ヒゲで抜いて終値で戻した足。
    スイングは右k本が確定した時点で利用可能。最初に抜かれた時点で消費する。"""
    n = len(H); bear = np.zeros(n, bool); bull = np.zeros(n, bool)
    sh = sl = None
    for i in range(n):
        j = i - k
        if j - k >= 0:
            if H[j] == H[j-k:j+k+1].max(): sh = H[j]
            if L[j] == L[j-k:j+k+1].min(): sl = L[j]
        if sh is not None and H[i] > sh:
            if C[i] < sh: bear[i] = True
            sh = None
        if sl is not None and L[i] < sl:
            if C[i] > sl: bull[i] = True
            sl = None
    return bear, bull

def main(period, path='sar_history_BTCUSDT_15m.csv.gz'):
    t0, t1, th = PERIODS[period]
    df = load(path, t1)
    H, L, C, T = df.high.values, df.low.values, df.close.values, df.t.values
    up = df.trend.values == 'up'; flips = np.where(df.reversed.values == 1)[0]
    n = len(df)

    # ---- 候補E:動画どおり(スイープ→SAR転換→転換前FVGへの戻りで指値、SL=スイープ先、TP=2R)
    k, W, M, R, buf = 3, 8, 16, 2.0, 0.0005
    bear, bull = sweeps(H, L, C, k)
    E = []
    for i in flips:
        if T[i] < t0: continue
        d = 1 if up[i] else -1
        lo = max(0, i - W + 1); sw = (bull if d == 1 else bear)[lo:i+1]
        if not sw.any(): continue
        s0 = lo + np.where(sw)[0][0]
        stop = H[s0:i+1].max()*(1+buf) if d == -1 else L[s0:i+1].min()*(1-buf)
        ent = None
        for j in range(i, max(s0, 2) - 1, -1):
            if d == -1 and H[j] < L[j-2]: ent = H[j]; break
            if d == 1 and L[j] > H[j-2]: ent = L[j]; break
        if ent is None: continue
        risk = abs(stop - ent) / ent * 100
        if risk <= 0.05: continue
        tp = ent * (1 + d * R * risk / 100)
        filled = None
        for m in range(i + 1, min(n, i + 1 + M)):
            if d == -1:
                if L[m] <= tp: break
                if H[m] >= ent: filled = m; break
            else:
                if H[m] >= tp: break
                if L[m] <= ent: filled = m; break
        if filled is None: continue
        r = None
        for j in range(filled, n):
            if d == -1:
                if H[j] >= stop: r = -risk - FEE; break
                if j > filled and L[j] <= tp: r = R * risk - FEE; break
            else:
                if L[j] <= stop: r = -risk - FEE; break
                if j > filled and H[j] >= tp: r = R * risk - FEE; break
        if r is not None: E.append((T[i], r, risk))

    # ---- 候補F:大きなスイングのスイープ直後のSAR転換を「ダマシ」とみなし、転換と逆方向に入る
    k, W = 8, 8
    bear, bull = sweeps(H, L, C, k)
    Fr = []
    for a, i in enumerate(flips):
        if T[i] < t0 or a + 1 >= len(flips): continue
        d = 1 if up[i] else -1
        lo = max(0, i - W + 1)
        if not (bull if d == 1 else bear)[lo:i+1].any(): continue
        nxt = flips[a + 1]
        Fr.append((T[i], -d * (C[nxt] / C[i] - 1) * 100 - FEE, None))

    for name, recs in (('E', E), ('F', Fr)):
        x = pd.DataFrame(recs, columns=['t', 'r', 'risk'])
        if len(x) == 0: print(name, '該当なし → 不合格'); continue
        ev = x.r.mean(); h1 = x[x.t < th].r.mean(); h2 = x[x.t >= th].r.mean()
        ok = ev > 0 and h1 > 0 and h2 > 0
        from scipy import stats
        daily = x.groupby(x.t // 86400).r.sum()
        p = stats.ttest_1samp(daily, 0, alternative='greater').pvalue
        top5 = np.sort(x.r.values)[:-5].mean() if len(x) > 5 else float('nan')
        extra = f" 平均SL幅{x.risk.mean():.2f}%" if name == 'E' else ''
        print(f"[{period}] 候補{name}: n={len(x)} 手数料後EV={ev:+.3f}% 前半{h1:+.3f}% 後半{h2:+.3f}% "
              f"→ {'合格' if ok else '不合格'}  (参考: 勝率{(x.r>0).mean():.2f} 日単位p={p:.3f} 上位5件除外EV={top5:+.3f}%{extra})")

if __name__ == '__main__':
    main(sys.argv[1])
