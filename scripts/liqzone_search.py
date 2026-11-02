#!/usr/bin/env python3
"""
推定の調整(事前登録は data/liqzone/README.md の 2026-11-01 と 2026-11-03 の行)。
目標: 推定の帯の価格を、CoinGlass の黄色の線の価格に近づける。損益は使わない。
探索範囲(宣言済み・全組合せ 3x3x3x2x3 = 162): 平滑化の幅 / レバレッジ段階の重み / 維持証拠金率 / 建玉価格の定義 / 古い建玉の重み付け。
評価: グループ単位の交差検証(1グループ除外)。選択は学習側のグループだけで行い、除外グループで評価する。
"""
import itertools, json, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_calib as C
import liqzone_v2 as V

OUT = os.path.join(C.ROOT, "est", "calib_search")
TIERS = {"current": [(10.0, .2), (25.0, .3), (50.0, .3), (100.0, .2)],
         "equal": [(10.0, .25), (25.0, .25), (50.0, .25), (100.0, .25)],
         "highlev": [(10.0, .1), (25.0, .2), (50.0, .3), (100.0, .4)]}
GRID = {"smooth": [1, 2, 4], "tiers": list(TIERS), "mmr": [0.004, 0.005, 0.006], "entry": ["typical", "close"], "halflife_h": [0, 12, 6]}
DEFAULT = {"smooth": 4, "tiers": "current", "mmr": 0.005, "entry": "typical", "halflife_h": 0}


def pmap(P, i, prm):
    a = max(1, i - V.WIN + 1)
    d = P.d[a: i + 1]
    pos = d > 0
    if not pos.any():
        z = np.zeros(V.NB)
        return z, z
    j = np.arange(a, i + 1)[pos]
    S = np.exp(P.CL[i] - P.CL[j])
    suf_min = np.minimum.accumulate(P.l[a: i + 1][::-1])[::-1][pos]
    suf_max = np.maximum.accumulate(P.h[a: i + 1][::-1])[::-1][pos]
    e = (P.entry if prm["entry"] == "typical" else P.c)[j]
    sh = P.share_l[j]
    base = d[pos] * S
    if prm["halflife_h"]:
        base = base * 0.5 ** ((i - j) * 5.0 / 60.0 / prm["halflife_h"])
    tiers = TIERS[prm["tiers"]]
    tw = sum(w for _, w in tiers)
    longs, shorts = np.zeros(V.NB), np.zeros(V.NB)
    for L, w in tiers:
        pl = e * (1 - 1 / L + prm["mmr"])
        ps = e * (1 + 1 / L - prm["mmr"])
        okl, oks = pl < suf_min, ps > suf_max
        np.add.at(longs, (pl[okl] / V.BIN).astype(int), (base * w / tw * sh)[okl])
        np.add.at(shorts, (ps[oks] / V.BIN).astype(int), (base * w / tw * (1 - sh))[oks])
    k = np.ones(2 * prm["smooth"] + 1)
    return np.convolve(longs, k, mode="same"), np.convolve(shorts, k, mode="same")


def peaks_for(P, i, prm):
    sl, ss = pmap(P, i, prm)
    cur = P.c[i]
    lo_u, hi_u = int(cur / V.BIN) + 1, int(cur * (1 + V.RANGE) / V.BIN)
    lo_d, hi_d = int(cur * (1 - V.RANGE) / V.BIN), int(cur / V.BIN)
    gmax = max(ss[lo_u:hi_u].max(), sl[lo_d:hi_d].max())
    up = [(b + .5) * V.BIN for b, s in V.peaks(ss, lo_u, hi_u, 0)]
    dn = [(b + .5) * V.BIN for b, s in V.peaks(sl, lo_d, hi_d, 0)]
    return cur, up, dn


def line_errors(cache, prm):
    rows = []
    for s, (P, i) in cache.items():
        cur, up, dn = peaks_for(P, i, prm)
        for p in s["alive"]:
            side = "U" if p > cur else "L"
            cand = up if side == "U" else dn
            rows.append((s["id"], s["group"], p, min(abs(p - c) for c in cand) if cand else np.nan))
    return pd.DataFrame(rows, columns=["id", "group", "line", "err"])


