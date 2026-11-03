#!/usr/bin/env python3
"""事前登録(README 2026-11-03)の評価: 維持証拠金率 0.005 → 0 の単一変更。検証用=A,B,C と 11-03 10:34 の4単位。"""
import json, os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_calib as C, liqzone_v2 as V, liqzone_search as S

OOS = {"A": ["20260925_2103"], "B": ["20261003_1210"], "C": ["20261029_0639"], "H": ["20261103_1034"]}
t = json.load(open(os.path.join(C.ROOT, "calibration", "targets.json"), encoding="utf-8"))["samples"]
unit = {i: u for u, ids in OOS.items() for i in ids}
rows = []
for s in t:
    T = int(pd.Timestamp(s["capture_utc"], tz="UTC").timestamp())
    df, last = C.load_window(T); P = V.Prep(df); i = len(df) - 1
    for name, prm in (("base", S.DEFAULT), ("mmr0", {**S.DEFAULT, "mmr": 0.0})):
        cur, up, dn = S.peaks_for(P, i, prm)
        for p in s["alive"]:
            cand = up if p > cur else dn
            rows.append((s["id"], unit.get(s["id"], "explore"), name, p, min(abs(p - c) for c in cand)))
R = pd.DataFrame(rows, columns=["id", "unit", "est", "line", "err"])
out = {}
for part, m in (("OOS", R.unit != "explore"), ("explore(参考)", R.unit == "explore"), ("all", R.unit.notna())):
    sub = R[m]; b = sub[sub.est == "base"].err; z = sub[sub.est == "mmr0"].err
    smp = [s for s in t if (s["id"] in unit) == (part == "OOS") or part == "all"]
    _, _, _, ep = C.score(smp, C.estimate_peaks, n_placebo=300)
    out[part] = {"lines": len(b), "base_median": float(b.median()), "mmr0_median": float(z.median()), "placebo_median": float(np.median(ep)),
                 "mmr0_within100": float((z <= 100).mean()), "mmr0_within200": float((z <= 200).mean()), "mmr0_within300": float((z <= 300).mean()),
                 "base_within200": float((b <= 200).mean())}
    print(part, json.dumps(out[part], ensure_ascii=False))
g = R[R.unit != "explore"].groupby(["unit", "est"]).err.median().unstack(); print(g)
print("improved units:", int((g.mmr0 < g.base).sum()), "/", len(g))
print(R[(R.est == "mmr0")].groupby("id").err.median().round())
os.makedirs(os.path.join(C.ROOT, "est", "mmr0"), exist_ok=True)
R.to_csv(os.path.join(C.ROOT, "est", "mmr0", "line_errors.csv"), index=False)
json.dump(out, open(os.path.join(C.ROOT, "est", "mmr0", "summary.json"), "w"), ensure_ascii=False, indent=1)
