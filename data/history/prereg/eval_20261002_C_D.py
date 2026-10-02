"""
事前登録 2026-10-02:候補C・候補D の評価スクリプト
  条件・指標・合格基準は prereg_20261002_C_D.md に固定済み。このファイルも登録時点の内容で固定する。

使い方(リポジトリのルートで実行):
  python data/history/prereg/eval_20261002_C_D.py --period explore                  # 動作確認用(探索期間)
  python data/history/prereg/eval_20261002_C_D.py --period confirm --i-have-committed # 本番:確認期間で1回だけ
  python data/history/prereg/eval_20261002_C_D.py --period final   --i-have-committed # 確認期間に合格した候補のみ
合格基準(prereg_20261002_C_D.md §4):確認期間・最終確認期間とも同じ
  手数料後の期待値>0、かつ前半・後半とも>0。日単位の片側t検定は参考として出力するのみ

データ:
  data/history/sar_history_BTCUSDT_15m.csv.gz     (15分足・SAR)
  data/history/deriv_metrics_BTCUSDT_5m.csv.gz    (全体ロング/ショート比率など)
読み込むのは「評価期間の終わり」より前の行だけ。評価期間より前の行は、特徴量(1時間足SAR、7日中央値)の計算にのみ使い、成績には使わない。
"""
import argparse, sys
from datetime import datetime, timezone
import numpy as np, pandas as pd
from scipy import stats

def ts(s): return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
PERIODS = {   # [開始, 前後半の境界, 終了(含まない)]
    'explore': (ts('2024-10-01'), ts('2025-04-01'), ts('2025-10-01')),
    'confirm': (ts('2025-10-01'), ts('2026-01-15'), ts('2026-05-01')),
    'final':   (ts('2026-05-01'), ts('2026-07-02'), ts('2026-09-02')),
}
FEE = 0.05                       # 往復%
SL, TP = 1.2, 2.4                # 候補A のSL/TP(%)
C_AF1_MIN = 0.14                 # 候補C:1時間足AFの下限
D_DEV_MAX = -0.1021              # 候補D:log(全体L/S比) − 直近672本中央値 の上限
WARMUP = 7 * 86400               # 評価期間の先頭からこの日数は、前のデータが無い場合にエントリーしない(探索期間のみ該当)
BASE = 'data/history/'

def wilder(h, l, step=0.02, mx=0.2):
    """標準のパラボリックSAR。各足の確定時点のトレンドとAFを返す"""
    n = len(h); tr = np.zeros(n, int); afv = np.zeros(n)
    up = h[1] > h[0]; ep = h[0] if up else l[0]; s = l[0] if up else h[0]; af = step
    tr[0] = 1 if up else -1; afv[0] = af
    for i in range(1, n):
        s = s + af * (ep - s)
        s = min(s, l[i-1], l[max(i-2, 0)]) if up else max(s, h[i-1], h[max(i-2, 0)])
        if up:
            if l[i] < s: up = False; s = ep; ep = l[i]; af = step
            elif h[i] > ep: ep = h[i]; af = min(af + step, mx)
        else:
            if h[i] > s: up = True; s = ep; ep = h[i]; af = step
            elif l[i] < ep: ep = l[i]; af = min(af + step, mx)
        tr[i] = 1 if up else -1; afv[i] = af
    return tr, afv

def sim(df, idx, dirs, start, end):
    """エントリー=idx足の終値。翌足以降の高値・安値で判定、同じ足で両方ならSL優先。評価期間の終わりまでに決着しなければNaN"""
    hi, lo, c, t = df.high.values, df.low.values, df.close.values, df.t.values
    n = len(df); res = np.full(len(idx), np.nan)
    for k, (i, d) in enumerate(zip(idx, dirs)):
        e = c[i]; slp = e*(1-SL/100) if d == 1 else e*(1+SL/100); tpp = e*(1+TP/100) if d == 1 else e*(1-TP/100)
        j = i + 1
        while j < n and t[j] < end:
            if (lo[j] <= slp) if d == 1 else (hi[j] >= slp): res[k] = -SL; break
            if (hi[j] >= tpp) if d == 1 else (lo[j] <= tpp): res[k] = TP; break
            j += 1
    return res

def day_test(g):
    net = g.pnl - FEE
    dm = net.groupby(pd.to_datetime(g.t, unit='s', utc=True).dt.floor('D')).mean()
    return (stats.ttest_1samp(dm, 0, alternative='greater') if len(dm) > 1 else (np.nan, np.nan)), len(dm)

