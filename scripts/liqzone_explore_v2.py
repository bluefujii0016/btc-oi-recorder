#!/usr/bin/env python3
"""定義v2の探索期間での集計(計画は data/liqzone/README.md)。探索期間のみ。"""
import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_est as E
import liqzone_v2 as V

THETA = [0.6, 0.8]
K = [0.5, 0.8]
M = [1.0, 1.5, 2.0]


def streak(x):
    best = cur = 0
    for v in x:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def main():
    df, miss = E.load_explore()
    P = V.Prep(df)
    out = os.path.join(E.ROOT, "est", "explore_v2")
    os.makedirs(out, exist_ok=True)
    rows = []
    for th in THETA:
        evs = {"real": V.find_events(P, th), "placebo+1.0": V.find_events(P, th, 1.0), "placebo-1.0": V.find_events(P, th, -1.0)}
        evs["random"] = V.random_events(P, len(evs["real"]))
        for lab, ev in evs.items():
            ev.to_csv(os.path.join(out, f"events_theta{th}_{lab}.csv"), index=False)
        print(f"\nθ={th}: 触れたイベント real={len(evs['real'])}(上 {int((evs['real'].side=='U').sum())} / 下 {int((evs['real'].side=='L').sum())}) "
              f"placebo+={len(evs['placebo+1.0'])} placebo-={len(evs['placebo-1.0'])} random={len(evs['random'])}", flush=True)
        for k in K:
            for lab, ev in evs.items():
                en, sk = V.entries(P, ev, k)
                for m in M:
                    r = V.simulate(P, en, m) if len(en) else np.array([])
                    rows.append({"theta": th, "k": k, "label": lab, "m": m, "events": len(ev), "trades": len(en),
                                 "win%": round((r > 0).mean() * 100, 1) if len(r) else np.nan,
                                 "exp_R": round(r.mean(), 3) if len(r) else np.nan,
                                 "se_R": round(r.std() / np.sqrt(len(r)), 3) if len(r) > 1 else np.nan,
                                 "sum_R": round(r.sum(), 1) if len(r) else np.nan,
                                 "max_lose_streak": streak(r) if len(r) else 0,
                                 "skip_noshrink": sk["no_shrink"], "skip_risk": sk["risk"]})
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(out, "grid.csv"), index=False)
    pd.set_option("display.width", 220)
    for (th, k), g in res.groupby(["theta", "k"]):
        print(f"\n== θ={th} k={k} ==")
        print(g.drop(columns=["theta", "k"]).to_string(index=False))


if __name__ == "__main__":
    main()