def main():
    t = json.load(open(os.path.join(C.ROOT, "calibration", "targets.json"), encoding="utf-8"))["samples"]
    cache = {}
    for s in t:
        T = int(pd.Timestamp(s["capture_utc"], tz="UTC").timestamp())
        df, last = C.load_window(T)
        cache[json.dumps(s, sort_keys=True)] = None
        cache[json.dumps(s, sort_keys=True)] = (V.Prep(df), len(df) - 1)
    cache = {json.loads(k): v for k, v in cache.items()} if False else {tuple(): None}
    # dict のキーに辞書は使えないので、標本を添字で持つ
    cache = []
    for s in t:
        T = int(pd.Timestamp(s["capture_utc"], tz="UTC").timestamp())
        df, last = C.load_window(T)
        cache.append((s, V.Prep(df), len(df) - 1))

    def errs(prm):
        rows = []
        for s, P, i in cache:
            cur, up, dn = peaks_for(P, i, prm)
            for p in s["alive"]:
                side = "U" if p > cur else "L"
                cand = up if side == "U" else dn
                rows.append((s["id"], s["group"], p, min(abs(p - c) for c in cand)))
        return pd.DataFrame(rows, columns=["id", "group", "line", "err"])

    # 検算: 既定のパラメータが、評価の道具(liqzone_calib / liqzone_v2)の基準と一致するか
    base = errs(DEFAULT)
    r0, *_ = C.score([s for s, _, _ in cache], C.estimate_peaks, n_placebo=1)
    same = np.allclose(np.sort(base.err.to_numpy()), np.sort(r0.err_usd.to_numpy()))
    print("既定のパラメータが基準と一致:", same, "| 基準の中央値", round(base.err.median(), 1), "| 線の数", len(base))
    assert same, "パラメータ化した推定が基準と一致しません"

    keys = list(GRID)
    combos = [dict(zip(keys, v)) for v in itertools.product(*GRID.values())]
    print("組合せ数", len(combos))
    res = []
    for prm in combos:
        e = errs(prm)
        res.append({**prm, "median": e.err.median(), "mean": e.err.mean(), "within200": float((e.err <= 200).mean()),
                    "within300": float((e.err <= 300).mean()), "lines": len(e), **{f"g{g}": float(x) for g, x in e.groupby("group").err.median().items()},
                    "_err": e})
    groups = sorted({s["group"] for s, _, _ in cache})

    def dist_to_default(prm):
        return sum(prm[k] != DEFAULT[k] for k in keys)

    cv_rows, cv_err = [], []
    for g in groups:
        best, bk = None, None
        for r in res:
            tr = r["_err"][r["_err"].group != g].err
            key = (round(tr.median(), 6), round(tr.mean(), 6), dist_to_default(r))
            if bk is None or key < bk:
                best, bk = r, key
        held = best["_err"][best["_err"].group == g]
        bg = base[base.group == g]
        cv_err.append(held.err)
        cv_rows.append({"held_out": g, "chosen": {k: best[k] for k in keys}, "train_median": bk[0],
                        "heldout_median": float(held.err.median()), "baseline_median": float(bg.err.median()), "lines": len(held),
                        "improved": bool(held.err.median() < bg.err.median())})
    cv_all = pd.concat(cv_err)
    _, _, _, ep = C.score([s for s, _, _ in cache], C.estimate_peaks, n_placebo=300)
    summary = {"cv_median": float(cv_all.median()), "cv_mean": float(cv_all.mean()), "cv_within200": float((cv_all <= 200).mean()),
               "cv_within300": float((cv_all <= 300).mean()), "baseline_median": float(base.err.median()),
               "placebo_median": float(np.median(ep)), "groups_improved": int(sum(r["improved"] for r in cv_rows)), "groups": len(cv_rows)}
    summary["crit_vs_placebo"] = bool(summary["cv_median"] <= 0.65 * summary["placebo_median"])
    summary["crit_vs_baseline"] = bool(summary["cv_median"] < summary["baseline_median"])
    summary["crit_groups"] = bool(summary["groups_improved"] >= 6)
    summary["adopt"] = bool(summary["crit_vs_placebo"] and summary["crit_vs_baseline"] and summary["crit_groups"])
    os.makedirs(OUT, exist_ok=True)
    grid = pd.DataFrame([{k: v for k, v in r.items() if k != "_err"} for r in res]).sort_values("median")
    grid.to_csv(os.path.join(OUT, "grid.csv"), index=False)
    json.dump({"summary": summary, "cv": cv_rows, "baseline_default": DEFAULT}, open(os.path.join(OUT, "cv.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    pd.set_option("display.width", 220)
    print(json.dumps(summary, indent=1))
    for r in cv_rows:
        print(r["held_out"], r["chosen"], "train", round(r["train_median"]), "heldout", round(r["heldout_median"]), "baseline", round(r["baseline_median"]), "improved", r["improved"])
    print("\nin-sample top 8 (参考。採用の根拠にしない):")
    print(grid.head(8)[keys + ["median", "mean", "within300"]].round(1).to_string(index=False))
    print("in-sample worst 3:")
    print(grid.tail(3)[keys + ["median", "mean", "within300"]].round(1).to_string(index=False))


if __name__ == "__main__":
    main()