def evaluate(name, g, start, mid, end, period, pooled=None):
    g = g.dropna(subset=['pnl']).copy()
    net = g.pnl - FEE
    day = pd.to_datetime(g.t, unit='s', utc=True).dt.floor('D')
    dm = net.groupby(day).mean()
    (t_day, p_day), _ = day_test(g)
    h1 = net[g.t < mid]; h2 = net[g.t >= mid]
    ok = (net.mean() > 0) and (h1.mean() > 0) and (h2.mean() > 0)        # 判定基準(確認・最終確認で共通)
    if period == 'final':
        pg = pd.concat([pooled.dropna(subset=['pnl']), g])
        (tp, pp), nd = day_test(pg)
        print(f'[{name}] (参考)確認期間+最終確認期間 合算: 件数 {len(pg)} / 日数 {nd} / 日単位の片側t検定 t={tp:.2f} p={pp:.4f}')
        # 合算の検定は参考。判定は確認期間と同じ「方向の再現」基準(prereg §4)
    print(f'--- {name} ---')
    print(f'件数 {len(g)}(決着しなかった件数 {g.attrs.get("unresolved", 0)})/ 日数 {len(dm)} / 勝率 {(g.pnl > 0).mean()*100:.1f}%')
    print(f'期待値/回 手数料前 {g.pnl.mean():+.3f}% / 手数料後 {net.mean():+.3f}%')
    print(f'前半 {h1.mean():+.3f}%(n={len(h1)}) / 後半 {h2.mean():+.3f}%(n={len(h2)})')
    print(f'日単位の片側t検定(参考) t={t_day:.2f} p={p_day:.4f}')
    print(f'判定: {"合格" if ok else "不合格"}')
    return net, day

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', choices=PERIODS, required=True)
    ap.add_argument('--i-have-committed', action='store_true')
    a = ap.parse_args()
    if a.period != 'explore' and not a.i_have_committed:
        sys.exit('確認期間・最終確認期間の評価には --i-have-committed が必要です(事前登録のコミット後に1回だけ実行)')
    start, mid, end = PERIODS[a.period]

    df = pd.read_csv(BASE + 'sar_history_BTCUSDT_15m.csv.gz')
    df = df[df.t < end].reset_index(drop=True)                 # 評価期間より後は読み込まない
    df['dir'] = np.where(df.trend == 'up', 1, -1)
    data_start = df.t.iloc[0]

    # 1時間足SAR(15分足から組み立て)。直前に確定した1時間足を参照
    df['hour'] = df.t // 3600 * 3600
    H = df.groupby('hour').agg(h=('high', 'max'), l=('low', 'min')).reset_index()
    H['tr1'], H['af1'] = wilder(H.h.values, H.l.values)
    ref = np.where((df.t % 3600) == 2700, df.hour, df.hour - 3600)
    Hm = H.set_index('hour')
    df['tr1'] = Hm.tr1.reindex(ref).values; df['af1'] = Hm.af1.reindex(ref).values

    # 全体ロング/ショート比率(口座数):足の確定5分前までの最新値、log、直近672本(7日)の中央値からの乖離
    m = pd.read_csv(BASE + 'deriv_metrics_BTCUSDT_5m.csv.gz', usecols=['t', 'count_long_short_ratio'])
    m = m[(m.t < end) & (m.count_long_short_ratio > 0)].sort_values('t')
    x = pd.merge_asof(pd.DataFrame({'k': df.t + 600}), m.rename(columns={'t': 'k'}), on='k', direction='backward')
    lr = np.log(x.count_long_short_ratio)
    df['gls_dev'] = (lr - lr.rolling(672, min_periods=200).median()).values

    def build(start, end):
        inper = (df.t >= start) & (df.t < end) & (df.t >= data_start + WARMUP)
        n1 = inper & (df.dots_since_flip == 1)

        # 候補C:15分足の転換(N=1)が、直前確定の1時間足トレンドと逆向き、かつ1時間足AF≥0.14 → 1時間足の方向へ
        cm = n1 & (df.tr1 != df.dir) & (df.af1 >= C_AF1_MIN - 1e-9)
        ci = np.where(cm)[0]; cd = -df.dir.values[ci]
        C = pd.DataFrame({'t': df.t.values[ci], 'dir': cd, 'pnl': sim(df, ci, cd, start, end)})
        C.attrs['unresolved'] = int(C.pnl.isna().sum())

        # 候補D:15分足の上転換(N=1)で、全体L/S比の乖離 ≤ −0.1021 → ロング
        dm_ = n1 & (df.dir == 1) & (df.gls_dev <= D_DEV_MAX)
        di = np.where(dm_)[0]
        D = pd.DataFrame({'t': df.t.values[di], 'dir': 1, 'pnl': sim(df, di, np.ones(len(di), int), start, end)})
        D.attrs['unresolved'] = int(D.pnl.isna().sum())
        return C, D, inper

    C, D, inper = build(start, end)
    if a.period == 'final':
        cs, _, ce = PERIODS['confirm']
        Cc, Dc, _ = build(cs, ce)
    else:
        Cc = Dc = None

    print(f'評価期間: {a.period}  {datetime.fromtimestamp(start, timezone.utc):%Y-%m-%d} 〜 {datetime.fromtimestamp(end, timezone.utc):%Y-%m-%d}(終了日を含まない)')
    print(f'定義: エントリー=N=1の足の終値 / SL{SL}%・TP{TP}%を高値・安値で判定、同じ足で両方ならSL優先 / 手数料 往復{FEE}%')
    netC, _ = evaluate('候補C', C, start, mid, end, a.period, Cc)
    print(f'  副指標 ロング側 {(C[C.dir == 1].pnl - FEE).mean():+.3f}%(n={(C.dir == 1).sum()}) / ショート側 {(C[C.dir == -1].pnl - FEE).mean():+.3f}%(n={(C.dir == -1).sum()})')
    netD, dayD = evaluate('候補D', D, start, mid, end, a.period, Dc)
    contrib = netD.groupby(dayD).sum().sort_values(ascending=False)
    share = contrib.head(10).sum() / contrib.sum() * 100 if contrib.sum() > 0 else np.nan
    print(f'  副指標 上位10日が合計に占める割合 {share:.0f}%')
    # 副指標:SARと無関係に、同じ条件の全15分足でロングした場合
    am = inper & (df.gls_dev <= D_DEV_MAX)
    ai = np.where(am)[0]
    pa = sim(df, ai, np.ones(len(ai), int), start, end)
    print(f'  副指標 SARと無関係に条件該当の全足でロング: 手数料後 {np.nanmean(pa) - FEE:+.3f}%(n={np.isfinite(pa).sum()})')

if __name__ == '__main__':
    main()
