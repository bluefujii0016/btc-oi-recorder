#!/usr/bin/env python3
"""
清算帯推定の位置の照合(キャリブレーション用の評価の道具)。
目標: 推定の帯の価格が、CoinGlass の黄色の線の価格に近いこと。
入力: data/liqzone/calibration/targets.json (画像から読んだ黄色の線)と raw データ。
出力: 位置の誤差(ドル・%)、近さの割合、推定の帯のうち黄色に近い割合、プラセボ(ランダムな帯)との比較。
損益は一切使わない。生きている線だけを照合する(通過済みの線は、撮影時点の推定には現れない)。
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_v2 as V

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "liqzone")
BAR = 300
LOOKBACK_DAYS = 5


def load_window(T):
    """時刻 T(UTC秒)までの直近 LOOKBACK_DAYS 日の5分足(欠けは直前値で補完)。"""
    def read(kind):
        days = [(pd.Timestamp(T, unit="s") - pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in range(LOOKBACK_DAYS + 2)]
        fs = [os.path.join(ROOT, "raw", kind, f"{d}.csv") for d in days]
        fs = [f for f in fs if os.path.exists(f)]
        d = pd.concat([pd.read_csv(f) for f in fs])
        return d.drop_duplicates("t", keep="last").set_index("t").sort_index()
    oi, px = read("oi_5m"), read("ohlcv_5m")
    last = int(min(oi.index.max(), px.index.max(), (T // BAR) * BAR - BAR))
    grid = pd.RangeIndex(last - LOOKBACK_DAYS * 86400, last + BAR, BAR)
    df = px[["o", "h", "l", "c", "vol_btc", "taker_buy_vol_btc"]].reindex(grid)
    df["oi"] = oi["oi_btc"].reindex(grid)
    df.loc[df.oi == 0, "oi"] = np.nan
    df.loc[df.vol_btc == 0, ["vol_btc", "taker_buy_vol_btc"]] = np.nan
    return df.ffill(), last


def estimate_peaks(df, params=None):
    """最終足の時点の推定の帯(上・下それぞれ TOP_K 本)。戻り値: (現在値, 上の[(価格, 相対強さ)], 下の[...])。"""
    P = V.Prep(df)
    i = len(df) - 1
    sl, ss = V.smoothed_map(P, i)
    cur = P.c[i]
    lo_u, hi_u = int(cur / V.BIN) + 1, int(cur * (1 + V.RANGE) / V.BIN)
    lo_d, hi_d = int(cur * (1 - V.RANGE) / V.BIN), int(cur / V.BIN)
    gmax = max(ss[lo_u:hi_u].max(), sl[lo_d:hi_d].max())
    up = [((b + .5) * V.BIN, s / gmax) for b, s in V.peaks(ss, lo_u, hi_u, 0)]
    dn = [((b + .5) * V.BIN, s / gmax) for b, s in V.peaks(sl, lo_d, hi_d, 0)]
    return cur, up, dn


def score(samples, peak_fn, n_placebo=300, seed=0):
    """各標本で、生きている黄色の線ごとに、同じ側の推定の帯の最寄りまでの距離を測る。"""
    rng = np.random.default_rng(seed)
    rows, prec_real, prec_plc, err_plc = [], [], [], []
    for s in samples:
        T = int(pd.Timestamp(s["capture_utc"], tz="UTC").timestamp())
        df, last = load_window(T)
        cur, up, dn = peak_fn(df)
        ys = [(p, "U" if p > cur else "L") for p in s["alive"]]
        for p, side in ys:
            cand = [x[0] for x in (up if side == "U" else dn)]
            e = min(abs(p - c) for c in cand) if cand else np.nan
            rows.append({"id": s["id"], "group": s["group"], "line": p, "side": side, "err_usd": e, "err_pct": e / p * 100})
        allx = [p for p in s["alive"] + s.get("lime", [])]
        est = [x[0] for x in up + dn]
        prec_real.append(np.mean([min(abs(c - p) for p in allx) <= 300 for c in est]) if est and allx else np.nan)
        # プラセボ: 同じ範囲(現在値±5%)にランダムな帯を同じ本数置く
        for _ in range(n_placebo):
            pu = rng.uniform(cur * 1.003, cur * 1.05, len(up)); pd_ = rng.uniform(cur * 0.95, cur * 0.997, len(dn))
            for p, side in ys:
                cand = pu if side == "U" else pd_
                if len(cand):
                    err_plc.append(min(abs(p - c) for c in cand))
            prec_plc.append(np.mean([min(abs(c - p) for p in allx) <= 300 for c in np.r_[pu, pd_]]) if allx else np.nan)
    r = pd.DataFrame(rows)
    return r, float(np.nanmean(prec_real)), float(np.nanmean(prec_plc)), np.array(err_plc)


def summarize(r, prec_real, prec_plc, err_plc):
    e = r.err_usd.to_numpy()
    out = {"lines": len(e), "median_err_usd": float(np.nanmedian(e)), "mean_err_usd": float(np.nanmean(e)),
           "within100": float(np.mean(e <= 100)), "within200": float(np.mean(e <= 200)), "within300": float(np.mean(e <= 300)),
           "precision300": prec_real,
           "placebo_median_err_usd": float(np.median(err_plc)), "placebo_within100": float(np.mean(err_plc <= 100)),
           "placebo_within200": float(np.mean(err_plc <= 200)), "placebo_within300": float(np.mean(err_plc <= 300)),
           "placebo_precision300": prec_plc}
    return out


if __name__ == "__main__":
    samples = json.load(open(os.path.join(ROOT, "calibration", "targets.json"), encoding="utf-8"))["samples"]
    r, pr, pp, ep = score(samples, estimate_peaks)
    pd.set_option("display.width", 200)
    print(r.groupby("id").agg(lines=("line", "count"), median_err=("err_usd", "median")).to_string())
    print(json.dumps(summarize(r, pr, pp, ep), indent=1))
    by = r.groupby("group").err_usd.median().round(0).to_dict()
    print("group median err (USD):", by)
    print("signed offsets (nearest est - line, USD) not shown; see liqzone_calib_signed in the next step")
